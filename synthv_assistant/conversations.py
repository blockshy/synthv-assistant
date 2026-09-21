"""持久化调教对话与人工确认状态机，不允许模型直接修改 SynthV。

会话文件包含公开消息和私有动作指纹；只有字段白名单能够进入 HTTP 响应或
模型上下文。动作必须经过 proposed → previewed → unknown → applied。
unknown 在宿主写入前落盘，进程崩溃、超时或写后保存失败都不能触发重复应用。
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
from .operations import OperationBusyError, OperationLock
from .metadata import LibraryMetadata, MetadataError
from .model_options import normalize_model_options, validate_for_config


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
PARAMETER_LIMITS = {"breathiness": 0.3, "tension": 0.3, "loudness": 6, "gender": 0.3, "pitchDelta": 100}
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
    result["notes"] = [{key: value for key, value in note.items() if key in note_fields}
                       for note in selection["notes"]]
    result["parameters"] = {name: {key: value for key, value in definition.items()
                                  if key in {"range", "defaultValue", "pointCount"}}
                            for name, definition in selection.get("parameters", {}).items()
                            if name in PARAMETER_LIMITS and isinstance(definition, dict)}
    return result


def _selection_identity(selection: dict, session: str) -> str:
    """参数单独绑定，避免应用气声后让同一乐句的张力提案无故失效。"""
    fields = ("projectFile", "groupUUID", "groupOffset", "notes", "startSeconds", "endSeconds", "noteCount")
    return _fingerprint({**{key: selection.get(key) for key in fields}, "bridgeSession": session})


def _parameter_identity(selection: dict, parameter: str) -> str:
    definition = selection.get("parameters", {}).get(parameter)
    if not isinstance(definition, dict):
        raise ConversationError("当前选区没有该参数的有效摘要，请重新读取后规划。")
    # 预览之前只有点数摘要，不能识别点数不变的手工曲线编辑；实际 apply 仍由
    # Lua 严格检查宿主预览时的完整曲线，绝不能把此摘要当成完整曲线指纹。
    return _fingerprint({key: definition.get(key) for key in ("range", "defaultValue", "pointCount")})


def _public_action(action: dict) -> dict:
    """只投影状态机的公开字段，私有指纹与会话不会进入前端响应。"""
    fields = {"id", "parameter", "delta", "reason", "status", "preview", "result"}
    return {key: value for key, value in action.items() if key in fields}


def _public_document(document: dict) -> dict:
    result = {key: document[key] for key in ("id", "title", "createdAt", "updatedAt")}
    result["modelOptions"] = normalize_model_options(document.get("modelOptions"))
    fields = {"id", "role", "text", "createdAt", "attachments", "inputMode", "provider", "model", "selection",
              "platformId", "reasoningEffort", "reasoningSummary"}
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
                        "messages": [], "_private": {"actions": {}},
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
                     model_options=None, on_progress=None) -> dict:
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
        for proposal in actions:
            if not isinstance(proposal, dict) or set(proposal) != {"parameter", "delta", "reason"}:
                raise ConversationError("模型动作格式无效。")
            parameter, delta, reason = proposal["parameter"], proposal["delta"], proposal["reason"]
            if (not isinstance(parameter, str) or parameter not in PARAMETER_LIMITS
                    or isinstance(delta, bool) or not isinstance(delta, (int, float))
                    or not -PARAMETER_LIMITS[parameter] <= delta <= PARAMETER_LIMITS[parameter]
                    or not math.isfinite(delta) or delta == 0
                    or not isinstance(reason, str) or not 1 <= len(reason) <= 2000):
                raise ConversationError("模型动作超出允许范围。")
            action = {"id": uuid.uuid4().hex, "parameter": parameter, "delta": delta, "reason": reason, "status": "proposed"}
            reply["actions"].append(action)
            guards[action["id"]] = {"selection": _selection_identity(selection, session), "session": session,
                                     "parameter": _parameter_identity(selection, parameter)}
        return reply, guards

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

    @staticmethod
    def _check_guard(action: dict, guard: dict, selection: dict, session: str) -> None:
        if session != guard.get("session") or _selection_identity(selection, session) != guard.get("selection"):
            raise ConversationError("桥接会话或目标选区已变化，请重新发送要求生成提案。")
        if _parameter_identity(selection, action["parameter"]) != guard.get("parameter"):
            raise ConversationError("目标参数摘要已变化，请重新生成该参数的调教提案。")

    def preview_action(self, action_id: str) -> dict:
        with self._lock():
            documents, document, action, guard = self._find_action(action_id)
            with self._lock(document["id"]), self.service.operation_lock:
                self.library.assert_available("conversation", document["id"])
                if action["status"] not in {"proposed", "previewed"}:
                    raise ConversationError("该提案已经应用或结果未知，不能重新预览或重复应用。")
                selection, session = self._capture_selection()
                self._check_guard(action, guard, selection, session)
                # Lua 只保留最近一次预览；先清除所有旧确认入口，跨会话也不例外。
                for other_document in documents:
                    changed = False
                    for message in other_document["messages"]:
                        for other_action in message.get("actions", []):
                            if other_action["status"] == "previewed":
                                other_action["status"] = "proposed"
                                other_action.pop("preview", None)
                                changed = True
                    if changed:
                        self._save(other_document)
                try:
                    preview = self.service.preview(action["parameter"], action["delta"])
                    current, current_session = self._capture_selection()
                    self._check_guard(action, guard, current, current_session)
                    if not isinstance(preview, dict) or not isinstance(preview.get("previewId"), str) or not preview["previewId"]:
                        raise ConversationError("宿主没有返回有效预览。")
                except ConversationError:
                    raise
                except Exception:
                    raise ConversationError("无法生成宿主预览，尚未修改工程；请检查桥接和当前选区。") from None
                fields = {"previewId", "parameter", "delta", "noteCount", "startSeconds", "endSeconds", "summary", "pointCount"}
                action["preview"] = {key: value for key, value in preview.items() if key in fields}
                action["status"] = "previewed"
                self._save(document)
                return json.loads(_encoded(_public_action(action)))

    def apply_action(self, action_id: str) -> dict:
        with self._lock():
            _, document, action, guard = self._find_action(action_id)
            with self._lock(document["id"]), self.service.operation_lock:
                self.library.assert_available("conversation", document["id"])
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
