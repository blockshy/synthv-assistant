"""Flutter 桌面应用专用的本机后台生命周期，不替换调教核心的权限与事务。

随机端口和认证令牌只通过父进程持有的 stdout 管道交付。stdin 管道代表
启动器的生存期；管道断开后进入排空状态，等待正在执行的录音、模型或宿主
动作完成，不能用关闭窗口或启动器崩溃作为强行中断工程写入的理由。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import threading
import time


PROTOCOL_VERSION = 1
APPLICATION_VERSION = "0.2.0"


def configure_desktop_data() -> Path:
    """在导入业务模块之前确定桌面资料目录；不复制、移动或解密旧数据。

    开发启动器通过 SYNTHV_ASSISTANT_DATA 显式复用现有仓库资料。正式安装
    默认使用当前用户 LocalAppData，避免普通账户向 Program Files 写入。
    """
    existing = os.environ.get("SYNTHV_ASSISTANT_DATA")
    if existing:
        return Path(existing).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("桌面后台需要 Windows LOCALAPPDATA，或显式指定 SYNTHV_ASSISTANT_DATA。")
    path = (Path(base) / "SynthV Assistant" / "data").resolve()
    os.environ["SYNTHV_ASSISTANT_DATA"] = str(path)
    return path


class DesktopRuntime:
    """将 HTTP 请求接收和退出决策串行化，避免检查空闲后又接受新写入。"""

    def __init__(self, service, data: Path, *, busy_probe=None):
        from .operations import operation_busy
        self.service = service
        self.data = Path(data)
        self._busy_probe = busy_probe or operation_busy
        self._lock = threading.RLock()
        self._active_requests = 0
        self._closing = False
        self._shutdown_started = False
        self._server = None

    def attach(self, server) -> None:
        """只在端口绑定成功后附加服务器，尚未进入服务循环时不调用 shutdown。"""
        self._server = server

    def begin_request(self) -> bool:
        """关闭开始后拒绝新的业务请求；已经登记的请求始终允许自然结束。"""
        with self._lock:
            if self._closing:
                return False
            self._active_requests += 1
            return True

    def end_request(self) -> None:
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)

    def status(self) -> dict:
        """仅返回生命周期计数，不读取会话内容、工程路径或供应商配置。"""
        with self._lock:
            # 宿主动作可能长时间持有 operation_lock；生命周期查询不能阻塞
            # UI。拿不到锁即保守报告忙碌，而不是等待操作结束才显示“空闲”。
            acquired = self.service.operation_lock.acquire(blocking=False)
            try:
                running = (sum(job.get("state") == "running" for job in self.service.jobs.values())
                           if acquired else 1)
                # 桌面进程独占自己的任务队列。客户端若丢失提交响应，可凭这里
                # 的编号恢复轮询同一任务；不返回正文、参数、附件或已完成记录。
                # 编号沿用业务层 UUID 白名单，错误键不能变成额外的公开数据。
                running_ids = ([identifier for identifier, job in self.service.jobs.items()
                                if isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier)
                                and job.get("state") == "running"][:50] if acquired else [])
                recording = bool(self.service.recording) if acquired else True
            finally:
                if acquired:
                    self.service.operation_lock.release()
            try:
                # 显式接入旧脚本后，宿主互斥锁随 IPC 绑定变化；退出保护必须
                # 探测同一个锁，不能继续只看当前资料目录中的空闲锁。
                path_getter = getattr(self.service, "operation_path", None)
                lock_path = path_getter() if callable(path_getter) else self.data / "operation.lock"
                operation_busy = self._busy_probe(lock_path)
                status_error = False
            except OSError:
                operation_busy, status_error = True, True
            return {"protocol": PROTOCOL_VERSION, "version": APPLICATION_VERSION,
                    "busy": bool(self._active_requests or running or recording or operation_busy),
                    "closing": self._closing, "activeRequests": self._active_requests,
                    "runningJobs": running, "runningJobIds": running_ids, "operationBusy": operation_busy,
                    "operationStatusError": status_error}

    def request_shutdown(self) -> tuple[bool, dict]:
        """用户主动关闭只接受空闲状态；与 begin_request 共用锁防止接收竞态。"""
        with self._lock:
            state = self.status()
            if state["busy"]:
                return False, {**state, "error": "录音、AI 或工程操作仍在进行，请完成后再退出。"}
            self._closing = True
            self._start_shutdown()
            return True, {**state, "closing": True}

    def _start_shutdown(self) -> None:
        if self._shutdown_started or self._server is None:
            return
        self._shutdown_started = True
        # BaseServer.shutdown 必须从 serve_forever 所在线程之外调用。该线程
        # 不触碰工程，不取消 Future，只停止接收；最终收尾在 run_desktop。
        threading.Thread(target=self._server.shutdown, name="desktop-shutdown", daemon=True).start()

    def owner_disconnected(self) -> None:
        """父进程已离开时停止新任务，等待在途任务完成，再安全关闭服务。

        不设置强杀倒计时。模型与采集器已有各自有界超时，宿主写入必须让原有
        事务和回滚流程收尾；崩溃后短暂保留后台比终止写入更安全。
        """
        with self._lock:
            self._closing = True
        while True:
            with self._lock:
                if not self.status()["busy"]:
                    self._start_shutdown()
                    return
            time.sleep(0.25)

    def watch_owner(self, stream) -> None:
        """stdin 只作为生存期管道，收到的字符不解释为命令或模型输入。"""
        try:
            while stream.read(1):
                pass
        except (OSError, ValueError):
            pass
        self.owner_disconnected()


def run_desktop() -> None:
    """启动随机回环端口，以单行 JSON 完成握手；stdout 不输出普通日志。"""
    # Dart 启动器按 UTF-8 读取管道，不能依赖 Windows 当前系统代码页。
    # 握手仍使用 ASCII JSON；固定 UTF-8 同时保证启动失败的中文 stderr 可读。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    data = configure_desktop_data()
    # 此前不能导入 config/service：它们的路径常量必须看到上面确定的目录。
    from .operations import OperationLock, OperationBusyError
    from .service import AssistantService
    from .server import make_server
    service = None
    server = None
    try:
        with OperationLock(data / "desktop.lock"):
            # NumPy 的原生库必须先在主线程完成首次加载。部分 Windows 环境中，
            # 第一次由 HTTP 工作线程导入会阻塞原生模块初始化，并占用 Python
            # 导入锁，进一步拖住其他音频/会话请求。预热只加载本地代码，不读取
            # 用户音频、不连接模型或 SynthV；失败会在握手前由启动器正确报告。
            from . import analysis as _audio_analysis  # noqa: F401
            service = AssistantService()
            runtime = DesktopRuntime(service, data)
            server = make_server(0, service, desktop_runtime=runtime)
            runtime.attach(server)
            handshake = {"protocol": PROTOCOL_VERSION, "port": server.server_port,
                         "token": server.desktop_token, "version": APPLICATION_VERSION,
                         "pid": os.getpid(), "dataDirectory": str(data)}
            print(json.dumps(handshake, ensure_ascii=True, separators=(",", ":")), flush=True)
            # 父进程必须保留 stdin 句柄。监听线程先启动也安全：shutdown 等待
            # serve_forever 真正结束，不会在握手后因紧邻 EOF 而遗留后台进程。
            threading.Thread(target=runtime.watch_owner, args=(sys.stdin,),
                             name="desktop-owner", daemon=True).start()
            try:
                server.serve_forever(poll_interval=0.1)
            finally:
                # 先等待业务任务，再关闭 HTTP 请求线程。客户端断开不会取消
                # 录音、写入或已计费的模型请求，也不会自动重发它们。
                service.executor.shutdown(wait=True, cancel_futures=False)
                server.server_close()
    except OperationBusyError:
        raise RuntimeError("该资料目录已有桌面后台，请等待原窗口或在途任务退出后重试。") from None
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.executor.shutdown(wait=True, cancel_futures=False)
