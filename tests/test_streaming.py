"""流式协议的离线回归；所有 HTTP 响应均为内存替身，不读取真实配置或调用云模型。"""

import json
import types
import unittest
from unittest.mock import Mock, patch
from urllib import error, request

from synthv_assistant import streaming


def sse(document, ending="\n\n"):
    """生成真实 UTF-8 SSE 帧，保留中文以覆盖多字节拆分。"""
    text = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
    return ("data: " + text + ending).encode("utf-8")


def openai_event(delta, finish=None, index=0):
    return {"choices": [{"index": index, "delta": delta, "finish_reason": finish}]}


def gemini_event(parts, finish=None, **extra):
    return {"candidates": [{"index": 0, "content": {"parts": parts}, "finishReason": finish, **extra}]}


class VirtualClock:
    """每次网络读取推进指定时间，离线验证长请求而无需真实等待。

    单调时钟读取本身不推进时间，避免把实现增加一次时间检查误当成超时。
    未指定的后续读取耗时为零，便于独立表达首包、后续停顿及 EOF 的时序。
    """

    def __init__(self, delays):
        self.started = self.now = 100.0
        self.delays = iter(delays)

    def __call__(self):
        return self.now

    def on_read(self):
        self.now += next(self.delays, 0)


class FakeResponse:
    """read1 按预设网络分段返回，模拟 UTF-8/CRLF 在任意字节位置断开。"""

    def __init__(self, chunks, content_type="text/event-stream; charset=utf-8", on_read=None):
        self.chunks = list(chunks)
        self.headers = {"Content-Type": content_type}
        self.closed = False
        self.on_read = on_read
        self.socket = Mock()
        self.fp = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=self.socket))

    def read1(self, size):
        if self.on_read:
            self.on_read()
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            return chunk[:size]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


