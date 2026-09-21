"""HTTP 边界回归测试：直接驱动请求处理器，不开放端口、不接触真实宿主。"""

from __future__ import annotations

from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from synthv_assistant.server import make_server
from synthv_assistant.settings import SettingsConflictError


class ServerTests(unittest.TestCase):
    """验证 Host、Origin、令牌、路由与音频范围，防止跨站调用编辑接口。"""

    def setUp(self):
        self.service = MagicMock()
        self.service.jobs = {}
        self.service.status.return_value = {"version": "test"}
        with patch("synthv_assistant.server.ThreadingHTTPServer") as constructor, \
                patch("synthv_assistant.server.secrets.token_urlsafe", return_value="test-token"):
            make_server(8765, self.service)
        self.handler_type = constructor.call_args.args[1]
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def request(self, method, path, *, headers=None, body=None):
        """构造内存请求流，保留实际认证及路由逻辑，只替换网络发送函数。"""
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = Message()
        for key, value in {"Host": "127.0.0.1:8765", **(headers or {})}.items():
            handler.headers[key] = value
        raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode("utf-8")
        if "Content-Length" not in handler.headers:
            handler.headers["Content-Length"] = str(len(raw))
        handler.rfile = io.BytesIO(raw)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        getattr(handler, "do_" + method)()
        status = handler.send_response.call_args.args[0]
        response_headers = dict(call.args for call in handler.send_header.call_args_list)
        return status, response_headers, handler.wfile.getvalue()

    def test_foreign_host_and_origin_are_rejected_before_service(self):
        for headers in ({"Host": "attacker.example:8765"}, {"Origin": "https://attacker.example"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("GET", "/api/status", headers=headers)
                self.assertEqual(status, 403)
        self.service.status.assert_not_called()

    def test_mutating_request_requires_current_token(self):
        for headers in ({}, {"X-SV-Token": "old-token"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/restore", headers=headers)
                self.assertEqual(status, 403)
        self.service.edit.assert_not_called()

    def test_shared_ui_module_is_served_without_exposing_other_files(self):
        """共享组件是页面启动依赖；白名单只开放该资源，不能顺带开放相邻开发文件。"""
        status, headers, body = self.request("GET", "/ui.js")
        self.assertEqual(status, 200)
        self.assertIn(b"window.SynthVUI", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("GET", "/AGENTS.md")[0], 404)

    def test_model_platform_routes_and_message_options(self):
        """平台与能力管理都验证令牌，聊天选择完整交给后台任务。"""
        self.service.list_model_platforms.return_value = {"items": [], "revision": "empty"}
        self.service.get_model_platform.return_value = {"id": "default"}
        self.service.save_model_platform.return_value = {"id": "default"}
        self.service.list_platform_models.return_value = {"models": []}
        self.service.model_capabilities.return_value = {"reasoning": {"options": []}}
        paths = [("GET", "/api/model-platforms"), ("GET", "/api/model-platforms/default"),
                 ("POST", "/api/model-platforms"), ("POST", "/api/model-platforms/default/models"),
                 ("POST", "/api/model-capabilities")]
        for method, path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path)[0], 403)
                self.assertEqual(self.request(method, path, headers={"X-SV-Token": "test-token"})[0], 200)
        self.service.submit.return_value = "job"
        options = {"platformId": "default", "model": "gpt-6-astra", "reasoningEffort": "high"}
        self.request("POST", "/api/conversations/" + "a" * 32 + "/messages", headers={"X-SV-Token": "test-token"},
                     body={"text": "讨论", "modelOptions": options})
        self.assertEqual(self.service.submit.call_args.args[-1], options)

    def test_authorized_apply_routes_only_preview_identifier(self):
        self.service.edit.return_value = {"verified": True}
        status, _, body = self.request("POST", "/api/apply", headers={"X-SV-Token": "test-token"},
                                       body={"previewId": "preview-1", "code": "ignored"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["verified"])
        self.service.edit.assert_called_once_with("apply", {"previewId": "preview-1"})

    def test_non_object_and_oversized_bodies_are_rejected(self):
        for body, extra in ((b"[]", {}), (b"{}", {"Content-Length": "65537"})):
            with self.subTest(body=body, extra=extra):
                status, _, _ = self.request("POST", "/api/restore", body=body,
                                             headers={"X-SV-Token": "test-token", **extra})
                self.assertEqual(status, 400)
        self.service.edit.assert_not_called()

    def test_unicode_token_is_rejected_as_unauthorized(self):
        # HTTP 头允许 Latin-1 字符；比较令牌时不能因此抛出未处理的 TypeError。
        status, _, _ = self.request("POST", "/api/restore", headers={"X-SV-Token": "é"})
        self.assertEqual(status, 403)
        self.service.edit.assert_not_called()

    def test_static_path_traversal_does_not_read_arbitrary_file(self):
        status, _, _ = self.request("GET", "/../synthv_assistant/config.py")
        self.assertEqual(status, 404)

    def test_conversations_and_upload_list_require_token(self):
        for path in ("/api/conversations", "/api/conversations/" + "a" * 32, "/api/uploads", "/api/jobs/private-job"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 403)
        self.service.list_conversations.assert_not_called()
        self.service.get_conversation.assert_not_called()
        self.service.list_uploads.assert_not_called()

    def test_text_message_queues_without_automatic_preview_or_apply(self):
        self.service.submit.return_value = "job-test"
        identifier = "b" * 32
        status, _, response = self.request("POST", "/api/conversations/" + identifier + "/messages",
                                          headers={"X-SV-Token": "test-token"},
                                          body={"text": "气声少一点", "includeSelection": True, "attachments": []})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response), {"jobId": "job-test"})
        self.service.submit.assert_called_once_with(self.service.send_message, identifier, "气声少一点", True, [])
        self.service.preview_action.assert_not_called()
        self.service.apply_action.assert_not_called()

    def test_action_preview_and_confirm_have_separate_endpoints(self):
        identifier = "c" * 32
        for verb in ("preview", "apply"):
            method = getattr(self.service, verb + "_action")
            method.return_value = {"id": identifier, "status": verb}
            status, _, _ = self.request("POST", f"/api/assistant/actions/{identifier}/{verb}",
                                         headers={"X-SV-Token": "test-token"})
            self.assertEqual(status, 200)
            method.assert_called_once_with(identifier)

    def test_raw_upload_is_authenticated_bounded_and_decodes_name(self):
        # 上传采用原始字节，不受 JSON 的 64 KiB 上限影响，也不接受任意目标路径。
        data = b"a" * 70000
        self.service.save_upload.return_value = {"id": "d" * 32}
        headers = {"X-SV-Token": "test-token", "Content-Type": "application/octet-stream",
                   "X-File-Name": "%E6%B5%8B%E8%AF%95.wav"}
        status, _, _ = self.request("POST", "/api/uploads", headers=headers, body=data)
        self.assertEqual(status, 200)
        self.service.save_upload.assert_called_once_with("测试.wav", data)
        self.service.save_upload.reset_mock()
        for extra in ({"X-SV-Token": "bad"}, {"Content-Length": "12000001"},
                      {"Content-Length": "70001"}, {"Content-Type": "application/json"}):
            with self.subTest(extra=extra):
                self.assertGreaterEqual(self.request("POST", "/api/uploads", headers={**headers, **extra}, body=data)[0], 400)
        self.service.save_upload.assert_not_called()

    def test_valid_audio_range_returns_exact_bytes_and_headers(self):
        audio = self.directory / "test.wav"
        audio.write_bytes(b"0123456789")
        self.service.read_audio.return_value = (audio, audio.read_bytes())
        status, headers, body = self.request("GET", "/audio/" + "a" * 32 + ".wav", headers={"Range": "bytes=2-5"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"2345")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(headers["Content-Length"], "4")

    def test_audio_range_past_end_is_rejected(self):
        audio = self.directory / "test.wav"
        audio.write_bytes(b"0123456789")
        self.service.read_audio.return_value = (audio, audio.read_bytes())
        status, _, _ = self.request("GET", "/audio/" + "a" * 32 + ".wav", headers={"Range": "bytes=20-30"})
        self.assertEqual(status, 416)

    def test_audio_settings_get_requires_current_token(self):
        # 设置读取也必须持有本次页面令牌，不能因其为 GET 就跳过认证。
        for headers in ({}, {"X-SV-Token": "old-token"}, {"X-SV-Token": "é"}):
            with self.subTest(headers=headers):
                status, response_headers, _ = self.request("GET", "/api/audio-settings", headers=headers)
                self.assertEqual(status, 403)
                self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.service.get_audio_settings.assert_not_called()

    def test_audio_settings_rejects_foreign_origin_and_host_even_with_token(self):
        # 有效令牌不能覆盖 Host/Origin 边界；读取、保存和清除三条路由都要先拒绝。
        routes = (("GET", "/api/audio-settings"), ("POST", "/api/audio-settings"),
                  ("POST", "/api/audio-settings/clear"))
        for method, path in routes:
            for extra in ({"Origin": "https://attacker.example"}, {"Host": "attacker.example:8765"}):
                with self.subTest(method=method, path=path, extra=extra):
                    status, _, _ = self.request(method, path, headers={"X-SV-Token": "test-token", **extra})
                    self.assertEqual(status, 403)
        self.service.get_audio_settings.assert_not_called()
        self.service.update_audio_settings.assert_not_called()
        self.service.clear_audio_settings.assert_not_called()

    def test_audio_settings_get_returns_public_metadata_without_key(self):
        # 业务层替身保存一把完全虚构的密钥，仅返回其是否存在的公开状态。
        fake_key = "sk-unit-test-audio-settings-never-a-real-key"
        self.service.private_test_key = fake_key
        public_settings = {"provider": "openai", "model": "test-audio-model", "apiKeyConfigured": True}
        self.service.get_audio_settings.return_value = public_settings
        status, headers, body = self.request("GET", "/api/audio-settings", headers={"X-SV-Token": "test-token"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), public_settings)
        self.assertNotIn(fake_key.encode(), body)
        self.assertNotIn("apiKey", json.loads(body))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.service.get_audio_settings.assert_called_once_with()

    def test_audio_settings_post_applies_immediately_and_returns_only_public_result(self):
        # 密钥只应进入保存方法参数；HTTP 回应使用业务层提供的脱敏状态。
        fake_key = "sk-unit-test-save-not-for-network"
        payload = {"provider": "openai", "model": "test-audio-model", "apiKey": fake_key}
        public_result = {"provider": "openai", "model": "test-audio-model", "apiKeyConfigured": True}
        self.service.update_audio_settings.return_value = public_result
        status, headers, body = self.request("POST", "/api/audio-settings",
                                             headers={"X-SV-Token": "test-token"}, body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), public_result)
        self.assertNotIn(fake_key.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.service.update_audio_settings.assert_called_once_with(payload)
        # 即时设置不能排入后台任务，否则下一次评审可能仍使用旧配置。
        self.service.submit.assert_not_called()

    def test_audio_settings_clear_calls_service_without_echoing_payload(self):
        self.service.clear_audio_settings.return_value = {"apiKeyConfigured": False}
        status, headers, body = self.request("POST", "/api/audio-settings/clear",
                                             headers={"X-SV-Token": "test-token"}, body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"apiKeyConfigured": False})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.service.clear_audio_settings.assert_called_once_with()

    def test_audio_settings_mutations_require_current_token(self):
        for path in ("/api/audio-settings", "/api/audio-settings/clear"):
            for headers in ({}, {"X-SV-Token": "old-token"}):
                with self.subTest(path=path, headers=headers):
                    status, _, _ = self.request("POST", path, headers=headers,
                                                 body={"apiKey": "sk-fictional-unauthorized-key"})
                    self.assertEqual(status, 403)
        self.service.update_audio_settings.assert_not_called()
        self.service.clear_audio_settings.assert_not_called()

    def test_audio_settings_unexpected_errors_never_echo_secret(self):
        # 系统异常可能附带完整配置或请求体。三个设置路由必须统一隐藏这种内部信息。
        fake_key = "sk-unit-test-sensitive-error-sentinel"
        routes = (("GET", "/api/audio-settings", self.service.get_audio_settings),
                  ("POST", "/api/audio-settings", self.service.update_audio_settings),
                  ("POST", "/api/audio-settings/clear", self.service.clear_audio_settings))
        for method, path, operation in routes:
            for error_type in (RuntimeError, OSError, TypeError, Exception):
                with self.subTest(method=method, path=path, error_type=error_type.__name__):
                    operation.side_effect = error_type("内部配置 apiKey=" + fake_key)
                    try:
                        status, headers, body = self.request(method, path,
                                                             headers={"X-SV-Token": "test-token"},
                                                             body={"apiKey": fake_key})
                    finally:
                        operation.side_effect = None
                    self.assertGreaterEqual(status, 400)
                    self.assertLess(status, 600)
                    self.assertIsInstance(json.loads(body).get("error"), str)
                    self.assertNotIn(fake_key.encode(), body)
                    self.assertNotIn("内部配置".encode(), body)
                    self.assertEqual(headers["Cache-Control"], "no-store")

    def test_audio_settings_validation_error_remains_actionable(self):
        # 经业务层主动生成且不含输入密钥的验证消息应保留，便于用户修正配置。
        self.service.update_audio_settings.side_effect = ValueError("模型名称不能为空。")
        status, _, body = self.request("POST", "/api/audio-settings", headers={"X-SV-Token": "test-token"},
                                       body={"apiKey": "sk-fictional-invalid-model-key", "model": ""})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "模型名称不能为空。")
        self.assertNotIn(b"sk-fictional-invalid-model-key", body)

    def test_audio_settings_revision_conflict_returns_409_without_key(self):
        # 并发版本冲突必须区别于普通验证错误，前端据此禁用旧版本的再次保存。
        message = "听评配置已被其他页面或进程更新，请重新加载设置后再保存。"
        fake_key = "sk-unit-test-stale-revision-key"
        self.service.update_audio_settings.side_effect = SettingsConflictError(message)
        status, headers, body = self.request("POST", "/api/audio-settings",
                                             headers={"X-SV-Token": "test-token"},
                                             body={"apiKey": fake_key, "revision": "stale-revision"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], message)
        self.assertNotIn(fake_key.encode(), body)
        self.assertNotIn(b"stale-revision", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_audio_settings_malformed_json_never_echoes_body_key(self):
        # JSON 解码在业务错误边界之前执行，也必须验证解析失败不回显敏感请求体。
        fake_key = b"sk-unit-test-malformed-json-sentinel"
        malformed = b'{"apiKey":"' + fake_key + b'","model":}'
        for path in ("/api/audio-settings", "/api/audio-settings/clear"):
            with self.subTest(path=path):
                status, headers, body = self.request("POST", path,
                                                     headers={"X-SV-Token": "test-token"}, body=malformed)
                self.assertEqual(status, 400)
                self.assertNotIn(fake_key, body)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.service.update_audio_settings.assert_not_called()
        self.service.clear_audio_settings.assert_not_called()

    def test_audio_settings_non_object_json_does_not_echo_embedded_key(self):
        # 顶层类型错误应使用固定提示；不能把数组或其中的配置对象转换成错误文字。
        fake_key = "sk-unit-test-wrong-json-type-sentinel"
        invalid = json.dumps([{"apiKey": fake_key}]).encode("utf-8")
        for path in ("/api/audio-settings", "/api/audio-settings/clear"):
            with self.subTest(path=path):
                status, _, body = self.request("POST", path,
                                                headers={"X-SV-Token": "test-token"}, body=invalid)
                self.assertEqual(status, 400)
                self.assertNotIn(fake_key.encode(), body)
                self.assertEqual(json.loads(body)["error"], "请求必须是 JSON 对象。")
        self.service.update_audio_settings.assert_not_called()
        self.service.clear_audio_settings.assert_not_called()

    def test_notes_and_trash_reading_require_current_token(self):
        for path in ("/api/trash", "/api/recordings"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 403)
        self.service.list_trash.assert_not_called()
        self.service.list_recordings.assert_not_called()
        self.service.list_trash.return_value = {"items": []}
        status, _, body = self.request("GET", "/api/trash", headers={"X-SV-Token": "test-token"})
        self.assertEqual((status, json.loads(body)), (200, {"items": []}))

    def test_metadata_delete_restore_routes_are_authenticated_and_strict(self):
        identifier = "a" * 32
        routes = [("/api/conversations/" + identifier + "/metadata", self.service.update_conversation_metadata,
                   {"note": "备注", "starred": True}, (identifier, {"note": "备注", "starred": True})),
                  ("/api/conversations/" + identifier + "/delete", self.service.delete_conversation, {}, (identifier,)),
                  ("/api/assets/upload/" + identifier + "/metadata", self.service.update_asset_metadata,
                   {"label": "主歌"}, ("upload", identifier, {"label": "主歌"})),
                  ("/api/assets/recording/" + identifier + "/delete", self.service.delete_asset, {}, ("recording", identifier)),
                  ("/api/trash/conversation/" + identifier + "/restore", self.service.restore_resource, {}, ("conversation", identifier))]
        for path, operation, payload, expected in routes:
            with self.subTest(path=path):
                operation.reset_mock()
                operation.return_value = {"id": identifier}
                self.assertEqual(self.request("POST", path, body=payload)[0], 403)
                operation.assert_not_called()
                status, _, _ = self.request("POST", path, headers={"X-SV-Token": "test-token"}, body=payload)
                self.assertEqual(status, 200)
                operation.assert_called_once_with(*expected)
                if path.endswith(("/delete", "/restore")):
                    operation.reset_mock()
                    self.assertEqual(self.request("POST", path, headers={"X-SV-Token": "test-token"}, body={"permanent": True})[0], 400)
                    operation.assert_not_called()
        for path in ("/api/assets/unknown/" + identifier + "/delete", "/api/trash/upload/../restore"):
            self.assertEqual(self.request("POST", path, headers={"X-SV-Token": "test-token"})[0], 404)

    def test_all_purge_routes_require_token_and_exact_boolean_confirmation(self):
        identifier = "a" * 32
        routes = [("/api/conversations/" + identifier + "/purge", self.service.purge_conversation, (identifier,)),
                  ("/api/assets/upload/" + identifier + "/purge", self.service.purge_asset, ("upload", identifier)),
                  ("/api/assets/recording/" + identifier + "/purge", self.service.purge_asset, ("recording", identifier))]
        routes += [("/api/trash/" + kind + "/" + identifier + "/purge", self.service.purge_trash, (kind, identifier))
                   for kind in ("conversation", "upload", "recording")]
        for path, operation, prefix in routes:
            with self.subTest(path=path):
                operation.reset_mock()
                operation.return_value = {"deleted": True, "permanent": True, "id": identifier, "kind": prefix[0] if len(prefix) == 2 else "conversation"}
                self.assertEqual(self.request("POST", path, body={"confirm": True})[0], 403)
                for payload in ({}, {"confirm": False}, {"confirm": 1}, {"confirm": "true"}, {"confirm": True, "unknown": 1}):
                    self.assertEqual(self.request("POST", path, headers={"X-SV-Token": "test-token"}, body=payload)[0], 400)
                self.assertEqual(self.request("POST", path, headers={"X-SV-Token": "test-token"},
                                              body=b'{"confirm":false,"confirm":true}')[0], 400)
                operation.assert_not_called()
                status, _, body = self.request("POST", path, headers={"X-SV-Token": "test-token"}, body={"confirm": True})
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["permanent"])
                operation.assert_called_once_with(*prefix, {"confirm": True})

    def test_audio_read_error_never_leaks_filesystem_path(self):
        self.service.read_audio.side_effect = OSError("private-path-should-not-leak")
        status, _, body = self.request("GET", "/audio/" + "a" * 32 + ".wav")
        self.assertEqual(status, 400)
        self.assertNotIn("private-path", body.decode())

    def test_default_platform_selection_route_uses_authenticated_settings_boundary(self):
        payload = {"id": "c" * 32, "revision": "test-revision"}
        self.service.set_default_model_platform.return_value = {"items": [], "defaultPlatformId": payload["id"], "revision": "new"}
        self.assertEqual(self.request("POST", "/api/model-platforms/default-selection", body=payload)[0], 403)
        self.service.set_default_model_platform.assert_not_called()
        status, _, body = self.request("POST", "/api/model-platforms/default-selection", headers={"X-SV-Token": "test-token"}, body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["defaultPlatformId"], payload["id"])
        self.service.set_default_model_platform.assert_called_once_with(payload)


if __name__ == "__main__":
    unittest.main()
