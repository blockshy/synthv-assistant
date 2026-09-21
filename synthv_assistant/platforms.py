"""命名模型平台注册表；默认平台继续使用原有 audio-settings.json。

平台注册表只保存新增平台，完整内容经当前 Windows 用户的 DPAPI 加密后原子
替换。任何公开接口均通过字段白名单投影，不返回密钥、密文或系统原始错误。
默认平台不迁移、不复制凭据，其读取与版本冲突语义保持原设置接口兼容。
新会话的默认平台只作为注册表内的独立偏好保存，不改变 A/B 听评使用的原设置。
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid

from .config import DATA
from .operations import OperationBusyError, OperationLock
from . import settings


MAX_PLATFORMS = 20
MAX_REGISTRY_BYTES = 512 * 1024
DEFAULT_PLATFORM_ID = "default"
DEFAULT_PLATFORM_NAME = "默认平台"
EMPTY_REVISION = "empty"


class PlatformError(settings.SettingsError):
    """仅包含固定中文消息的平台错误，不回显输入、文件路径或凭据。"""


class PlatformConflictError(settings.SettingsConflictError, PlatformError):
    """平台注册表版本已变化，调用方应重新读取后再编辑。"""


def _identifier(value: object, *, allow_default: bool = True) -> str:
    if not isinstance(value, str) or not ((allow_default and value == DEFAULT_PLATFORM_ID)
                                         or re.fullmatch(r"[0-9a-f]{32}", value)):
        raise PlatformError("模型平台编号无效，请重新选择平台。")
    return value


def _name(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 80
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PlatformError("平台名称需为 1 至 80 个字符，且不能包含控制字符。")
    return value.strip()


def _decode_pairs(pairs: list[tuple]) -> dict:
    """注册表不允许重复字段，避免不同解析器对加密内容作出不同解释。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlatformError("模型平台注册表损坏，已停止读取新增平台。")
        result[key] = value
    return result


