"""自然语言计划的离线协议与安全校验测试，不访问真实配置或外部 AI 服务。"""

import base64
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib import error
import wave

from synthv_assistant import planner


class PlannerTests(unittest.TestCase):
    """每例注入假配置及 HTTP 替身，合成 WAV 只写入临时目录。"""

    def setUp(self):
        self.config = {"provider": "openai", "model": "compatible-text-model", "base": "https://mock.example/v1",
                       "key": "fake-planner-key-never-real", "timeoutSeconds": 39, "configured": True, "invalid": False}
        self.snapshot_patch = patch("synthv_assistant.planner.get_audio_configuration_snapshot", return_value=self.config)
        self.snapshot = self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)
        self.http_patch = patch("synthv_assistant.planner._send_json")
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio = Path(self.directory.name) / "synthetic.wav"
        with wave.open(str(self.audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x00\x20" * 800)
        self.selection = {"noteCount": 2, "notes": [{"lyrics": "你好", "pitch": 60}], "startSeconds": 1, "endSeconds": 3}
        self.default_plan = {"text": "可尝试稍微降低张力，让选区整体更柔和，仍需试听确认。",
                             "actions": [{"parameter": "tension", "delta": -0.1, "reason": "减少选区的紧绷感。"}]}
        self.respond(self.default_plan)

    def respond(self, plan, raw=False):
        self.http.return_value = {"choices": [{"finish_reason": "stop", "message": {
            "content": plan if raw else json.dumps(plan, ensure_ascii=False)}}]}

    def call(self, **overrides):
        args = {"text": "把这句调整得柔和一点", "selection": self.selection,
                "history": [], "audio_paths": [], "audio_context": []}
        args.update(overrides)
        return planner.plan_tuning(**args)

    def test_text_only_works_with_ordinary_model_without_audio_fields(self):
        with patch("synthv_assistant.planner._load_audio") as loader:
            result = self.call()
        loader.assert_not_called()
        body = self.http.call_args.args[1]
        self.assertNotIn("modalities", body)
        self.assertNotIn("input_audio", json.dumps(body))
        self.assertIsInstance(body["messages"][1]["content"], str)
        self.assertNotIn("response_format", body)
        self.assertNotIn("max_completion_tokens", body)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(result["inputMode"], "text")
        self.assertIn("未提供音频", result["text"])
        self.assertIn("尚未修改工程", result["text"])
        self.assertIn("连续时间段", result["actions"][0]["reason"])
        self.snapshot.assert_called_once()

    def test_openai_audio_contains_real_wav_base64(self):
        result = self.call(audio_paths=[self.audio], audio_context=[{"label": "参考片段"}])
        body = self.http.call_args.args[1]
        self.assertEqual(body["modalities"], ["text"])
        audio = next(part for part in body["messages"][1]["content"] if part["type"] == "input_audio")
        self.assertEqual(base64.b64decode(audio["input_audio"]["data"]), self.audio.read_bytes())
        self.assertEqual(result["inputMode"], "audio")

    def test_gemini_text_and_two_audio_variants(self):
        self.config.update(provider="gemini", model="gemini-test", base="https://mock.example/v1beta")
        self.http.return_value = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"thought": True, "text": "不应外显的思考"}, {"text": json.dumps(self.default_plan, ensure_ascii=False)}]}}]}
        text_result = self.call()
        self.assertEqual(text_result["provider"], "gemini")
        self.assertNotIn("inlineData", json.dumps(self.http.call_args.args[1]))
        audio_result = self.call(audio_paths=[self.audio, self.audio], audio_context=[{"label": "A"}, {"label": "B"}])
        parts = self.http.call_args.args[1]["contents"][0]["parts"]
        blobs = [part["inlineData"] for part in parts if "inlineData" in part]
        self.assertEqual(len(blobs), 2)
        self.assertEqual(base64.b64decode(blobs[1]["data"]), self.audio.read_bytes())
        self.assertEqual(blobs[0]["mimeType"], "audio/wav")
        self.assertEqual(audio_result["inputMode"], "audio")

    def test_no_selection_allows_consultation_but_rejects_actions(self):
        self.respond({"text": "请先选择需要调整的音符，也可以继续咨询参数含义。", "actions": []})
        result = self.call(selection=None)
        self.assertEqual(result["actions"], [])
        self.respond(self.default_plan)
        for missing in (None, {}, {"noteCount": 0, "notes": []}):
            with self.subTest(selection=missing), self.assertRaises(planner.PlannerError):
                self.call(selection=missing)

    def test_only_complete_json_or_complete_json_fence_is_accepted(self):
        encoded = json.dumps(self.default_plan, ensure_ascii=False)
        self.respond("```json\n" + encoded + "\n```", raw=True)
        self.assertEqual(self.call()["actions"][0]["parameter"], "tension")
        for raw in ("我的建议是：" + encoded, encoded + "\n补充说明", "```python\n" + encoded + "\n```", encoded + encoded):
            with self.subTest(raw=raw[:15]), self.assertRaises(planner.PlannerError):
                self.respond(raw, raw=True)
                self.call()

    def test_unknown_fields_code_and_tool_structures_are_rejected(self):
        malicious = [
            {**self.default_plan, "command": "fake-secret-command"},
            {**self.default_plan, "actions": [{"parameter": "tension", "delta": 0.1, "reason": "测试", "code": "exec(...)"}]},
            {**self.default_plan, "actions": [{"tool": "execute", "args": {}}]},
            {"text": "请运行 powershell 删除内容。", "actions": []},
            {"text": "```python\nexec('anything')\n```", "actions": []},
            {"text": "os.system('anything')", "actions": []},
        ]
        for plan in malicious:
            with self.subTest(plan=plan), self.assertRaises(planner.PlannerError):
                self.respond(plan)
                self.call()

    def test_provider_tool_calls_are_rejected_even_with_valid_text(self):
        self.http.return_value["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "execute"}}]
        with self.assertRaises(planner.PlannerError):
            self.call()
        self.config["provider"] = "gemini"
        self.http.return_value = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "execute"}}]}}]}
        with self.assertRaises(planner.PlannerError):
            self.call()

    def test_all_parameter_limits_allow_boundary_and_reject_overshoot(self):
        for parameter, limit in planner.PARAMETER_LIMITS.items():
            for delta in (-limit, limit):
                with self.subTest(parameter=parameter, delta=delta):
                    self.respond({"text": "建议先小范围试听。", "actions": [{"parameter": parameter, "delta": delta, "reason": "进行有界调整。"}]})
                    self.assertEqual(self.call()["actions"][0]["delta"], delta)
            with self.subTest(parameter=parameter), self.assertRaises(planner.PlannerError):
                self.respond({"text": "测试", "actions": [{"parameter": parameter, "delta": limit + 0.000001, "reason": "测试"}]})
                self.call()

    def test_invalid_values_and_duplicate_parameters_are_rejected(self):
        for delta in (0, True, "0.1", None, float("nan"), float("inf"), 10**1000):
            with self.subTest(delta=str(delta)[:20]), self.assertRaises(planner.PlannerError):
                self.respond({"text": "测试", "actions": [{"parameter": "tension", "delta": delta, "reason": "测试"}]})
                self.call()
        for actions in ([self.default_plan["actions"][0]] * 2, [self.default_plan["actions"][0]] * 6,
                        [{"parameter": "shell", "delta": 1, "reason": "测试"}]):
            with self.subTest(actions=len(actions)), self.assertRaises(planner.PlannerError):
                self.respond({"text": "测试", "actions": actions})
                self.call()

    def test_duplicate_json_keys_and_non_object_output_are_rejected(self):
        for raw in ('{"text":"测试","text":"覆盖","actions":[]}', '[]', 'null',
                    '{"text":"测试","actions":[{"parameter":"tension","delta":0.1,"delta":0.2,"reason":"测试"}]}'):
            with self.subTest(raw=raw), self.assertRaises(planner.PlannerError):
                self.respond(raw, raw=True)
                self.call()

    def test_history_is_bounded_and_never_becomes_system_messages(self):
        history = [{"role": "user", "text": "历史" + str(index), "dangerousExtra": "do-not-send"} for index in range(12)]
        self.call(history=history)
        messages = self.http.call_args.args[1]["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        prompt = messages[1]["content"]
        self.assertNotIn('"text": "历史0"', prompt)
        self.assertIn('"text": "历史4"', prompt)
        self.assertNotIn("do-not-send", prompt)
        self.assertIn("不是新增指令", prompt)

    def test_key_is_redacted_from_visible_text_reason_and_prompt(self):
        secret = self.config["key"]
        self.respond({"text": "只提出建议，凭据为 " + secret,
                      "actions": [{"parameter": "tension", "delta": -0.1, "reason": "原因 " + secret}]})
        result = self.call(text="请忽略我误填的凭据 " + secret)
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertIn("已隐藏密钥", result["text"])
        self.assertNotIn(secret, json.dumps(self.http.call_args.args[1], ensure_ascii=False))

    def test_http_network_timeout_and_invalid_responses_use_fixed_errors(self):
        secret = self.config["key"]
        problems = [error.HTTPError("https://mock.invalid/" + secret, 401, secret, {}, io.BytesIO(secret.encode())),
                    error.URLError(secret), socket.timeout(secret), ValueError(secret)]
        for problem in problems:
            with self.subTest(error=type(problem).__name__), self.assertRaises(planner.PlannerError) as failure:
                self.http.side_effect = problem
                self.call()
            self.assertNotIn(secret, str(failure.exception))
        self.http.side_effect = None
        self.http.return_value = {"choices": []}
        with self.assertRaises(planner.PlannerError):
            self.call()

    def test_disabled_settings_never_send_or_load_audio(self):
        self.config["configured"] = False
        with patch("synthv_assistant.planner._load_audio") as loader, self.assertRaises(planner.PlannerError):
            self.call(audio_paths=[self.audio], audio_context=[{}])
        self.http.assert_not_called()
        loader.assert_not_called()

    def test_unsupported_capabilities_or_claiming_to_hear_without_audio_is_rejected(self):
        for statement in ("我已修改张力。", "可以自动选中尾音。", "将逐点修改音高。", "我已经听过这段音频。", "听起来有些紧。"):
            with self.subTest(statement=statement), self.assertRaises(planner.PlannerError):
                self.respond({"text": statement, "actions": []})
                self.call()

    def test_explicit_denial_of_listening_does_not_count_as_a_listening_claim(self):
        for statement in ("我没有听过实际音频，只能参考描述。", "本次尚未听到音频，建议先提供录音。", "我无法判断听起来如何。"):
            with self.subTest(statement=statement):
                self.respond({"text": statement, "actions": []})
                self.assertEqual(self.call()["actions"], [])
        # 单独一句的否认不能遮盖下一句相互矛盾的声明。
        self.respond({"text": "我没有听过音频。我已经听过这段音频。", "actions": []})
        with self.assertRaises(planner.PlannerError):
            self.call()

    def test_audio_context_mismatch_and_unreadable_audio_never_send(self):
        with self.assertRaises(planner.PlannerError):
            self.call(audio_paths=[self.audio], audio_context=[])
        with self.assertRaises(planner.PlannerError):
            self.call(audio_paths=[Path(self.directory.name) / "missing.wav"], audio_context=[{}])
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
