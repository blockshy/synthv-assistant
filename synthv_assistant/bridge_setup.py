"""诊断与修复固定 SynthV 用户脚本的 IPC 绑定，不迁移业务资料或操作工程。

所有目录信息只经认证 API 返回。客户端只能选择“当前资料”或“已安装脚本”，
不能指定任意路径；后者必须来自本机固定脚本的单一声明，且通过路径与心跳校验。
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .bridge import BridgeClient, render_script
from .operations import OperationBusyError, OperationLock


CONFIG_NAME = "bridge-connection.json"
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
SCRIPT_HEADER = "-- 本文件由安装器生成；请编辑项目内源文件。"
BUSY_MESSAGE = "桥接或后台任务仍在运行，请完成当前操作后再调整连接。"


class BridgeSetupConflict(ValueError):
    """诊断已过期或绑定正在使用，调用者应刷新而不能重复提交旧操作。"""


class _UnconfirmedBridgeClient(BridgeClient):
    """保存的目标失效时拒绝所有宿主命令，不能静默改用另一个活跃工程。"""

    def status(self) -> dict:
        # BridgeClient.call 在发送任何请求前检查 status，因此读取、预览和
        # 写入入口都会被同一边界阻止；不依赖页面是否及时禁用按钮。
        return {"connected": False, "writeEnabled": False,
                "message": "保存的桥接绑定已变化，请在连接管理中明确选择连接目标。"}


def _ordinary_path(path: Path, *, directory: bool | None = None) -> None:
    """拒绝符号链接、Windows junction/reparse point 和不匹配的文件类型。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("桥接路径包含重定向链接，无法安全使用。")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise ValueError("桥接目录不是普通目录，无法安全使用。")
    if directory is False and not stat.S_ISREG(info.st_mode):
        raise ValueError("桥接文件不是普通文件，无法安全使用。")


def _local_directory(path: Path, *, must_exist: bool) -> Path:
    """只接受本机绝对目录，逐级检查而非先 resolve 掩盖链接或越界部分。"""
    if not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\", "//")):
        raise ValueError("桥接目录必须是本机绝对路径。")
    if any(ord(character) < 32 for character in str(path)):
        raise ValueError("桥接目录包含无效字符。")
    for ancestor in reversed((path, *path.parents)):
        _ordinary_path(ancestor, directory=True)
    if must_exist and not path.is_dir():
        raise ValueError("已安装脚本的桥接目录不存在，请先在 SynthV 运行脚本。")
    return path.resolve()


def script_path() -> Path:
    """安装目标固定在 SynthV Studio 2 用户脚本目录，不接受 API 传入文件名。"""
    base = os.environ.get("APPDATA", "")
    if not base:
        raise ValueError("无法确定 SynthV 用户脚本目录，请检查 APPDATA。")
    root = _local_directory(Path(base), must_exist=True)
    parent = root / "Dreamtonics" / "Synthesizer V Studio 2" / "scripts"
    _local_directory(parent, must_exist=False)
    target = parent / "SynthVAssistant.lua"
    _ordinary_path(target, directory=False)
    return target


def _read_script() -> tuple[dict, bytes | None, Path | None]:
    """最多读取固定脚本两 MiB；只提取生成器的一个 IPC 声明，不执行 Lua。"""
    public = {"path": None, "exists": False, "recognized": False, "ipcDirectory": None}
    try:
        path = script_path()
        public["path"] = str(path)
        if not path.is_file():
            return public, None, None
        public["exists"] = True
        if not 0 < path.stat().st_size <= MAX_SCRIPT_BYTES:
            return public, None, None
        raw = path.read_bytes()
        source = raw.decode("utf-8-sig")
        matches = re.findall(r"^local IPC_DIR = \[\[([^\r\n]*)\]\]\r?$", source, re.M)
        if not source.startswith(SCRIPT_HEADER) or len(matches) != 1:
            return public, raw, None
        directory = Path(matches[0])
        if directory.name.lower() != "ipc":
            return public, raw, None
        directory = _local_directory(directory, must_exist=False)
        public.update(recognized=True, ipcDirectory=str(directory))
        return public, raw, directory
    except (OSError, UnicodeError, ValueError):
        return public, None, None


