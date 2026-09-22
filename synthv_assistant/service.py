"""控制台与 MCP 共用的业务层，集中管理录音、参数编辑和评审任务。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import unicodedata
import uuid

from .bridge import BridgeClient, BridgeError, atomic_json
from .capture import CaptureError, find_capture_executable, prepare_capture
from .config import DATA, RECORDINGS, ensure_directories
from .operations import exclusive_operation, operation_busy
from .metadata import LibraryMetadata, validate_identity
from .parameters import LEGACY_PARAMETERS, finite, public_preview, validate_change, selection_preview_notes


def finite_number(value, name: str, low: float, high: float) -> float:
    """统一拒绝 bool、NaN、无穷值及超出范围的时间参数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}必须是数字。")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}必须在 {low:g} 到 {high:g} 之间。")
    return float(value)


def find_synthv_pid() -> int:
    """只识别 SynthV 进程；多个实例时要求明确指定，避免录错工程。"""
    if os.name != "nt":
        raise CaptureError("真实音频采集当前只支持 Windows。")
    from .processes import synthv_process_ids
    try:
        pids = synthv_process_ids()
    except OSError as error:
        raise CaptureError("无法读取 SynthV 进程列表：" + str(error)) from error
    explicit = os.environ.get("SYNTHV_ASSISTANT_PID")
    if explicit:
        selected = int(finite_number(int(explicit), "进程号", 1, 2**31))
        if selected not in pids:
            raise CaptureError("指定进程不是正在运行的 SynthV 实例，请重新检查进程号。")
        return selected
    if len(pids) != 1:
        raise CaptureError("需要一个已打开的 SynthV 实例；多个实例请通过 SYNTHV_ASSISTANT_PID 指定。")
    return pids[0]


