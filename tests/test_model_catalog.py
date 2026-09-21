"""模型目录协议测试：所有 HTTP 均由替身处理，不读取真实平台或调用供应商。"""

import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error
from urllib.parse import parse_qs, urlsplit

from synthv_assistant import model_catalog
from synthv_assistant.review import _NoRedirect


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        # 所有成功请求现在都会写缓存，测试必须从一开始隔离数据目录。
        temporary = tempfile.TemporaryDirectory()
        self.root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        data = patch.object(model_catalog, "DATA", self.root)
        data.start()
        self.addCleanup(data.stop)
        # 使用可逆的虚构加密替身，验证协议与失败处理而不读取当前账户或真实凭据。
        encrypt = patch.object(model_catalog.settings, "_encrypt", side_effect=lambda raw: b"sealed:" + raw[::-1])
        decrypt = patch.object(model_catalog.settings, "_decrypt", side_effect=lambda raw: raw.removeprefix(b"sealed:")[::-1])
        encrypt.start()
        decrypt.start()
        self.addCleanup(encrypt.stop)
        self.addCleanup(decrypt.stop)
        self.key = "sk-fictional-model-catalog-key-not-real"
        self.config = {"id": "a" * 32, "provider": "openai", "base": "https://mock.example/v1",
                       "key": self.key, "configured": True, "invalid": False, "timeoutSeconds": 60}
        snapshot = patch.object(model_catalog, "get_model_platform_snapshot", return_value=self.config)
        transport = patch.object(model_catalog, "_get_json", return_value={"data": [{"id": "gpt-test"}]})
        self.snapshot, self.http = snapshot.start(), transport.start()
        self.addCleanup(snapshot.stop)
        self.addCleanup(transport.stop)
        self.transport_patch = transport

    def call(self):
        return model_catalog.list_platform_models(self.config["id"])

    def test_openai_uses_selected_snapshot_and_get_models_without_changing_model(self):
        result = self.call()
        self.assertEqual(result["platformId"], self.config["id"])
        self.assertEqual(result["models"], [{"id": "gpt-test", "label": "gpt-test"}])
        self.assertEqual(result["source"], "api")
        url, headers, timeout = self.http.call_args.args
        self.assertEqual(url, "https://mock.example/v1/models")
        self.assertEqual(headers, {"Authorization": "Bearer " + self.key})
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)
        self.snapshot.assert_called_once_with(self.config["id"])
        self.assertNotIn(self.key, json.dumps(result))

    def test_openai_pagination_is_same_origin_encoded_bounded_and_deduplicated(self):
        self.http.side_effect = [
            {"data": [{"id": "vendor/model-one"}], "has_more": True, "last_id": "cursor/?a=b", "next": "https://untrusted.example"},
            {"data": [{"id": "vendor/model-one"}, {"id": "model-two", "name": "第二模型"}], "has_more": False},
        ]
        result = self.call()
        self.assertEqual([item["id"] for item in result["models"]], ["vendor/model-one", "model-two"])
        second = urlsplit(self.http.call_args_list[1].args[0])
        self.assertEqual(second.netloc, "mock.example")
        self.assertEqual(parse_qs(second.query)["after"], ["cursor/?a=b"])
        self.assertEqual(result["pages"], 2)
        self.assertFalse(result["truncated"])
        self.snapshot.assert_called_once()

    def test_gemini_filters_generate_content_and_removes_models_prefix(self):
        self.config.update(provider="gemini", base="https://gemini.mock/v1beta")
        self.http.side_effect = [
            {"models": [{"name": "models/gemini-text", "displayName": "Gemini 示例", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/embed-only", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "opaque+=token"},
            {"models": [{"name": "models/gemini-audio", "supportedGenerationMethods": ["generateContent", "countTokens"]}]},
        ]
        result = self.call()
        self.assertEqual([entry["id"] for entry in result["models"]], ["gemini-text", "gemini-audio"])
        self.assertEqual(self.http.call_args.args[1], {"x-goog-api-key": self.key})
        query = parse_qs(urlsplit(self.http.call_args.args[0]).query)
        self.assertEqual(query["pageToken"], ["opaque+=token"])
        self.assertEqual(query["pageSize"], ["100"])

    def test_page_limit_and_repeated_cursor_cannot_loop(self):
        pages = [{"data": [{"id": "model-" + str(index)}], "has_more": True, "last_id": "cursor-" + str(index)} for index in range(10)]
        self.http.side_effect = pages
        result = self.call()
        self.assertEqual(self.http.call_count, model_catalog.MAX_PAGES)
        self.assertTrue(result["truncated"])
        self.http.reset_mock(side_effect=True)
        self.http.return_value = {"data": [{"id": "one"}], "has_more": True, "last_id": "same"}
        result = self.call()
        self.assertEqual(self.http.call_count, 2)
        self.assertTrue(result["truncated"])

    def test_model_count_is_bounded_and_invalid_ids_are_not_exposed(self):
        self.http.return_value = {"data": [{"id": "../unsafe"}, {"id": "contains space"}, {"id": "https://bad.example"},
                                          {"id": "model\ncontrol"}, {"id": []}, None]
                                 + [{"id": "model-" + str(index)} for index in range(600)]}
        result = self.call()
        self.assertEqual(len(result["models"]), 500)
        self.assertEqual(result["models"][0]["id"], "model-0")
        self.assertTrue(result["truncated"])
        self.http.assert_called_once()

    def test_no_key_disabled_and_invalid_config_never_issue_http(self):
        for change in ({"configured": False}, {"invalid": True}, {"key": ""}, {"provider": "none"}):
            original = dict(self.config)
            self.config.update(change)
            with self.subTest(change=change), self.assertRaises(model_catalog.ModelCatalogError):
                self.call()
            self.config.clear()
            self.config.update(original)
        self.http.assert_not_called()

    def test_supplier_cannot_echo_key_in_public_identifier_or_label(self):
        self.http.return_value = {"data": [{"id": self.key}, {"id": "valid-model", "name": "供应商返回 " + self.key + "\n"}]}
        result = self.call()
        self.assertNotIn(self.key, json.dumps(result))
        self.assertEqual(len(result["models"]), 1)
        self.assertIn("已隐藏密钥", result["models"][0]["label"])
        self.assertNotIn("\n", result["models"][0]["label"])

    def test_http_errors_never_echo_key_url_or_response_and_never_retry(self):
        for status in (301, 302, 401, 403, 404, 429, 500):
            self.http.reset_mock()
            self.http.side_effect = error.HTTPError("https://private.example/" + self.key, status, self.key, {}, io.BytesIO(self.key.encode()))
            with self.subTest(status=status), self.assertRaises(model_catalog.ModelCatalogError) as caught:
                self.call()
            self.assertNotIn(self.key, str(caught.exception))
            self.assertNotIn("private.example", str(caught.exception))
            self.http.assert_called_once()

    def test_network_timeout_and_malformed_responses_have_fixed_errors(self):
        for failure in (error.URLError(self.key), socket.timeout(self.key), ValueError(self.key), KeyError(self.key)):
            self.http.side_effect = failure
            with self.assertRaises(model_catalog.ModelCatalogError) as caught:
                self.call()
            self.assertNotIn(self.key, str(caught.exception))
        self.http.side_effect = None
        for response in ({}, {"data": {}}, {"data": [], "has_more": True}, {"data": [], "has_more": True, "last_id": [self.key]}):
            self.http.return_value = response
            with self.assertRaises(model_catalog.ModelCatalogError):
                self.call()

    def test_elapsed_refresh_budget_prevents_new_request(self):
        with patch.object(model_catalog.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(model_catalog.ModelCatalogError, "超时"):
                self.call()
        self.http.assert_not_called()

    def test_gemini_invalid_pagination_and_methods_are_not_trusted(self):
        self.config["provider"] = "gemini"
        self.http.return_value = {"models": [{"name": "models/fake", "supportedGenerationMethods": "generateContent"}]}
        self.assertEqual(self.call()["models"], [])
        self.http.return_value = {"models": [], "nextPageToken": "a" * 2049}
        with self.assertRaises(model_catalog.ModelCatalogError):
            self.call()

    def test_real_transport_is_get_and_installs_no_redirect_handler(self):
        self.transport_patch.stop()
        opener = MagicMock()
        opener.open.return_value = io.BytesIO(b'{"data":[{"id":"get-only"}]}')
        with patch.object(model_catalog.request, "build_opener", return_value=opener) as builder:
            result = self.call()
        self.assertEqual(result["models"][0]["id"], "get-only")
        self.assertIsInstance(builder.call_args.args[0], _NoRedirect)
        outgoing = opener.open.call_args.args[0]
        self.assertEqual(outgoing.get_method(), "GET")
        self.assertIsNone(outgoing.data)
        self.assertEqual(outgoing.get_header("Authorization"), "Bearer " + self.key)
        with self.assertRaises(error.HTTPError):
            builder.call_args.args[0].redirect_request(outgoing, None, 302, "moved", {}, "https://another.example")

    def test_transport_size_and_json_limits_do_not_leak_supplier_text(self):
        self.transport_patch.stop()
        for raw in (b"x" * (model_catalog.MAX_RESPONSE_BYTES + 1), b'{"data":' + self.key.encode(), b"[]"):
            opener = MagicMock()
            opener.open.return_value = io.BytesIO(raw)
            with patch.object(model_catalog.request, "build_opener", return_value=opener):
                with self.assertRaises(model_catalog.ModelCatalogError) as caught:
                    self.call()
                self.assertNotIn(self.key, str(caught.exception))

    def test_catalog_persists_across_reads_without_outbound_requests_or_secret_storage(self):
        fresh = self.call()
        self.assertTrue(fresh["cachePersisted"])
        self.http.reset_mock()
        for _ in range(2):
            cached = model_catalog.get_cached_platform_models(self.config["id"])
            self.assertTrue(cached["cacheHit"])
            self.assertEqual(cached["source"], "cache")
            self.assertEqual(cached["models"], fresh["models"])
            self.assertIsNotNone(cached["cachedAt"])
        self.http.assert_not_called()
        raw = next((self.root / "model-catalogs").glob("*.json")).read_text(encoding="utf-8")
        for private in (self.key, self.config["base"], "Authorization"):
            self.assertNotIn(private, raw)
            self.assertNotIn(private, json.dumps(cached))
        self.assertNotIn("fingerprint", cached)
        self.assertNotIn("configurationProof", cached)
        self.assertNotIn(model_catalog._configuration_fingerprint(self.config), raw)

    def test_cache_miss_and_disabled_platform_never_fetch_models(self):
        self.assertFalse(model_catalog.get_cached_platform_models(self.config["id"])["cacheHit"])
        self.call()
        self.http.reset_mock()
        self.config["configured"] = False
        self.assertEqual(model_catalog.get_cached_platform_models(self.config["id"])["models"], [])
        self.http.assert_not_called()

    def test_connection_changes_invalidate_but_display_and_default_changes_reuse(self):
        self.call()
        self.http.reset_mock()
        self.config.update(name="改名", model="new-default", timeoutSeconds=180, revision="unrelated-registry-change")
        self.assertTrue(model_catalog.get_cached_platform_models(self.config["id"])["cacheHit"])
        for name, replacement in (("provider", "gemini"), ("base", "https://changed.example/v1"),
                                  ("key", "fictional-other-account-key"), ("id", "b" * 32)):
            previous = self.config[name]
            self.config[name] = replacement
            with self.subTest(field=name):
                self.assertFalse(model_catalog.get_cached_platform_models(self.config["id"])["cacheHit"])
            self.config[name] = previous
        self.http.assert_not_called()

    def test_old_cache_remains_available_with_explicit_stale_marker(self):
        with patch.object(model_catalog.time, "time", return_value=1_700_000_000):
            self.call()
        self.http.reset_mock()
        with patch.object(model_catalog.time, "time", return_value=1_700_000_000 + model_catalog.CACHE_STALE_SECONDS + 10):
            cached = model_catalog.get_cached_platform_models(self.config["id"])
        self.assertTrue(cached["cacheHit"])
        self.assertTrue(cached["stale"])
        self.assertEqual(len(cached["models"]), 1)
        self.http.assert_not_called()

    def test_explicit_refresh_replaces_cache_but_failed_refresh_keeps_previous(self):
        self.call()
        self.http.return_value = {"data": [{"id": "new-model"}]}
        self.call()
        self.http.side_effect = TimeoutError
        with self.assertRaises(model_catalog.ModelCatalogError):
            self.call()
        self.http.reset_mock()
        cached = model_catalog.get_cached_platform_models(self.config["id"])
        self.assertEqual(cached["models"][0]["id"], "new-model")
        self.http.assert_not_called()

    def test_broken_oversized_and_poisoned_cache_is_ignored_without_network(self):
        self.call()
        path = model_catalog._cache_path(self.config["id"])
        good = json.loads(path.read_text(encoding="utf-8"))
        poisoned = {**good, "models": [{"id": "safe-model", "label": self.key}]}
        duplicate = {**good, "models": good["models"] * 2}
        future = {**good, "savedAt": 1e100}
        self.http.reset_mock()
        for raw in (b"broken", b"x" * (model_catalog.MAX_CACHE_BYTES + 1),
                    json.dumps(poisoned).encode(), json.dumps(duplicate).encode(), json.dumps(future).encode()):
            path.write_bytes(raw)
            cached = model_catalog.get_cached_platform_models(self.config["id"])
            self.assertFalse(cached["cacheHit"])
            self.assertNotIn(self.key, json.dumps(cached))
        self.http.assert_not_called()

    def test_failed_cache_write_keeps_existing_cache_and_returns_fresh_models(self):
        self.call()
        path = model_catalog._cache_path(self.config["id"])
        previous = path.read_bytes()
        self.http.return_value = {"data": [{"id": "new-model"}]}
        with patch.object(model_catalog.os, "replace", side_effect=OSError("private-path")):
            result = self.call()
        self.assertEqual(result["models"][0]["id"], "new-model")
        self.assertFalse(result["cachePersisted"])
        self.assertNotIn("private-path", json.dumps(result))
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_old_request_cache_does_not_match_reconfigured_account(self):
        original = dict(self.config)
        self.call()
        self.config["key"] = "fictional-reconfigured-key"
        # 模拟旧平台请求在改配置之后才落盘；读接口仍按最新身份拒绝它。
        model_catalog._save_cache(original, {"models": [{"id": "old-model", "label": "old-model"}], "pages": 1, "truncated": False})
        self.http.reset_mock()
        self.assertFalse(model_catalog.get_cached_platform_models(self.config["id"])["cacheHit"])
        self.http.assert_not_called()

    def test_gemini_empty_and_normalized_catalogs_can_be_cached(self):
        self.config["provider"] = "gemini"
        self.http.return_value = {"models": [{"name": "models/gemini-test", "displayName": "测试模型", "supportedGenerationMethods": ["generateContent"]}]}
        self.call()
        self.assertEqual(model_catalog.get_cached_platform_models(self.config["id"])["models"][0]["id"], "gemini-test")
        self.http.return_value = {"models": []}
        self.call()
        cached = model_catalog.get_cached_platform_models(self.config["id"])
        self.assertTrue(cached["cacheHit"])
        self.assertEqual(cached["models"], [])

    def test_dpapi_failure_never_saves_plain_identity_or_blocks_manual_model_input(self):
        with patch.object(model_catalog.settings, "_encrypt", side_effect=ValueError("private-encryption-error")):
            result = self.call()
        self.assertFalse(result["cachePersisted"])
        self.assertEqual(len(result["models"]), 1)
        self.assertFalse(model_catalog._cache_path(self.config["id"]).exists())
        self.assertNotIn("private-encryption-error", json.dumps(result))
        self.call()
        self.http.reset_mock()
        with patch.object(model_catalog.settings, "_decrypt", side_effect=ValueError("private-decryption-error")):
            cached = model_catalog.get_cached_platform_models(self.config["id"])
        self.assertFalse(cached["cacheHit"])
        self.assertEqual(cached["models"], [])
        self.assertNotIn("private-decryption-error", json.dumps(cached))
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
