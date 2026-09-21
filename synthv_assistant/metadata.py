"""本地资料的备注、星标与可恢复删除覆盖层。

普通删除只写可恢复标记；显式永久删除仅移除对应资源，不级联其他资料。每个资源使用独立的
固定锁文件和原子 JSON 替换，跨网页、进程的更新不会覆盖彼此的其他字段。
备注只供本地展示；调用模型的代码必须继续使用自己的字段白名单。
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .operations import OperationBusyError, OperationLock


KINDS = {"conversation", "upload", "recording"}
MAX_METADATA_BYTES = 24_000


class MetadataError(ValueError):
    """固定中文业务错误，不包含磁盘路径、用户备注或原始系统异常。"""


def validate_purge_confirmation(payload: object) -> None:
    """永久删除必须收到唯一、精确的布尔确认；数字 1 不视为 true。"""
    if not isinstance(payload, dict) or set(payload) != {"confirm"} or payload["confirm"] is not True:
        raise MetadataError("永久删除必须明确提交 confirm: true，且不能包含其他字段。")


def validate_identity(kind: object, identifier: object) -> tuple[str, str]:
    """类型和编号都来自白名单；任何用户输入都不能成为相对路径。"""
    if (not isinstance(kind, str) or kind not in KINDS
            or not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier)):
        raise MetadataError("资料类型或编号无效。")
    return kind, identifier


def validate_patch(kind: str, payload: object) -> dict:
    """先完成所有字段校验，再进入写入阶段，保证失败不会部分保存。"""
    name = "title" if kind == "conversation" else "label"
    if not isinstance(payload, dict) or not payload or set(payload) - {name, "note", "starred"}:
        raise MetadataError("备注更新必须包含有效的名称、备注或星标字段，不能包含未知字段。")
    result = {}
    for key, value in payload.items():
        if key == "starred":
            if not isinstance(value, bool):
                raise MetadataError("星标状态必须是布尔值。")
        elif key == "note":
            if not isinstance(value, str) or len(value) > 2000 or "\x00" in value:
                raise MetadataError("备注必须是最多 2000 个字符的文字，不能包含空字符。")
        else:
            limit = 80 if kind == "conversation" else 100
            if (not isinstance(value, str) or not 1 <= len(value.strip()) <= limit
                    or any(ord(char) < 32 for char in value)):
                raise MetadataError("会话标题需为 1 至 80 字符，音频名称需为 1 至 100 字符，且不能包含控制字符。")
            value = value.strip()
        result[key] = value
    return result


class LibraryMetadata:
    """所有路径显式以调用方 DATA 为根，便于隔离测试而不读取真实资料。"""

    def __init__(self, data: Path):
        self.data = Path(data)
        self.directory = self.data / "metadata"
        self.locks = self.data / "asset-locks"
        self.purges = self.data / "purged"

    def checked_path(self, path: Path) -> Path:
        """校验固定 DATA 内的普通路径，拒绝符号链接、junction 和其他 reparse。

        先按词法检查所属目录，再逐级 lstat，不能先 resolve 后把外部目标当成本地
        文件。每次实际 unlink 前再次调用；删除只针对单个文件，绝不递归删除目录。
        固定锁与 UUID 路径用于协调本程序的并发，不接受请求传入任何磁盘路径。
        """
        root, target = self.data.absolute(), Path(path).absolute()
        try:
            target.relative_to(root)
            if ".." in target.parts:
                raise ValueError
            for component in (*reversed(target.parents), target):
                try:
                    info = component.lstat()
                except FileNotFoundError:
                    continue
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ValueError
                if component != target and not stat.S_ISDIR(info.st_mode):
                    raise ValueError
                if component == target and not stat.S_ISREG(info.st_mode):
                    raise ValueError
            return target
        except (OSError, ValueError):
            raise MetadataError("资料路径不安全或无法访问，未继续永久删除。") from None

    def _purge_path(self, kind: str, identifier: str) -> Path:
        validate_identity(kind, identifier)
        return self.checked_path(self.purges / (kind + "-" + identifier + ".json"))

    def _read_purge(self, kind: str, identifier: str) -> dict | None:
        """永久标记只存 ID、类型和状态，不保留会话、标签、备注或音频内容。"""
        try:
            with self._purge_path(kind, identifier).open("rb") as source:
                raw = source.read(2049)
            marker = json.loads(raw)
            if (len(raw) > 2048 or not isinstance(marker, dict)
                    or set(marker) != {"version", "id", "kind", "state", "startedAt", "fromTrash"}
                    or marker["version"] != 1 or marker["id"] != identifier or marker["kind"] != kind
                    or marker["state"] not in {"pending", "complete"}
                    or not isinstance(marker["startedAt"], str) or not isinstance(marker["fromTrash"], bool)):
                raise ValueError
            return marker
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise MetadataError("永久删除记录损坏或无法读取，资料保持不可用，请检查本地数据存档。") from None

    def _write_purge(self, marker: dict) -> None:
        """先原子提交停用标记，再删除用户内容；崩溃后只能重试删除，不能恢复。"""
        temporary = None
        try:
            destination = self._purge_path(marker["kind"], marker["id"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.checked_path(self.purges / ("." + uuid.uuid4().hex + ".tmp"))
            with temporary.open("xb") as output:
                output.write(json.dumps(marker, ensure_ascii=True, allow_nan=False).encode("ascii"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._purge_path(marker["kind"], marker["id"]))
        finally:
            if temporary is not None:
                try:
                    self.checked_path(temporary).unlink(missing_ok=True)
                except (OSError, MetadataError):
                    pass

    def _path(self, kind: str, identifier: str) -> Path:
        validate_identity(kind, identifier)
        return self.directory / (kind + "-" + identifier + ".json")

    @contextmanager
    def resource_lock(self, kind: str, identifier: str):
        """资源正在参与模型请求时拒绝备注修改/删除，不占工程操作锁。"""
        validate_identity(kind, identifier)
        try:
            with OperationLock(self.checked_path(self.locks / (kind + "-" + identifier + ".lock"))):
                yield
        except OperationBusyError:
            raise MetadataError("该资料正在处理或被 AI 请求使用，请等待完成后重试。") from None
        except OSError:
            raise MetadataError("无法访问资料标记，请检查本地数据目录权限。") from None

    def read(self, kind: str, identifier: str) -> dict:
        """原子替换允许无锁读取完整版本；损坏标记必须失败关闭，不能复活已删资源。"""
        marker = self._read_purge(kind, identifier)
        if marker:
            return {"version": 1, "id": identifier, "kind": kind, "note": "", "starred": False,
                    "deletedAt": marker["startedAt"], "permanent": True,
                    "purgePending": marker["state"] == "pending"}
        path = self._path(kind, identifier)
        try:
            with path.open("rb") as source:
                raw = source.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise ValueError
            state = json.loads(raw)
            allowed = {"version", "id", "kind", "note", "starred", "title", "label", "deletedAt", "updatedAt"}
            if (not isinstance(state, dict) or set(state) - allowed or state.get("version") != 1
                    or state.get("id") != identifier or state.get("kind") != kind):
                raise ValueError
            editable = {key: state[key] for key in ("title", "label", "note", "starred") if key in state}
            validate_patch(kind, editable)
            if state.get("deletedAt") is not None and not isinstance(state["deletedAt"], str):
                raise ValueError
            if not isinstance(state.get("updatedAt"), str):
                raise ValueError
            return state
        except FileNotFoundError:
            return {"version": 1, "id": identifier, "kind": kind, "note": "", "starred": False,
                    "deletedAt": None}
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise MetadataError("资料标记损坏或无法读取，请检查本地数据存档；未绕过删除状态。") from None

    def assert_available(self, kind: str, identifier: str) -> dict:
        state = self.read(kind, identifier)
        if state.get("permanent"):
            raise MetadataError("该资料已永久删除或正在完成永久删除，无法恢复和使用。")
        if state.get("deletedAt"):
            raise MetadataError("该资料已移入回收站，请先恢复后再使用。")
        return state

    def decorate(self, kind: str, item: dict, state: dict | None = None) -> dict:
        """只增加公开覆盖字段；录音旧版 note 是技术说明，保留为 captureNote。"""
        state = state if state is not None else self.read(kind, item["id"])
        result = dict(item)
        if kind == "recording" and "captureNote" not in result and result.get("note"):
            result["captureNote"] = result["note"]
        name = "title" if kind == "conversation" else "label"
        if name in state:
            result[name] = state[name]
        result.update(note=state.get("note", ""), starred=state.get("starred", False),
                      deleted=bool(state.get("deletedAt")))
        if state.get("permanent"):
            result.update(permanent=True, purgePending=bool(state.get("purgePending")))
        if kind != "conversation":
            result.update(kind=kind, available=not result["deleted"])
        return result

    def _write(self, state: dict) -> None:
        """密钥不在本模块中；备注 JSON 经 fsync 后原子替换，写失败保留旧标记。"""
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(raw) > MAX_METADATA_BYTES:
                raise MetadataError("资料标记过大，未保存此次修改。")
            destination = self._path(state["kind"], state["id"])
            temporary = self.directory / ("." + uuid.uuid4().hex + ".tmp")
            with temporary.open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        except (OSError, UnicodeError, TypeError, ValueError):
            raise MetadataError("资料标记保存失败，原有资料和标记保持不变。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def update(self, kind: str, identifier: str, payload: dict) -> dict:
        validate_identity(kind, identifier)
        checked = validate_patch(kind, payload)
        with self.resource_lock(kind, identifier):
            state = self.assert_available(kind, identifier)
            state.update(checked)
            state["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self._write(state)
            return state

    def delete(self, kind: str, identifier: str, display_name: str) -> dict:
        """只增加删除时间；重复删除不改变第一次删除时间，不操作原始文件。"""
        with self.resource_lock(kind, identifier):
            state = self.read(kind, identifier)
            if state.get("permanent"):
                raise MetadataError("该资料已进入永久删除流程，不能移入可恢复回收站。")
            if not state.get("deletedAt"):
                now = datetime.now(timezone.utc).isoformat()
                name = "title" if kind == "conversation" else "label"
                # 旧版名称可能含换行；只清理回收站摘要，不改写原始资料中的名称。
                safe_name = "".join(char if ord(char) >= 32 else " " for char in str(display_name)).strip()
                state.setdefault(name, safe_name[:80 if kind == "conversation" else 100] or "未命名资料")
                state.update(deletedAt=now, updatedAt=now)
                self._write(state)
        return {"deleted": True, "id": identifier, "kind": kind}

    def restore(self, kind: str, identifier: str) -> dict:
        """原始文件存在性由业务层核对；这里只撤销删除标记，保留备注和星标。"""
        with self.resource_lock(kind, identifier):
            state = self.read(kind, identifier)
            if state.get("permanent"):
                raise MetadataError("该资料已永久删除或正在完成永久删除，无法恢复。")
            if not state.get("deletedAt"):
                raise MetadataError("该资料不在回收站中。")
            state.update(deletedAt=None, updatedAt=datetime.now(timezone.utc).isoformat())
            self._write(state)
        return {"restored": True, "id": identifier, "kind": kind}

    def purge(self, kind: str, identifier: str, payload: object, *, require_trash: bool = False) -> dict:
        """不可逆地清理单项资料；部分失败保留无内容的标记供安全重试。

        会话调用方还须持有会话锁和 assistant.lock；音频的同一资源锁覆盖模型、
        比较及采集全过程。不会删除固定锁文件、日志、其他会话、工程或备份。
        """
        validate_purge_confirmation(payload)
        validate_identity(kind, identifier)
        folder = {"conversation": "conversations", "upload": "uploads", "recording": "recordings"}[kind]
        extensions = (".json",) if kind == "conversation" else (".wav", ".json")
        targets = [self.data / folder / (identifier + extension) for extension in extensions]
        targets.append(self._path(kind, identifier))
        with self.resource_lock(kind, identifier):
            # 全部目标预检完成才允许写停用标记，遇到链接不能先删掉其他正常文件。
            targets = [self.checked_path(path) for path in targets]
            marker = self._read_purge(kind, identifier)
            if marker is None:
                state = self.read(kind, identifier)
                if require_trash and not state.get("deletedAt"):
                    raise MetadataError("该资料不在回收站中，未执行永久删除。")
                if not any(path.exists() for path in targets):
                    raise MetadataError("资料不存在，未执行永久删除。")
                marker = {"version": 1, "id": identifier, "kind": kind, "state": "pending",
                          "startedAt": datetime.now(timezone.utc).isoformat(),
                          "fromTrash": bool(state.get("deletedAt"))}
                try:
                    self._write_purge(marker)
                except (OSError, ValueError, TypeError):
                    raise MetadataError("无法保存永久删除状态，尚未删除资料，请检查权限后重试。") from None
            elif require_trash and marker["state"] == "complete" and not marker["fromTrash"]:
                raise MetadataError("该资料不在回收站中，未执行永久删除。")
            try:
                # pending 无论由哪个入口产生都显示在回收站，那里只能继续永久删除。
                if require_trash and not marker["fromTrash"]:
                    marker["fromTrash"] = True
                    self._write_purge(marker)
                for path in targets:
                    self.checked_path(path).unlink(missing_ok=True)
                marker["state"] = "complete"
                self._write_purge(marker)
            except (OSError, ValueError, TypeError):
                raise MetadataError("永久删除未完成；该资料已停用且不能恢复，请重试永久删除。") from None
        return {"deleted": True, "permanent": True, "id": identifier, "kind": kind}

    def trash(self) -> dict:
        """回收站只读取标记摘要，不加载歌曲、会话文本或工程路径。"""
        items = []
        try:
            for path in self.directory.glob("*.json"):
                match = re.fullmatch(r"(conversation|upload|recording)-([0-9a-f]{32})", path.stem)
                if not match:
                    continue
                state = self.read(match[1], match[2])
                if state.get("deletedAt") and not state.get("permanent"):
                    items.append({key: state.get(key, "" if key in {"title", "label", "note"} else False)
                                  for key in ("id", "kind", "title", "label", "note", "starred", "deletedAt")})
            for path in self.purges.glob("*.json"):
                match = re.fullmatch(r"(conversation|upload|recording)-([0-9a-f]{32})", path.stem)
                if not match:
                    continue
                marker = self._read_purge(match[1], match[2])
                if marker and marker["state"] == "pending":
                    items.append({"id": marker["id"], "kind": marker["kind"],
                                  "title": "永久删除未完成", "label": "永久删除未完成", "note": "",
                                  "starred": False, "deletedAt": marker["startedAt"],
                                  "permanent": True, "purgePending": True, "restorable": False})
            return {"items": sorted(items, key=lambda item: item["deletedAt"], reverse=True)}
        except OSError:
            raise MetadataError("无法读取回收站，请检查本地数据目录权限。") from None

    @contextmanager
    def use_assets(self, attachments: list):
        """请求从解析附件到模型返回始终持锁，删除不能改变在途请求的资源状态。"""
        if not isinstance(attachments, list) or len(attachments) > 2:
            raise MetadataError("一次最多使用两个音频附件。")
        identities = []
        for item in attachments:
            if not isinstance(item, dict) or set(item) != {"kind", "id"}:
                raise MetadataError("附件必须使用有效的音频类型和编号。")
            kind, identifier = validate_identity(item["kind"], item["id"])
            if kind == "conversation" or (kind, identifier) in identities:
                raise MetadataError("音频附件类型无效或存在重复。")
            identities.append((kind, identifier))
        # 固定排序可避免两个请求以不同顺序获取 A/B 素材锁。锁均非阻塞，不会死等。
        with ExitStack() as stack:
            for kind, identifier in sorted(identities):
                stack.enter_context(self.resource_lock(kind, identifier))
                self.assert_available(kind, identifier)
            yield