def heartbeat(directory: Path | None) -> dict:
    """只返回连接所需白名单字段，不透传心跳内的工程名、路径或未知信息。"""
    result = {"connected": False, "heartbeatAge": None, "writeEnabled": False,
              "pendingRequest": False}
    if directory is None:
        return result
    try:
        _local_directory(directory, must_exist=True)
        result["pendingRequest"] = any((directory / name).exists()
                                       for name in ("request.json", "processing.json", "client.lock"))
        path = directory / "heartbeat.json"
        _ordinary_path(path, directory=False)
        if not 0 < path.stat().st_size <= 65536:
            return result
        payload = json.loads(path.read_text(encoding="utf-8"))
        timestamp, session = payload.get("timestamp"), payload.get("session")
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or not isinstance(session, str)
                or not 0 < len(session) <= 128 or isinstance(payload.get("protocol"), bool)
                or payload.get("protocol") not in (1, 2)):
            return result
        age = time.time() - timestamp
        result.update(connected=-2 <= age <= 5, heartbeatAge=round(age, 2),
                      writeEnabled=payload.get("writeEnabled") is True)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return result


def _digest(value: bytes | None) -> str:
    return hashlib.sha256(value or b"").hexdigest()


def _current_directory(service, data: Path) -> Path:
    directory = getattr(service.bridge, "directory", None)
    return directory if isinstance(directory, Path) else data / "ipc"


def diagnostics(service, data: Path) -> dict:
    """诊断不新建旧 IPC，不发送宿主命令；revision 不含不断变化的心跳时间。"""
    installed, raw, discovered = _read_script()
    current = _current_directory(service, data)
    current_heartbeat = heartbeat(current)
    installed_heartbeat = heartbeat(discovered)
    saved_invalid = bool(getattr(service, "_bridge_saved_invalid", False))
    if saved_invalid:
        # 默认目录即使有心跳，也只是未确认候选，不能在诊断或权限控件中
        # 冒充已经接入的当前连接；用户显式选择后才恢复真实连接状态。
        current_heartbeat.update(connected=False, writeEnabled=False)
    acquired = service.operation_lock.acquire(blocking=False)
    binding_acquired = service.bridge_binding_lock.acquire(blocking=False)
    try:
        busy = (not acquired or not binding_acquired or service.recording or
                any(job.get("state") == "running" for job in service.jobs.values()))
    finally:
        if acquired:
            service.operation_lock.release()
        if binding_acquired:
            service.bridge_binding_lock.release()
    reason = "script_not_running"
    message = "尚未收到有效心跳，请在 SynthV 脚本菜单运行 SynthV Assistant。"
    if saved_invalid:
        reason, message = "saved_connection_invalid", "之前选择的脚本绑定已变化；已暂停宿主连接，请明确选择当前资料目录或正在运行的脚本。"
    elif current_heartbeat["connected"]:
        reason, message = "connected", "已连接正在运行的 SynthV 脚本。"
    elif discovered is not None and discovered != current and installed_heartbeat["connected"]:
        reason, message = "directory_mismatch", "脚本正在运行，但桥接目录不同；可直接连接正在运行的脚本。"
    elif not installed["exists"]:
        reason, message = "script_missing", "没有找到本助手脚本，请安装匹配脚本后在 SynthV 中运行。"
    elif not installed["recognized"]:
        reason, message = "script_invalid", "已安装脚本的绑定无法安全识别，可备份后安装匹配脚本。"
    revision = _digest(json.dumps([str(data), str(current), installed, _digest(raw), saved_invalid],
                                  ensure_ascii=False, sort_keys=True).encode("utf-8"))
    can_install = False
    try:
        script_path()
        can_install = True
    except (OSError, ValueError):
        pass
    return {"schemaVersion": 1, "dataDirectory": str(data),
            "defaultIpcDirectory": str(data / "ipc"), "currentIpcDirectory": str(current),
            "connectionTarget": "current" if current == data / "ipc" else "installed",
            "installedScript": installed, "currentHeartbeat": current_heartbeat,
            "installedHeartbeat": installed_heartbeat, "reason": reason, "message": message,
            "revision": revision, "busy": bool(busy),
            "canConnectInstalled": bool(not busy and discovered is not None and (discovered != current or saved_invalid)
                                        and installed_heartbeat["connected"] and not installed_heartbeat["pendingRequest"]),
            "canUseCurrent": bool(not busy and (current != data / "ipc" or saved_invalid)),
            "canInstall": bool(not busy and can_install)}


