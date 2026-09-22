"""持久化调教对话与人工确认状态机，不允许模型直接修改 SynthV。

会话文件包含公开消息和私有动作指纹；只有字段白名单能够进入 HTTP 响应或
模型上下文。动作必须经过 proposed → previewed → unknown → applied。
unknown 在宿主写入前落盘，进程崩溃、超时或写后保存失败都不能触发重复应用。
已确认 applied 的提案，仅在完整参数指纹证明撤销回到原状态后才能重新预览；
重新应用仍需新的宿主预览及用户确认，不能直接复用旧 previewId。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

from .config import DATA
from .bridge import BridgeError
from .operations import BUSY_MESSAGE, OperationBusyError, OperationLock
from .metadata import LibraryMetadata, MetadataError
from .model_options import normalize_model_options, validate_for_config
from .parameters import (ParameterError, public_parameter_catalog, public_preview, validate_action,
                         normalize_render_mode, selection_preview_notes)


def _default_platform_id() -> str:
    """仅新会话读取默认偏好；旧会话缺失 modelOptions 时仍按原 default 解释。"""
    from .platforms import get_default_model_platform_id
    return get_default_model_platform_id()


MAX_MESSAGES = 100
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CONVERSATIONS = 200
MAX_TEXT_CHARS = 4_000
MAX_REPLY_CHARS = 24_000
REPLY_RESERVE_BYTES = 192 * 1024
ACTION_STATES = {"proposed", "previewed", "applied", "unknown"}


class ConversationError(ValueError):
    """仅包含可以公开展示的固定提示，不拼接模型、密钥或文件系统原始异常。"""


def _now() -> str:
    """以 UTC ISO 时间记录顺序，跨进程可直接按字符串排序。"""
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ConversationError("会话或提案编号无效。")
    return value


def _encoded(value: object) -> bytes:
    """禁止 NaN/Infinity，保证持久文件与浏览器使用相同的 JSON 语义。"""
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ConversationError("会话包含无法保存的数据，请重新读取后重试。") from None


def _fingerprint(value: object) -> str:
    """只保存哈希，不把工程绝对路径或完整内部快照写入公开消息。"""
    return hashlib.sha256(_encoded(value)).hexdigest()


def _plan_tuning(text, selection, history, audio_paths, audio_context, **options):
    """延迟导入规划器，保持会话浏览不依赖云服务配置或网络。"""
    from .planner import plan_tuning
    return plan_tuning(text, selection, history, audio_paths, audio_context, **options)


def _resolve_attachments(attachments):
    """附件解析由固定 ID 的资产白名单实现，模型不能提供本机任意路径。"""
    from .assets import resolve_audio_attachments
    return resolve_audio_attachments(attachments)


def _safe_attachments(items: list) -> list:
    fields = {"kind", "id", "name", "label", "url", "mimeType", "durationSeconds", "sampleRate",
              "channels", "sizeBytes", "createdAt", "startSeconds", "endSeconds", "mix", "captureNote"}
    if not isinstance(items, list) or len(items) > 2 or any(not isinstance(item, dict) for item in items):
        raise ConversationError("音频附件信息无效，请重新选择附件。")
    return [{key: value for key, value in item.items() if key in fields} for item in items]


def _safe_selection(selection: dict) -> dict:
    """仅向模型和页面提供音符、参数摘要及时间范围，排除项目路径、会话和组标识。"""
    note_fields = {"index", "pitch", "lyrics", "onset", "duration", "onsetSeconds", "durationSeconds"}
    result = {key: selection.get(key) for key in ("startSeconds", "endSeconds", "noteCount")}
    if "groupPitchOffset" in selection:
        result["groupPitchOffset"] = selection["groupPitchOffset"]
    result["notes"] = [{key: value for key, value in note.items() if key in note_fields}
                       for note in selection["notes"]]
    result["parameters"] = public_parameter_catalog(selection)
    capabilities = selection.get("capabilities")
    if isinstance(capabilities, dict):
        result["capabilities"] = {key: capabilities[key] for key in ("curves", "nativePitch")
                                  if isinstance(capabilities.get(key), bool)}
    warnings = selection.get("capabilityWarnings")
    if isinstance(warnings, list):
        result["capabilityWarnings"] = [item for item in warnings[:16] if isinstance(item, str) and len(item) <= 1000]
    return result


def _selection_identity(selection: dict, session: str) -> str:
    """参数单独绑定，避免应用气声后让同一乐句的张力提案无故失效。"""
    fields = ("projectFile", "groupUUID", "groupOffset", "notes", "startSeconds", "endSeconds", "noteCount")
    identity = {**{key: selection.get(key) for key in fields}, "bridgeSession": session}
    # 缺少新字段的旧桥接维持原摘要格式；新桥接的声库身份与组移调均参与绑定。
    identity.update({key: selection[key] for key in ("groupPitchOffset", "voiceFingerprint") if key in selection})
    return _fingerprint(identity)


def _parameter_identity(selection: dict, parameter: str) -> str:
    definition = selection.get("parameters", {}).get(parameter)
    if not isinstance(definition, dict):
        raise ConversationError("当前选区没有该参数的有效摘要，请重新读取后规划。")
    # 新桥接提供完整参数 fingerprint，能识别点数不变的手工编辑；旧桥接仍只
    # 有点数摘要，保留兼容，但 apply 始终必须由 Lua 检查真实曲线，不能以此替代。
    identity = {key: definition.get(key) for key in ("range", "defaultValue", "pointCount")}
    identity.update({key: definition[key] for key in
                     ("fingerprint", "kind", "maxDelta", "modeName", "available") if key in definition})
    return _fingerprint(identity)


def _proposal_selection_identity(selection: dict) -> str:
    """提案的稳定目标身份不含连接编号和音符索引；应用仍核对严格预览身份。

    在组内插入其他音符可能只改变 index，相同起点的音符也可能换序。这些展示
    顺序变化不改变建议含义；歌词、音高、时值、秒坐标、声库与工程位置仍绑定。
    """
    snapshot = dict(selection)
    snapshot["notes"] = sorted(
        [{key: value for key, value in note.items() if key != "index"} for note in selection["notes"]],
        key=_encoded,
    )
    return _selection_identity(snapshot, "")


def _target_scope(selection: dict) -> dict:
    """以底层音符组及组内 blick 定位，组移动和变速不能绕过已应用重叠保护。

    声音曲线属于 NoteGroup 而非其工程时间位置。只比较绝对秒范围或把组偏移
    加进组身份，会将移动后的同一条曲线误认成新目标，重复叠加已经应用的增量。
    缺少可靠组内时间时保留空范围，后续对已应用方案拒绝猜测。
    """
    notes = selection.get("notes", [])
    valid = notes and all(isinstance(note, dict) and all(
        not isinstance(note.get(key), bool) and isinstance(note.get(key), (int, float))
        and math.isfinite(note[key]) for key in ("onset", "duration")) and note["duration"] > 0 for note in notes)
    return {"group": _fingerprint({key: selection.get(key) for key in ("projectFile", "groupUUID")}),
            "start": min(note["onset"] for note in notes) if valid else None,
            "end": max(note["onset"] + note["duration"] for note in notes) if valid else None}


def _melody_layout(selection: dict) -> list[tuple[float, float, float]]:
    """用实际音高和归一化秒坐标描述旋律，允许平移/整体拉伸，不忽略休止与变速。

    参数曲线使用选区时间的 0..1 坐标，因此比较秒比例比只比较节拍更可靠。
    数值必须有限，缺失音符、零时长或不完整快照不能冒充结构一致。
    """
    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConversationError("音高方案缺少可靠的音符和时间资料，无法复用；请重新读取选区生成音高建议。")
        return float(value)

    start, end = number(selection.get("startSeconds")), number(selection.get("endSeconds"))
    notes = selection.get("notes")
    if end <= start or not isinstance(notes, list) or not 1 <= len(notes) <= 128:
        raise ConversationError("音高方案的选区时间或音符资料无效，无法复用。")
    offset = number(selection.get("groupPitchOffset", 0))
    result = []
    for note in notes:
        if not isinstance(note, dict):
            raise ConversationError("音高方案的音符资料无效，无法复用。")
        onset, duration = number(note.get("onsetSeconds")), number(note.get("durationSeconds"))
        if duration <= 0 or onset < start - 1e-6 or onset + duration > end + 1e-6:
            raise ConversationError("音高方案的音符超出选区或时长无效，无法复用。")
        result.append(((onset - start) / (end - start), (onset + duration - start) / (end - start),
                       number(note.get("pitch")) + offset))
    result.sort()
    if any(left[1] > right[0] + 1e-6 for left, right in zip(result, result[1:])):
        raise ConversationError("选区存在重叠音符，无法确定单条音高曲线的对应关系；请缩小选区后复用。")
    return result


def _public_action(action: dict) -> dict:
    """只投影状态机的公开字段，私有指纹与会话不会进入前端响应。"""
    fields = {"id", "parameter", "delta", "curve", "renderMode", "reason", "status", "preview", "result",
              "label", "unit", "kind", "modeName", "previewBatchId"}
    return {key: value for key, value in action.items() if key in fields}


def _public_document(document: dict) -> dict:
    result = {key: document[key] for key in ("id", "title", "createdAt", "updatedAt")}
    result["modelOptions"] = normalize_model_options(document.get("modelOptions"))
    result["renderMode"] = normalize_render_mode(document.get("renderMode", "smooth"))
    fields = {"id", "role", "text", "createdAt", "attachments", "inputMode", "provider", "model", "selection",
              "platformId", "reasoningEffort", "reasoningSummary", "renderMode", "origin"}
    result["messages"] = [{**{key: value for key, value in message.items() if key in fields},
                           "actions": [_public_action(action) for action in message.get("actions", [])]}
                          for message in document["messages"]]
    # 返回深拷贝，调用者修改响应不能污染即将保存的内部状态。
    return json.loads(_encoded(result))


class ConversationManager:
    """以固定文件锁协调 HTTP/MCP 进程；云请求只占用对应会话的锁。"""

    def __init__(self, service):
        self.service = service
        self.data = Path(DATA)
        self.directory = self.data / "conversations"
        self.locks = self.data / "conversation-locks"
        self.library = LibraryMetadata(self.data)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.locks.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _lock(self, conversation_id: str | None = None):
        path = self.locks / (conversation_id + ".lock") if conversation_id else self.data / "assistant.lock"
        try:
            with OperationLock(path):
                yield
        except OperationBusyError:
            raise ConversationError("该会话或调教提案正在处理，请等待当前操作完成后重试。") from None
        except OSError:
            raise ConversationError("无法访问本地会话数据，请检查数据目录权限。") from None

    def _paths(self) -> list[Path]:
        """有界扫描避免会话索引无界增长；不扫描项目目录或用户工程。"""
        paths = []
        for path in self.directory.glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{32}", path.stem):
                if self.library.read("conversation", path.stem).get("deletedAt"):
                    continue
                paths.append(path)
                if len(paths) > MAX_CONVERSATIONS:
                    raise ConversationError("本地会话数量超过上限，请先整理会话存档。")
        return paths

    def _read(self, identifier: str, *, allow_deleted: bool = False) -> dict:
        path = self.directory / (_identifier(identifier) + ".json")
        if not allow_deleted:
            self.library.assert_available("conversation", identifier)
        try:
            with path.open("rb") as source:
                raw = source.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ConversationError("会话文件超过 2 MB，无法继续处理。")
            document = json.loads(raw)
            if (not isinstance(document, dict) or document.get("id") != identifier
                    or not isinstance(document.get("messages"), list) or len(document["messages"]) > MAX_MESSAGES
                    or not isinstance(document.get("_private"), dict)):
                raise ConversationError("会话文件格式无效，请新建会话。")
            _public_document(document)
            return document
        except FileNotFoundError:
            raise ConversationError("会话不存在。") from None
        except ConversationError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise ConversationError("无法读取完整会话文件，请检查本地会话存档。") from None

    def _save(self, document: dict) -> None:
        """写临时文件、fsync 后原子替换；任何失败保留完整旧版状态。"""
        if len(document["messages"]) > MAX_MESSAGES:
            raise ConversationError("单个会话最多 100 条消息，请新建会话。")
        document["updatedAt"] = _now()
        raw = _encoded(document)
        if len(raw) > MAX_FILE_BYTES:
            raise ConversationError("会话已达到 2 MB 上限，请新建会话。")
        destination = self.directory / (_identifier(document["id"]) + ".json")
        temporary = self.directory / ("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        except OSError:
            raise ConversationError("会话保存失败；请检查数据目录，勿重复应用结果未知的提案。") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _public(self, document: dict) -> dict:
        """本地备注不写回消息；历史附件每次展示时重新核对当前删除/可用状态。"""
        result = self.library.decorate("conversation", _public_document(document))
        for message in result["messages"]:
            for attachment in message.get("attachments", []):
                kind, identifier = attachment.get("kind"), attachment.get("id")
                try:
                    state = self.library.read(kind, identifier)
                    directory = self.data / ("uploads" if kind == "upload" else "recordings")
                    complete = ((directory / (identifier + ".wav")).is_file()
                                and (directory / (identifier + ".json")).is_file())
                    attachment.update(deleted=bool(state.get("deletedAt")),
                                      permanent=bool(state.get("permanent")),
                                      available=complete and not bool(state.get("deletedAt")))
                    if state.get("label"):
                        attachment["label"] = state["label"]
                except (MetadataError, OSError, TypeError):
                    # 坏标记和丢失文件不能借历史播放器重新暴露，但不阻止阅读其他消息。
                    attachment.update(available=False)
        return result

    def list_conversations(self) -> dict:
        items = []
        for path in self._paths():
            try:
                document = self._read(path.stem)
            except (ConversationError, MetadataError):
                # 枚举与读取之间可能完成删除；只跳过已确认删除或消失的条目。
                if not path.exists() or self.library.read("conversation", path.stem).get("deletedAt"):
                    continue
                raise
            summary = {**{key: document[key] for key in ("id", "title", "createdAt", "updatedAt")},
                       "messageCount": len(document["messages"])}
            items.append(self.library.decorate("conversation", summary))
        return {"items": sorted(items, key=lambda item: item["updatedAt"], reverse=True)}

    def create_conversation(self, title: str = "新调教会话") -> dict:
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            raise ConversationError("会话标题应为 1 至 80 个字符。")
        with self._lock():
            if len(self._paths()) >= MAX_CONVERSATIONS:
                raise ConversationError("最多保留 200 个会话，请先整理会话存档。")
            now = _now()
            document = {"id": uuid.uuid4().hex, "title": title.strip(), "createdAt": now, "updatedAt": now,
                        "messages": [], "_private": {"actions": {}}, "renderMode": "smooth",
                        "modelOptions": {"platformId": _default_platform_id(), "model": "",
                                         "reasoningEffort": "default"}}
            self._save(document)
            return self._public(document)

    def get_conversation(self, identifier: str) -> dict:
        return self._public(self._read(identifier))

    def update_metadata(self, identifier: str, payload: dict) -> dict:
        """竞争会话请求锁，避免重命名与首次消息自动标题覆盖互相干扰。"""
        identifier = _identifier(identifier)
        with self._lock(identifier):
            document = self._read(identifier)
            self.library.update("conversation", identifier, payload)
            return self._public(document)

    def update_model_options(self, identifier: str, payload: dict) -> dict:
        """模型选择属于会话；与发送共享锁，不能改变正在运行请求的选择。"""
        from .platforms import get_model_platform
        options = normalize_model_options(payload)
        config = dict(get_model_platform(options["platformId"]))
        config["model"] = options["model"] or config["model"]
        validate_for_config(config, options)
        with self._lock(_identifier(identifier)), self._lock():
            document = self._read(identifier)
            document["modelOptions"] = options
            self._save(document)
            return self._public(document)

    def update_render_mode(self, identifier: str, payload: dict) -> dict:
        """绘制模式属于当前会话；与发送共用锁，不能改变已开始规划的请求。"""
        if not isinstance(payload, dict) or set(payload) != {"renderMode"}:
            raise ConversationError("绘制设置只接受 renderMode 字段。")
        mode = normalize_render_mode(payload["renderMode"])
        with self._lock(_identifier(identifier)), self._lock():
            document = self._read(identifier)
            document["renderMode"] = mode
            self._save(document)
            return self._public(document)

    def delete_conversation(self, identifier: str) -> dict:
        """只标记移入回收站；模型/预览/应用持有同一会话锁时明确拒绝。"""
        identifier = _identifier(identifier)
        with self._lock(identifier):
            document = self._read(identifier, allow_deleted=True)
            return self.library.delete("conversation", identifier, document["title"])

    def restore_conversation(self, identifier: str) -> dict:
        identifier = _identifier(identifier)
        # 与新建共享容量锁。回收站不占 200 个活跃会话名额，恢复也不能绕过上限。
        with self._lock(), self._lock(identifier):
            self._read(identifier, allow_deleted=True)
            if len(self._paths()) >= MAX_CONVERSATIONS:
                raise ConversationError("活跃会话已达到 200 个，请先移除其他会话后再恢复。")
            return self.library.restore("conversation", identifier)

    def purge_conversation(self, identifier: str, payload: object, *, require_trash: bool = False) -> dict:
        """只删本会话与备注，不删除附件、宿主工程或任何备份。

        assistant.lock 同时保护预览对其他会话的旧提案清理；仅持本会话锁不足以
        防止其他会话预览把已读快照重新保存。所有文件锁非阻塞，遇到在途请求即拒绝。
        """
        identifier = _identifier(identifier)
        # 先校验锁路径，避免固定锁目录被链接到 DATA 之外后仍执行破坏性操作。
        self.library.checked_path(self.data / "assistant.lock")
        self.library.checked_path(self.locks / (identifier + ".lock"))
        with self._lock(), self._lock(identifier):
            return self.library.purge("conversation", identifier, payload, require_trash=require_trash)

    def _capture_selection(self, *, require_write: bool = False) -> tuple[dict, str]:
        """冻结前后核对同一桥接会话；未连接或未选择音符时明确失败。"""
        try:
            before = self.service.bridge.status()
            if not before.get("connected") or not isinstance(before.get("session"), str) or not before["session"]:
                raise ConversationError("请先连接 SynthV 桥接，再发送包含选区的请求。")
            selection = self.service.get_selection()
            after = self.service.bridge.status()
            if not after.get("connected") or after.get("session") != before["session"]:
                raise ConversationError("桥接会话已变化，请重新读取选区。")
            if require_write and after.get("writeEnabled") is False:
                # 明确的只读状态可以在写入之前确认，不应把尚未执行的提案消耗为 unknown。
                raise ConversationError("当前工程尚未开启写入；请先保存工作副本并开启写入，再确认此预览。")
            if (not isinstance(selection, dict) or not isinstance(selection.get("notes"), list)
                    or not selection["notes"] or not selection.get("groupUUID")):
                raise ConversationError("请先在 SynthV 选中要调教的音符；本次不会省略选区继续请求。")
            if len(selection["notes"]) > 128 or any(not isinstance(note, dict) for note in selection["notes"]):
                raise ConversationError("一次最多规划 128 个音符，请缩小选区。")
            return json.loads(_encoded(selection)), before["session"]
        except ConversationError:
            raise
        except Exception:
            raise ConversationError("无法读取 SynthV 当前选区，请检查桥接连接后重试。") from None

    @staticmethod
    def _history(document: dict) -> list:
        # 历史只保留文本，最多八条各 2000 字；不重传附件、私有指纹或旧选区。
        messages = [message for message in document["messages"] if message.get("role") in {"user", "assistant"}][-8:]
        return [{"role": message["role"], "text": message["text"][:2000]} for message in messages]

    @staticmethod
    def _message(role: str, text: str, **extra) -> dict:
        return {"id": uuid.uuid4().hex, "role": role, "text": text, "createdAt": _now(),
                "attachments": [], "actions": [], **extra}

    def send_message(self, identifier: str, text: str, include_selection: bool, attachments: list,
                     model_options=None, on_progress=None, render_mode=None) -> dict:
        identifier = _identifier(identifier)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT_CHARS:
            raise ConversationError("请输入 1 至 4000 个字符的调教要求。")
        if not isinstance(include_selection, bool):
            raise ConversationError("是否包含选区必须是布尔值。")
        if not isinstance(attachments, list) or len(attachments) > 2:
            raise ConversationError("一次最多选择两个音频附件。")
        for item in attachments:
            if (not isinstance(item, dict) or set(item) != {"kind", "id"}
                    or not isinstance(item.get("kind"), str)
                    or item.get("kind") not in {"upload", "recording"}):
                raise ConversationError("附件必须通过已上传音频或录音编号选择。")
            _identifier(item.get("id"))
        text = text.strip()
        with self._lock(identifier), self.library.use_assets(attachments):
            # 本锁只阻止同一会话重复发送；模型等待期间其他会话和本地录音仍可进行。
            current = self._read(identifier)
            # 模式是本次请求的快照；修改偏好不会重写历史提案的实际表示方式。
            mode = normalize_render_mode(render_mode if render_mode is not None else current.get("renderMode", "smooth"))
            chosen = model_options if model_options is not None else current.get("modelOptions")
            options = normalize_model_options(chosen)
            if on_progress is not None:
                on_progress({"stage": "正在读取选区" if include_selection else "正在准备会话", "text": ""})
            selection, session = None, None
            if include_selection:
                with self.service.operation_lock:
                    selection, session = self._capture_selection()
            paths, metadata = _resolve_attachments(attachments) if attachments else ([], [])
            metadata = _safe_attachments(metadata)
            safe_selection = _safe_selection(selection) if selection is not None else None
            with self._lock():
                document = self._read(identifier)
                if len(document["messages"]) > MAX_MESSAGES - 2:
                    raise ConversationError("会话已达到消息上限，请新建会话后继续。")
                history = self._history(document)
                if chosen is not None:
                    document["modelOptions"] = options
                if not any(message["role"] == "user" for message in document["messages"]):
                    document["title"] = text[:40]
                user = self._message("user", text, attachments=metadata)
                document["renderMode"] = mode
                user["renderMode"] = mode
                if safe_selection is not None:
                    user["selection"] = safe_selection
                document["messages"].append(user)
                if len(_encoded(document)) + REPLY_RESERVE_BYTES > MAX_FILE_BYTES:
                    raise ConversationError("会话剩余空间不足以保存回复，请新建会话。")
                self._save(document)
            from .planner import PlannerError
            last_progress = {}

            def publish_progress(value):
                """保留供应商已公开的摘要；最终失败也不丢失用户刚刚看到的资料。"""
                if isinstance(value, dict):
                    last_progress.update(value)
                    if on_progress is not None:
                        on_progress(value)

            try:
                extra = {}
                if render_mode is not None or mode != "smooth":
                    extra["render_mode"] = mode
                if chosen is not None:
                    extra["model_options"] = options
                if on_progress is not None:
                    extra["on_progress"] = publish_progress
                plan = _plan_tuning(text, safe_selection, history, paths, metadata, **extra)
                reply, guards = self._planned_message(plan, selection, session)
            except PlannerError as error:
                # 规划器专用异常只含已脱敏的固定提示，保留认证/限流/未配置等可操作原因。
                reply = self._message("error", str(error))
                guards = {}
            except Exception:
                # 模型异常可能含供应商响应或密钥，禁止把异常文本加入会话或任务状态。
                reply = self._message("error", "模型请求失败或未返回有效调教方案；未修改工程，请检查模型设置后重新发送。")
                guards = {}
            if reply["role"] == "error" and isinstance(last_progress.get("reasoning"), str):
                reply["reasoningSummary"] = last_progress["reasoning"][:12000]
            with self._lock():
                # 长请求期间其他会话可能使旧预览失效，重新读取后追加，不能覆盖其状态。
                document = self._read(identifier)
                document["messages"].append(reply)
                document["_private"]["actions"].update(guards)
                self._save(document)
                return self._public(document)

    def _planned_message(self, plan: dict, selection: dict | None, session: str | None) -> tuple[dict, dict]:
        """即使规划器已经校验，再次限制动作白名单；咨询消息永远不生成可执行动作。"""
        if not isinstance(plan, dict) or not isinstance(plan.get("text"), str) or not 1 <= len(plan["text"]) <= MAX_REPLY_CHARS:
            raise ConversationError("模型回复格式无效。")
        actions = plan.get("actions", [])
        if not isinstance(actions, list) or len(actions) > 5:
            raise ConversationError("模型提案数量无效。")
        reply = self._message("assistant", plan["text"])
        for key in ("inputMode", "provider", "model", "platformId", "reasoningEffort"):
            if isinstance(plan.get(key), str) and len(plan[key]) <= 200:
                reply[key] = plan[key]
        # 摘要只作显示资料，不进入 _history，也不被解释为参数动作或系统指令。
        if isinstance(plan.get("reasoningSummary"), str):
            reply["reasoningSummary"] = plan["reasoningSummary"][:12000]
        guards = {}
        if selection is None:
            return reply, guards
        seen = set()
        for proposal in actions:
            try:
                normalized = validate_action(proposal, selection)
            except ParameterError as exc:
                raise ConversationError(str(exc)) from None
            parameter = normalized["parameter"]
            if parameter in seen:
                raise ConversationError("同一提案不能包含重复参数，请将分段变化合并为一条曲线。")
            seen.add(parameter)
            action = {"id": uuid.uuid4().hex, **normalized, "status": "proposed"}
            # 展示标签来自捕获时的宿主目录，而非模型任意字段或之后切换的声库。
            definition = selection.get("parameters", {}).get(parameter, {})
            for key in ("label", "unit", "kind", "modeName"):
                value = definition.get(key)
                if isinstance(value, str) and len(value) <= 200:
                    action[key] = value
            reply["actions"].append(action)
            guards[action["id"]] = {"selection": _selection_identity(selection, session), "session": session,
                                     "parameter": _parameter_identity(selection, parameter),
                                     "proposalSelection": _proposal_selection_identity(selection),
                                     "targetScope": _target_scope(selection)}
        return reply, guards

    @staticmethod
    def _source_selection(document: dict, action_id: str) -> dict | None:
        """优先取复用消息自身的快照，旧模型消息才回溯紧邻的用户请求快照。"""
        original = None
        for message in document["messages"]:
            if message.get("role") == "user":
                original = message.get("selection")
            if any(action.get("id") == action_id for action in message.get("actions", [])):
                candidate = message.get("selection", original)
                return candidate if isinstance(candidate, dict) else None
        return None

    def _prepare_preview_guard(self, document: dict, action: dict, guard: dict,
                               selection: dict, session: str, *, after_undo: bool = False) -> None:
        """只在用户请求重新预览时重新绑定；不能在应用或预览后校验时迁移凭据。

        旧文件可用当前内容加旧 session 验证原哈希；索引变化时先由旧公开快照
        重建并验证原哈希，证明工程、组和声库相同，再忽略索引比较。不能用
        相似旋律绕过私有目标保护；跨位置复用另走显式复制入口。
        """
        strict = session == guard.get("session") and _selection_identity(selection, session) == guard.get("selection")
        stable = _proposal_selection_identity(selection)
        if not strict:
            same = stable == guard.get("proposalSelection")
            if not same and not guard.get("proposalSelection"):
                same = _selection_identity(selection, guard.get("session")) == guard.get("selection")
                original = self._source_selection(document, action["id"])
                if not same and original and isinstance(original.get("notes"), list):
                    reconstructed = dict(selection)
                    for key in ("notes", "noteCount", "startSeconds", "endSeconds", "groupPitchOffset"):
                        if key in original:
                            reconstructed[key] = original[key]
                    same = (_selection_identity(reconstructed, guard.get("session")) == guard.get("selection")
                            and _proposal_selection_identity(reconstructed) == stable)
            if not same:
                raise ConversationError("当前选区与原提案目标不同。可回到原选区重新预览，或点击“复用到当前选区”生成新预览，无需重复发送要求。")
        candidate = {**guard, "selection": _selection_identity(selection, session), "session": session,
                     "proposalSelection": stable, "targetScope": _target_scope(selection)}
        # 跨连接必须有完整曲线指纹，避免点数相同却值已变化。旧摘要在同连接仍兼容。
        self._check_guard(action, candidate, selection, session, after_undo=after_undo)
        if not strict and session != guard.get("session"):
            self._require_parameter_fingerprint(action, selection, reconnect=True)
        guard.update(candidate)

    @staticmethod
    def _require_parameter_fingerprint(action: dict, selection: dict, *, reconnect: bool = False) -> None:
        """撤销和跨连接恢复都必须由完整指纹证明参数原态，不能仅凭点数判断。"""
        definition = selection.get("parameters", {}).get(action["parameter"], {})
        capabilities = selection.get("capabilities", {})
        fingerprint = definition.get("fingerprint") if isinstance(definition, dict) else None
        if (not isinstance(capabilities, dict) or capabilities.get("curves") is not True
                or not isinstance(definition, dict) or definition.get("available") is not True
                or not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{16}:[1-9][0-9]{0,11}", fingerprint) is None):
            if reconnect:
                raise ConversationError("桥接会话已重连，但缺少完整参数指纹，无法验证原预览；请更新桥接，或使用“复用到当前选区”重新预览。")
            raise ConversationError("当前桥接缺少完整参数指纹，无法确认撤销结果；请更新桥接并重新生成提案。")

    def _find_action(self, action_id: str) -> tuple[list, dict, dict, dict]:
        action_id = _identifier(action_id)
        documents = [self._read(path.stem) for path in self._paths()]
        for document in documents:
            for message in document["messages"]:
                for action in message.get("actions", []):
                    if action.get("id") == action_id:
                        guard = document["_private"].get("actions", {}).get(action_id)
                        if not isinstance(guard, dict) or action.get("status") not in ACTION_STATES:
                            raise ConversationError("提案保护信息缺失，请重新生成方案。")
                        return documents, document, action, guard
        raise ConversationError("调教提案不存在。")

    def reuse_message(self, identifier: str, message_id: str) -> dict:
        """将用户明确选择的方案复制到当前选区，始终创建新的待预览动作。

        此入口既不请求模型，也不读取/上传原消息音频，更不写工程。音高曲线
        要求音符旋律及相对节奏相符；通用增量包络按当前选区时长缩放。新的
        目标基线由当前参数定义，不能携带旧 previewId、结果或确认票据。
        """
        identifier, message_id = _identifier(identifier), _identifier(message_id)
        with self._lock(), self._lock(identifier), self.service.operation_lock:
            document = self._read(identifier)
            source = next((message for message in document["messages"] if message["id"] == message_id), None)
            if not source or source.get("role") != "assistant" or not 1 <= len(source.get("actions", [])) <= 5:
                raise ConversationError("请从包含参数建议的助手消息复用方案。")
            actions = source["actions"]
            if any(action.get("status") not in {"proposed", "previewed", "applied"}
                   or (action.get("status") == "applied" and action.get("result", {}).get("verified") is not True)
                   for action in actions):
                raise ConversationError("原方案包含结果未知或未经核实的修改，不能通过复用重复执行；请先检查工程。")
            if len(document["messages"]) >= MAX_MESSAGES:
                raise ConversationError("单个会话最多 100 条消息，请新建会话。")
            selection, session = self._capture_selection()
            current_scope = _target_scope(selection)
            normalized, excluded = [], []
            for action in actions:
                guard = document["_private"].get("actions", {}).get(action.get("id"))
                if not isinstance(guard, dict):
                    raise ConversationError("原方案缺少保护资料，无法复用。")
                original = self._source_selection(document, action["id"])
                if action["status"] == "applied":
                    old_scope = guard.get("targetScope")
                    # 旧提案缺少组定位哈希时，重叠组内范围保守拒绝；不要把缺失资料
                    # 当成另一个目标。用户仍可回原选区执行正常的撤销后重新预览。
                    group_matches = not isinstance(old_scope, dict) or old_scope.get("group") == current_scope["group"]
                    source_scope = old_scope if isinstance(old_scope, dict) else _target_scope(original or {})
                    start, end = source_scope.get("start"), source_scope.get("end")
                    values = (start, end, current_scope["start"], current_scope["end"])
                    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
                        raise ConversationError("已应用方案缺少可靠目标范围，无法判断是否重复叠加；请先检查工程。")
                    if group_matches and max(start, current_scope["start"]) < min(end, current_scope["end"]):
                        raise ConversationError("当前选区与已应用方案的原选区重叠，不能再次复制叠加；请先撤销，再使用原建议的重新预览。")
                try:
                    proposal = {key: action[key] for key in ("parameter", "delta", "curve", "renderMode", "reason") if key in action}
                    checked = validate_action(proposal, selection)
                    if checked["parameter"] in {"pitchCurve", "pitchDelta"} and "curve" in checked:
                        if original is None:
                            raise ConversationError("原音高方案没有音符快照，无法验证复用对应关系。")
                        before, after = _melody_layout(original), _melody_layout(selection)
                        if len(before) != len(after) or any(abs(old - new) > 1e-6
                                for old_note, new_note in zip(before, after) for old, new in zip(old_note, new_note)):
                            raise ConversationError("音符音高或相对节奏不匹配，未复用音高曲线；请选择相同旋律和节奏的片段，或另行生成音高建议。")
                    if checked["parameter"] == "pitchCurve":
                        from .pitch_shapes import validate_note_alignment
                        validate_note_alignment(checked.get("curve"), selection)
                    normalized.append(checked)
                except (ParameterError, ConversationError) as error:
                    excluded.append(f"{action.get('label') or action['parameter']}：{error}")
            if not normalized:
                raise ConversationError("没有适用于当前选区的参数。" + "；".join(excluded))
            text = (f"已在本地复用 {len(normalized)} 项参数建议到当前选区，未再次调用模型或发送音频。"
                    "曲线按当前选区时长缩放，并基于当前参数生成新预览；尚未修改工程，请核对新范围与效果后确认。")
            if excluded:
                text += "\n\n未复用的参数：\n" + "\n".join(excluded)
            reply, guards = self._planned_message({"text": text, "actions": normalized}, selection, session)
            reply.update(origin="reuse", selection=_safe_selection(selection))
            # 复制建议本身不会删除原消息及应用记录，但必须撤销所有旧预览确认资格。
            # 使用同一 document 实例，避免从磁盘读回旧副本覆盖本次新增消息。
            for path in self._paths():
                other = document if path.stem == identifier else self._read(path.stem)
                changed = other["_private"].pop("batch", None) is not None
                for message in other["messages"]:
                    for item in message.get("actions", []):
                        if item.get("status") == "previewed":
                            item["status"] = "proposed"
                            for key in ("preview", "previewBatchId"):
                                item.pop(key, None)
                            changed = True
                if changed and other is not document:
                    self._save(other)
            document["messages"].append(reply)
            document["_private"]["actions"].update(guards)
            # 元数据装饰可能因资料损坏失败，必须在追加落盘前完成，避免 HTTP 400
            # 被页面解释为“尚未创建”后重复复制。成功写入后只更新普通时间字段。
            public = self._public(document)
            self._save(document)
            public["updatedAt"] = document["updatedAt"]
            return {"conversation": public, "messageId": reply["id"]}

    @staticmethod
    def _check_guard(action: dict, guard: dict, selection: dict, session: str, *, after_undo: bool = False) -> None:
        if session != guard.get("session") or _selection_identity(selection, session) != guard.get("selection"):
            raise ConversationError("本次预览的桥接会话或选区已变化，请重新预览后再确认；若已改选其他片段，可使用“复用到当前选区”。")
        if action["parameter"] == "pitchCurve" and selection.get("parameters", {}).get("pitchCurve", {}).get("available") is False:
            # 已存提案可能早于能力诊断升级；先给出本地固定的具体能力提示，不能
            # 把非零音高偏移的叠加保护误报为无缘由的摘要变化。
            from .parameters import parameter_policy
            try:
                parameter_policy("pitchCurve", selection)
            except ParameterError as error:
                raise ConversationError(str(error)) from None
        if after_undo:
            # 旧桥接只报告点数，无法分辨「已经撤销」与「同点数但不同数值」。
            # 只有新桥接的完整曲线指纹可证明原状态；格式与 Lua fingerprint()
            # 一致，为两个 32 位散列及序列化长度，拒绝 unavailable/oversized。
            ConversationManager._require_parameter_fingerprint(action, selection)
        if _parameter_identity(selection, action["parameter"]) != guard.get("parameter"):
            if after_undo:
                raise ConversationError("目标参数尚未恢复到本提案生成前的状态；请先撤销对应修改，再重新预览。若已继续编辑，请重新生成提案。")
            raise ConversationError("目标参数摘要已变化。若要沿用原建议，请点击“复用到当前选区”，基于当前参数重新预览并确认。")
        try:
            # 能力可能在连接期间变化；即使所有点数未变，也不能应用已不可用的操作。
            validate_action({key: action[key] for key in ("parameter", "delta", "curve", "renderMode", "reason")
                             if key in action}, selection)
        except ParameterError as exc:
            raise ConversationError(str(exc)) from None

    def preview_action(self, action_id: str) -> dict:
        with self._lock():
            documents, document, action, guard = self._find_action(action_id)
            with self._lock(document["id"]), self.service.operation_lock:
                self.library.assert_available("conversation", document["id"])
                if action["status"] == "unknown":
                    raise ConversationError("该提案应用结果未知，不能重新预览或重复应用；请检查工程并重新生成提案。")
                after_undo = action["status"] == "applied"
                if after_undo and action.get("result", {}).get("verified") is not True:
                    raise ConversationError("该提案缺少已确认的应用记录，不能重新执行；请检查工程并重新生成提案。")
                selection, session = self._capture_selection()
                self._prepare_preview_guard(document, action, guard, selection, session, after_undo=after_undo)
                if after_undo:
                    # 宿主原生撤销和网页恢复都表现为完整原指纹重现。确认后先持久
                    # 退回 proposed，并丢弃旧确认凭据；即使本次预览中断也只能重做
                    # 只读预览。不能把 applied 直接解锁为可执行的旧 previewed。
                    action["status"] = "proposed"
                    action.pop("preview", None)
                    action.pop("previewBatchId", None)
                    action.pop("result", None)
                    self._save(document)
                # Lua 只保留最近一次预览；先清除所有旧确认入口，跨会话也不例外。
                for other_document in documents:
                    changed = other_document["_private"].pop("batch", None) is not None
                    for message in other_document["messages"]:
                        for other_action in message.get("actions", []):
                            if other_action["status"] == "previewed":
                                other_action["status"] = "proposed"
                                other_action.pop("preview", None)
                                other_action.pop("previewBatchId", None)
                                changed = True
                    if changed:
                        self._save(other_document)
                try:
                    if action["parameter"] == "pitchCurve":
                        # 持久会话可能含旧版模型生成的粗略绝对音高，不能因为原指纹
                        # 尚未变化就直接复用。重新逐音符核验后才允许请求只读预览。
                        from .pitch_shapes import validate_note_alignment, validate_preview_note_alignment
                        validate_note_alignment(action.get("curve"), selection)
                    if "curve" in action:
                        preview = self.service.preview(action["parameter"], curve=action["curve"],
                                                       render_mode=action.get("renderMode", "smooth"))
                    elif action.get("renderMode", "smooth") != "smooth":
                        preview = self.service.preview(action["parameter"], action["delta"], render_mode=action["renderMode"])
                    else:
                        # 不给旧增量补额外参数，保持现有 MCP、替身及桥接调用兼容。
                        preview = self.service.preview(action["parameter"], action["delta"])
                    current, current_session = self._capture_selection()
                    self._check_guard(action, guard, current, current_session, after_undo=after_undo)
                    preview = public_preview({**preview, "notes": selection_preview_notes(current)})
                    if action["parameter"] == "pitchCurve":
                        # 原生宿主插值不保证等同于模型节点间的直线。仅比较宿主实际
                        # 采样命中的音符主体，不重插值显示采样或虚构短音符测量结果。
                        validate_preview_note_alignment(preview, current)
                except ConversationError:
                    raise
                except BridgeError as error:
                    # 宿主拒绝可能与边界、已有音高或插值能力有关，不能一律误报
                    # 为桥接/选区故障。只公开桥接白名单常量，保留未知异常的隐私边界。
                    message = error.public_message
                    if message is None:
                        message = "无法生成宿主预览，尚未修改工程；请检查桥接和当前选区。"
                    else:
                        message = "未生成预览，尚未修改工程：" + message
                    raise ConversationError(message) from None
                except ParameterError as error:
                    # 参数契约异常只含本地固定中文提示，不含宿主返回的任意字符串。
                    raise ConversationError("未生成预览，尚未修改工程：" + str(error)) from None
                except OperationBusyError:
                    raise ConversationError(BUSY_MESSAGE) from None
                except Exception:
                    raise ConversationError("无法生成宿主预览，尚未修改工程；请检查桥接和当前选区。") from None
                action["preview"] = preview
                action["status"] = "previewed"
                self._save(document)
                return json.loads(_encoded(_public_action(action)))

    def _batch_actions(self, action_ids: object) -> tuple[list, dict, list, list]:
        """只允许同一助手消息中的不同参数，不能拼接两个会话或两轮建议。"""
        if (not isinstance(action_ids, list) or not 1 <= len(action_ids) <= 5
                or any(not isinstance(item, str) for item in action_ids)
                or len(set(action_ids)) != len(action_ids)):
            raise ConversationError("组合预览须包含 1 至 5 个不重复的提案编号。")
        checked = [_identifier(item) for item in action_ids]
        documents, document, _, _ = self._find_action(checked[0])
        for message in document["messages"]:
            by_id = {action.get("id"): action for action in message.get("actions", [])}
            if checked[0] in by_id:
                if message.get("role") != "assistant" or any(item not in by_id for item in checked):
                    raise ConversationError("组合预览只能包含同一条助手消息中的参数。")
                actions = [by_id[item] for item in checked]
                parameters = [action.get("parameter") for action in actions]
                if len(set(parameters)) != len(parameters):
                    raise ConversationError("组合预览不能包含重复参数。")
                if {"pitchCurve", "pitchDelta"} <= set(parameters):
                    raise ConversationError("原生音高与音高偏移不能同时应用，请分别生成方案，避免重复叠加音高。")
                guards = [document["_private"].get("actions", {}).get(item) for item in checked]
                if any(not isinstance(guard, dict) for guard in guards) or any(
                        action.get("status") not in ACTION_STATES for action in actions):
                    raise ConversationError("提案保护信息缺失，请重新生成方案。")
                return documents, document, actions, guards
        raise ConversationError("调教提案不存在。")

    def preview_batch(self, action_ids: object) -> dict:
        """一次只读 IPC 生成组合候选；每个失败项保持 proposed，不获得确认权限。

        批次身份随会话原子保存。任何新预览先使旧确认凭据失效，宿主再独立保存
        最新候选的完整快照。页面刷新可以展示该批次，但不能跨消息或重放已写入批次。
        """
        with self._lock():
            documents, document, actions, guards = self._batch_actions(action_ids)
            with self._lock(document["id"]), self.service.operation_lock:
                self.library.assert_available("conversation", document["id"])
                selection, session = self._capture_selection()
                # 旧桥接不提供批次持久快照，不能悄悄退化成逐项预览后只剩最后一项。
                if selection.get("capabilities", {}).get("batchPreview") is not True:
                    raise ConversationError("当前桥接不支持组合预览，请更新并重新启动 SynthV Assistant 桥接。")
                errors, eligible_ids = [], set()
                for action, guard in zip(actions, guards):
                    if action["status"] == "unknown":
                        raise ConversationError("该提案应用结果未知，不能重新预览或重复应用；请检查工程并重新生成提案。")
                    restored = action["status"] == "applied"
                    if restored and action.get("result", {}).get("verified") is not True:
                        raise ConversationError("该提案缺少已确认的应用记录，不能重新执行；请检查工程并重新生成提案。")
                    try:
                        self._prepare_preview_guard(document, action, guard, selection, session, after_undo=restored)
                        eligible_ids.add(action["id"])
                    except ConversationError as error:
                        # 同一乐句的一项能力或参数守卫失败，不妨碍其他参数只读预演。
                        # applied 项只有完整撤销指纹验证通过才会被重新变成 proposed。
                        errors.append({"actionId": action["id"], "message": str(error)})
                for other_document in documents:
                    changed = other_document["_private"].pop("batch", None) is not None
                    for message in other_document["messages"]:
                        for item in message.get("actions", []):
                            if item.get("status") == "previewed" or (other_document is document and item.get("id") in eligible_ids):
                                item["status"] = "proposed"
                                item.pop("preview", None)
                                item.pop("previewBatchId", None)
                                item.pop("result", None)
                                changed = True
                    if changed:
                        self._save(other_document)
                batch_id = uuid.uuid4().hex
                eligible = []
                for action in actions:
                    if action["id"] not in eligible_ids:
                        continue
                    try:
                        if action["parameter"] == "pitchCurve":
                            from .pitch_shapes import validate_note_alignment
                            validate_note_alignment(action.get("curve"), selection)
                        eligible.append(action)
                    except ParameterError as error:
                        errors.append({"actionId": action["id"], "message": "未生成预览，尚未修改工程：" + str(error)})
                if eligible:
                    try:
                        result = self.service.preview_batch(batch_id, eligible)
                    except BridgeError as error:
                        message = error.public_message or "无法生成宿主预览，尚未修改工程；请检查桥接和当前选区。"
                        raise ConversationError(message) from None
                    except OperationBusyError:
                        raise ConversationError(BUSY_MESSAGE) from None
                    except Exception:
                        raise ConversationError("无法生成组合宿主预览，尚未修改工程；请检查桥接和当前选区。") from None
                    current, current_session = self._capture_selection()
                    for action, guard in zip(actions, guards):
                        if action["id"] in eligible_ids:
                            self._check_guard(action, guard, current, current_session)
                    errors.extend(result["errors"])
                    by_id = {item["actionId"]: item["preview"] for item in result["previews"]}
                    for action in eligible:
                        if action["id"] not in by_id:
                            continue
                        try:
                            candidate = public_preview({**by_id[action["id"]], "notes": selection_preview_notes(current)})
                            if action["parameter"] == "pitchCurve":
                                from .pitch_shapes import validate_preview_note_alignment
                                validate_preview_note_alignment(candidate, current)
                            action.update(status="previewed", preview=candidate, previewBatchId=batch_id)
                        except ParameterError as error:
                            errors.append({"actionId": action["id"], "message": "未生成预览，尚未修改工程：" + str(error)})
                successful = [action for action in actions if action["status"] == "previewed"]
                if successful:
                    # 仅保存通过前后双重校验的成功子集，应用请求不能再自行添加或删减项。
                    document["_private"]["batch"] = {"id": batch_id, "actionIds": [action["id"] for action in successful]}
                self._save(document)
                return {"batchId": batch_id if successful else None,
                        "actions": [_public_action(action) for action in actions], "errors": errors}

    def apply_batch(self, batch_id: object) -> dict:
        """确认一次只写入本批成功子集；所有 unknown 必须先于任何宿主写入落盘。"""
        identifier = _identifier(batch_id)
        with self._lock():
            documents = [self._read(path.stem) for path in self._paths()]
            document = next((item for item in documents if item["_private"].get("batch", {}).get("id") == identifier), None)
            if document is None:
                raise ConversationError("组合预览不存在或已失效，请重新预览后确认。")
            with self._lock(document["id"]), self.service.operation_lock:
                self.library.assert_available("conversation", document["id"])
                ticket = document["_private"]["batch"]
                _, document, actions, guards = self._batch_actions(ticket.get("actionIds"))
                if any(action["status"] != "previewed" or action.get("previewBatchId") != identifier
                       or not action.get("preview", {}).get("previewId") for action in actions):
                    raise ConversationError("组合预览已应用或失效，请重新预览后确认。")
                selection, session = self._capture_selection(require_write=True)
                for action, guard in zip(actions, guards):
                    self._check_guard(action, guard, selection, session)
                preview_ids = [action["preview"]["previewId"] for action in actions]
                for action in actions:
                    action["status"] = "unknown"
                document["_private"].pop("batch", None)
                self._save(document)
                results = []
                try:
                    result = self.service.edit("apply_batch", {"batchId": identifier, "previewIds": preview_ids})
                    returned = result.get("results") if isinstance(result, dict) else None
                    if (not isinstance(result, dict) or result.get("verified") is not True
                            or not isinstance(returned, list) or len(returned) != len(actions)
                            or any(not isinstance(item, dict) or item.get("verified") is not True
                                   or item.get("parameter") != action["parameter"]
                                   for item, action in zip(returned, actions))):
                        raise ConversationError("宿主未确认组合应用结果。")
                    for action, item in zip(actions, returned):
                        action["result"] = {key: item[key] for key in ("verified", "parameter", "pointCount", "undoRecords", "message") if key in item}
                        action["status"] = "applied"
                        results.append(action["result"])
                except Exception:
                    for action in actions:
                        action["result"] = {"verified": False, "message": "组合应用结果未确认；请先在 SynthV 检查或撤销，勿重复提交此批次。"}
                        results.append(action["result"])
                self._save(document)
                return {"batchId": identifier, "actions": [_public_action(action) for action in actions], "results": results}

    def apply_action(self, action_id: str) -> dict:
        with self._lock():
            _, document, action, guard = self._find_action(action_id)
            with self._lock(document["id"]), self.service.operation_lock:
                self.library.assert_available("conversation", document["id"])
                if action.get("previewBatchId"):
                    raise ConversationError("该参数属于组合预览，请确认并应用完整的预览成功项。")
                if action["status"] != "previewed" or not action.get("preview", {}).get("previewId"):
                    raise ConversationError("请先生成并确认最新宿主预览；已应用或结果未知的提案不能重复执行。")
                selection, session = self._capture_selection(require_write=True)
                self._check_guard(action, guard, selection, session)
                # 先持久化不可重放状态，再发写请求；绝不为调用者自动开启工程写入。
                action["status"] = "unknown"
                self._save(document)
                try:
                    result = self.service.edit("apply", {"previewId": action["preview"]["previewId"]})
                    if not isinstance(result, dict) or result.get("verified") is not True:
                        raise ConversationError("宿主未确认应用结果。")
                    fields = {"verified", "parameter", "pointCount", "undoRecords", "message"}
                    action["result"] = {key: value for key, value in result.items() if key in fields}
                    action["status"] = "applied"
                except Exception:
                    action["result"] = {"verified": False, "message": "应用结果未确认；请先在 SynthV 检查或撤销，勿重复提交此提案。"}
                self._save(document)
                return json.loads(_encoded(_public_action(action)))