def _read_registry() -> dict:
    """读取完整旧版或新版；坏密文不回退环境变量，也不覆盖为一个空注册表。"""
    try:
        with (DATA / "model-platforms.json").open("rb") as source:
            raw = source.read(MAX_REGISTRY_BYTES + 1)
    except FileNotFoundError:
        return {"revision": EMPTY_REVISION, "items": [], "defaultPlatformId": DEFAULT_PLATFORM_ID}
    except OSError:
        raise PlatformError("无法读取模型平台注册表，请检查本地数据目录权限。") from None
    try:
        if len(raw) > MAX_REGISTRY_BYTES:
            raise ValueError
        envelope = json.loads(raw, object_pairs_hook=_decode_pairs)
        if (not isinstance(envelope, dict) or set(envelope) != {"version", "storage", "revision", "encrypted"}
                or type(envelope["version"]) is not int or envelope["version"] != 1
                or envelope["storage"] != settings.STORAGE
                or not isinstance(envelope["revision"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", envelope["revision"])):
            raise ValueError
        protected_bytes = settings._decrypt(base64.b64decode(envelope["encrypted"], validate=True))
        protected = json.loads(protected_bytes, object_pairs_hook=_decode_pairs)
        # 旧注册表没有新会话偏好字段；仅允许这一种历史格式，其他缺失或多余字段仍拒绝。
        legacy_fields = {"kind", "revision", "items"}
        if (not isinstance(protected, dict)
                or set(protected) not in (legacy_fields, legacy_fields | {"defaultPlatformId"})
                or protected["kind"] != "model-platforms-v1" or protected["revision"] != envelope["revision"]
                or not isinstance(protected["items"], list) or len(protected["items"]) > MAX_PLATFORMS - 1):
            raise ValueError
        checked, identifiers = [], set()
        for item in protected["items"]:
            if not isinstance(item, dict) or set(item) != {"id", "name", "provider", "model", "base", "timeoutSeconds", "key"}:
                raise ValueError
            identifier = _identifier(item["id"], allow_default=False)
            if identifier in identifiers:
                raise ValueError
            values = settings._validate_values(item["provider"], item["model"], item["base"], item["timeoutSeconds"], item["key"])
            checked.append({**values, "id": identifier, "name": _name(item["name"])})
            identifiers.add(identifier)
        default_id = _identifier(protected.get("defaultPlatformId", DEFAULT_PLATFORM_ID))
        if default_id != DEFAULT_PLATFORM_ID:
            # 命名平台被停用时，保存操作会同时回退到 default，因此悬空或停用目标表示
            # 文件不满足持久化约束。不能悄悄改选另一平台，避免请求发往意外供应商。
            selected = next((item for item in checked if item["id"] == default_id), None)
            if selected is None or selected["provider"] == "none" or not selected["key"]:
                raise ValueError
        return {"revision": envelope["revision"], "items": checked, "defaultPlatformId": default_id}
    except (ValueError, TypeError, KeyError, UnicodeError, OSError, RecursionError, OverflowError):
        raise PlatformError("模型平台注册表损坏或无法解密，已停止读取新增平台；默认平台配置不受改写。") from None


def _snapshot(item: dict, revision: str) -> dict:
    """生成请求级内部快照，不缓存，并明确标记注册表版本。"""
    values = {key: item[key] for key in ("provider", "model", "base", "timeoutSeconds", "key")}
    result = settings._decorate_snapshot(values, "local", revision)
    result.update(id=item["id"], name=item["name"])
    return result


def _public(snapshot: dict) -> dict:
    """公开视图只有配置表单和密钥是否存在，绝不泄露密钥长度或片段。"""
    return {**settings._public_settings(snapshot), "id": snapshot["id"], "name": snapshot["name"]}


def get_model_platform_snapshot(identifier: str) -> dict:
    """仅供后端单次请求持有：包含 key，不能写日志、返回 HTTP 或传入提示词。"""
    identifier = _identifier(identifier)
    if identifier == DEFAULT_PLATFORM_ID:
        return {**settings.get_audio_configuration_snapshot(), "id": DEFAULT_PLATFORM_ID, "name": DEFAULT_PLATFORM_NAME}
    registry = _read_registry()
    for item in registry["items"]:
        if item["id"] == identifier:
            return _snapshot(item, registry["revision"])
    raise PlatformError("模型平台不存在，请重新选择平台。")


def get_model_platform(identifier: str) -> dict:
    return _public(get_model_platform_snapshot(identifier))


def list_model_platforms() -> dict:
    """只读列举平台；不会创建注册表、迁移默认设置或调用任何外部服务。"""
    return _public_registry(_read_registry())


def _public_registry(registry: dict) -> dict:
    """根据同一个注册表快照投影，避免保存返回值混入另一次并发修改的版本。"""
    default = get_model_platform(DEFAULT_PLATFORM_ID)
    return {"items": [default, *[_public(_snapshot(item, registry["revision"])) for item in registry["items"]]],
            "defaultPlatformId": registry["defaultPlatformId"], "revision": registry["revision"]}


def get_default_model_platform_id() -> str:
    """只读获取新会话偏好；文件损坏时明确报错，不退回到可能不同的供应商。

    无注册表或旧版注册表仍返回 default。该 ID 指向原配置，但不会改变或复制
    原配置；已有会话应继续使用创建时记录的平台，不受之后的偏好修改影响。
    """
    return _read_registry()["defaultPlatformId"]


def _save_registry(items: list[dict], default_platform_id: str) -> str:
    """调用者持有注册表锁；临时文件自始至终只包含 DPAPI 密文。"""
    revision = uuid.uuid4().hex
    protected = {"kind": "model-platforms-v1", "revision": revision, "items": items,
                 "defaultPlatformId": default_platform_id}
    try:
        encrypted = settings._encrypt(json.dumps(protected, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        envelope = {"version": 1, "storage": settings.STORAGE, "revision": revision,
                    "encrypted": base64.b64encode(encrypted).decode("ascii")}
        raw = json.dumps(envelope, ensure_ascii=True).encode("utf-8")
        if len(raw) > MAX_REGISTRY_BYTES:
            raise PlatformError("模型平台注册表超过本地大小限制，未保存此次修改。")
    except (ValueError, TypeError, UnicodeError, OSError):
        raise PlatformError("无法安全加密模型平台，未保存任何明文配置。") from None
    temporary = DATA / (".model-platforms-" + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, DATA / "model-platforms.json")
    except OSError:
        raise PlatformError("无法安全保存模型平台，原配置未被覆盖，请检查本地数据目录权限。") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return revision


def set_default_model_platform(payload: dict) -> dict:
    """选择未来新会话的平台；与平台编辑共享注册表锁和 CAS 版本。

    只接受已配置的平台，验证失败不写文件。显式选择 default 时只读取旧设置，
    不调用旧设置的保存接口，因此不会改变 A/B 听评或其凭据。返回完整公开列表，
    供页面一次性更新偏好、平台状态和下一次保存所需的注册表版本。
    """
    if (not isinstance(payload, dict) or set(payload) != {"id", "revision"}
            or not isinstance(payload["revision"], str)):
        raise PlatformError("默认平台字段缺失或格式无效，请重新读取列表后再保存。")
    identifier = _identifier(payload["id"])
    try:
        with OperationLock(DATA / "model-platforms.lock"):
            registry = _read_registry()
            if payload["revision"] != registry["revision"]:
                raise PlatformConflictError("模型平台已被其他页面或进程更新，请重新加载列表后再保存。")
            if identifier == DEFAULT_PLATFORM_ID:
                selected = get_model_platform_snapshot(DEFAULT_PLATFORM_ID)
            else:
                item = next((item for item in registry["items"] if item["id"] == identifier), None)
                if item is None:
                    raise PlatformError("模型平台不存在，请重新选择平台。")
                selected = _snapshot(item, registry["revision"])
            if not selected["configured"]:
                raise PlatformError("请先完成该平台的模型和 API key 配置，再将其设为新会话默认平台。")
            # 选择偏好也是一次注册表更新：始终生成新版本，使旧表单不能覆盖此次选择。
            revision = _save_registry(registry["items"], identifier)
            return _public_registry({**registry, "revision": revision, "defaultPlatformId": identifier})
    except OperationBusyError:
        raise PlatformError("其他页面或进程正在保存模型平台，请稍后重新加载再试。") from None
    except OSError:
        raise PlatformError("无法锁定模型平台注册表，请检查本地数据目录权限。") from None


def save_model_platform(payload: dict) -> dict:
    """创建或更新平台。所有非默认平台共享一个 CAS revision，并在锁内合并。

    空 key 只允许同一平台、供应商及规范化地址保留旧凭据；切换地址必须再次输入。
    provider=none 清除此平台密钥。此函数不会为了验证设置而发起模型请求。
    """
    required = {"provider", "model", "baseUrl", "timeoutSeconds", "revision"}
    if (not isinstance(payload, dict) or set(payload) - (required | {"id", "name", "apiKey"})
            or not required <= set(payload) or not isinstance(payload["revision"], str)):
        raise PlatformError("平台配置字段缺失或格式无效，请重新读取后再保存。")
    identifier = _identifier(payload["id"]) if "id" in payload else None
    if identifier == DEFAULT_PLATFORM_ID:
        # 默认平台名称固定；只转交旧设置接口认识的字段，保留旧文件与修复语义。
        public = settings.update_audio_settings({key: value for key, value in payload.items() if key not in {"id", "name"}})
        return {**public, "id": DEFAULT_PLATFORM_ID, "name": DEFAULT_PLATFORM_NAME}
    name = _name(payload.get("name"))
    values = settings._validate_values(payload["provider"], payload["model"], payload["baseUrl"],
                                       payload["timeoutSeconds"], payload.get("apiKey", ""))
    try:
        with OperationLock(DATA / "model-platforms.lock"):
            registry = _read_registry()
            if payload["revision"] != registry["revision"]:
                raise PlatformConflictError("模型平台已被其他页面或进程更新，请重新加载列表后再保存。")
            previous = next((item for item in registry["items"] if item["id"] == identifier), None)
            if identifier is not None and previous is None:
                raise PlatformError("模型平台不存在，请重新选择平台。")
            if identifier is None and len(registry["items"]) >= MAX_PLATFORMS - 1:
                raise PlatformError("最多保留 20 个模型平台，请使用现有平台。")
            if values["provider"] != "none" and not values["key"]:
                if previous and values["provider"] == previous["provider"] and values["base"] == previous["base"]:
                    values["key"] = previous["key"]
                if not values["key"]:
                    raise PlatformError("新增平台、启用供应商或修改 API 地址时，请重新填写 API key。")
            item = {**values, "id": identifier or uuid.uuid4().hex, "name": name}
            items = [item if old["id"] == identifier else old for old in registry["items"]]
            if identifier is None:
                items.append(item)
            default_id = registry["defaultPlatformId"]
            if item["id"] == default_id and not _snapshot(item, registry["revision"])["configured"]:
                # 停用当前新会话默认平台时，与配置修改原子地回退到旧平台 ID。旧平台
                # 可能尚未配置，调用方会正常提示配置；绝不擅自挑选另一个云端平台。
                default_id = DEFAULT_PLATFORM_ID
            revision = _save_registry(items, default_id)
            return _public(_snapshot(item, revision))
    except OperationBusyError:
        raise PlatformError("其他页面或进程正在保存模型平台，请稍后重新加载再试。") from None
    except OSError:
        raise PlatformError("无法锁定模型平台注册表，请检查本地数据目录权限。") from None
