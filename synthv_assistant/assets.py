"""管理用户主动上传的短音频；上传只写本机，不触发任何模型请求。

所有文件由随机编号寻址，原始文件名只作为展示文字，绝不参与磁盘路径拼接。
WAV 保留可用的整数 PCM；MP3 使用已安装的 FFmpeg 在本机转换为标准 WAV。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from .analysis import analyze_wav
from .bridge import atomic_json
from .config import DATA, RECORDINGS
from .operations import OperationLock, OperationBusyError
from .metadata import LibraryMetadata, MetadataError, validate_identity


MAX_UPLOAD_BYTES = 12_000_000
MAX_UPLOAD_SECONDS = 60
MAX_UPLOADS = 200


def _identifier(value: object) -> str:
    """严格检查不透明编号，禁止路径穿越及用户任意文件读取。"""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("音频编号无效。")
    return value


def _uploads_directory() -> Path:
    directory = DATA / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def upload_path(identifier: str) -> Path:
    """播放器及模型仅能读取完整导入的 WAV，不能访问原始临时文件。"""
    identifier = _identifier(identifier)
    LibraryMetadata(DATA).assert_available("upload", identifier)
    directory = _uploads_directory()
    path = directory / (identifier + ".wav")
    if not path.is_file() or not (directory / (identifier + ".json")).is_file():
        raise ValueError("上传音频不存在或尚未导入完成。")
    return path


def get_upload(identifier: str) -> dict:
    """只向浏览器返回固定元数据字段，不返回本机路径。"""
    path = upload_path(identifier)
    try:
        data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        if data["id"] != identifier or data["kind"] != "upload":
            raise ValueError
        item = {key: data[key] for key in ("id", "kind", "name", "label", "url", "format",
                                          "durationSeconds", "createdAt", "analysis")}
        return LibraryMetadata(DATA).decorate("upload", item)
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("上传音频信息损坏，请重新上传该片段。") from None


def list_uploads() -> dict:
    """按时间返回已完成的导入；不把正在转换的临时文件展示为可用素材。"""
    items = []
    # 素材可以在另一个线程永久删除；枚举后消失的索引应被跳过，不能使整页失败。
    paths = []
    for path in _uploads_directory().glob("*.json"):
        try:
            paths.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
    for _, path in sorted(paths, reverse=True):
        try:
            items.append(get_upload(path.stem))
            if len(items) >= MAX_UPLOADS:
                break
        except ValueError:
            continue
    return {"items": items}


def _convert_mp3(source: Path, output: Path) -> None:
    """限制输入解复用器、协议和输出时长，避免把伪装文件当作播放列表执行。

    仅使用系统已有 FFmpeg，不下载或安装工具；参数始终使用数组传递，不经过
    shell。最多解码 61 秒，超过允许长度后整体拒绝，不悄悄截断用户的作品。
    """
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("导入 MP3 需要本机已有 FFmpeg；当前请先导出 16/24/32 位 PCM WAV 后上传。")
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-protocol_whitelist", "file,pipe", "-f", "mp3", "-i", str(source),
               "-t", "61", "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
               "-c:a", "pcm_s16le", "-f", "wav", str(output)]
    try:
        # stderr 不返回给网页，它可能带原始文件路径。每次转换有硬超时。
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=35,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("MP3 转换失败或超时，请改用短 PCM WAV 片段。") from None
    if result.returncode != 0 or not output.is_file():
        raise ValueError("无法解码这份 MP3，请确认音频文件完整。")


def save_upload(filename: str, data: bytes) -> dict:
    """校验并保存最多 12 MB、60 秒的 WAV/MP3；无模型调用和自动上传外网。"""
    if not isinstance(filename, str) or not filename or len(filename) > 400:
        raise ValueError("请提供有效的音频文件名。")
    name = re.split(r"[\\/]", filename)[-1].strip()
    if not name or any(ord(char) < 32 for char in name):
        raise ValueError("音频文件名无效。")
    extension = Path(name).suffix.lower()
    if extension not in {".wav", ".mp3"}:
        raise ValueError("当前支持 WAV 和 MP3 音频，请先导出需要调教的短片段。")
    if not isinstance(data, bytes) or not 44 < len(data) <= MAX_UPLOAD_BYTES:
        raise ValueError("音频不能为空，单个上传文件不得超过 12 MB。")
    directory = _uploads_directory()
    identifier = uuid.uuid4().hex
    source = directory / (identifier + ".source" + extension)
    output = directory / (identifier + ".wav")
    metadata = directory / (identifier + ".json")
    completed = False
    try:
        # 与录音/工程锁分离，两个同时上传的请求不能绕过素材数量上限。
        with OperationLock(DATA / "uploads.lock"):
            if len(list_uploads()["items"]) >= MAX_UPLOADS:
                raise ValueError("本机素材库已达到 200 个文件，请整理素材后再上传。")
            if extension == ".mp3":
                source.write_bytes(data)
                _convert_mp3(source, output)
            else:
                output.write_bytes(data)
            if output.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValueError("解码后的音频超过 12 MB，请缩短片段后再上传。")
            try:
                analysis = analyze_wav(output)
            except (ValueError, OSError):
                raise ValueError("无法读取完整音频；WAV 请使用 16/24/32 位整数 PCM 格式。") from None
            if not 0 < analysis["duration"] <= MAX_UPLOAD_SECONDS:
                raise ValueError("上传音频需为大于 0 秒且不超过 60 秒的片段，未保存截断版本。")
            item = {"id": identifier, "kind": "upload", "name": name, "label": name,
                    "url": "/uploads/" + identifier + ".wav", "format": "wav",
                    "durationSeconds": analysis["duration"], "analysis": analysis,
                    "createdAt": datetime.now(timezone.utc).isoformat()}
            atomic_json(metadata, item)
            completed = True
            return LibraryMetadata(DATA).decorate("upload", item)
    except OperationBusyError:
        raise ValueError("另一个音频正在导入，请等待完成后重试。") from None
    except OSError:
        raise ValueError("无法保存上传音频，请检查本地数据目录权限。") from None
    finally:
        # 只清理本次由随机编号创建的文件，不删除已有素材或用户原始音频。
        for temporary in ([source] if completed else [source, output, metadata]):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # 清理失败不覆盖最初的验证错误；没有元数据的残片不会出现在素材库。
                pass


def resolve_audio_attachments(attachments: list) -> tuple[list[Path], list[dict]]:
    """把显式选中的至多两个附件转换为白名单文件；空列表完全不读取音频。

    历史消息中的附件不会自动加入本轮请求。元数据只用于本地会话展示，模型
    上下文由调用方再次按白名单构造，不能携带录音记录中的工程绝对路径。
    """
    if not isinstance(attachments, list) or len(attachments) > 2:
        raise ValueError("每条消息最多附带两个音频；也可以不附音频直接发送文字。")
    paths, items, seen = [], [], set()
    total_bytes = 0
    for attachment in attachments:
        if not isinstance(attachment, dict) or set(attachment) != {"kind", "id"}:
            raise ValueError("附件必须使用素材编号，不能传入文件路径。")
        kind, identifier = attachment["kind"], _identifier(attachment["id"])
        if not isinstance(kind, str) or kind not in {"upload", "recording"} or (kind, identifier) in seen:
            raise ValueError("附件类型无效或包含重复的音频。")
        seen.add((kind, identifier))
        if kind == "upload":
            path, item = upload_path(identifier), get_upload(identifier)
        else:
            LibraryMetadata(DATA).assert_available("recording", identifier)
            path = RECORDINGS / (identifier + ".wav")
            try:
                raw = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                if raw.get("id") != identifier or not path.is_file():
                    raise ValueError
                item = {"id": identifier, "kind": "recording", "name": raw.get("label", "工程录音"),
                        "label": raw.get("label", "工程录音"), "url": "/audio/" + identifier + ".wav",
                        "format": "wav", "durationSeconds": raw.get("analysis", {}).get("duration", raw.get("durationSeconds")),
                        "createdAt": raw.get("createdAt"), "startSeconds": raw.get("startSeconds")}
                item = LibraryMetadata(DATA).decorate("recording", item)
            except (OSError, ValueError, TypeError, AttributeError):
                raise ValueError("选中的工程录音不可用，请重新选择。") from None
        total_bytes += path.stat().st_size
        if total_bytes > MAX_UPLOAD_BYTES:
            raise ValueError("本次所选音频合计超过 12 MB，请只发送一个或缩短片段。")
        paths.append(path)
        items.append(item)
    return paths, items


def _stored_asset(kind: str, identifier: str) -> dict:
    """读取完整原始素材；回收站恢复也使用此入口，绝不恢复已丢失的 WAV。"""
    validate_identity(kind, identifier)
    if kind not in {"upload", "recording"}:
        raise MetadataError("这里只能操作上传音频或工程录音。")
    directory = DATA / "uploads" if kind == "upload" else RECORDINGS
    path = directory / (identifier + ".wav")
    try:
        if not path.is_file():
            raise ValueError
        with path.with_suffix(".json").open("rb") as source:
            encoded = source.read(2_000_001)
        if len(encoded) > 2_000_000:
            raise ValueError
        raw = json.loads(encoded)
        if not isinstance(raw, dict) or raw.get("id") != identifier:
            raise ValueError
        # 保留现有播放器与分析需要的公开字段，排除工程路径、采集进程和内部调用结果。
        fields = {"id", "name", "label", "format", "durationSeconds", "createdAt", "analysis",
                  "startSeconds", "mix", "captureNote"}
        item = {key: value for key, value in raw.items() if key in fields}
        if kind == "recording" and "captureNote" not in item and raw.get("note"):
            item["captureNote"] = raw["note"]
        item.update(kind=kind, format="wav", url=("/uploads/" if kind == "upload" else "/audio/") + identifier + ".wav")
        item.setdefault("label", item.get("name", "工程录音"))
        return item
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MetadataError("音频不存在、尚未完成或资料损坏，无法进行此操作。") from None


def update_asset_metadata(kind: str, identifier: str, payload: dict) -> dict:
    """只更新本地覆盖层，音频字节和原始采集记录保持不变。"""
    item = _stored_asset(kind, identifier)
    library = LibraryMetadata(DATA)
    state = library.update(kind, identifier, payload)
    return library.decorate(kind, item, state)


def delete_asset(kind: str, identifier: str) -> dict:
    item = _stored_asset(kind, identifier)
    return LibraryMetadata(DATA).delete(kind, identifier, item.get("label", "音频"))


def purge_asset(kind: str, identifier: str, payload: object, *, require_trash: bool = False) -> dict:
    """永久删除仅寻址 DATA 内该音频的 WAV、索引与本地覆盖标记。

    不先调用要求双文件完整的 _stored_asset：上次删除中断后必须能清理剩余文件。
    资源存在性、路径白名单、确认及跨进程互斥统一在永久删除事务内检查。
    """
    validate_identity(kind, identifier)
    if kind not in {"upload", "recording"}:
        raise MetadataError("这里只能永久删除上传音频或工程录音。")
    return LibraryMetadata(DATA).purge(kind, identifier, payload, require_trash=require_trash)


def restore_asset(kind: str, identifier: str) -> dict:
    _stored_asset(kind, identifier)
    if kind == "upload":
        try:
            # 与导入共享容量锁：回收站不占活跃素材名额，恢复也必须重新检查上限。
            with OperationLock(DATA / "uploads.lock"):
                if len(list_uploads()["items"]) >= MAX_UPLOADS:
                    raise MetadataError("活跃上传音频已达到 200 个，请先移除其他音频后再恢复。")
                return LibraryMetadata(DATA).restore(kind, identifier)
        except OperationBusyError:
            raise MetadataError("另一个音频正在导入或恢复，请稍后重试。") from None
        except OSError:
            raise MetadataError("无法恢复音频，请检查本地数据目录权限。") from None
    return LibraryMetadata(DATA).restore(kind, identifier)
