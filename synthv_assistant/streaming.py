"""有界解析 OpenAI 兼容协议和 Gemini 的 SSE 响应，不重试或跟随重定向。

这里只传递供应商实际返回的正文和可见思考摘要，不生成、推断或补写思考。
调用者仍须对聚合后的普通响应执行原有计划校验，不能把流式片段当成可执行计划。
"""

from __future__ import annotations

import codecs
import json
import math
import time
from typing import Callable
from urllib import error, request
from urllib.parse import urlsplit

from .review import MAX_REQUEST_BYTES, _NoRedirect


MAX_RESPONSE_BYTES = 1_000_000
MAX_REASONING_CHARACTERS = 12_000
READ_SIZE = 4096
# 空闲计时可随有效输出续期；另设较长、独立的总上限，防止异常服务永久占用任务。
MAX_STREAM_SECONDS = 30 * 60
ProgressCallback = Callable[[dict[str, str]], None]
__all__ = ["StreamingError", "StreamingTimeoutError", "send_stream_json"]


class StreamingError(ValueError):
    """可安全展示的固定中文流式错误，不携带供应商响应、认证头或完整 URL。"""


class StreamingTimeoutError(TimeoutError):
    """只包含本地计时阶段与固定提示，不附带网络异常或供应商返回的原文。"""


class _StreamDeadline:
    """分别约束首次输出、后续输出空闲时间以及整次请求的最长生命周期。

    平台 timeoutSeconds 是等待有效输出的窗口，不是整个推理过程的长度。
    只有已解析、类型正确的正文/可见思考摘要续期；SSE 心跳、空事件和半截
    数据不会让服务永久占用连接。续期发生在显示截断和密钥脱敏之前，避免
    摘要达到显示上限或暂存凭据前缀时，将仍然活跃的模型误判为超时。
    """

    def __init__(self, idle_seconds: float):
        self.idle_seconds = idle_seconds
        started = time.monotonic()
        self.idle_deadline = started + idle_seconds
        self.total_deadline = started + MAX_STREAM_SECONDS
        self.received_output = False

    def timeout_error(self, *, connecting: bool = False) -> StreamingTimeoutError:
        """对底层 socket 超时也使用同一阶段判定，并优先说明已触达的总上限。"""
        if time.monotonic() >= self.total_deadline:
            message = f"AI 流式请求达到 {MAX_STREAM_SECONDS / 60:g} 分钟总上限，已停止等待。"
        elif connecting:
            message = f"等待 AI 服务建立连接或返回响应头超时（{self.idle_seconds:g} 秒）。"
        elif self.received_output:
            message = f"AI 已连续 {self.idle_seconds:g} 秒没有新的正文或思考摘要，等待超时。"
        else:
            message = f"AI 在 {self.idle_seconds:g} 秒内未返回正文或思考摘要，等待首次输出超时。"
        return StreamingTimeoutError(message + "本次未自动重试，也未修改工程。")

    def remaining(self, *, connecting: bool = False) -> float:
        """每次阻塞读取前后核对单调时钟，迟到的数据不能复活已过期请求。"""
        remaining = min(self.idle_deadline, self.total_deadline) - time.monotonic()
        if remaining <= 0:
            raise self.timeout_error(connecting=connecting)
        return remaining

    def activity(self):
        """仅由协议解析后的有效文本调用；总上限从不随内容续期。"""
        self.remaining()
        self.received_output = True
        self.idle_deadline = time.monotonic() + self.idle_seconds


def _read_chunk(response, deadline: _StreamDeadline, size: int) -> bytes:
    """优先 read1 及时返回数据，socket 等待不得超过空闲与总预算的较小值。

    urllib 的正常 HTTPS 响应在 fp.raw._sock 保存底层 socket；已读到 EOF 时
    fp 可为空。外部注入的文件型响应没有 socket 时仍在读取前后检查预算。
    """
    remaining = deadline.remaining()
    raw = getattr(getattr(response, "fp", None), "raw", None)
    connection = getattr(raw, "_sock", None)
    if connection is not None:
        connection.settimeout(remaining)
    reader = getattr(response, "read1", None) or response.read
    try:
        data = reader(size)
    except TimeoutError:
        # 不继续读取已超时的缓冲流，也不重新发送可能已计费的模型请求。
        raise deadline.timeout_error() from None
    deadline.remaining()
    if not isinstance(data, bytes):
        raise StreamingError("AI 流式响应不是有效的字节数据。")
    return data


