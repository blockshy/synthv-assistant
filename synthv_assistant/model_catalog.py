"""按用户主动刷新请求读取供应商模型目录，不推断目录未提供的模型能力。

接口只访问所选平台的 HTTPS 根地址，不跟随重定向或响应里的任意下一页 URL。
每次刷新使用一个配置快照，限制页数、模型数、响应大小和请求超时，不自动重试。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from urllib import error, request
from urllib.parse import urlencode, urlsplit, urlunsplit

from .config import DATA
from .platforms import get_model_platform_snapshot
from .review import _NoRedirect
from . import settings


MAX_PAGES = 5
MAX_MODELS = 500
MAX_RESPONSE_BYTES = 2_000_000
MAX_REFRESH_SECONDS = 30.0
MAX_CACHE_BYTES = 512 * 1024
CACHE_STALE_SECONDS = 7 * 24 * 60 * 60


class ModelCatalogError(ValueError):
    """固定中文目录错误；不返回供应商响应体、认证头、密钥或完整请求地址。"""


def _cache_path(identifier: str):
    """缓存文件只采用已验证的平台编号，禁止输入影响本地保存目录。"""
    if not isinstance(identifier, str) or not re.fullmatch(r"default|[0-9a-f]{32}", identifier):
        raise ModelCatalogError("模型平台编号无效，请重新选择平台。")
    return DATA / "model-catalogs" / (identifier + ".json")


def _configuration_fingerprint(config: dict) -> str:
    """只在内存中组合连接身份；保存前还需由当前用户 DPAPI 加密该摘要。

    平台 ID、协议、地址或 API key 改变后，旧账户目录立即失效。名称、默认模型、
    超时和其他平台的注册表版本不影响该目录，避免无关设置导致重复刷新。
    """
    identity = [config[name] for name in ("id", "provider", "base", "key")]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def _cache_result(config: dict, value: dict | None = None, *, unavailable: bool = False) -> dict:
    """构造缓存公开视图；连接身份摘要和本地文件位置不进入 HTTP 响应。"""
    if value is None:
        return {"platformId": config["id"], "provider": config.get("provider"), "models": [],
                "source": "none", "cacheHit": False, "cachedAt": None, "stale": False,
                "ageSeconds": None, "pages": 0, "truncated": False,
                "message": ("本地模型缓存不可用，可主动刷新或手动填写模型 ID。" if unavailable else
                            "尚无当前平台配置的模型缓存，可主动刷新或手动填写模型 ID。")}
    age = max(0, int(time.time() - value["savedAt"]))
    stale = age >= CACHE_STALE_SECONDS
    return {"platformId": config["id"], "provider": config["provider"], "models": value["models"],
            "source": "cache", "cacheHit": True,
            "cachedAt": datetime.fromtimestamp(value["savedAt"], timezone.utc).isoformat(),
            "stale": stale, "ageSeconds": age, "pages": value["pages"], "truncated": value["truncated"],
            "message": ("已复用本地模型目录；缓存超过 7 天，可按需主动刷新。" if stale else
                        "已复用本地模型目录；刷新按钮可重新向供应商获取。")}


def _save_cache(config: dict, result: dict) -> dict | None:
    """原子保存已脱敏目录，失败不撤销本次成功获取，也不覆盖原缓存。

    缓存只辅助选模型，不是账户设置；因此磁盘写入失败时仍允许使用本次目录。
    请求使用的身份随缓存一起保存，配置更新期间返回的旧请求不能成为新配置缓存。
    """
    destination = _cache_path(config["id"])
    temporary = destination.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        # 摘要本身也受 DPAPI 保护，避免缓存文件成为低熵兼容服务密钥的猜测验证器。
        # 不提供明文回退；安全保存失败仅影响缓存，不影响已经获取的模型列表。
        proof = settings._encrypt(_configuration_fingerprint(config).encode("ascii"))
        value = {"version": 1, "platformId": config["id"], "configurationProof": base64.b64encode(proof).decode("ascii"),
                 "savedAt": time.time(), "models": result["models"], "pages": result["pages"],
                 "truncated": result["truncated"]}
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_CACHE_BYTES:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        return value
    except (OSError, ValueError, TypeError):
        return None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def get_cached_platform_models(identifier: str) -> dict:
    """只读本地目录；未命中、过期、损坏或配置变更都不会自动访问供应商。

    超过七天仅作陈旧标记，仍可选择；是否刷新始终由用户决定。每次读取重新核对
    连接身份，因此页面刷新、服务重启及不同会话可复用，改用其他账户不会混用目录。
    """
    config = get_model_platform_snapshot(identifier)
    path = _cache_path(config["id"])
    if (not config.get("configured") or config.get("invalid") or not config.get("key")
            or config.get("provider") not in {"openai", "gemini", "qwen"}):
        return _cache_result(config)
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError
        value = json.loads(raw)
        required = {"version", "platformId", "configurationProof", "savedAt", "models", "pages", "truncated"}
        if (not isinstance(value, dict) or set(value) != required or type(value["version"]) is not int
                or value["version"] != 1 or value["platformId"] != config["id"]
                or not isinstance(value["configurationProof"], str)
                or len(value["configurationProof"]) > 8192):
            return _cache_result(config)
        fingerprint = settings._decrypt(base64.b64decode(value["configurationProof"], validate=True))
        if fingerprint != _configuration_fingerprint(config).encode("ascii"):
            return _cache_result(config)
        if (type(value["savedAt"]) not in {int, float} or not math.isfinite(value["savedAt"])
                or not 0 <= value["savedAt"] <= time.time() + 300
                or type(value["pages"]) is not int or not 1 <= value["pages"] <= MAX_PAGES
                or type(value["truncated"]) is not bool or not isinstance(value["models"], list)
                or len(value["models"]) > MAX_MODELS):
            raise ValueError
        seen = set()
        for item in value["models"]:
            if not isinstance(item, dict) or set(item) != {"id", "label"}:
                raise ValueError
            # 缓存与供应商响应使用同一白名单，防止坏缓存插入任意 ID、控制符或密钥。
            raw_item = ({"name": "models/" + str(item["id"]), "displayName": item["label"],
                         "supportedGenerationMethods": ["generateContent"]} if config["provider"] == "gemini"
                        else {"id": item["id"], "name": item["label"]})
            checked = _model_entry(raw_item, config["provider"], config["key"])
            if checked != item or item["id"] in seen:
                raise ValueError
            seen.add(item["id"])
        return _cache_result(config, value)
    except FileNotFoundError:
        return _cache_result(config)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return _cache_result(config, unavailable=True)


def _get_json(url: str, headers: dict, timeout: float) -> dict:
    """执行一次 GET；测试以替身替换本函数，不需要任何真实 API key。"""
    outbound = request.Request(url, headers={"Accept": "application/json", **headers}, method="GET")
    opener = request.build_opener(_NoRedirect())
    deadline = time.monotonic() + timeout
    pieces, size = [], 0
    with opener.open(outbound, timeout=timeout) as response:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            chunk = response.read(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            pieces.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ModelCatalogError("模型目录响应超过本地大小限制，请检查供应商接口。")
    result = json.loads(b"".join(pieces).decode("utf-8"))
    if not isinstance(result, dict):
        raise ModelCatalogError("供应商没有返回有效的模型目录。")
    return result


def _model_entry(raw: object, provider: str, key: str) -> dict | None:
    """仅保留可用作配置模型 ID 的条目；供应商名称一律作为普通展示文字。"""
    if not isinstance(raw, dict):
        return None
    if provider == "gemini":
        methods = raw.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:
            return None
        identifier = raw.get("name")
        if isinstance(identifier, str) and identifier.startswith("models/"):
            identifier = identifier[len("models/"):]
        pattern = r"[A-Za-z0-9_.-]{1,128}"
        label = raw.get("displayName")
    else:
        # 百炼原生目录使用 model，缓存仍统一保存 id / label；兼容缓存读取
        # 的 id 回退只用于投影数据，不改变任何请求地址或模型能力判定。
        identifier = raw.get("model", raw.get("id")) if provider == "qwen" else raw.get("id")
        pattern = r"[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*"
        label = raw.get("name") or raw.get("displayName")
    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 200
            or not re.fullmatch(pattern, identifier) or (key and key in identifier)):
        return None
    if not isinstance(label, str) or not label.strip():
        label = identifier
    # 即使供应商意外回显认证内容，公开目录也不展示它；不允许控制字符进入 UI。
    label = "".join(char for char in label if ord(char) >= 32 and ord(char) != 127).strip()
    label = label.replace(key, "[已隐藏密钥]") if key else label
    return {"id": identifier, "label": label[:200] or identifier}


def _qwen_catalog_base(base: str) -> str:
    """仅为官方已确认的百炼根地址推导同源原生目录，禁止猜测自定义网关路由。

    Qwen 的 /models 使用原生 /api/v1 协议，并非 Chat 的兼容路由。保持 scheme、
    authority 完全不变，不因地区、业务空间或供应商下一页 URL 而转发认证头。
    其他地址仍可发起用户配置的 Chat 请求，目录功能则明确提示手动填写模型。
    """
    parsed = urlsplit(base)
    official = parsed.hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
                                  "cn-hongkong.dashscope.aliyuncs.com"}
    workspace = re.fullmatch(r"[a-z0-9][a-z0-9-]*\.(?:cn-beijing|ap-northeast-1|eu-central-1|us-east-1)\.maas\.aliyuncs\.com",
                             parsed.hostname or "")
    if (parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != "/compatible-mode/v1"
            or not (official or workspace)):
        raise ModelCatalogError("此 Qwen 地址的模型目录路径尚未适配，请手动填写模型 ID；未尝试其他地址。")
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/models", "", ""))


def list_platform_models(identifier: str) -> dict:
    """返回公开模型列表；只有显式调用此函数才产生外网 GET 请求。

    Gemini 只展示支持 generateContent 的条目。目录通常不含完整推理、音频或
    参数能力，因此返回结果不承诺任何输入能力，也不自动修改平台选中的模型。
    """
    try:
        config = get_model_platform_snapshot(identifier)
        if (not config.get("configured") or config.get("invalid") or not config.get("key")
                or config.get("provider") not in {"openai", "gemini", "qwen"}):
            raise ModelCatalogError("此平台尚未配置或已停用，请先保存有效 API key 再刷新模型。")
        provider, key = config["provider"], config["key"]
        headers = {"x-goog-api-key": key} if provider == "gemini" else {"Authorization": "Bearer " + key}
        base = _qwen_catalog_base(config["base"]) if provider == "qwen" else config["base"] + "/models"
        deadline = time.monotonic() + min(float(config["timeoutSeconds"]), MAX_REFRESH_SECONDS)
        models, model_ids, seen_cursors = [], set(), set()
        cursor, truncated, pages, raw_count = None, False, 0, 0
        for page in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # 只拼接同一个已配置端点的查询参数，绝不访问供应商返回的 next URL。
            query = ({"page_no": page + 1, "page_size": 100} if provider == "qwen" else
                     {"pageSize": 100, **({"pageToken": cursor} if cursor else {})} if provider == "gemini"
                     else ({"after": cursor, "limit": 100} if cursor else {}))
            response = _get_json(base + ("?" + urlencode(query) if query else ""), headers, remaining)
            pages = page + 1
            source = response.get("output") if provider == "qwen" else response
            if not isinstance(source, dict):
                raise ModelCatalogError("供应商没有返回有效的模型目录。")
            entries = source.get("data" if provider == "openai" else "models")
            if not isinstance(entries, list):
                raise ModelCatalogError("供应商没有返回有效的模型目录。")
            if provider == "qwen":
                # 页码从 1 开始，结束条件来自原生 output.total；按接收条目数
                # 计数而非已过滤模型数，避免非法/重复 ID 造成多余分页或无限等待。
                total = source.get("total")
                raw_count += len(entries)
                if type(total) is not int or total < 0 or (not entries and raw_count < total):
                    raise ModelCatalogError("供应商模型目录的分页格式无效，请检查接口兼容性。")
                next_cursor = str(page + 2) if raw_count < total else None
            elif provider == "gemini":
                next_cursor = response.get("nextPageToken")
            else:
                next_cursor = (response.get("last_id") or (entries[-1].get("id") if entries and isinstance(entries[-1], dict) else None)) if response.get("has_more") is True else None
                if response.get("has_more") is True and not next_cursor:
                    raise ModelCatalogError("供应商模型目录的分页格式无效，请检查接口兼容性。")
            if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 2048
                                            or any(ord(char) < 32 for char in next_cursor)):
                raise ModelCatalogError("供应商模型目录的分页格式无效，请检查接口兼容性。")
            for index, raw in enumerate(entries):
                entry = _model_entry(raw, provider, key)
                if entry is not None and entry["id"] not in model_ids:
                    models.append(entry)
                    model_ids.add(entry["id"])
                if len(models) >= MAX_MODELS:
                    truncated = bool(index + 1 < len(entries) or next_cursor)
                    break
            if len(models) >= MAX_MODELS or not next_cursor:
                break
            if page + 1 >= MAX_PAGES or next_cursor in seen_cursors:
                truncated = True
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        message = ("已获取模型目录；目录不保证模型支持音频或特定推理参数。" if models else
                   "供应商未返回可选择的生成模型，请检查账户权限或手动填写模型 ID。")
        if truncated:
            message += " 已达到本地分页或数量限制，列表可能不完整。"
        result = {"platformId": config["id"], "models": models, "source": "api", "provider": provider,
                  "pages": pages, "truncated": truncated, "message": message}
        cached = _save_cache(config, result)
        result.update(cachePersisted=cached is not None, stale=False,
                      cachedAt=datetime.fromtimestamp(cached["savedAt"], timezone.utc).isoformat() if cached else None)
        if cached is None:
            result["message"] += " 本次目录已获取，但本地缓存未能保存；仍可使用当前列表。"
        return result
    except ModelCatalogError:
        raise
    except error.HTTPError as exc:
        messages = {401: "模型目录认证失败，请检查此平台 API key。",
                    403: "模型目录访问被拒绝，请检查账户权限。",
                    404: "此平台未提供兼容的模型列表接口，可手动填写模型 ID。",
                    429: "模型目录请求受到限流，本次未自动重试。"}
        raise ModelCatalogError(messages.get(exc.code, "读取模型目录失败，接口响应或重定向不被支持；本次未自动重试。")) from None
    except (TimeoutError, socket.timeout):
        raise ModelCatalogError("刷新模型目录超时，本次未自动重试。") from None
    except error.URLError:
        raise ModelCatalogError("无法连接模型目录，请检查网络、代理和此平台 API 地址。") from None
    except (ValueError, OSError, TypeError, KeyError, IndexError, AttributeError, RecursionError, OverflowError):
        raise ModelCatalogError("无法安全读取此平台的模型目录，请检查配置或手动填写模型 ID。") from None
