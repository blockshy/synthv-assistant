"""可即时生效的音频供应商设置，使用 Windows DPAPI 保存完整配置。

公开函数只返回脱敏视图。含密钥的快照仅供 review 模块在一次请求内部使用，
不得写日志、返回 HTTP/MCP 或加入模型上下文。没有本地配置文件时才使用
环境变量；本地文件损坏、无法读取或无法解密时停用听评，不回退到环境密钥。
"""

from __future__ import annotations

import base64
import ctypes
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
import uuid

from .config import DATA
from .operations import OperationBusyError, OperationLock


DEFAULTS = {
    "openai": ("gpt-audio-1.5", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "gemini": ("gemini-3.8-flash", "https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY"),
}
STORAGE = "windows-dpapi"
MAX_SETTINGS_BYTES = 128 * 1024
_ENTROPY = b"SynthV Assistant audio settings v1"
_CORRUPT_MESSAGE = "本地听评配置损坏或无法解密，已停用听评；请重新保存或清除配置。"


class SettingsError(ValueError):
    """只携带固定中文文案的配置错误，不能包含用户输入和系统原始错误。"""


class SettingsConflictError(SettingsError):
    """页面持有旧 revision，需要重新读取配置，不能覆盖其他客户端的新设置。"""


class _DataBlob(ctypes.Structure):
    """Windows DATA_BLOB；长度固定为 DWORD，指针宽度由当前 Python 决定。"""
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, decrypt: bool) -> bytes:
    """调用用户范围 DPAPI，不使用机器范围、不弹窗，也不提供明文降级。"""
    if os.name != "nt":
        raise SettingsError("安全保存听评配置需要 Windows DPAPI；当前平台不支持，未保存任何明文配置。")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_pointer = ctypes.POINTER(_DataBlob)
        protect = crypt32.CryptProtectData
        protect.argtypes = [blob_pointer, ctypes.c_wchar_p, blob_pointer, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
        protect.restype = ctypes.c_int
        unprotect = crypt32.CryptUnprotectData
        unprotect.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_uint32, blob_pointer]
        unprotect.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        buffer = ctypes.create_string_buffer(data)
        entropy_buffer = ctypes.create_string_buffer(_ENTROPY)
        incoming = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy = _DataBlob(len(_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = _DataBlob()
        try:
            # CRYPTPROTECT_UI_FORBIDDEN=1；不设置 LOCAL_MACHINE，密文绑定当前用户。
            if decrypt:
                success = unprotect(ctypes.byref(incoming), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing))
            else:
                success = protect(ctypes.byref(incoming), "SynthV Assistant", ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing))
            if not success:
                raise SettingsError("Windows DPAPI 加密或解密失败，未使用明文配置。")
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            # 及时清理 ctypes 中间缓冲；Python 返回 bytes 的寿命仍由请求快照管理。
            ctypes.memset(buffer, 0, len(data))
            if outgoing.pbData:
                ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
                kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))
    except SettingsError:
        raise
    except (OSError, AttributeError, ctypes.ArgumentError):
        raise SettingsError("Windows DPAPI 不可用，未保存任何明文配置。") from None


def _encrypt(data: bytes) -> bytes:
    """加密全部配置；便于测试模拟系统服务失败而不改变存储协议。"""
    return _crypt(data, decrypt=False)


def _decrypt(data: bytes) -> bytes:
    """解密后还需校验配置结构和 revision，不能盲信得到的字节。"""
    return _crypt(data, decrypt=True)


