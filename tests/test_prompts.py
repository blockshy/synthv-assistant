"""提示词分支与请求组装回归；所有模型响应均在本机模拟，不评价模型音质。"""

import copy
import json
import unittest
from unittest.mock import patch

from synthv_assistant import planner, prompts, streaming
from tests.test_tuning_quality import phrase


class PromptTests(unittest.TestCase):
    """验证系统规则随实际能力变化，且用户资料始终留在低优先级上下文。"""

    def build(self, selection=None, *, mode="smooth", count=0):
        return prompts.build_planner_instruction(selection, has_selection=planner._has_selection(selection),
                                                 render_mode=mode, audio_count=count)

    def test_consultation_omits_irrelevant_execution_contracts(self):
        instruction = self.build()
        complete = self.build(phrase())
        self.assertIn("actions必须为空", instruction)
        self.assertIn("本次没有音频", instruction)
        self.assertNotIn("transitionMs", instruction)
        self.assertNotIn("noteIndex是", instruction)
        self.assertLess(len(instruction), len(complete) / 2)

    def test_legacy_and_current_capabilities_select_allowed_contracts(self):
        legacy = {"noteCount": 1, "notes": [{"pitch": 60}]}
        self.assertIn("只可给可用参数delta", self.build(legacy))
        current = phrase()
        self.assertIn("noteIndex是", self.build(current))
        self.assertIn("transitionMs", self.build(current))
        current["parameters"]["pitchCurve"]["available"] = False
        self.assertNotIn("transitionMs", self.build(current))
        self.assertIn("原生音高不可用", self.build(current))
        current["parameters"]["pitchCurve"]["available"] = True
        self.assertNotIn("transitionMs", self.build(current, mode="points"))
        current["notes"][0].pop("durationSeconds")
        self.assertNotIn("noteIndex是", self.build(current))
        self.assertIn("没有可靠的逐音符时间布局", self.build(current))

    def test_audio_count_changes_evidence_without_assuming_a_winner(self):
        single = self.build(count=1)
        pair = self.build(count=2)
        self.assertIn("本次仅有音频A", single)
        self.assertNotIn("本次有音频A、B", single)
        self.assertIn("标签只表示附件顺序", pair)
        self.assertIn("无法对应就分别评价", pair)
        self.assertIn("单段听评", prompts.build_review_instruction(1))
        self.assertIn("双段听评", prompts.build_review_instruction(2))
        self.assertNotIn("严格 JSON", prompts.build_review_instruction(2))

    def test_untrusted_selection_text_is_not_promoted_to_system_role(self):
        selection = phrase()
        original = copy.deepcopy(selection)
        marker = "untrusted-selector-override"
        selection["notes"][0]["lyrics"] = marker
        selection["parameters"]["tension"]["label"] = marker
        selection["capabilityWarnings"] = [marker]
        self.assertEqual(self.build(selection), self.build(original))
        self.assertNotIn(marker, self.build(selection))

    def test_complete_json_examples_satisfy_actual_plan_parser(self):
        """说明中的完整 JSON 例子必须能穿过生产解析器，防止示例与代码契约漂移。"""
        examples = [json.loads(line) for line in self.build(phrase()).splitlines()
                    if line.startswith('{"')]
        self.assertGreaterEqual(len(examples), 2)
        for example in examples:
            plan = example if "actions" in example else {"text": "局部修饰建议。", "actions": [example]}
            parsed = planner._parse_plan(json.dumps(plan), has_selection=True, has_audio=False,
                                          key="", selection=phrase())
            self.assertIsInstance(parsed["actions"], list)

    def test_all_providers_send_same_applicable_system_contract(self):
        """检查实际供应商请求，而不是只断言常量文字；外部网络全部被截获。"""
        config = {"provider": "openai", "model": "mock-text", "base": "https://mock.example/v1",
                  "key": "fake-system-key", "timeoutSeconds": 30, "configured": True, "invalid": False}
        reply = json.dumps({"text": "可以先讨论调教目标。", "actions": []})
        expected = self.build()
        for provider in ("openai", "qwen", "gemini"):
            config["provider"] = provider
            response = ({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": reply}]}}]}
                        if provider == "gemini" else
                        {"choices": [{"finish_reason": "stop", "message": {"content": reply}}]})
            with self.subTest(provider=provider), \
                    patch.object(planner, "get_audio_configuration_snapshot", return_value=config), \
                    patch.object(planner, "_send_json", return_value=response) as send:
                result = planner.plan_tuning("咨询调教方向", None, [], [], [])
            body = send.call_args.args[1]
            actual = (body["systemInstruction"]["parts"][0]["text"] if provider == "gemini" else
                      body["messages"][0]["content"])
            self.assertEqual(actual, expected)
            self.assertEqual(result["actions"], [])

    def test_long_summary_callback_does_not_discard_a_complete_answer(self):
        """传输层之外的回调也只截取摘要；保留正式正文并遮盖摘要中出现的凭据。"""
        config = {"provider": "openai", "model": "mock-text", "base": "https://mock.example/v1",
                  "key": "fake-summary-secret", "timeoutSeconds": 30, "configured": True, "invalid": False}
        reply = json.dumps({"text": "先保留现有音高。", "actions": []})
        updates = []

        def simulated_stream(_url, _body, _headers, _timeout, _provider, callback, **limits):
            self.assertEqual(limits["max_text_characters"], planner.MAX_RESPONSE_CHARACTERS)
            callback({"reasoningDelta": "A" * 11990 + config["key"] + "B" * 80000})
            callback({"reasoningDelta": "后续摘要" * 10000})
            callback({"textDelta": reply})
            return {"choices": [{"finish_reason": "stop", "message": {"content": reply}}]}

        with patch.object(planner, "get_audio_configuration_snapshot", return_value=config), \
                patch.object(streaming, "send_stream_json", side_effect=simulated_stream) as send:
            result = planner.plan_tuning("先咨询", None, [], [], [], on_progress=updates.append)
        send.assert_called_once()
        self.assertEqual(result["actions"], [])
        self.assertIn("先保留现有音高", result["text"])
        self.assertLessEqual(len(result["reasoningSummary"]), 12000)
        self.assertNotIn(config["key"], json.dumps(updates + [result]))


if __name__ == "__main__":
    unittest.main()
