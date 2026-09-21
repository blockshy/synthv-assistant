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


class BridgeError(RuntimeError):
    """供控制台和 MCP 统一报告的宿主连接或执行错误。"""


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
