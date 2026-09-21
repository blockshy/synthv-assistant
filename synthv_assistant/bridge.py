"""通过本地文件与 SynthV Lua 脚本通信。

每次仅允许一个请求。写入前检查会话和截止时间，超时不会自动重试，
因为宿主可能已执行命令。跨进程锁也覆盖多个 MCP 客户端的并发访问。
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .config import IPC, ensure_directories


_PUBLIC_PREVIEW_ERRORS = frozenset({
    "SynthV 桥接未连接，请先在脚本菜单启动助手。",
    "桥接正被其他操作占用；请等待当前操作完成。",
    "上一次请求尚未处理完毕；请检查连接，勿重复提交写入。",
    "SynthV 请求超时，执行结果未知；先读取当前状态，再决定是否重试。",
    "请先在 SynthV 打开一个音符组。",
    "请在钢琴卷帘中选中要调整的音符。",
    "当前宿主没有返回可编辑的此参数，请重新读取选区。",
    "一次最多调整128个音符，请缩小选区。",
    "一次最多调整30秒，请缩小选区。",
    "选区持续时间必须大于零。",
    "选区过短，无法建立可靠的曲线边缘。",
    "此音符组被多个位置共用；本版本拒绝修改，以免同时影响其他片段。",
    "当前曲线插值方式尚未通过边界校验支持，未生成可应用预览。",
    "候选曲线会影响选区以外的插值，已拒绝预览；请扩大选区或手动调整边界。",
    "该曲线超过4000个控制点，请缩短或先整理工程。",
    "该曲线超过4000个控制点，请先在工程副本中简化后再使用。",
    "候选曲线超过4000个控制点，请缩短选区。",
    "音高偏移插值方式未知，无法确认其在选区内为零。",
    "选区内音高偏移并非零，无法确认原生音高叠加顺序；请先处理音高偏移曲线。",
    "选区内已有原生音高引导点；移除它可能影响邻近音高，请先手动处理。",
    "现有原生音高曲线不足两个点，无法确认安全范围。",
    "现有原生音高曲线的实际时间位置无效，已拒绝预览。",
    "已有原生音高曲线跨越选区边界，已拒绝预览；请扩大选区或手动处理。",
    "曲线时间点在宿主时间精度下重合或越界，请增加点间距离。",
    "原生音高插值采样超出允许音高范围，已拒绝预览。",
    "原生音高候选节点与插值读回不一致，已拒绝预览。",
    "原生音高插值坐标语义无法唯一校准，已拒绝处理。",
    "宿主无法将真实控制点转换为有效的预览时间位置。",
    "宿主无法将原生音高节点转换为有效的预览时间位置。",
    "宿主无法将选中音符转换为有效的预览坐标。",
})
_CURVE_REJECTION_SUFFIX = " 未写入；可缩短选区或选择控制点模式。"


class BridgeError(RuntimeError):
    """保留本地诊断原因，并为公开会话提供独立的固定提示白名单。

    宿主异常可能含脚本路径、工程信息或未知插件返回值，不能直接用于消息框。
    public_message 只返回本文件中的常量；兼容旧协议的 Lua 位置前缀和曲线
    校验后缀，不要求用户为显示错误而重新启动桥接，也不信任响应中的额外字段。
    """

    @property
    def public_message(self) -> str | None:
        """只识别完整固定提示；未知文本仍由调用层显示通用失败信息。"""
        if len(self.args) != 1 or not isinstance(self.args[0], str):
            return None
        source = self.args[0]
        if len(source) > 16_384:
            return None
        # 迭代校验有时在固定错误后再补一条固定建议；先移除它再做白名单匹配。
        # 不返回截取后的原文，避免脚本位置前缀或恶意附加文本进入公开会话。
        candidate = source.removesuffix(_CURVE_REJECTION_SUFFIX)
        for message in _PUBLIC_PREVIEW_ERRORS:
            if candidate == message or candidate.endswith(": " + message):
                return message
        return None


def atomic_json(path: Path, value: Any) -> None:
    """先完整写入同目录临时文件，再原子替换，避免宿主读到半个 JSON。"""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


class BridgeClient:
    def __init__(self, directory: Path = IPC):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict:
        """心跳只表示脚本仍在运行，不代表后台音频已经合成完毕。"""
        try:
            data = json.loads((self.directory / "heartbeat.json").read_text(encoding="utf-8"))
            age = time.time() - float(data["timestamp"])
            return {**data, "connected": -2 <= age <= 5, "heartbeatAge": round(age, 2)}
        except (OSError, ValueError, KeyError, TypeError):
            return {"connected": False, "message": "请在 SynthV 脚本菜单启动 SynthV Assistant。"}

    def call(self, action: str, args: dict | None = None, timeout: float = 12.0) -> dict:
        """发送有截止时间的请求；会话改变或超时均直接停止本次操作。"""
        status = self.status()
        if not status.get("connected"):
            raise BridgeError("SynthV 桥接未连接，请先在脚本菜单启动助手。")
        request_id = uuid.uuid4().hex
        lock_path = self.directory / "client.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise BridgeError("桥接正被其他操作占用；请等待当前操作完成。") from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as lock:
                json.dump({"pid": os.getpid(), "id": request_id, "time": time.time()}, lock)
            if (self.directory / "request.json").exists() or (self.directory / "processing.json").exists():
                raise BridgeError("上一次请求尚未处理完毕；请检查连接，勿重复提交写入。")
            atomic_json(self.directory / "request.json", {
                "id": request_id, "session": status["session"], "action": action,
                "args": args or {}, "expires": time.time() + timeout,
            })
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    result = json.loads((self.directory / "response.json").read_text(encoding="utf-8"))
                    if result.get("id") == request_id:
                        if not result.get("ok"):
                            raise BridgeError(result.get("error", "宿主拒绝执行。"))
                        return result.get("result", {})
                except (OSError, ValueError):
                    pass
                time.sleep(0.025)
            raise BridgeError("SynthV 请求超时，执行结果未知；先读取当前状态，再决定是否重试。")
        finally:
            lock_path.unlink(missing_ok=True)


def build_script(destination: Path | None = None) -> Path:
    """把 JSON 解析器和桥接打包成单个 Lua 文件，脚本目录无需额外依赖。"""
    from .config import ROOT
    ensure_directories()
    destination = destination or DATA_INSTALL()
    destination.parent.mkdir(parents=True, exist_ok=True)
    parser = (ROOT / "synthv" / "json.lua").read_text(encoding="utf-8")
    # 原生音高模块与自动化桥接在同一脚本实例内，共用预览、撤销和失败恢复生命周期。
    pitch = (ROOT / "synthv" / "pitch.lua").read_text(encoding="utf-8")
    body = (ROOT / "synthv" / "bridge.lua").read_text(encoding="utf-8")
    ipc_literal = str(IPC).replace("\\", "/")
    if "]]" in ipc_literal:
        raise ValueError("IPC 路径不能包含 Lua 长字符串终止符。")
    destination.write_text("-- 本文件由安装器生成；请编辑项目内源文件。\nlocal json = (function()\n" + parser + "\nend)()\nlocal NativePitch=(function()\n" + pitch + "\nend)()\nlocal IPC_DIR = [[" + ipc_literal + "]]\n" + body, encoding="utf-8")
    return destination


def DATA_INSTALL() -> Path:
    """返回待安装文件；生成本身不会写入 SynthV 配置目录。"""
    from .config import DATA
    return DATA / "install" / "SynthVAssistant.lua"