class StreamingTests(unittest.TestCase):
    """断言完整聚合结果、进度字段与安全标志，避免只验证实现内部细节。"""

    def call(self, chunks, *, provider="openai", body=None, headers=None, callback=None,
             content_type="text/event-stream", on_read=None, timeout=30):
        self.response = FakeResponse(chunks, content_type, on_read)
        self.opener = Mock()
        self.opener.open.return_value = self.response
        self.progress = []
        with patch.object(streaming.request, "build_opener", return_value=self.opener) as build:
            result = streaming.send_stream_json(
                "https://mock.invalid/v1/chat/completions" if provider == "openai" else
                "https://mock.invalid/v1beta/models/fake:streamGenerateContent?alt=sse",
                body if body is not None else {"model": "fake"},
                headers if headers is not None else {"Authorization": "Bearer fake-secret-token"},
                timeout, provider, callback or self.progress.append)
            self.assertIsInstance(build.call_args.args[0], streaming._NoRedirect)
        self.opener.open.assert_called_once()
        self.assertTrue(self.response.closed)
        return result

    def test_openai_utf8_one_byte_chunks_and_nonmutating_stream_request(self):
        original = {"model": "fake", "messages": [{"role": "user", "content": "你好"}]}
        payload = sse(openai_event({"content": '{"text":"柔和'})) + sse(openai_event({"content": '一点","actions":[]}'}, "stop")) + sse("[DONE]")
        result = self.call([payload[index:index + 1] for index in range(len(payload))], body=original)
        self.assertEqual(result["choices"][0]["message"]["content"], '{"text":"柔和一点","actions":[]}')
        self.assertEqual("".join(item["textDelta"] for item in self.progress), result["choices"][0]["message"]["content"])
        self.assertNotIn("stream", original)
        sent = self.opener.open.call_args.args[0]
        self.assertTrue(json.loads(sent.data)["stream"])
        self.assertEqual(sent.get_header("Accept"), "text/event-stream")

    def test_crlf_split_multiline_data_bom_and_comments(self):
        payload = b'\xef\xbb\xbf: keepalive\r\ndata: {"choices":\r\ndata: [{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\r\n\r\n'
        result = self.call([payload[:17], payload[17:18], payload[18:]])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

    def test_reasoning_content_and_public_details_ignore_encrypted_reasoning(self):
        frames = [sse(openai_event({"reasoning_content": "先看选区。"})),
                  sse(openai_event({"reasoning": "再看参数。"})),
                  sse(openai_event({"reasoning_details": [
                      {"type": "reasoning.text", "text": "可见解释。"},
                      {"type": "reasoning.summary", "summary": "公开摘要。"},
                      {"type": "reasoning.encrypted", "data": "never-publish-hidden-data"}]})),
                  sse(openai_event({"content": "answer"}, "stop"))]
        result = self.call(frames)
        thoughts = "".join(item["reasoningDelta"] for item in self.progress)
        self.assertEqual(thoughts, "先看选区。再看参数。可见解释。公开摘要。")
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"], thoughts)
        self.assertNotIn("never-publish", json.dumps(self.progress))

    def test_only_first_openai_choice_is_aggregated(self):
        result = self.call([sse({"choices": [
            {"index": 1, "delta": {"content": "ignored"}, "finish_reason": "length"},
            {"index": 0, "delta": {"content": "first"}, "finish_reason": "stop"}]}), sse("[DONE]")])
        self.assertEqual(result["choices"][0]["message"]["content"], "first")
        self.assertNotIn("ignored", json.dumps(self.progress))

    def test_openai_tool_call_function_call_refusal_and_truncation_are_preserved(self):
        result = self.call([
            sse(openai_event({"content": "plain", "tool_calls": [{"index": 0, "function": {"name": "unsafe"}}],
                              "function_call": {"name": "legacy"}, "refusal": "refused"}, "length")),
            sse(openai_event({"tool_calls": [], "refusal": ""}, "stop"))])
        choice = result["choices"][0]
        self.assertEqual(choice["finish_reason"], "length")
        self.assertTrue(choice["message"]["tool_calls"])
        self.assertTrue(choice["message"]["function_call"])
        self.assertEqual(choice["message"]["refusal"], "refused")

    def test_gemini_concatenates_text_without_inserting_json_breaks(self):
        result = self.call([sse(gemini_event([{"thought": True, "text": "公开摘要"}])),
                           sse(gemini_event([{"text": '{"text":"柔'}])),
                           sse(gemini_event([{"text": '和","actions":[]}'}], "STOP"))], provider="gemini")
        parts = result["candidates"][0]["content"]["parts"]
        self.assertEqual(parts[0]["text"], '{"text":"柔和","actions":[]}')
        self.assertEqual(parts[1], {"thought": True, "text": "公开摘要"})
        self.assertEqual("".join(item["reasoningDelta"] for item in self.progress), "公开摘要")
        self.assertNotIn("stream", json.loads(self.opener.open.call_args.args[0].data))

    def test_openai_tool_argument_fragments_are_joined_and_never_executed(self):
        result = self.call([
            sse(openai_event({"tool_calls": [{"index": 8, "id": "fake", "function": {"name": "unsafe", "arguments": '{"x":'}}]})),
            sse(openai_event({"tool_calls": [{"index": 8, "function": {"arguments": '1}'}}]}, "tool_calls"))])
        calls = result["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["arguments"], '{"x":1}')
        self.assertEqual(self.progress, [])

    def test_duplicate_transport_fields_and_nonstandard_constants_are_rejected(self):
        for frame in [b'data: {"choices": [], "choices": []}\n\n', b'data: {"choices": [], "x": NaN}\n\n']:
            with self.subTest(frame=frame):
                with self.assertRaises(ValueError):
                    self.call([frame])

    def test_redaction_marker_does_not_exceed_thought_output_limit(self):
        with patch.object(streaming, "MAX_REASONING_CHARACTERS", 12):
            result = self.call([sse(openai_event({"reasoning": "12345678901x", "content": "answer"}, "stop"))],
                               headers={"x-goog-api-key": "xyz"})
        thoughts = result["choices"][0]["message"]["reasoning_content"]
        self.assertLessEqual(len(thoughts), 12)
        self.assertLessEqual(sum(len(item["reasoningDelta"]) for item in self.progress), 12)

    def test_gemini_preserves_tools_and_nonstop_finish_reason(self):
        result = self.call([sse(gemini_event([
            {"functionCall": {"name": "unsafe", "args": {}}, "text": "a"},
            {"executableCode": {"code": "no-execution"}}], "MAX_TOKENS")),
            sse(gemini_event([{"text": "b"}], "STOP"))], provider="gemini")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["finishReason"], "MAX_TOKENS")
        self.assertTrue(any("functionCall" in part for part in candidate["content"]["parts"]))
        self.assertTrue(any("executableCode" in part for part in candidate["content"]["parts"]))

    def test_secret_split_across_events_is_never_sent_to_progress(self):
        secret = "private-test-key-5678"
        result = self.call([sse(openai_event({"content": "hello private-test-", "reasoning_content": "private-"})),
                           sse(openai_event({"content": "key-5678 done", "reasoning_content": "test-key-5678"}, "stop"))],
                           headers={"Authorization": "Bearer " + secret})
        visible = json.dumps(self.progress, ensure_ascii=False)
        self.assertNotIn(secret, "".join(item["textDelta"] for item in self.progress))
        self.assertNotIn("private-test-", visible)
        self.assertEqual(result["choices"][0]["message"]["content"], "hello [已隐藏密钥] done")
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"], "[已隐藏密钥]")

    def test_thought_limit_does_not_discard_later_answer(self):
        result = self.call([sse(openai_event({"reasoning": "想" * 12_500})),
                           sse(openai_event({"reasoning": "更多", "content": "complete"}, "stop"))])
        self.assertEqual(len(result["choices"][0]["message"]["reasoning_content"]), 12_000)
        self.assertEqual(result["choices"][0]["message"]["content"], "complete")
        self.assertEqual(sum(len(item["reasoningDelta"]) for item in self.progress), 12_000)

    def test_wire_limit_counts_keepalive_bytes(self):
        with patch.object(streaming, "MAX_RESPONSE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "大小限制"):
                self.call([b":" + b"x" * 100 + b"\n\n"])
        self.assertTrue(self.response.closed)
        self.opener.open.assert_called_once()

    def test_first_output_deadline_does_not_restart_for_heartbeat(self):
        """心跳只能证明连接存活，迟到的有效正文不能使已超时的请求复活。"""
        clock = VirtualClock([0.6, 0.6])
        with patch.object(streaming.time, "monotonic", side_effect=clock):
            with self.assertRaisesRegex(streaming.StreamingTimeoutError, "等待首次输出超时"):
                self.call([b":ping\n\n", sse(openai_event({"content": "late"}, "stop"))],
                          timeout=1, on_read=clock.on_read)
        timeouts = [call.args[0] for call in self.response.socket.settimeout.call_args_list]
        self.assertGreater(timeouts[0], timeouts[-1])
        self.assertEqual(self.progress, [])
        self.assertTrue(self.response.closed)
        self.opener.open.assert_called_once()

    def test_continuous_body_or_reasoning_can_run_beyond_ten_minutes(self):
        """两种供应商的有效正文或可见摘要均续期，不能再被 60/180 秒总时限截断。"""
        cases = [("openai", "text", 60), ("openai", "reasoning", 180),
                 ("gemini", "text", 180), ("gemini", "reasoning", 60)]
        for provider, channel, timeout in cases:
            with self.subTest(provider=provider, channel=channel, timeout=timeout):
                clock = VirtualClock([45] * 18)
                if provider == "openai":
                    field = "content" if channel == "text" else "reasoning_content"
                    chunks = [sse(openai_event({field: "继续"})) for _ in range(16)]
                    chunks.append(sse(openai_event({"content": "complete"}, "stop")))
                else:
                    chunks = [sse(gemini_event([{"text": "继续", "thought": channel == "reasoning"}]))
                              for _ in range(16)]
                    chunks.append(sse(gemini_event([{"text": "complete"}], "STOP")))
                with patch.object(streaming.time, "monotonic", side_effect=clock):
                    result = self.call(chunks, provider=provider, timeout=timeout, on_read=clock.on_read)
                self.assertGreater(clock.now - clock.started, 600)
                expected_text = "继续" * 16 + "complete" if channel == "text" else "complete"
                expected_reasoning = "继续" * 16 if channel == "reasoning" else ""
                if provider == "openai":
                    message = result["choices"][0]["message"]
                    self.assertEqual(message["content"], expected_text)
                    self.assertEqual(message.get("reasoning_content", ""), expected_reasoning)
                else:
                    parts = result["candidates"][0]["content"]["parts"]
                    self.assertEqual(parts[0]["text"], expected_text)
                    self.assertEqual(parts[1]["text"] if len(parts) > 1 else "", expected_reasoning)
                self.assertEqual("".join(item["textDelta"] for item in self.progress), expected_text)
                self.assertEqual("".join(item["reasoningDelta"] for item in self.progress), expected_reasoning)

    def test_reasoning_beyond_display_limit_still_renews_without_exposing_secrets(self):
        """摘要达到显示上限后仍可能持续推理；续期必须早于截断与跨帧凭据脱敏。"""
        secret = "private-test-key-5678"
        for provider in ("openai", "gemini"):
            with self.subTest(provider=provider):
                clock = VirtualClock([45] * 18)
                # 每帧小于 read1 的分段大小，确保模拟的是完整、有效的模型事件。
                thoughts = ["a" * 1000] * 13 + ["private-test-", "key-5678", "仍在推理"]
                if provider == "openai":
                    chunks = [sse(openai_event({"reasoning_content": text})) for text in thoughts]
                    chunks.append(sse(openai_event({"content": "complete"}, "stop")))
                else:
                    chunks = [sse(gemini_event([{"text": text, "thought": True}])) for text in thoughts]
                    chunks.append(sse(gemini_event([{"text": "complete"}], "STOP")))
                with patch.object(streaming.time, "monotonic", side_effect=clock):
                    result = self.call(chunks, provider=provider, timeout=60, on_read=clock.on_read,
                                       headers={"Authorization": "Bearer " + secret})
                self.assertGreater(clock.now - clock.started, 600)
                visible = "".join(item["reasoningDelta"] for item in self.progress)
                self.assertEqual(visible, "a" * 12_000)
                self.assertEqual("".join(item["textDelta"] for item in self.progress), "complete")
                self.assertNotIn(secret, json.dumps(result))
                self.assertNotIn("private-test-", json.dumps(self.progress))

    def test_secret_prefix_buffering_does_not_prevent_activity_renewal(self):
        """尚未显示的密钥前缀也属于实际收到的文本，等待脱敏不能引发误超时。"""
        secret = "private-test-key-5678"
        clock = VirtualClock([0.6] * 5)
        chunks = [sse(openai_event({"reasoning": text}))
                  for text in ("private-", "test-", "key-5678")]
        chunks.append(sse(openai_event({"content": "answer"}, "stop")))
        with patch.object(streaming.time, "monotonic", side_effect=clock):
            result = self.call(chunks, timeout=1, on_read=clock.on_read,
                               headers={"Authorization": "Bearer " + secret})
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"], "[已隐藏密钥]")
        self.assertNotIn(secret, json.dumps(self.progress))
        self.assertNotIn("private-", json.dumps(self.progress))

    def test_metadata_empty_deltas_and_hidden_reasoning_do_not_renew(self):
        """非正文元数据、空片段、隐藏内容及其他候选不能冒充可见的有效输出。"""
        cases = [
            ("openai", b":ping\n\n"),
            ("openai", b"event: heartbeat\nid: 1\nretry: 1000\n\n"),
            ("openai", sse({"choices": [], "usage": {"total_tokens": 10}})),
            ("openai", sse(openai_event({"role": "assistant"}))),
            ("openai", sse(openai_event({"content": "", "reasoning_content": ""}))),
            ("openai", sse(openai_event({"reasoning_details": [{"type": "reasoning.encrypted", "data": "secret"}]}))),
            ("openai", sse(openai_event({"content": "其他候选"}, index=1))),
            ("gemini", sse({"usageMetadata": {"totalTokenCount": 10}})),
            ("gemini", sse(gemini_event([{"text": ""}, {"thought": True, "text": ""}]))),
            ("gemini", sse(gemini_event([{"thoughtSignature": "opaque-signature"}]))),
        ]
        for provider, frame in cases:
            with self.subTest(provider=provider, frame=frame[:50]):
                clock = VirtualClock([0.6, 0.6])
                with patch.object(streaming.time, "monotonic", side_effect=clock):
                    with self.assertRaisesRegex(streaming.StreamingTimeoutError, "等待首次输出超时"):
                        self.call([frame, frame], provider=provider, timeout=1, on_read=clock.on_read)
                self.assertEqual(self.progress, [])
                self.assertTrue(self.response.closed)
                self.opener.open.assert_called_once()

    def test_incomplete_sse_event_does_not_extend_wait(self):
        """即使字节持续到达，也必须等完整 SSE 事件解析出文本后才视作有效活动。"""
        frame = sse(openai_event({"content": "未完成的事件"}))
        clock = VirtualClock([0.4, 0.4, 0.4])
        with patch.object(streaming.time, "monotonic", side_effect=clock):
            with self.assertRaisesRegex(streaming.StreamingTimeoutError, "等待首次输出超时"):
                self.call([frame[:20], frame[20:-2], frame[-2:]], timeout=1, on_read=clock.on_read)
        self.assertEqual(self.progress, [])
        self.assertTrue(self.response.closed)
        self.opener.open.assert_called_once()

    def test_stall_after_output_reports_idle_timeout_and_keeps_received_summary(self):
        """已有摘要后的停顿应报告连续空闲时间，且不丢弃已交付的进度或自动重试。"""
        clock = VirtualClock([0.6, 0.6, 0.6])
        with patch.object(streaming.time, "monotonic", side_effect=clock):
            with self.assertRaisesRegex(streaming.StreamingTimeoutError, "已连续 1 秒没有新的正文或思考摘要"):
                self.call([sse(openai_event({"reasoning": "已收到的摘要"})), b":ping\n\n",
                           sse(openai_event({"content": "late"}, "stop"))], timeout=1, on_read=clock.on_read)
        self.assertEqual("".join(item["reasoningDelta"] for item in self.progress), "已收到的摘要")
        self.assertEqual("".join(item["textDelta"] for item in self.progress), "")
        self.assertTrue(self.response.closed)
        self.opener.open.assert_called_once()

    def test_thirty_minute_total_cap_is_not_extended_by_continuous_output(self):
        """有效输出可延长空闲窗口，但固定总上限仍关闭连接，不能无限续期或重试。"""
        clock = VirtualClock([100] * 18)
        frames = [sse(openai_event({"reasoning": "继续"})) for _ in range(18)]
        with patch.object(streaming.time, "monotonic", side_effect=clock):
            with self.assertRaisesRegex(streaming.StreamingTimeoutError, "30 分钟总上限"):
                self.call(frames, timeout=180, on_read=clock.on_read)
        self.assertEqual(clock.now - clock.started, 1800)
        self.assertEqual("".join(item["reasoningDelta"] for item in self.progress), "继续" * 17)
        self.assertTrue(self.response.closed)
        self.opener.open.assert_called_once()

    def test_socket_timeout_reports_first_output_or_idle_stage_without_network_details(self):
        """底层 read1 的超时也映射为固定中文，不显示 URL、凭据或异常原文。"""
        for after_output in (False, True):
            with self.subTest(after_output=after_output):
                reads = [0]

                def timeout_on_read():
                    reads[0] += 1
                    if not after_output or reads[0] > 1:
                        raise TimeoutError("https://mock.invalid/private?api_key=fake-secret-token")

                expected = "已连续" if after_output else "等待首次输出超时"
                with self.assertRaisesRegex(streaming.StreamingTimeoutError, expected) as raised:
                    self.call([sse(openai_event({"reasoning": "摘要"}))], on_read=timeout_on_read)
                self.assertNotIn("fake-secret-token", str(raised.exception))
                self.assertNotIn("mock.invalid", str(raised.exception))
                self.assertTrue(self.response.closed)
                self.opener.open.assert_called_once()

    def test_connection_timeouts_include_safe_stage_and_do_not_retry(self):
        """urllib 可直接抛出或包装 socket 超时；两种路径均说明连接阶段且只发送一次。"""
        for failure in (TimeoutError("fake-secret-token"), error.URLError(TimeoutError("fake-secret-token"))):
            with self.subTest(failure=type(failure).__name__):
                opener = Mock()
                opener.open.side_effect = failure
                with patch.object(streaming.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(streaming.StreamingTimeoutError, "建立连接或返回响应头超时") as raised:
                        streaming.send_stream_json("https://mock.invalid", {}, {}, 60, "openai", None)
                self.assertNotIn("fake-secret-token", str(raised.exception))
                self.assertIn("60 秒", str(raised.exception))
                opener.open.assert_called_once()

    def test_late_headers_close_response_and_report_connection_stage(self):
        """多阶段建连迟到但未抛 socket 超时时，仍关闭响应并说明连接阶段。"""
        clock = VirtualClock([])
        response = FakeResponse([sse(openai_event({"content": "late"}, "stop"))])
        opener = Mock()

        def late_response(*_args, **_kwargs):
            clock.now += 61
            return response

        opener.open.side_effect = late_response
        with patch.object(streaming.time, "monotonic", side_effect=clock), \
             patch.object(streaming.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(streaming.StreamingTimeoutError, "建立连接或返回响应头超时"):
                streaming.send_stream_json("https://mock.invalid", {}, {}, 60, "openai", None)
        self.assertTrue(response.closed)
        opener.open.assert_called_once()

    def test_truncated_stream_and_wrong_content_type_do_not_fallback(self):
        cases = [([sse(openai_event({"content": "partial"})), sse("[DONE]")], "text/event-stream"),
                 ([b'{"choices":[]}'], "application/json")]
        for chunks, content_type in cases:
            with self.subTest(content_type=content_type):
                with self.assertRaises(ValueError):
                    self.call(chunks, content_type=content_type)
                self.opener.open.assert_called_once()

    def test_json_response_raises_public_streaming_error_before_reading_body(self):
        """不支持 SSE 的兼容服务应明确报错；不解析正文，更不能再次发起计费请求。"""
        read = Mock()
        with self.assertRaises(streaming.StreamingError) as raised:
            self.call([b'{"message":"fake-secret-token"}'],
                      content_type="application/json; charset=utf-8", on_read=read)
        self.assertEqual(str(raised.exception), "AI 服务未返回 SSE 流式响应，本次未自动回退或重试。")
        read.assert_not_called()
        self.opener.open.assert_called_once()
        self.assertTrue(self.response.closed)
        self.assertEqual(self.progress, [])

    def test_bad_utf8_json_or_event_error_is_fixed_message(self):
        for frame in [b"data: \xff\n\n", b"data: fake-secret-token\n\n", sse({"error": {"message": "fake-secret-token"}})]:
            with self.subTest(frame=frame[:10]):
                with self.assertRaises(ValueError) as raised:
                    self.call([frame])
                self.assertNotIn("fake-secret-token", str(raised.exception))

    def test_http_error_is_preserved_for_planner_without_retry(self):
        opener = Mock()
        failure = error.HTTPError("https://mock.invalid", 429, "fake-secret-token", {}, None)
        opener.open.side_effect = failure
        with patch.object(streaming.request, "build_opener", return_value=opener):
            with self.assertRaises(error.HTTPError) as raised:
                streaming.send_stream_json("https://mock.invalid", {}, {}, 10, "openai", None)
        self.assertIs(raised.exception, failure)
        opener.open.assert_called_once()

    def test_redirect_handler_refuses_forwarding_credentials(self):
        handler = streaming._NoRedirect()
        with self.assertRaises(error.HTTPError):
            handler.redirect_request(request.Request("https://mock.invalid"), None, 302, "moved", {}, "https://other.invalid")

    def test_callback_error_is_redacted_and_not_retried(self):
        def broken(_item):
            raise RuntimeError("fake-secret-token")
        with self.assertRaisesRegex(ValueError, "无法更新") as raised:
            self.call([sse(openai_event({"content": "visible"}, "stop"))], callback=broken)
        self.assertNotIn("fake-secret-token", str(raised.exception))
        self.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