def _atomic_bytes(path: Path, raw: bytes) -> None:
    """同目录独占临时文件加原子替换；失败也只清理本次临时文件。"""
    _ordinary_path(path, directory=False)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_choice(data: Path, target: str, directory: Path, raw: bytes | None) -> None:
    """只保存已确认的连接位置；不保存工程、心跳正文、会话内容或模型凭据。"""
    payload = {"version": 1, "target": target}
    if target == "installed":
        payload.update(ipcDirectory=str(directory), scriptSha256=_digest(raw))
    _atomic_bytes(data / CONFIG_NAME, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def restore_choice(service, data: Path) -> None:
    """重启时重新验证固定已安装脚本，文件或目录变化不会静默接入另一个位置。"""
    path = data / CONFIG_NAME
    if not path.exists():
        return
    try:
        _ordinary_path(path, directory=False)
        if path.stat().st_size > 4096:
            raise ValueError("连接配置过大。")
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved == {"version": 1, "target": "current"}:
            return
        if not isinstance(saved, dict) or set(saved) != {"version", "target", "ipcDirectory", "scriptSha256"}:
            raise ValueError("连接配置无效。")
        installed, raw, directory = _read_script()
        if (saved["version"] != 1 or saved["target"] != "installed" or directory is None
                or saved["ipcDirectory"] != str(directory) or saved["scriptSha256"] != _digest(raw)):
            raise ValueError("已安装脚本绑定发生变化。")
        _local_directory(directory, must_exist=True)
        service.bridge = BridgeClient(directory)
        service._bridge_operation_root = directory.parent
    except (OSError, ValueError, TypeError):
        service._bridge_saved_invalid = True
        service.bridge = _UnconfirmedBridgeClient(data / "ipc")
        service._bridge_operation_root = None


@contextmanager
def _reconfigure(service, data: Path, directories: list[Path]):
    """切换持有绑定锁、进程内状态锁和新旧跨进程锁，拒绝在途请求和未知写入。"""
    binding = service.bridge_binding_lock
    if not binding.acquire(blocking=False):
        raise BridgeSetupConflict(BUSY_MESSAGE)
    state_locked = False
    try:
        state_locked = service.operation_lock.acquire(blocking=False)
        if not state_locked or service.recording or any(job.get("state") == "running" for job in service.jobs.values()):
            raise BridgeSetupConflict(BUSY_MESSAGE)
        with ExitStack() as stack:
            roots = {data, *(directory.parent for directory in directories)}
            for root in sorted(roots, key=lambda item: os.path.normcase(str(item))):
                stack.enter_context(OperationLock(root / "operation.lock"))
            if any(heartbeat(directory)["pendingRequest"] for directory in directories):
                raise BridgeSetupConflict("桥接仍有未完成请求，请先确认宿主处理结果后再切换连接。")
            yield
    except OperationBusyError:
        raise BridgeSetupConflict(BUSY_MESSAGE) from None
    finally:
        if state_locked:
            service.operation_lock.release()
        binding.release()


def _validate_revision(service, data: Path, payload: dict, fields: set[str]) -> dict:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("桥接操作参数无效，请使用连接管理页面。")
    snapshot = diagnostics(service, data)
    if not isinstance(payload["revision"], str) or payload["revision"] != snapshot["revision"]:
        raise BridgeSetupConflict("桥接诊断已变化，请刷新后重试。")
    return snapshot


def connect(service, data: Path, payload: dict) -> dict:
    """接入已识别且活跃的旧脚本，或返回当前资料目录；不读取或迁移业务数据。"""
    _validate_revision(service, data, payload, {"target", "revision"})
    target = payload["target"]
    if target not in ("current", "installed"):
        raise ValueError("请选择当前资料目录或已安装脚本。")
    installed, raw, discovered = _read_script()
    directory = data / "ipc" if target == "current" else discovered
    if directory is None:
        raise ValueError("无法安全识别已安装脚本的桥接目录。")
    _local_directory(directory, must_exist=True)
    if target == "installed" and not heartbeat(directory)["connected"]:
        raise BridgeSetupConflict("已安装脚本没有有效心跳，请在 SynthV 启动后刷新诊断。")
    with _reconfigure(service, data, [_current_directory(service, data), directory]):
        _validate_revision(service, data, payload, {"target", "revision"})
        # 锁定之后再次读取脚本与心跳，避免检查和使用之间切换到未知目标。
        if target == "installed":
            _, latest_raw, latest = _read_script()
            if latest != directory or _digest(latest_raw) != _digest(raw) or not heartbeat(directory)["connected"]:
                raise BridgeSetupConflict("已安装脚本绑定或运行状态已变化，请刷新诊断。")
        client = BridgeClient(directory)
        _save_choice(data, target, directory, raw)
        service.bridge = client
        service._bridge_operation_root = None if target == "current" else directory.parent
        service._bridge_saved_invalid = False
    return diagnostics(service, data)


def install(service, data: Path, payload: dict) -> dict:
    """备份固定同名脚本后原子安装；不停止 SynthV、不运行 Lua、不改动工程。"""
    _validate_revision(service, data, payload, {"confirm", "revision"})
    if payload["confirm"] is not True:
        raise ValueError("请确认备份并安装匹配脚本。")
    target = script_path()
    current = _local_directory(data / "ipc", must_exist=True)
    _, _, installed_directory = _read_script()
    directories = [_current_directory(service, data), current]
    if installed_directory is not None and installed_directory.is_dir():
        directories.append(installed_directory)
    with _reconfigure(service, data, directories):
        target.parent.mkdir(parents=True, exist_ok=True)
        # 两个不同资料目录也会安装同一个宿主脚本，因此还需固定目标旁的锁。
        # 不删除锁文件，避免多个安装器锁住不同的文件对象。
        with OperationLock(target.parent / "SynthVAssistant.install.lock"):
            _validate_revision(service, data, payload, {"confirm", "revision"})
            target = script_path()
            generated = render_script(current).encode("utf-8")
            backup_created = target.exists()
            previous = None
            if backup_created:
                if target.stat().st_size > MAX_SCRIPT_BYTES:
                    raise ValueError("现有脚本异常过大，未执行替换；请先人工核对。")
                previous = target.read_bytes()
                backups = _local_directory(data / "backups" / "bridge", must_exist=False)
                backups.mkdir(parents=True, exist_ok=True)
                backup = backups / ("SynthVAssistant-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex + ".lua.bak")
                with backup.open("xb") as output:
                    output.write(previous)
                    output.flush()
                    os.fsync(output.fileno())
            # 写入前最后复核固定路径，不能跟随安装期间出现的重定向文件。
            if script_path() != target:
                raise BridgeSetupConflict("脚本安装位置已变化，请刷新诊断。")
            client = BridgeClient(current)
            _atomic_bytes(target, generated)
            try:
                _save_choice(data, "current", current, None)
            except (OSError, ValueError):
                # 配置保存失败时回滚本次安装，不能留下“脚本已改、连接未改”的假成功。
                # 若外部工具又修改了文件，拒绝覆盖第三方修改并明确要求重新诊断。
                try:
                    if target.read_bytes() != generated:
                        raise OSError("安装后文件已变化。")
                    if previous is None:
                        target.unlink()
                    else:
                        _atomic_bytes(target, previous)
                except (OSError, ValueError):
                    raise BridgeSetupConflict("脚本可能已更新，但连接配置未保存；请刷新诊断并保留备份。") from None
                raise
            service.bridge = client
            service._bridge_operation_root = None
            service._bridge_saved_invalid = False
    result = diagnostics(service, data)
    result.update(restartRequired=True, backupCreated=backup_created,
                  message="脚本已安装。请在 SynthV 重新扫描脚本，结束旧桥接后重新运行 SynthV Assistant。")
    return result
