"""按用户主动刷新请求读取供应商模型目录，不推断目录未提供的模型能力。

接口只访问所选平台的 HTTPS 根地址，不跟随重定向或响应里的任意下一页 URL。
每次刷新使用一个配置快照，限制页数、模型数、响应大小和请求超时，不自动重试。
"""

from __future__ import annotations

import json
import re
import socket
import time
from urllib import error, request
from urllib.parse import urlencode

from .platforms import get_model_platform_snapshot
from .review import _NoRedirect


MAX_PAGES = 5
MAX_MODELS = 500
MAX_RESPONSE_BYTES = 2_000_000
MAX_REFRESH_SECONDS = 30.0


class ModelCatalogError(ValueError):
    """固定中文目录错误；不返回供应商响应体、认证头、密钥或完整请求地址。"""


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
        identifier = raw.get("id")
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


def list_platform_models(identifier: str) -> dict:
    """返回公开模型列表；只有显式调用此函数才产生外网 GET 请求。

    Gemini 只展示支持 generateContent 的条目。目录通常不含完整推理、音频或
    参数能力，因此返回结果不承诺任何输入能力，也不自动修改平台选中的模型。
    """
    try:
        config = get_model_platform_snapshot(identifier)
        if (not config.get("configured") or config.get("invalid") or not config.get("key")
                or config.get("provider") not in {"openai", "gemini"}):
            raise ModelCatalogError("此平台尚未配置或已停用，请先保存有效 API key 再刷新模型。")
        provider, key = config["provider"], config["key"]
        headers = {"Authorization": "Bearer " + key} if provider == "openai" else {"x-goog-api-key": key}
        base = config["base"] + "/models"
        deadline = time.monotonic() + min(float(config["timeoutSeconds"]), MAX_REFRESH_SECONDS)
        models, model_ids, seen_cursors = [], set(), set()
        cursor, truncated, pages = None, False, 0
        for page in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            # 只拼接同一个已配置端点的查询参数，绝不访问供应商返回的 next URL。
            query = ({"pageSize": 100, **({"pageToken": cursor} if cursor else {})} if provider == "gemini"
                     else ({"after": cursor, "limit": 100} if cursor else {}))
            response = _get_json(base + ("?" + urlencode(query) if query else ""), headers, remaining)
            pages = page + 1
            entries = response.get("data" if provider == "openai" else "models")
            if not isinstance(entries, list):
                raise ModelCatalogError("供应商没有返回有效的模型目录。")
            if provider == "gemini":
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
        return {"platformId": config["id"], "models": models, "source": "api", "provider": provider,
                "pages": pages, "truncated": truncated, "message": message}
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