class AssistantService:
    def __init__(self):
        ensure_directories()
        self.bridge = BridgeClient()
        self.jobs: dict[str, dict] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synthv-audio")
        self.operation_lock = threading.RLock()
        self.recording = False
        # 会话管理器延迟构造；现有 MCP/录音功能不必提前加载模型调教模块。
        self._conversations = None

    def conversations(self):
        """共享唯一会话管理器，其持久化锁独立于工程操作锁。"""
        with self.operation_lock:
            if self._conversations is None:
                from .conversations import ConversationManager
                self._conversations = ConversationManager(self)
            return self._conversations

    def list_conversations(self):
        return self.conversations().list_conversations()

    def create_conversation(self, title="新调教会话"):
        return self.conversations().create_conversation(title)

    def get_conversation(self, identifier):
        return self.conversations().get_conversation(identifier)

    def update_conversation_metadata(self, identifier, payload):
        return self.conversations().update_metadata(identifier, payload)

    def update_conversation_model_options(self, identifier, payload):
        return self.conversations().update_model_options(identifier, payload)

    def update_conversation_render_mode(self, identifier, payload):
        """只保存本会话的绘制偏好，不修改已有动作或宿主参数。"""
        return self.conversations().update_render_mode(identifier, payload)

    def list_model_platforms(self):
        from .platforms import list_model_platforms
        return list_model_platforms()

    def get_model_platform(self, identifier):
        from .platforms import get_model_platform
        return get_model_platform(identifier)

    def save_model_platform(self, payload):
        from .platforms import save_model_platform
        return save_model_platform(payload)

    def set_default_model_platform(self, payload):
        """只保存新会话默认平台偏好，不读取模型音频、不发送供应商请求。"""
        from .platforms import set_default_model_platform
        return set_default_model_platform(payload)

    def list_platform_models(self, identifier):
        from .model_catalog import list_platform_models
        return list_platform_models(identifier)

    def cached_platform_models(self, identifier):
        """平台切换只读本机目录缓存；显式刷新才访问模型供应商。"""
        from .model_catalog import get_cached_platform_models
        return get_cached_platform_models(identifier)

    def model_capabilities(self, payload):
        """能力查询只读取脱敏平台信息；选择框刷新不产生外部请求。"""
        from .model_options import normalize_model_options, capabilities
        if not isinstance(payload, dict) or set(payload) - {"platformId", "model"}:
            raise ValueError("模型能力查询参数无效。")
        options = normalize_model_options(payload)
        return capabilities(self.get_model_platform(options["platformId"]), options["model"])

    def delete_conversation(self, identifier):
        return self.conversations().delete_conversation(identifier)

    def purge_conversation(self, identifier, payload):
        return self.conversations().purge_conversation(identifier, payload)

    def update_asset_metadata(self, kind, identifier, payload):
        from .assets import update_asset_metadata
        return update_asset_metadata(kind, identifier, payload)

    def delete_asset(self, kind, identifier):
        from .assets import delete_asset
        return delete_asset(kind, identifier)

    def purge_asset(self, kind, identifier, payload):
        from .assets import purge_asset
        return purge_asset(kind, identifier, payload)

    def purge_trash(self, kind, identifier, payload):
        """回收站入口要求已有删除标记，不能借该路由删除仍活跃的资源。"""
        validate_identity(kind, identifier)
        if kind == "conversation":
            return self.conversations().purge_conversation(identifier, payload, require_trash=True)
        from .assets import purge_asset
        return purge_asset(kind, identifier, payload, require_trash=True)

    def read_audio(self, kind, identifier):
        """持资源锁完成播放器读入；发送响应前释放，避免 purge 与 read_bytes 竞争。"""
        with LibraryMetadata(DATA).use_assets([{"kind": kind, "id": identifier}]):
            try:
                path = self.upload_path(identifier) if kind == "upload" else self.recording_path(identifier)
                return path, path.read_bytes()
            except OSError:
                raise ValueError("音频无法读取或已被删除，请刷新资料列表。") from None

    def list_trash(self):
        """回收站只提供本地标记摘要，不读取密钥，也不加载原始 WAV。"""
        return LibraryMetadata(DATA).trash()

    def restore_resource(self, kind, identifier):
        validate_identity(kind, identifier)
        if kind == "conversation":
            return self.conversations().restore_conversation(identifier)
        from .assets import restore_asset
        return restore_asset(kind, identifier)

    def send_message(self, identifier, text, include_selection, attachments, model_options=None, render_mode=None, on_progress=None):
        """只生成待预览建议；发送自然语言不能直接写入 SynthV 工程。"""
        # 未提供模式的旧 MCP 调用保留原签名，实际默认值仍由会话持久层决定。
        extra = {"render_mode": render_mode} if render_mode is not None else {}
        return self.conversations().send_message(identifier, text, include_selection, attachments,
                                                 model_options=model_options, on_progress=on_progress, **extra)

    def preview_action(self, identifier):
        return self.conversations().preview_action(identifier)

    def reuse_message(self, identifier, message_id):
        """仅在本地复制参数建议到当前选区；不启动模型任务，也不复制旧确认票据。"""
        return self.conversations().reuse_message(identifier, message_id)

    def apply_action(self, identifier):
        """会话模块校验预览状态后再调用现有的宿主编辑入口。"""
        return self.conversations().apply_action(identifier)

    def preview_action_batch(self, action_ids):
        """组合提案仍由会话状态机验证，浏览器不能直接提供宿主参数或快照。"""
        return self.conversations().preview_batch(action_ids)

    def apply_action_batch(self, batch_id):
        """只接受上次只读预览签发的批次编号，不接受客户端重组成功项。"""
        return self.conversations().apply_batch(batch_id)

    def list_uploads(self):
        from .assets import list_uploads
        return list_uploads()

    def save_upload(self, filename, data):
        from .assets import save_upload
        return save_upload(filename, data)

    def upload_path(self, identifier):
        from .assets import upload_path
        return upload_path(identifier)

    def status(self) -> dict:
        from .review import provider_status
        bridge = self.bridge.status()
        try:
            busy = operation_busy(DATA / "operation.lock")
            operation_error = None
        except OSError:
            # 锁探测失败不能被解释成“空闲”；真正修改仍会重新申请锁并失败关闭。
            busy = None
            operation_error = "无法读取跨进程操作锁，请检查本地数据目录访问权限。"
        try:
            executable = find_capture_executable()
            # recording 只描述本服务的任务。其他进程可能正在编辑而非录音，
            # 因此跨进程互斥状态单独返回 operationBusy，不能混作 recording。
            capture = {"available": True, "scope": "process_tree", "recording": self.recording,
                       "recordingStatusScope": "this_service"}
        except CaptureError as error:
            capture = {"available": False, "message": str(error)}
        return {"bridge": bridge, "capture": capture, "review": provider_status(),
                "operationBusy": busy, "operationStatusError": operation_error,
                "writeEnabled": bool(bridge.get("connected") and bridge.get("writeEnabled")), "version": "0.1.0"}

    def get_project(self) -> dict:
        return self.bridge.call("get_project")

    def get_selection(self) -> dict:
        return self.bridge.call("get_selection")

    @exclusive_operation(lambda _service: DATA / "operation.lock")
    def register_vocal_mode(self, payload: dict) -> dict:
        """把用户已核对的声线名称临时补入当前宿主目录，不写工程或持久文件。

        官方 getVoice 返回值不保证枚举全部模式，因此这个入口仅接收用户在声线
        面板确认的原名。页面须附带读取时的身份快照，由 Lua 在同一次处理内核对；
        不能在工程、音符组或声库已切换后，把旧页面输入注册到新的上下文中。
        """
        with self.operation_lock:
            if self.recording:
                raise ValueError("正在录音，请等待结束后补充声线目录。")
            if not isinstance(payload, dict) or set(payload) != {"name", "selection"}:
                raise ValueError("补充声线目录只接受名称和当前选区身份。")
            raw_name, selection = payload["name"], payload["selection"]
            if not isinstance(raw_name, str):
                raise ValueError("声线名称必须是非空文本，且 UTF-8 长度不得超过 80 字节。")
            name = raw_name.strip()
            try:
                valid_name = 1 <= len(name.encode("utf-8")) <= 80
            except UnicodeError:
                valid_name = False
            if not valid_name or any(unicodedata.category(character) == "Cc" for character in raw_name):
                raise ValueError("声线名称必须是非空原名，不含控制字符，且 UTF-8 长度不得超过 80 字节。")
            fields = ("projectFile", "groupUUID", "groupOffset", "groupPitchOffset", "voiceFingerprint")
            if not isinstance(selection, dict) or any(key not in selection for key in fields):
                raise ValueError("当前选区身份不完整，请重新读取选区后补充声线名称。")
            expected = {key: selection[key] for key in fields}
            # projectFile 可为空，允许尚未保存工程的只读目录管理；开启写入仍由
            # 原有备份流程另行要求已保存 SVP。这里不解析或访问任何客户端路径。
            if (not isinstance(expected["projectFile"], str) or len(expected["projectFile"]) > 4096
                    or not isinstance(expected["groupUUID"], str) or not 1 <= len(expected["groupUUID"]) <= 128
                    or not isinstance(expected["voiceFingerprint"], str) or not 1 <= len(expected["voiceFingerprint"]) <= 256
                    or not finite(expected["groupOffset"], -(2 ** 53), 2 ** 53)
                    or not finite(expected["groupPitchOffset"], -127, 127)):
                raise ValueError("当前选区身份格式无效，请重新读取选区后补充声线名称。")
            result = self.bridge.call("register_vocal_mode", {"name": name, "expected": expected})
            if not isinstance(result, dict):
                raise ValueError("宿主未返回有效的更新目录，请重新读取选区。")
            return result

    def get_audio_settings(self) -> dict:
        """返回可展示的听评设置，密钥只在后端请求供应商时使用。"""
        from .settings import get_audio_settings
        return get_audio_settings()

    def update_audio_settings(self, payload: dict) -> dict:
        """即时保存本机配置；不会为验证设置而上传音频或自动调用模型。

        设置模块独立处理加密、原子写入和版本冲突，避免阻塞工程编辑锁。
        后续听评会重新读取完整配置，已经开始的请求继续使用原有快照。
        """
        from .settings import update_audio_settings
        return update_audio_settings(payload)

    def clear_audio_settings(self) -> dict:
        """删除已保存密钥并停用听评，保留显式停用状态以覆盖环境配置。"""
        from .settings import clear_audio_settings
        return clear_audio_settings()

    @exclusive_operation(lambda _service: DATA / "operation.lock")
    def write_mode(self, enabled: bool) -> dict:
        """开启写入前备份磁盘版本；未保存的更改仍须由用户先另存副本。"""
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值。")
        with self.operation_lock:
            if self.recording:
                raise ValueError("正在录音，请等待结束后切换编辑模式。")
            backup = None
            expected_project = None
            if enabled:
                project = self.get_project()
                expected_project = project.get("projectFile")
                source = Path(project.get("projectFile", ""))
                if not source.is_file() or source.suffix.lower() != ".svp":
                    raise ValueError("当前工程尚无可访问的已保存 SVP 文件，请先另存副本。")
                backup = DATA / "backups" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".svp")
                shutil.copy2(source, backup)
            # 备份与 IPC 之间用户可能切换工程；由宿主再次校验同一完整路径。
            result = self.bridge.call("write_mode", {"enabled": enabled, "expectedProject": expected_project})
            if backup:
                result["backupFile"] = str(backup)
            return result

    @exclusive_operation(lambda _service: DATA / "operation.lock")
    def preview(self, parameter: str, delta: float | None = None, *, curve=None, render_mode="smooth") -> dict:
        """生成经过能力协商的预览，写入始终由另一次明确的 apply 完成。

        五种旧增量保留原 IPC 形状；新增参数、绘制曲线或 points 表示必须先读取
        本次宿主目录。Lua 还会再次检查读取与预览之间的选区/声库变化。
        """
        with self.operation_lock:
            if self.recording:
                raise ValueError("请等待录音结束后预览参数修改。")
            if not isinstance(parameter, str):
                raise ValueError("参数名称必须是字符串。")
            selection = None
            if curve is not None or render_mode != "smooth" or parameter not in LEGACY_PARAMETERS:
                selection = self.get_selection()
            args = validate_change(parameter, delta, curve=curve, render_mode=render_mode, selection=selection)
            preview = self.bridge.call("preview", args)
            if selection is not None:
                preview = {**preview, "notes": selection_preview_notes(selection)}
            return public_preview(preview)

    @exclusive_operation(lambda _service: DATA / "operation.lock")
    def preview_batch(self, batch_id: str, actions: list[dict]) -> dict:
        """在一次宿主请求中预演最多五个参数，安全转换逐项失败信息。

        原始宿主异常仅用于本机桥接诊断；HTTP 返回严格使用固定提示白名单。
        Lua 与 Python 都校验条目身份，防止畸形响应交换两个参数的确认对象。
        """
        with self.operation_lock:
            if self.recording:
                raise ValueError("请等待录音结束后预览参数修改。")
            if not isinstance(batch_id, str) or re.fullmatch(r"[0-9a-f]{32}", batch_id) is None:
                raise ValueError("组合预览编号无效。")
            if not isinstance(actions, list) or not 1 <= len(actions) <= 5:
                raise ValueError("组合预览须包含 1 至 5 个参数。")
            selection = self.get_selection()
            requests = []
            for action in actions:
                args = validate_change(action["parameter"], action.get("delta"), curve=action.get("curve"),
                                       render_mode=action.get("renderMode", "smooth"), selection=selection)
                requests.append({"actionId": action["id"], "change": args})
            raw = self.bridge.call("preview_batch", {"batchId": batch_id, "actions": requests}, timeout=40.0)
            if not isinstance(raw, dict) or raw.get("batchId") != batch_id or not isinstance(raw.get("items"), list):
                raise ValueError("宿主组合预览响应无效，尚未修改工程。")
            if len(raw["items"]) != len(actions):
                raise ValueError("宿主组合预览条目不完整，尚未修改工程。")
            previews, errors = [], []
            for action, item in zip(actions, raw["items"]):
                if not isinstance(item, dict) or item.get("actionId") != action["id"]:
                    raise ValueError("宿主组合预览身份不匹配，尚未修改工程。")
                if item.get("ok") is True:
                    try:
                        preview = public_preview(item.get("preview"))
                        if preview.get("parameter") != action["parameter"]:
                            raise ValueError("宿主组合预览参数不匹配，尚未修改工程。")
                        previews.append({"actionId": action["id"], "preview": preview})
                    except ValueError:
                        errors.append({"actionId": action["id"], "message": "宿主预览资料无效，尚未修改工程。"})
                else:
                    safe = BridgeError(item.get("error")).public_message
                    errors.append({"actionId": action["id"], "message": "未生成预览，尚未修改工程：" + safe if safe
                                   else "无法生成此参数的宿主预览，尚未修改工程；请检查桥接和当前选区。"})
            return {"previews": previews, "errors": errors}

    @exclusive_operation(lambda _service: DATA / "operation.lock")
    def edit(self, action: str, args: dict | None = None) -> dict:
        if action not in {"apply", "apply_batch", "restore"}:
            raise ValueError("未知编辑操作。")
        with self.operation_lock:
            if self.recording:
                raise ValueError("正在录音，暂不能修改参数。")
            result = self.bridge.call(action, args)
            self._log(action, result)
            return result

    def submit(self, operation, *args) -> str:
        """耗时工作放入有界队列，界面与 MCP 不需要长时间保持一个 HTTP 请求。"""
        with self.operation_lock:
            if any(job["state"] == "running" for job in self.jobs.values()):
                raise ValueError("当前还有录音或 AI 任务，请等待它完成。")
            identifier = uuid.uuid4().hex
            self.jobs[identifier] = {"state": "running", "createdAt": time.time()}
            # 完成任务只保留最近50项，避免长期开启时内存持续增长。
            for old in list(self.jobs)[:-50]:
                if self.jobs[old]["state"] != "running":
                    del self.jobs[old]

        started = time.monotonic()

        def progress(value):
            """整体替换任务快照，轮询线程不会读到半写入的摘要或字典。"""
            if not isinstance(value, dict):
                return
            previous = self.jobs.get(identifier, {})
            if previous.get("state") != "running":
                return
            safe = dict(previous.get("progress", {}))
            for name, limit in (("stage", 120), ("text", 6000), ("reasoning", 12000)):
                if isinstance(value.get(name), str):
                    safe[name] = value[name][:limit]
            safe["reasoningAvailable"] = bool(safe.get("reasoning"))
            safe["elapsedSeconds"] = round(time.monotonic() - started, 1)
            if isinstance(value.get("receivedCharacters"), int):
                safe["receivedCharacters"] = max(0, min(24000, value["receivedCharacters"]))
            self.jobs[identifier] = {**previous, "progress": safe}

        def worker():
            try:
                if operation == self.send_message:
                    progress({"stage": "正在准备请求", "text": "", "reasoning": ""})
                    result = operation(*args, on_progress=progress)
                else:
                    result = operation(*args)
                self.jobs[identifier] = {"state": "done", "result": result,
                                         "progress": self.jobs[identifier].get("progress", {})}
            except Exception as error:
                # 主动验证错误及桥接错误可供用户处理；系统/编程异常可能含路径、
                # 配置或内部响应，不能原样存入供网页查询的任务结果。
                message = str(error) if isinstance(error, (ValueError, BridgeError, CaptureError)) else "任务未完成，请检查本机服务状态后重试。"
                # 网络断开等失败也保留已经验证和脱敏的进度，便于查看先前收到的摘要。
                self.jobs[identifier] = {"state": "error", "error": message,
                                         "progress": self.jobs[identifier].get("progress", {})}

        self.executor.submit(worker)
        return identifier

    @exclusive_operation(lambda _service: DATA / "operation.lock")
    def record(self, start_seconds: float, duration_seconds: float, label: str = "片段") -> dict:
        """先启动采集，再播放；默认录制工程当前混音，不擅自改变轨道静音/独奏。"""
        from .analysis import analyze_wav
        start = finite_number(start_seconds, "开始时间", 0, 24 * 3600)
        duration = finite_number(duration_seconds, "片段时长", 1, 30)
        if not isinstance(label, str) or len(label) > 80:
            raise ValueError("录音名称最长80个字符。")
        with self.operation_lock:
            if self.recording:
                raise ValueError("已有录音正在进行。")
            self.recording = True
        session = None
        playing = False
        resources = ExitStack()
        try:
            project = self.get_project()
            if project.get("playbackStatus") != "stopped":
                raise ValueError("请先停止 SynthV 当前播放。")
            pid = find_synthv_pid()
            identifier = uuid.uuid4().hex
            # 录音完成与审计写入之前持有素材锁；即使列表已看到完整文件，删除也
            # 必须等待这个录音任务完全结束，不会返回一个刚生成却已删的结果。
            resources.enter_context(LibraryMetadata(DATA).resource_lock("recording", identifier))
            output = RECORDINGS / (identifier + ".wav")
            session = prepare_capture(pid, duration + 1.0, output)
            ready = session.start()
            time.sleep(0.15)
            request_time = datetime.now(timezone.utc).isoformat()
            # 请求超时也可能已经启动播放，因此在发送之前登记停止责任。
            playing = True
            playback = self.bridge.call("play_segment", {"startSeconds": start, "durationSeconds": duration})
            result = session.wait()
            analysis = analyze_wav(output)
            item = {"id": identifier, "label": label, "url": "/audio/" + identifier + ".wav",
                    "createdAt": datetime.now(timezone.utc).isoformat(), "startSeconds": start,
                    "durationSeconds": duration, "analysis": analysis,
                    "capture": result, "playback": playback, "playRequestAt": request_time,
                    "projectFile": project["projectFile"], "mix": "工程当前完整混音",
                    "captureNote": "录音包含约1秒准备/尾部余量；播放与采集起点存在小量延迟，不用于毫秒级音素对齐。"}
            atomic_json(RECORDINGS / (identifier + ".json"), item)
            self._log("record", {"id": identifier, "silent": analysis["silent"]})
            return LibraryMetadata(DATA).decorate("recording", item)
        except Exception:
            if session:
                session.cancel()
            if playing:
                try:
                    self.bridge.call("stop_playback", timeout=3)
                except BridgeError:
                    pass
            raise
        finally:
            try:
                resources.close()
            finally:
                with self.operation_lock:
                    self.recording = False

    def recording_path(self, identifier: str) -> Path:
        """音频工具仅访问本项目生成的录音，拒绝任意本机路径。"""
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("无效的录音编号。")
        LibraryMetadata(DATA).assert_available("recording", identifier)
        path = RECORDINGS / (identifier + ".wav")
        if not path.is_file():
            raise ValueError("录音不存在。")
        return path

    def list_recordings(self) -> dict:
        items = []
        paths = []
        for path in RECORDINGS.glob("*.json"):
            try:
                paths.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
        for _, path in sorted(paths, reverse=True):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                self.recording_path(item["id"])
                items.append(LibraryMetadata(DATA).decorate("recording", item))
                if len(items) >= 100:
                    break
            except (OSError, ValueError, KeyError):
                continue
        return {"items": items}

    def compare(self, before_id: str, after_id: str) -> dict:
        validate_identity("recording", before_id)
        validate_identity("recording", after_id)
        attachments = [{"kind": "recording", "id": identifier} for identifier in dict.fromkeys([before_id, after_id])]
        with LibraryMetadata(DATA).use_assets(attachments):
            return self._compare_recordings(before_id, after_id)

    def _compare_recordings(self, before_id: str, after_id: str) -> dict:
        """持有素材锁后比较，回收站操作不会使同一次比较前后读取不同状态。"""
        from .analysis import compare_wavs
        result = compare_wavs(self.recording_path(before_id), self.recording_path(after_id))
        # 同长录音也可能来自不同乐句，必须检查项目与播放位置。
        before = json.loads((RECORDINGS / (before_id + ".json")).read_text(encoding="utf-8"))
        after = json.loads((RECORDINGS / (after_id + ".json")).read_text(encoding="utf-8"))
        if any(before.get(k) != after.get(k) for k in ("projectFile", "startSeconds", "durationSeconds")):
            result.setdefault("warnings", []).append("两段录音的工程或播放范围不同，不能据此判断调教改善。")
        return result

    def review(self, identifiers: list[str], prompt: str) -> dict:
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 2:
            raise ValueError("一次评审选择一段或两段录音。")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 4000:
            raise ValueError("请填写1至4000字的评审目标。")
        with LibraryMetadata(DATA).use_assets([{"kind": "recording", "id": identifier} for identifier in identifiers]):
            return self._review_recordings(identifiers, prompt)

    def _review_recordings(self, identifiers: list[str], prompt: str) -> dict:
        """模型调用全程持素材锁；用户备注只在覆盖层，绝不加入云端上下文。"""
        from .review import review_audio
        paths = [self.recording_path(identifier) for identifier in identifiers]
        context = {"recordings": []}
        for identifier in identifiers:
            metadata = json.loads((RECORDINGS / (identifier + ".json")).read_text(encoding="utf-8"))
            # 不向模型提交本机工程路径、进程号或系统信息。
            context["recordings"].append({key: metadata.get(key) for key in ("label", "startSeconds", "durationSeconds", "mix", "captureNote")})
        result = review_audio(paths, prompt, context)
        atomic_json(DATA / "logs" / ("review-" + uuid.uuid4().hex + ".json"), result)
        return result

    @staticmethod
    def _log(action: str, result: dict) -> None:
        """本地审计只记录操作结果，不记录密钥、完整工程或云请求头。"""
        atomic_json(DATA / "logs" / (uuid.uuid4().hex + ".json"), {"action": action, "at": time.time(), "result": result})
