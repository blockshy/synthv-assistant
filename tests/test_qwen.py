"""Qwen 百炼接入的离线契约回归：只用临时目录、合成 WAV、假密钥和内存 HTTP。"""

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
import wave

from synthv_assistant import model_catalog, model_options, planner, platforms, review, settings, streaming
from tests.test_streaming import FakeResponse, VirtualClock, openai_event, sse


class QwenTests(unittest.TestCase):
    """在同一隔离配置下覆盖平台保存、能力、Chat/SSE、音频限制与原生模型目录。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        for target in (settings, platforms, model_catalog):
            self.patcher(patch.object(target, "DATA", self.root))
        self.patcher(patch.dict(os.environ, {}, clear=True))
        # 测试替身仅用于验证无明文落盘和跨读取行为，不调用真实账户的 DPAPI。
        self.patcher(patch.object(settings, "_encrypt", side_effect=lambda raw: b"sealed:" + raw[::-1]))
        self.patcher(patch.object(settings, "_decrypt", side_effect=lambda raw: raw.removeprefix(b"sealed:")[::-1]))
        self.key = "fictional-qwen-key-never-real"
        self.config = {"id": "default", "provider": "qwen", "model": "qwen3.8-flash",
                       "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "key": self.key,
                       "timeoutSeconds": 30, "configured": True, "invalid": False,
                       "source": "local", "revision": "test", "message": "已配置"}
        self.response = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"text": "可以先讨论调教方向。", "actions": []}, ensure_ascii=False)}}]}
        self.wav = self.root / "synthetic.wav"
        with wave.open(str(self.wav), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(8000)
            target.writeframes(b"\x00\x10" * 800)
        self.patcher(patch.object(planner, "get_audio_configuration_snapshot", return_value=self.config))
        self.patcher(patch.object(review, "get_audio_configuration_snapshot", return_value=self.config))
        self.outbound = self.patcher(patch.object(planner, "_send_json", return_value=self.response))
        self.stream = self.patcher(patch.object(streaming, "send_stream_json", return_value=self.response))

    def patcher(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def plan(self, **extra):
        paths = extra.pop("audio_paths", [])
        return planner.plan_tuning("讨论调教", None, [], paths, [{"label": "合成测试音频"} for _path in paths], **extra)

    def test_environment_defaults_are_qwen_and_only_dashscope_key_is_used(self):
        os.environ.update(SYNTHV_AUDIO_PROVIDER="qwen", DASHSCOPE_API_KEY=self.key,
                          OPENAI_API_KEY="wrong-provider-key", GEMINI_API_KEY="wrong-provider-key")
        snapshot = settings.get_audio_configuration_snapshot()
        self.assertEqual(snapshot["model"], "qwen3.8-flash")
        self.assertEqual(snapshot["base"], self.config["base"])
        self.assertEqual(snapshot["key"], self.key)
        self.assertNotIn(self.key, json.dumps(settings.get_audio_settings()))
        self.assertFalse((self.root / "audio-settings.json").exists())

    def test_qwen_platform_persists_as_default_without_plaintext_key_or_cross_provider_reuse(self):
        payload = {"name": "Qwen 示例", "provider": "qwen", "model": "", "baseUrl": "",
                   "timeoutSeconds": 60, "revision": "empty", "apiKey": self.key}
        created = platforms.save_model_platform(payload)
        self.assertEqual(created["model"], "qwen3.8-flash")
        self.assertEqual(created["baseUrl"], self.config["base"])
        selected = platforms.set_default_model_platform({"id": created["id"], "revision": created["revision"]})
        self.assertEqual(platforms.get_default_model_platform_id(), created["id"])
        self.assertEqual(platforms.get_model_platform_snapshot(created["id"])["key"], self.key)
        self.assertNotIn(self.key, json.dumps(selected))
        self.assertNotIn(self.key.encode(), (self.root / "model-platforms.json").read_bytes())
        with self.assertRaises(settings.SettingsError):
            platforms.save_model_platform({**payload, "id": created["id"], "provider": "openai",
                                           "baseUrl": self.config["base"], "revision": selected["revision"], "apiKey": ""})

    def test_three_models_have_explicit_audio_and_native_reasoning_capabilities(self):
        for model in ("qwen3.8-flash", "qwen3.8-max", "qwen3.8-omni-flash"):
            info = model_options.capabilities({**self.config, "model": model})
            self.assertEqual(info["audioInput"], "supported" if "omni" in model else "unsupported")
            self.assertEqual([item["value"] for item in info["reasoning"]["options"]],
                             ["default", "none", "low", "medium", "xhigh"])
        # 不能把其他 Qwen 型号臆测成 Omni，亦不允许沿用未知 OpenAI 兼容模型的档位。
        unknown = model_options.capabilities({**self.config, "model": "unverified-model"})
        self.assertEqual(unknown["audioInput"], "unsupported")
        self.assertEqual([item["value"] for item in unknown["reasoning"]["options"]], ["default"])

    def test_qwen_reasoning_is_top_level_and_never_replays_truncated_thinking(self):
        for effort in ("default", "none", "low", "medium", "xhigh"):
            body = {}
            model_options.apply_reasoning(body, self.config, effort, include_summary=True)
            expected = {"preserve_thinking": False}
            if effort != "default":
                expected["reasoning_effort"] = effort
            self.assertEqual(body, expected)
        for invalid in ("high", "max", "minimal"):
            with self.assertRaises(ValueError):
                model_options.apply_reasoning({}, self.config, invalid)

    def test_text_chat_uses_compatible_endpoint_and_preserves_provider_identity(self):
        with patch.object(planner, "_load_audio") as read:
            result = self.plan()
        read.assert_not_called()
        url, body, headers, timeout = self.outbound.call_args.args
        self.assertEqual(url, self.config["base"] + "/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer " + self.key})
        self.assertEqual(timeout, 30)
        self.assertIsInstance(body["messages"][1]["content"], str)
        self.assertFalse(body["preserve_thinking"])
        self.assertNotIn("input_audio", json.dumps(body))
        self.assertNotIn("generationConfig", body)
        self.assertEqual(result["provider"], "qwen")
        self.assertEqual(result["inputMode"], "text")

    def test_qwencloud_uses_the_explicit_base_without_falling_back_to_dashscope(self):
        # QwenCloud 的按量接口使用同一 Chat 契约，但密钥与百炼互不相同；地址由
        # 用户显式选择，后端不得根据供应商名称重写主机或自动探测备用地址。
        for host in ("maas.qwencloudapi.com", "maas.qianwenaiapi.com"):
            self.config["base"] = "https://" + host + "/compatible-mode/v1"
            self.config["model"] = "qwen3.8-flash"
            self.plan()
            self.assertEqual(self.outbound.call_args.args[0], self.config["base"] + "/chat/completions")
            self.config["model"] = "qwen3.8-omni-flash"
            self.plan(audio_paths=[self.wav])
            self.assertEqual(self.stream.call_args.args[0], self.config["base"] + "/chat/completions")
            with patch.object(model_catalog, "get_model_platform_snapshot", return_value=self.config), \
                 patch.object(model_catalog, "_get_json") as outbound:
                with self.assertRaisesRegex(model_catalog.ModelCatalogError, "手动填写"):
                    model_catalog.list_platform_models("default")
                outbound.assert_not_called()

    def test_non_audio_models_block_attachments_before_reading_or_network(self):
        for model in ("qwen3.8-flash", "qwen3.8-max", "unverified-model"):
            self.config["model"] = model
            with patch.object(planner, "_load_audio") as read:
                with self.assertRaisesRegex(planner.PlannerError, "不支持音频"):
                    self.plan(audio_paths=[self.wav])
            read.assert_not_called()
            with patch.object(review, "_load_audio") as read:
                result = review.review_audio([self.wav], "听一听")
            read.assert_not_called()
            self.assertEqual(result["status"], "error")
        self.outbound.assert_not_called()
        self.stream.assert_not_called()

    def test_omni_audio_uses_data_uri_and_sse_even_without_progress_callback(self):
        self.config["model"] = "qwen3.8-omni-flash"
        result = self.plan(audio_paths=[self.wav, self.wav])
        self.outbound.assert_not_called()
        url, body, headers, timeout, provider, _callback = self.stream.call_args.args
        self.assertEqual(provider, "qwen")
        self.assertEqual(body["modalities"], ["text"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertFalse(body["preserve_thinking"])
        blobs = [item["input_audio"] for item in body["messages"][1]["content"] if item["type"] == "input_audio"]
        self.assertEqual(len(blobs), 2)
        for blob in blobs:
            self.assertTrue(blob["data"].startswith("data:;base64,"))
            self.assertEqual(base64.b64decode(blob["data"].split(",", 1)[1]), self.wav.read_bytes())
            self.assertEqual(blob["format"], "wav")
        self.assertEqual(result["inputMode"], "audio")

    def test_conversation_audio_labels_are_neutral_and_only_standalone_review_declares_before_after(self):
        """从实际发出的请求检查语义，避免普通参考附件被误标为修改前后对照。"""
        self.config["model"] = "qwen3.8-omni-flash"
        self.plan(audio_paths=[self.wav, self.wav])
        parts = self.stream.call_args.args[1]["messages"][1]["content"]
        self.assertEqual([part["text"] for part in parts[1:] if part["type"] == "text"], ["音频 A", "音频 B"])
        review.review_audio([self.wav, self.wav], "比较修改前后的听感")
        parts = self.stream.call_args.args[1]["messages"][1]["content"]
        self.assertEqual([part["text"] for part in parts[1:] if part["type"] == "text"],
                         ["片段 A（修改前）", "片段 B（修改后）"])

    def test_omni_encoded_total_limit_rejects_exact_boundary_without_upload(self):
        self.config["model"] = "qwen3.8-omni-flash"
        size = len("data:;base64,") + 4 * ((self.wav.stat().st_size + 2) // 3)
        with patch.object(review, "MAX_QWEN_AUDIO_DATA_BYTES", 2 * size):
            with self.assertRaisesRegex(planner.PlannerError, "小于 10 MB"):
                self.plan(audio_paths=[self.wav, self.wav])
            result = review.review_audio([self.wav, self.wav], "比较")
            self.assertEqual(result["errorCode"], "invalid_input")
            self.assertIn("小于 10 MB", result["message"])
        self.stream.assert_not_called()
        self.outbound.assert_not_called()

    def test_standalone_omni_review_collects_only_normal_completed_text(self):
        self.config["model"] = "qwen3.8-omni-flash"
        self.stream.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": "尾音平稳。"}}]}
        result = review.review_audio([self.wav], "评价")
        self.assertEqual(result["review"], "尾音平稳。")
        self.assertIsNone(self.stream.call_args.args[-1])
        self.assertFalse(self.stream.call_args.args[1]["preserve_thinking"])
        self.stream.return_value["choices"][0]["finish_reason"] = "length"
        self.assertEqual(review.review_audio([self.wav], "评价")["status"], "error")

    def test_standalone_omni_review_preserves_safe_stream_timeout_stage_without_retry(self):
        self.config["model"] = "qwen3.8-omni-flash"
        message = "AI 已连续 30 秒没有新的正文或思考摘要，等待超时。本次未自动重试，也未修改工程。"
        self.stream.side_effect = streaming.StreamingTimeoutError(message)
        result = review.review_audio([self.wav], "评价")
        self.assertEqual(result["errorCode"], "timeout")
        self.assertEqual(result["message"], message)
        self.stream.assert_called_once()
        self.outbound.assert_not_called()

    def test_native_catalog_paginates_same_origin_and_reuses_identity_bound_cache(self):
        pages = [{"output": {"total": 3, "models": [{"model": "qwen3.8-flash"}, {"model": "qwen3.8-max"}]}},
                 {"output": {"total": 3, "models": [{"model": "qwen3.8-omni-flash"}]},
                  "next": "https://untrusted.example/"}]
        with patch.object(model_catalog, "get_model_platform_snapshot", return_value=self.config), \
             patch.object(model_catalog, "_get_json", side_effect=pages) as outbound:
            result = model_catalog.list_platform_models("default")
            self.assertEqual([item["id"] for item in result["models"]],
                             ["qwen3.8-flash", "qwen3.8-max", "qwen3.8-omni-flash"])
            self.assertEqual(result["pages"], 2)
            for index, call in enumerate(outbound.call_args_list):
                parsed = urlsplit(call.args[0])
                self.assertEqual(parsed.netloc, "dashscope.aliyuncs.com")
                self.assertEqual(parsed.path, "/api/v1/models")
                self.assertEqual(parse_qs(parsed.query), {"page_no": [str(index + 1)], "page_size": ["100"]})
                self.assertEqual(call.args[1], {"Authorization": "Bearer " + self.key})
            outbound.reset_mock()
            cached = model_catalog.get_cached_platform_models("default")
            self.assertTrue(cached["cacheHit"])
            self.assertEqual(cached["models"], result["models"])
            self.config["key"] = "different-fake-key"
            self.assertFalse(model_catalog.get_cached_platform_models("default")["cacheHit"])
            outbound.assert_not_called()

    def test_catalog_never_guesses_custom_gateway_or_switches_origin(self):
        for base in ("https://mock.example/compatible-mode/v1", "https://dashscope.aliyuncs.com.evil.example/compatible-mode/v1",
                     "https://dashscope.aliyuncs.com/custom-path", "https://dashscope.aliyuncs.com:444/compatible-mode/v1"):
            self.config["base"] = base
            with patch.object(model_catalog, "get_model_platform_snapshot", return_value=self.config), \
                 patch.object(model_catalog, "_get_json") as outbound:
                with self.assertRaisesRegex(model_catalog.ModelCatalogError, "手动填写"):
                    model_catalog.list_platform_models("default")
                outbound.assert_not_called()
        for host in ("dashscope-intl.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com",
                     "testspace.cn-beijing.maas.aliyuncs.com", "testspace.ap-northeast-1.maas.aliyuncs.com",
                     "testspace.eu-central-1.maas.aliyuncs.com", "testspace.us-east-1.maas.aliyuncs.com"):
            self.assertEqual(model_catalog._qwen_catalog_base("https://" + host + "/compatible-mode/v1"),
                             "https://" + host + "/api/v1/models")

    def test_native_catalog_rejects_malformed_total_without_caching_or_leaking_keys(self):
        for total, entries in ((True, []), (-1, []), ("2", []), (2, [])):
            with patch.object(model_catalog, "get_model_platform_snapshot", return_value=self.config), \
                 patch.object(model_catalog, "_get_json", return_value={"output": {"total": total, "models": entries}}):
                with self.assertRaises(model_catalog.ModelCatalogError):
                    model_catalog.list_platform_models("default")
        self.assertFalse((self.root / "model-catalogs").exists())

    def test_qwen_sse_reasoning_extends_idle_deadline_and_usage_frame_does_not_erase_answer(self):
        # 使用真正 SSE 聚合器和虚拟时钟，确认 Qwen 沿用有效输出续期，而非重新
        # 引入固定总超时；思考、正文、usage 和 [DONE] 均采用官方兼容字段。
        frames = [sse(openai_event({"reasoning_content": "先分析。"})),
                  sse(openai_event({"reasoning_content": "再比较。"})),
                  sse(openai_event({"content": "最终建议。"}, "stop")),
                  sse({"choices": [], "usage": {"total_tokens": 8}}), sse("[DONE]")]
        clock = VirtualClock([20, 20, 20, 0, 0])
        response = FakeResponse(frames, on_read=clock.on_read)
        opener = Mock()
        opener.open.return_value = response
        updates = []
        # 保存于模块级的未替换引用，让本例不依赖 patch 的内部属性或真实网络。
        with patch.object(streaming.request, "build_opener", return_value=opener), \
             patch.object(streaming.time, "monotonic", side_effect=clock):
            result = ORIGINAL_STREAM("https://mock.invalid/compatible-mode/v1/chat/completions",
                                     {"model": "qwen3.8-flash"}, {"Authorization": "Bearer " + self.key},
                                     30, "qwen", updates.append)
        self.assertEqual(result["choices"][0]["message"]["content"], "最终建议。")
        self.assertEqual("".join(item["reasoningDelta"] for item in updates), "先分析。再比较。")
        self.assertTrue(json.loads(opener.open.call_args.args[0].data)["stream"])
        self.assertEqual(result["usage"]["total_tokens"], 8)
        self.assertEqual(clock.now - clock.started, 60)


# 在测试 setUp 安装发送替身之前保留真实函数；它仍只会读取本用例 FakeResponse。
ORIGINAL_STREAM = streaming.send_stream_json


if __name__ == "__main__":
    unittest.main()
