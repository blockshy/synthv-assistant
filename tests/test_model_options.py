"""会话级平台/模型/推理参数与摘要回调的离线测试，绝不读取真实凭据。"""

import json
import unittest
from unittest.mock import patch

from synthv_assistant import planner
from synthv_assistant.model_options import normalize_model_options, capabilities, apply_reasoning


class ModelOptionsTests(unittest.TestCase):
    def setUp(self):
        self.config = {"id": "default", "provider": "openai", "model": "gpt-6-astra",
                       "base": "https://mock.invalid/v1", "key": "test-secret-never-real",
                       "timeoutSeconds": 30, "configured": True, "invalid": False}
        self.plan = {"text": "可以先讨论整体表达，再确定选区。", "actions": []}
        self.response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.plan)}}]}

    def test_only_public_options_can_be_stored(self):
        self.assertEqual(normalize_model_options()["reasoningEffort"], "default")
        for value in ({"apiKey": "invalid"}, {"model": "https://wrong"}, {"platformId": "../path"},
                      {"reasoningEffort": "invented"}, {"model": False}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_model_options(value)

    def test_known_models_and_unknown_platform_do_not_claim_same_capabilities(self):
        choices = lambda config: [x["value"] for x in capabilities(config)["reasoning"]["options"]]
        self.assertNotIn("none", choices(self.config))
        self.assertIn("max", choices(self.config))
        self.assertEqual(choices({"provider": "openai", "model": "gpt-audio-1.5"}), ["default"])
        self.assertIsNone(capabilities({"provider": "openai", "model": "custom/test"})["reasoning"]["supported"])

    def test_default_does_not_force_zero_or_none(self):
        body = {}
        apply_reasoning(body, self.config, "default")
        self.assertEqual(body, {})
        with self.assertRaises(ValueError):
            apply_reasoning(body, self.config, "none")

    def test_gemini_level_and_budget_are_distinct(self):
        body = {"generationConfig": {"maxOutputTokens": 4096}}
        apply_reasoning(body, {"provider": "gemini", "model": "gemini-3.8-flash"}, "high", include_summary=True)
        self.assertEqual(body["generationConfig"], {"thinkingConfig": {"thinkingLevel": "high", "includeThoughts": True}})
        body = {}
        apply_reasoning(body, {"provider": "gemini", "model": "gemini-2.5-pro"}, "high")
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingBudget": 16384})
        with self.assertRaises(ValueError):
            apply_reasoning({}, {"provider": "gemini", "model": "gemini-2.5-pro"}, "none")

    def test_selected_platform_and_model_use_one_snapshot(self):
        selected = {"platformId": "a" * 32, "model": "gpt-5.6-sol", "reasoningEffort": "high"}
        with patch("synthv_assistant.platforms.get_model_platform_snapshot", return_value=self.config) as snapshot, \
             patch("synthv_assistant.planner.get_audio_configuration_snapshot") as legacy, \
             patch("synthv_assistant.planner._send_json", return_value=self.response) as outbound:
            result = planner.plan_tuning("讨论调教", None, [], [], [], model_options=selected)
        snapshot.assert_called_once_with("a" * 32)
        legacy.assert_not_called()
        body = outbound.call_args.args[1]
        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(self.config["model"], "gpt-6-astra")
        self.assertEqual(result["platformId"], selected["platformId"])

    def test_unsupported_audio_is_rejected_before_reading_or_uploading(self):
        # 后端必须独立于前端校验，确保不支持的模型不会收到用户音频。
        with patch("synthv_assistant.planner.get_audio_configuration_snapshot", return_value=self.config), \
             patch("synthv_assistant.planner._load_audio") as read, \
             patch("synthv_assistant.planner._send_json") as outbound:
            with self.assertRaisesRegex(planner.PlannerError, "不支持音频"):
                planner.plan_tuning("听这段", None, [], ["unused.wav"], [])
        read.assert_not_called()
        outbound.assert_not_called()

    def test_abnormal_finish_never_creates_a_plan_even_with_valid_json(self):
        # 有完整 JSON 也不代表模型正常结束；过滤或工具分支不能成为可应用提案。
        for reason in ("tool_calls", "function_call", "content_filter", "unexpected"):
            with self.subTest(reason=reason), self.assertRaises(planner.PlannerError):
                planner._openai_text({"choices": [{"finish_reason": reason, "message": self.response["choices"][0]["message"]}]})

    def test_streaming_summaries_are_real_redacted_and_never_raw_plan(self):
        updates = []

        def stream(_url, _body, _headers, _timeout, _provider, callback):
            # 模拟密钥被拆到两个网络事件；首段不能泄露尚未凑齐的凭据前缀。
            callback({"reasoningDelta": "先检查上下文。test-secret-", "textDelta": ""})
            callback({"reasoningDelta": "never-real\n再比较目标。", "textDelta": '{"text":'})
            return self.response

        with patch("synthv_assistant.planner.get_audio_configuration_snapshot", return_value=self.config), \
             patch("synthv_assistant.streaming.send_stream_json", side_effect=stream) as outbound:
            result = planner.plan_tuning("讨论调教", None, [], [], [], on_progress=updates.append)
        outbound.assert_called_once()
        self.assertTrue(any(item["reasoningAvailable"] for item in updates))
        for item in updates:
            self.assertEqual(item["text"], "")
            self.assertNotIn("test-secret-", item["reasoning"])
        self.assertIn("[已隐藏密钥]", result["reasoningSummary"])
        self.assertIn("再比较目标", result["reasoningSummary"])

    def test_gemini_stream_url_and_summary_request(self):
        config = {**self.config, "provider": "gemini", "model": "gemini-3.8-flash"}
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(self.plan)}]}}]}
        with patch("synthv_assistant.planner.get_audio_configuration_snapshot", return_value=config), \
             patch("synthv_assistant.streaming.send_stream_json", return_value=response) as outbound:
            planner.plan_tuning("讨论调教", None, [], [], [], on_progress=lambda _value: None)
        url, body = outbound.call_args.args[:2]
        self.assertTrue(url.endswith(":streamGenerateContent?alt=sse"))
        self.assertEqual(body["generationConfig"], {"thinkingConfig": {"includeThoughts": True}})

    def test_safe_stream_failure_explains_unsupported_protocol_without_retry(self):
        from synthv_assistant.streaming import StreamingError
        # 用户应看到明确的协议错误；只保留本地固定文案，不附带服务器错误正文。
        with patch("synthv_assistant.planner.get_audio_configuration_snapshot", return_value=self.config), \
             patch("synthv_assistant.streaming.send_stream_json", side_effect=StreamingError("AI 服务未返回 SSE 流式响应，本次未自动回退或重试。")) as outbound, \
             patch("synthv_assistant.planner._send_json") as fallback:
            with self.assertRaisesRegex(planner.PlannerError, "未返回 SSE"):
                planner.plan_tuning("讨论调教", None, [], [], [], on_progress=lambda _value: None)
        outbound.assert_called_once()
        fallback.assert_not_called()
