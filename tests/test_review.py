"""通过 HTTP mock 检查官方音频请求结构；测试期间不发送任何网络请求。"""

import base64
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib import error
import wave

from synthv_assistant import review, settings


class AudioReviewTests(unittest.TestCase):
    """验证未配置、凭据隔离、输入限制及供应商响应的处理。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # 模型配置只允许从本测试目录读取，绝不接触用户已保存的 DPAPI 配置。
        self.settings_directory = patch("synthv_assistant.settings.DATA", Path(self.directory.name))
        self.settings_directory.start()
        self.addCleanup(self.settings_directory.stop)
        self.path = Path(self.directory.name) / "voice.wav"
        with wave.open(str(self.path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(b"\x00\x10" * 800)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # 所有测试全局截获 opener；即使代码意外提前联网也会留在 mock 内。
        self.openers = patch("synthv_assistant.review.request.build_opener")
        self.opener_factory = self.openers.start()
        self.addCleanup(self.openers.stop)
        self.opener = self.opener_factory.return_value

    def configure(self, provider="openai"):
        os.environ["SYNTHV_AUDIO_PROVIDER"] = provider
        os.environ["OPENAI_API_KEY" if provider == "openai" else "GEMINI_API_KEY"] = "test-secret-do-not-print"

    def respond(self, document):
        response = MagicMock()
        response.read.return_value = json.dumps(document).encode("utf-8")
        self.opener.open.return_value.__enter__.return_value = response

    def test_unconfigured_does_not_read_file_or_call_network(self):
        result = review.review_audio([Path("not-present.wav")], "评价")
        self.assertEqual(result["status"], "not_configured")
        self.assertIsNone(result["review"])
        self.opener_factory.assert_not_called()

    def test_key_alone_does_not_enable_upload(self):
        os.environ["OPENAI_API_KEY"] = "test-secret-do-not-print"
        self.assertFalse(review.provider_status()["configured"])
        review.review_audio([self.path], "评价")
        self.opener_factory.assert_not_called()

    def test_status_never_echoes_key_or_custom_endpoint(self):
        self.configure()
        os.environ["SYNTHV_AUDIO_BASE_URL"] = "https://my-private-gateway.example/v1"
        result = review.provider_status()
        self.assertTrue(result["configured"])
        self.assertTrue(result["keyConfigured"])
        self.assertTrue(result["baseUrlConfigured"])
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertNotIn("my-private-gateway", json.dumps(result))

    def test_openai_request_has_actual_wav_and_text_output(self):
        self.configure()
        self.respond({"choices": [{"message": {"content": "尾音较平稳。"}}]})
        result = review.review_audio([self.path], "评价尾音", {"lyrics": "你好"})
        self.assertEqual(result["status"], "ok")
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-secret-do-not-print")
        document = json.loads(req.data)
        self.assertEqual(document["model"], "gpt-audio-1.5")
        self.assertEqual(document["modalities"], ["text"])
        audio = document["messages"][1]["content"][-1]["input_audio"]
        self.assertEqual(base64.b64decode(audio["data"]), self.path.read_bytes())
        self.assertEqual(audio["format"], "wav")

    def test_gemini_comparison_has_labeled_audio_parts(self):
        self.configure("gemini")
        self.respond({"candidates": [{"content": {"parts": [
            {"text": "private thought", "thought": True}, {"text": "B 更轻，但不一定更好。"}
        ]}}]})
        result = review.review_audio([self.path, self.path], "比较")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("private thought", result["review"])
        req = self.opener.open.call_args.args[0]
        self.assertIn(":generateContent", req.full_url)
        self.assertNotIn("test-secret", req.full_url)
        document = json.loads(req.data)
        parts = document["contents"][0]["parts"]
        self.assertIn("修改前", parts[1]["text"])
        self.assertIn("修改后", parts[3]["text"])
        self.assertEqual(parts[2]["inlineData"]["mimeType"], "audio/wav")
        self.assertEqual(base64.b64decode(parts[4]["inlineData"]["data"]), self.path.read_bytes())

    def test_http_errors_never_expose_server_details(self):
        self.configure()
        self.opener.open.side_effect = error.HTTPError(
            "https://example.invalid/?key=test-secret-do-not-print", 401,
            "test-secret-do-not-print", {}, io.BytesIO(b"test-secret-do-not-print"))
        result = review.review_audio([self.path], "评价")
        self.assertEqual(result["httpStatus"], 401)
        self.assertIsNone(result["review"])
        self.assertNotIn("test-secret", json.dumps(result))

    def test_timeout_and_malformed_response_do_not_invent_review(self):
        self.configure()
        self.opener.open.side_effect = socket.timeout("sensitive endpoint")
        self.assertEqual(review.review_audio([self.path], "评价")["errorCode"], "timeout")
        self.opener.open.side_effect = None
        self.respond({"choices": []})
        result = review.review_audio([self.path], "评价")
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["review"])

    def test_invalid_config_and_size_are_rejected_before_network(self):
        self.configure()
        os.environ["SYNTHV_AUDIO_BASE_URL"] = "http://example.invalid/v1"
        self.assertFalse(review.provider_status()["configured"])
        review.review_audio([self.path], "评价")
        self.opener_factory.assert_not_called()
        del os.environ["SYNTHV_AUDIO_BASE_URL"]
        with patch("synthv_assistant.review.MAX_AUDIO_BYTES", 50):
            result = review.review_audio([self.path], "评价")
        self.assertEqual(result["errorCode"], "invalid_input")
        self.opener_factory.assert_not_called()

    def test_redirect_is_blocked(self):
        # 重定向默认会保留部分认证头；明确阻止，避免密钥跨站泄漏。
        with self.assertRaises(error.HTTPError):
            review._NoRedirect().redirect_request(
                MagicMock(full_url="https://api.openai.com/v1/chat/completions"),
                None, 302, "redirect", {}, "https://another.example")

    def test_truncated_wav_does_not_upload(self):
        self.configure()
        self.path.write_bytes(self.path.read_bytes()[:-8])
        result = review.review_audio([self.path], "评价")
        self.assertEqual(result["errorCode"], "invalid_input")
        self.opener_factory.assert_not_called()

    def test_request_uses_only_one_configuration_snapshot(self):
        # 请求开始后即使配置源变成另一供应商，也必须继续用同一模型、地址、密钥和超时。
        first = {"provider": "openai", "model": "gpt-audio-1.5", "base": "https://first.example/v1",
                 "key": "snapshot-test-key", "timeoutSeconds": 37, "configured": True,
                 "invalid": False, "source": "local", "revision": "one", "message": "configured"}
        second = {**first, "provider": "gemini", "key": "wrong-provider-key", "timeoutSeconds": 88}
        self.respond({"choices": [{"message": {"content": "已对音频提出建议。"}}]})
        with patch("synthv_assistant.review.get_audio_configuration_snapshot", side_effect=[first, second]) as snapshots:
            result = review.review_audio([self.path], "评价")
        self.assertEqual(result["status"], "ok")
        snapshots.assert_called_once()
        outbound = self.opener.open.call_args.args[0]
        self.assertEqual(outbound.full_url, "https://first.example/v1/chat/completions")
        self.assertEqual(outbound.get_header("Authorization"), "Bearer snapshot-test-key")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 37)

    def test_corrupt_local_configuration_does_not_use_environment_key(self):
        self.configure()
        (Path(self.directory.name) / "audio-settings.json").write_text("invalid", encoding="utf-8")
        result = review.review_audio([self.path], "评价")
        self.assertEqual(result["status"], "not_configured")
        self.assertIn("损坏", result["message"])
        self.opener_factory.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "配置持久化使用 Windows DPAPI。")
    def test_saved_update_applies_to_next_request_without_changing_active_snapshot(self):
        # 在第一个请求已组装期间保存新设置；当次继续使用旧值，下次立即采用新值。
        settings.update_audio_settings({"provider": "openai", "model": "gpt-audio-1.5",
                                        "baseUrl": "https://api.openai.com/v1", "timeoutSeconds": 30,
                                        "apiKey": "fake-first-request-key", "revision": "environment"})
        requests = []

        def send_without_network(url, body, headers, timeout):
            requests.append((url, body["model"], headers["Authorization"], timeout))
            if len(requests) == 1:
                settings.update_audio_settings({"provider": "openai", "model": "gpt-audio",
                                                "baseUrl": "https://api.openai.com/v1", "timeoutSeconds": 75,
                                                "apiKey": "fake-next-request-key", "revision": settings.get_audio_settings()["revision"]})
            return {"choices": [{"message": {"content": "这是一条模拟听评回复。"}}]}

        with patch("synthv_assistant.review._send_json", side_effect=send_without_network):
            first = review.review_audio([self.path], "评价")
            second = review.review_audio([self.path], "再次评价")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(requests[0][1:], ("gpt-audio-1.5", "Bearer fake-first-request-key", 30.0))
        self.assertEqual(requests[1][1:], ("gpt-audio", "Bearer fake-next-request-key", 75.0))
        self.opener_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