def _sse_events(response, deadline: _StreamDeadline):
    """增量解码 UTF-8 和 SSE，支持跨网络分段的汉字、CRLF 与多行 data。

    上限统计原始字节，包含注释和心跳，避免服务持续发送无用数据绕过限制。
    单个未结束事件也受同一上限约束；EOF 仅补交已收到的数据，不补造完成标记。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    data_lines: list[str] = []
    total = 0
    first_text = True
    while True:
        chunk = _read_chunk(response, deadline, min(READ_SIZE, MAX_RESPONSE_BYTES + 1 - total))
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise StreamingError("AI 流式响应超过本地大小限制。")
        final = not chunk
        try:
            decoded = decoder.decode(chunk, final=final)
        except UnicodeDecodeError:
            raise StreamingError("AI 流式响应包含无效字符编码。") from None
        if first_text and decoded:
            decoded = decoded.removeprefix("\ufeff")
            first_text = False
        buffer += decoded
        while buffer:
            lf, cr = buffer.find("\n"), buffer.find("\r")
            positions = [position for position in (lf, cr) if position >= 0]
            if not positions:
                if not final:
                    break
                line, buffer = buffer, ""
            else:
                end = min(positions)
                # 网络分段恰好停在 CR 时等下一段，避免把 CRLF 误认成两个换行。
                if buffer[end] == "\r" and end == len(buffer) - 1 and not final:
                    break
                consumed = 2 if buffer[end:end + 2] == "\r\n" else 1
                line, buffer = buffer[:end], buffer[end + consumed:]
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines.clear()
            elif line.startswith("data:"):
                data = line[5:]
                data_lines.append(data[1:] if data.startswith(" ") else data)
            elif line == "data":
                data_lines.append("")
            # event/id/retry 和冒号注释属于传输元数据，不进入模型正文。
        if final:
            if data_lines:
                yield "\n".join(data_lines)
            return


def _secrets(headers: dict) -> tuple[str, ...]:
    """只读取请求认证头用于脱敏，不记录、不返回认证头本身。"""
    found = set()
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name.lower() == "authorization":
            found.add(value)
            if " " in value:
                found.add(value.split(" ", 1)[1])
        elif name.lower() in {"x-goog-api-key", "api-key", "x-api-key"}:
            found.add(value)
    return tuple(sorted((value for value in found if value), key=len, reverse=True))


class _TextChannel:
    """跨 SSE 事件遮盖凭据，防止密钥分成几段后被逐段回调泄漏。"""

    def __init__(self, secrets: tuple[str, ...], emit: Callable[[str], None], limit: int | None = None):
        self.secrets = secrets
        self.emit = emit
        self.limit = limit
        self.published_length = 0
        self.pending = ""
        self.parts: list[str] = []

    def _publish(self, text: str):
        if self.limit is not None:
            text = text[:max(0, self.limit - self.published_length)]
        if text:
            self.published_length += len(text)
            self.parts.append(text)
            self.emit(text)

    def append(self, text: str):
        self.pending += text
        for secret in self.secrets:
            self.pending = self.pending.replace(secret, "[已隐藏密钥]")
        keep = 0
        # 仅暂存与密钥前缀相同的末尾字符；一般文本无需等待完整答案才显示。
        for secret in self.secrets:
            for length in range(min(len(self.pending), len(secret) - 1), keep, -1):
                if self.pending.endswith(secret[:length]):
                    keep = length
                    break
        visible = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ""
        self._publish(visible)

    def finish(self):
        # 若响应以凭据前缀结束，也不把已识别的潜在密钥片段暴露给界面。
        if self.pending:
            self._publish("[已隐藏密钥片段]")
            self.pending = ""

    @property
    def text(self) -> str:
        return "".join(self.parts)


class _Progress:
    """正文与思考分通道累计；思考达到上限后继续读取正文，不猜测省略部分。"""

    def __init__(self, headers: dict, callback: ProgressCallback | None, deadline: _StreamDeadline):
        self.callback = callback
        self.deadline = deadline
        self.reasoning_length = 0
        secrets = _secrets(headers)
        self.text = _TextChannel(secrets, lambda text: self._emit("textDelta", text))
        self.reasoning = _TextChannel(secrets, lambda text: self._emit("reasoningDelta", text), MAX_REASONING_CHARACTERS)

    def _emit(self, field: str, value: str):
        if self.callback is not None:
            try:
                self.callback({"reasoningDelta": value if field == "reasoningDelta" else "",
                               "textDelta": value if field == "textDelta" else ""})
            except Exception:
                raise StreamingError("无法更新 AI 请求进度，本次未自动重试。") from None

    def append(self, text: object = None, reasoning: object = None):
        if text is not None:
            if not isinstance(text, str):
                raise StreamingError("AI 流式正文格式无效。")
            if text:
                self.deadline.activity()
            self.text.append(text)
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise StreamingError("AI 思考摘要格式无效。")
            if reasoning:
                self.deadline.activity()
            accepted = reasoning[:max(0, MAX_REASONING_CHARACTERS - self.reasoning_length)]
            self.reasoning_length += len(accepted)
            self.reasoning.append(accepted)

    def finish(self):
        self.text.finish()
        self.reasoning.finish()


def _unique_object(pairs: list[tuple]) -> dict:
    """协议层也拒绝重复键，避免工具调用或结束标记被同名后续字段覆盖。"""
    result = {}
    for name, value in pairs:
        if name in result:
            raise StreamingError("AI 流式事件含有重复字段。")
        result[name] = value
    return result


def _invalid_constant(_value: str):
    raise StreamingError("AI 流式事件包含非法数值。")


def _document(data: str) -> dict:
    try:
        parsed = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise StreamingError("AI 流式响应不是完整的 JSON 事件。") from None
    if not isinstance(parsed, dict) or parsed.get("error") is not None:
        raise StreamingError("AI 服务返回了无效或失败的流式事件。")
    return parsed


def _first_candidate(document: dict, name: str) -> dict | None:
    """只汇总索引 0 的候选，其他候选不能污染本次计划或进度文本。"""
    items = document.get(name, [])
    if not isinstance(items, list):
        raise StreamingError("AI 流式候选格式无效。")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise StreamingError("AI 流式候选格式无效。")
        if item.get("index", index) == 0:
            return item
    return None


def _finish_reason(previous: object, incoming: object, normal: str):
    """不允许后续正常标记覆盖已经收到的截断、拒绝或其他异常结束原因。"""
    if incoming is None:
        return previous
    if not isinstance(incoming, str) or not incoming:
        raise StreamingError("AI 流式结束标记无效。")
    return previous if previous not in (None, normal) else incoming


def _openai(events, progress: _Progress) -> dict:
    message: dict = {"role": "assistant"}
    calls_by_index: dict[int, dict] = {}
    result: dict = {"choices": []}
    finish = None
    seen = False
    for event in events:
        if event.strip() == "[DONE]":
            break
        document = _document(event)
        for name in ("id", "model", "created", "usage", "system_fingerprint"):
            if name in document:
                result[name] = document[name]
        choice = _first_candidate(document, "choices")
        if choice is None:
            continue
        seen = True
        finish = _finish_reason(finish, choice.get("finish_reason"), "stop")
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise StreamingError("AI 流式消息格式无效。")
        progress.append(text=delta.get("content"))
        # 只读取供应商明确返回的可见字段，不解密 encrypted reasoning 或其他隐藏字段。
        reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("reasoning")
        if isinstance(reasoning, str):
            progress.append(reasoning=reasoning)
        details = delta.get("reasoning_details", [])
        if details is not None:
            if not isinstance(details, list):
                raise StreamingError("AI 思考摘要格式无效。")
            for detail in details:
                kind = detail.get("type") if isinstance(detail, dict) else None
                if isinstance(kind, str) and kind in {"reasoning.text", "reasoning.summary", "summary"}:
                    value = detail.get("text", detail.get("summary")) if kind == "reasoning.text" else detail.get("summary", detail.get("text"))
                    if value is not None:
                        progress.append(reasoning=value)
        # 保留工具、函数调用和拒绝字段，使既有计划校验能够继续拒绝这些响应。
        if delta.get("tool_calls") is not None:
            calls = delta["tool_calls"]
            if not isinstance(calls, list):
                raise StreamingError("AI 工具调用字段格式无效。")
            for position, call in enumerate(calls):
                if not isinstance(call, dict):
                    raise StreamingError("AI 工具调用字段格式无效。")
                index = call.get("index", position)
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise StreamingError("AI 工具调用索引无效。")
                # 使用字典索引聚合，不按供应商给出的索引扩容列表，避免稀疏大索引分配。
                saved = calls_by_index.setdefault(index, {"index": index})
                for name, value in call.items():
                    if name == "function":
                        _merge_function(saved.setdefault("function", {}), value)
                    elif name != "index":
                        saved[name] = value
        if delta.get("function_call") is not None:
            call = delta["function_call"]
            if not isinstance(call, dict):
                raise StreamingError("AI 函数调用字段格式无效。")
            _merge_function(message.setdefault("function_call", {}), call)
        if delta.get("refusal") is not None:
            refusal = delta["refusal"]
            if not isinstance(refusal, str):
                raise StreamingError("AI 拒绝消息格式无效。")
            message["refusal"] = message.get("refusal", "") + refusal
    if not seen or finish is None:
        raise StreamingError("AI 流式响应未完整结束，未生成可应用计划。")
    progress.finish()
    message["content"] = progress.text.text
    if calls_by_index:
        message["tool_calls"] = [calls_by_index[index] for index in sorted(calls_by_index)]
    if progress.reasoning.text:
        message["reasoning_content"] = progress.reasoning.text
    result["choices"] = [{"index": 0, "finish_reason": finish, "message": message}]
    return result


def _merge_function(target: dict, incoming: object):
    """保留并拼接函数名/参数片段；仅供拒绝校验查看，绝不执行聚合后的调用。"""
    if not isinstance(incoming, dict):
        raise StreamingError("AI 函数调用字段格式无效。")
    for name, value in incoming.items():
        if name in {"name", "arguments"}:
            if not isinstance(value, str):
                raise StreamingError("AI 函数调用字段格式无效。")
            target[name] = target.get(name, "") + value
        else:
            target[name] = value


def _gemini(events, progress: _Progress) -> dict:
    result: dict = {"candidates": []}
    candidate: dict = {"index": 0}
    extras: list[dict] = []
    finish = None
    seen = False
    for event in events:
        if event.strip() == "[DONE]":
            break
        document = _document(event)
        for name in ("usageMetadata", "modelVersion", "promptFeedback", "responseId"):
            if name in document:
                result[name] = document[name]
        incoming = _first_candidate(document, "candidates")
        if incoming is None:
            continue
        seen = True
        finish = _finish_reason(finish, incoming.get("finishReason"), "STOP")
        for name, value in incoming.items():
            if name not in {"content", "finishReason", "index"}:
                candidate[name] = value
        content = incoming.get("content", {})
        if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
            raise StreamingError("AI 流式消息格式无效。")
        for part in content.get("parts", []):
            if not isinstance(part, dict):
                raise StreamingError("AI 流式内容格式无效。")
            if "text" in part:
                if part.get("thought") is True:
                    progress.append(reasoning=part["text"])
                else:
                    progress.append(text=part["text"])
            # 保留 functionCall/executableCode 等字段，不能因聚合文本而抹掉违规工具内容。
            extra = {name: value for name, value in part.items() if name not in {"text", "thought"}}
            if extra:
                extras.append(extra)
    if not seen or finish is None:
        raise StreamingError("AI 流式响应未完整结束，未生成可应用计划。")
    progress.finish()
    parts = [{"text": progress.text.text}]
    if progress.reasoning.text:
        parts.append({"thought": True, "text": progress.reasoning.text})
    parts.extend(extras)
    candidate.update(finishReason=finish, content={"role": "model", "parts": parts})
    result["candidates"] = [candidate]
    return result


def send_stream_json(url: str, body: dict, headers: dict, timeout: float, provider: str,
                     on_progress: ProgressCallback | None) -> dict:
    """单次发送并聚合 SSE；HTTPError 留给规划层映射为认证、限流等安全提示。

    Gemini 的 streamGenerateContent URL 和 includeThoughts 由调用层按配置构造。
    本模块为 OpenAI / Qwen 兼容请求增加 stream:true，不改变调用者持有的请求对象。
    timeout 约束首次有效输出及相邻有效输出间隔；持续输出可跨越该时间窗口。
    """
    if provider not in {"openai", "gemini", "qwen"} or not isinstance(body, dict) or not isinstance(headers, dict):
        raise StreamingError("AI 流式请求配置无效。")
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout <= 180 or not math.isfinite(timeout):
        raise StreamingError("AI 流式请求超时配置无效。")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise StreamingError("AI 流式接口必须使用有效的 HTTPS 地址。")
    payload = {**body, "stream": True} if provider in {"openai", "qwen"} else body
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise StreamingError("完整模型请求超过本地大小限制。")
    outbound = request.Request(url, data=encoded, headers={"Content-Type": "application/json", "Accept": "text/event-stream", **headers}, method="POST")
    opener = request.build_opener(_NoRedirect())
    deadline = _StreamDeadline(timeout)
    progress = _Progress(headers, on_progress, deadline)
    try:
        response = opener.open(outbound, timeout=deadline.remaining())
    except TimeoutError:
        raise deadline.timeout_error(connecting=True) from None
    except error.URLError as exc:
        # urllib 会将建立连接阶段的 socket 超时包装在 URLError.reason 中。
        if isinstance(exc.reason, TimeoutError):
            raise deadline.timeout_error(connecting=True) from None
        raise
    with response:
        # DNS、代理或多阶段连接可能令 open 总耗时超过单次 socket 等待。
        # 在响应上下文内补查，既报告正确阶段，也保证迟到的响应被关闭。
        deadline.remaining(connecting=True)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "text/event-stream":
            raise StreamingError("AI 服务未返回 SSE 流式响应，本次未自动回退或重试。")
        events = _sse_events(response, deadline)
        # Qwen 的正文与公开思考输出分别使用 delta.content / reasoning_content，
        # 可共用现有聚合器、有效输出空闲计时和跨事件凭据脱敏，不另建超时规则。
        result = _openai(events, progress) if provider in {"openai", "qwen"} else _gemini(events, progress)
        deadline.remaining()
        return result