def _normalize_base(value: object) -> str:
    """规范化 HTTPS 根地址；等价大小写、443 端口与末尾斜线可安全沿用密钥。"""
    message = "API 根地址必须是有效 HTTPS 地址，不能包含凭据、查询参数、片段或空白。"
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise SettingsError(message)
    value = value.strip()
    if not value or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise SettingsError(message)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value):
            raise SettingsError(message)
        host = parsed.hostname
        if ":" in host:
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        else:
            host = host.encode("idna").decode("ascii").lower().rstrip(".")
            if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
                raise SettingsError(message)
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise SettingsError(message)
        authority = host + (":" + str(port) if port not in (None, 443) else "")
        return urlunsplit(("https", authority, parsed.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise SettingsError(message) from None


def _validate_values(provider: object, model: object, base: object, timeout: object, key: object) -> dict:
    """所有输入错误都使用固定文本；校验失败不得更改原配置文件。"""
    if not isinstance(provider, str) or provider not in {"none", "openai", "gemini"}:
        raise SettingsError("请选择停用、OpenAI 或 Gemini 供应商。")
    if provider == "none":
        return {"provider": "none", "model": "", "base": "", "timeoutSeconds": 60.0, "key": ""}
    if not isinstance(model, str):
        raise SettingsError("模型名称格式无效。")
    model = model.strip() or DEFAULTS[provider][0]
    # 兼容服务常使用 namespace/model 路由；OpenAI 的模型字段位于 JSON 中，
    # 可以接受分段 ID。Gemini 会拼入 URL，保持更严格的单段字符白名单。
    pattern = r"[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*" if provider == "openai" else r"[A-Za-z0-9_.-]{1,128}"
    if len(model) > 200 or not re.fullmatch(pattern, model):
        raise SettingsError("模型名称应为有效模型 ID，不包含 URL 或空格；Gemini 请勿填写 models/ 前缀。")
    if isinstance(base, str) and not base.strip():
        base = DEFAULTS[provider][1]
    base = _normalize_base(base)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 5 <= timeout <= 180 or not math.isfinite(timeout)):
        raise SettingsError("超时时间必须为 5 至 180 之间的秒数。")
    if not isinstance(key, str) or len(key) > 8192:
        raise SettingsError("API key 格式无效，请重新输入。")
    key = key.strip()
    if any(not 33 <= ord(char) <= 126 for char in key):
        raise SettingsError("API key 不能包含空格、换行或非 ASCII 字符。")
    return {"provider": provider, "model": model, "base": base, "timeoutSeconds": float(timeout), "key": key}


def _decorate_snapshot(values: dict, source: str, revision: str, *, invalid: bool = False, message: str | None = None) -> dict:
    """建立一次性内部快照，不缓存；调用者不能把该结果直接返回给用户。"""
    configured = bool(not invalid and values["provider"] != "none" and values["key"])
    return {**values, "configured": configured, "invalid": invalid, "source": source,
            "storage": STORAGE, "revision": revision,
            "message": message or ("听评配置已生效；调用听评时才会上传所选音频。" if configured else
                                   "听评接口未配置或已停用；本地分析可用，尚未调用模型试听。")}


def _disabled_snapshot(source: str, revision: str, message: str) -> dict:
    return _decorate_snapshot(_validate_values("none", "", "", 60, ""), source, revision, invalid=True, message=message)


def _environment_snapshot() -> dict:
    """仅作为无本地文件时的初始来源；不把环境密钥写到磁盘或返回公开接口。"""
    provider = os.environ.get("SYNTHV_AUDIO_PROVIDER", "").strip().lower() or "none"
    defaults = DEFAULTS.get(provider, ("", "", ""))
    try:
        timeout = float(os.environ.get("SYNTHV_AUDIO_TIMEOUT_SECONDS", "60"))
        values = _validate_values(provider, os.environ.get("SYNTHV_AUDIO_MODEL", ""),
                                  os.environ.get("SYNTHV_AUDIO_BASE_URL", ""), timeout,
                                  os.environ.get(defaults[2], "") if defaults[2] else "")
        return _decorate_snapshot(values, "environment", "environment")
    except (ValueError, TypeError):
        return _disabled_snapshot("environment", "environment", "环境中的听评配置无效，已停用听评；请在设置中重新配置。")


def get_audio_configuration_snapshot() -> dict:
    """内部接口：读取一致的最新配置快照，包含密钥，仅供单次听评请求持有。

    原子替换使读取者只能得到完整旧版或完整新版，无需占用设置写锁。配置损坏
    采用失败关闭，不会悄悄切回环境中的其他账户或服务地址。
    """
    path = DATA / "audio-settings.json"
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        return _environment_snapshot()
    except OSError:
        return _disabled_snapshot("local", "invalid", _CORRUPT_MESSAGE)
    try:
        if len(raw) > MAX_SETTINGS_BYTES:
            raise SettingsError(_CORRUPT_MESSAGE)
        envelope = json.loads(raw.decode("utf-8"))
        if (not isinstance(envelope, dict) or envelope.get("version") != 1 or envelope.get("storage") != STORAGE
                or not isinstance(envelope.get("revision"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", envelope["revision"])):
            raise SettingsError(_CORRUPT_MESSAGE)
        encrypted = base64.b64decode(envelope["encrypted"], validate=True)
        protected = json.loads(_decrypt(encrypted).decode("utf-8"))
        if not isinstance(protected, dict) or protected.get("revision") != envelope["revision"]:
            raise SettingsError(_CORRUPT_MESSAGE)
        values = _validate_values(protected["provider"], protected["model"], protected["base"],
                                  protected["timeoutSeconds"], protected["key"])
        return _decorate_snapshot(values, "local", envelope["revision"])
    except (ValueError, TypeError, KeyError, UnicodeError, OSError):
        return _disabled_snapshot("local", "invalid", _CORRUPT_MESSAGE)


def _public_settings(snapshot: dict) -> dict:
    """通过字段白名单返回表单值，不包含 key、密文、密钥长度或密钥片段。"""
    return {"provider": snapshot["provider"], "model": snapshot["model"], "baseUrl": snapshot["base"],
            "timeoutSeconds": snapshot["timeoutSeconds"], "keyConfigured": bool(snapshot["key"]),
            "configured": snapshot["configured"], "source": snapshot["source"], "storage": STORAGE,
            "revision": snapshot["revision"], "message": snapshot["message"]}


def get_audio_settings() -> dict:
    """公开读取接口：后续读取会立即看到其他 HTTP/MCP 进程保存的新设置。"""
    return _public_settings(get_audio_configuration_snapshot())


def _save(values: dict) -> dict:
    """调用者已持有 settings.lock；只将 DPAPI 密文写入同目录临时文件。"""
    revision = uuid.uuid4().hex
    protected = {**values, "revision": revision}
    encrypted = _encrypt(json.dumps(protected, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    envelope = {"version": 1, "storage": STORAGE, "revision": revision,
                "encrypted": base64.b64encode(encrypted).decode("ascii")}
    destination = DATA / "audio-settings.json"
    temporary = DATA / (".audio-settings-" + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(json.dumps(envelope, ensure_ascii=True).encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except OSError:
        raise SettingsError("无法安全保存听评配置；原配置未被覆盖，请检查本地数据目录权限。") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # 临时文件仅含密文；清理失败不能暴露明文，也不应掩盖主要保存结果。
            pass
    return _public_settings(_decorate_snapshot(values, "local", revision))


def update_audio_settings(payload: dict) -> dict:
    """校验并原子更新设置；同一供应商和规范化地址不变时，空 key 表示保留。

    revision 是必填的并发版本条件。新供应商或新地址必须显式重新填写密钥，
    避免把旧服务凭据误送给另一站点。所有校验与加密成功后才替换现有文件。
    """
    if not isinstance(payload, dict):
        raise SettingsError("听评配置必须是 JSON 对象。")
    fields = {"provider", "model", "baseUrl", "timeoutSeconds", "apiKey", "revision"}
    if set(payload) - fields or not {"provider", "model", "baseUrl", "timeoutSeconds", "revision"} <= set(payload):
        raise SettingsError("听评配置字段缺失或不受支持，请刷新页面后重试。")
    if not isinstance(payload["revision"], str):
        raise SettingsError("配置版本无效，请刷新页面后重试。")
    values = _validate_values(payload["provider"], payload["model"], payload["baseUrl"],
                              payload["timeoutSeconds"], payload.get("apiKey", ""))
    try:
        with OperationLock(DATA / "settings.lock"):
            previous = get_audio_configuration_snapshot()
            if payload["revision"] != previous["revision"]:
                raise SettingsConflictError("听评配置已被其他页面或进程更新，请重新加载设置后再保存。")
            if values["provider"] != "none" and not values["key"]:
                if (not previous["invalid"] and values["provider"] == previous["provider"]
                        and values["base"] == previous["base"]):
                    values["key"] = previous["key"]
                if not values["key"]:
                    raise SettingsError("启用供应商或修改 API 地址时，请重新填写 API key。")
            return _save(values)
    except OperationBusyError:
        raise SettingsError("其他页面或进程正在保存听评配置，请稍后重新加载并重试。") from None
    except OSError:
        raise SettingsError("无法锁定听评配置，请检查本地数据目录权限。") from None


def clear_audio_settings() -> dict:
    """清除本地密钥并持久化 none，明确覆盖环境 fallback；不删除配置文件。"""
    try:
        with OperationLock(DATA / "settings.lock"):
            return _save(_validate_values("none", "", "", 60, ""))
    except OperationBusyError:
        raise SettingsError("其他页面或进程正在保存听评配置，请稍后重试清除操作。") from None
    except OSError:
        raise SettingsError("无法安全清除听评配置，请检查本地数据目录权限。") from None
