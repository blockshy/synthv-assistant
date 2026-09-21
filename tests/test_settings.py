"""即时模型配置测试：仅使用临时目录和假密钥，不联网、不读取用户配置。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from synthv_assistant import settings
from synthv_assistant.operations import OperationLock


class SettingsBase(unittest.TestCase):
    """给所有用例注入隔离目录，并用空环境阻断真实 API key 的来源。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "audio-settings.json"
        self.data_patch = patch("synthv_assistant.settings.DATA", self.root)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        # Windows 子进程加载系统 DLL 需要 SystemRoot；仅保留系统目录变量，
        # 不继承任何 API key、代理认证或供应商配置。
        system_environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ}
        self.environment = patch.dict(os.environ, system_environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def payload(self, **overrides):
        """构造完整表单；每次先读取 revision，模拟页面保存前的配置状态。"""
        return {"provider": "openai", "model": "gpt-audio-1.5", "baseUrl": "https://api.openai.com/v1",
                "timeoutSeconds": 60, "apiKey": "fake-settings-key-for-tests", "revision": settings.get_audio_settings()["revision"],
                **overrides}


class SettingsValidationTests(SettingsBase):
    """不要求 Windows 的输入校验与失败关闭测试。"""

    def test_openai_routed_model_names_and_gemini_url_boundary(self):
        # OpenAI 兼容路由模型是 JSON 数据；Gemini 模型在 URL 中，拒绝路径段。
        values = settings._validate_values("openai", "vendor/model:free", "https://example.com/v1", 60, "fake")
        self.assertEqual(values["model"], "vendor/model:free")
        for provider, model in (("gemini", "vendor/model"), ("openai", "https://example.com/model"),
                                ("openai", "../secret"), ("openai", "model?api_key=bad")):
            with self.subTest(provider=provider, model=model), self.assertRaises(settings.SettingsError):
                settings._validate_values(provider, model, "https://example.com/v1", 60, "fake")

    def test_environment_is_only_initial_fallback_and_is_redacted(self):
        os.environ.update({"SYNTHV_AUDIO_PROVIDER": "openai", "OPENAI_API_KEY": "fake-environment-key"})
        public = settings.get_audio_settings()
        self.assertEqual(public["source"], "environment")
        self.assertEqual(public["revision"], "environment")
        self.assertEqual(public["model"], "gpt-audio-1.5")
        self.assertTrue(public["keyConfigured"])
        self.assertNotIn("fake-environment-key", json.dumps(public))
        self.assertFalse(self.path.exists())

    def test_malformed_environment_is_disabled_without_echo(self):
        os.environ.update({"SYNTHV_AUDIO_PROVIDER": "openai", "OPENAI_API_KEY": "fake-environment-key",
                           "SYNTHV_AUDIO_BASE_URL": "http://fake-secret-in-url.example"})
        public = settings.get_audio_settings()
        self.assertFalse(public["configured"])
        self.assertEqual(public["provider"], "none")
        self.assertNotIn("fake-secret", json.dumps(public))

    def test_corrupt_file_blocks_environment_fallback(self):
        os.environ.update({"SYNTHV_AUDIO_PROVIDER": "openai", "OPENAI_API_KEY": "fake-environment-key"})
        for raw in (b"not-json", b"{}", b"null", b"[]"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                public = settings.get_audio_settings()
                self.assertFalse(public["configured"])
                self.assertFalse(public["keyConfigured"])
                self.assertEqual(public["source"], "local")
                self.assertEqual(public["revision"], "invalid")

    def test_all_validation_errors_are_fixed_and_do_not_write(self):
        invalid_fields = [
            {"provider": "fake-secret-provider"}, {"model": "fake-secret /invalid"},
            {"baseUrl": "http://fake-secret.example"}, {"baseUrl": "https://user:fake-secret@example.com/v1"},
            {"baseUrl": "https://example.com/v1?api_key=fake-secret"},
            {"baseUrl": "https://example.com:999999/v1"}, {"baseUrl": "https://example.com\\fake-secret"},
            {"timeoutSeconds": float("nan")}, {"timeoutSeconds": True}, {"timeoutSeconds": 999},
            {"timeoutSeconds": 10**1000},
            {"apiKey": "fake-secret\ninvalid"}, {"apiKey": {"fake-secret": True}},
        ]
        for override in invalid_fields:
            with self.subTest(field=list(override)), self.assertRaises(settings.SettingsError) as failure:
                settings.update_audio_settings(self.payload(**override))
            self.assertNotIn("fake-secret", str(failure.exception))
            self.assertFalse(self.path.exists())

    def test_missing_revision_and_unknown_fields_are_rejected(self):
        payload = self.payload()
        del payload["revision"]
        with self.assertRaises(settings.SettingsError):
            settings.update_audio_settings(payload)
        with self.assertRaises(settings.SettingsError):
            settings.update_audio_settings(self.payload(unrecognized="fake-secret"))

    def test_settings_lock_is_independent_and_conflicts_are_clear(self):
        with OperationLock(self.root / "settings.lock"):
            with self.assertRaisesRegex(settings.SettingsError, "正在保存"):
                settings.update_audio_settings(self.payload())
            with self.assertRaisesRegex(settings.SettingsError, "正在保存"):
                settings.clear_audio_settings()


@unittest.skipUnless(os.name == "nt", "真实 DPAPI 仅在 Windows 上可用。")
class SettingsDpapiTests(SettingsBase):
    """调用系统 DPAPI 验证落盘与恢复，所有凭据都是专门构造的测试字符串。"""

    def test_full_configuration_is_encrypted_and_public_results_are_redacted(self):
        public = settings.update_audio_settings(self.payload(baseUrl="https://private-test.example/v1"))
        stored = self.path.read_bytes()
        self.assertNotIn(b"fake-settings-key-for-tests", stored)
        self.assertNotIn(b"private-test.example", stored)
        self.assertNotIn(b"gpt-audio-1.5", stored)
        self.assertNotIn("apiKey", public)
        self.assertNotIn("fake-settings-key-for-tests", json.dumps(public))
        snapshot = settings.get_audio_configuration_snapshot()
        self.assertEqual(snapshot["key"], "fake-settings-key-for-tests")
        self.assertEqual(public["source"], "local")
        self.assertEqual(public["storage"], "windows-dpapi")
        self.assertTrue(public["configured"])
        self.assertEqual(settings.get_audio_settings(), public)

    def test_saved_configuration_is_visible_to_another_process_immediately(self):
        first = settings.update_audio_settings(self.payload(timeoutSeconds=31))
        script = """
import json
from pathlib import Path
import sys
from synthv_assistant import settings
settings.DATA = Path(sys.argv[1])
print(json.dumps(settings.get_audio_settings()))
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                timeout=10, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        observed = json.loads(result.stdout)
        self.assertEqual(observed["revision"], first["revision"])
        self.assertEqual(observed["timeoutSeconds"], 31)
        self.assertTrue(observed["keyConfigured"])
        self.assertNotIn(b"fake-settings-key-for-tests", result.stdout)

    def test_empty_key_preserves_only_same_provider_and_normalized_endpoint(self):
        settings.update_audio_settings(self.payload())
        result = settings.update_audio_settings(self.payload(apiKey="", baseUrl="https://API.OPENAI.COM:443/v1/", timeoutSeconds=42))
        self.assertTrue(result["keyConfigured"])
        self.assertEqual(result["baseUrl"], "https://api.openai.com/v1")
        self.assertEqual(settings.get_audio_configuration_snapshot()["key"], "fake-settings-key-for-tests")
        self.assertEqual(result["timeoutSeconds"], 42)

    def test_changing_endpoint_or_provider_requires_new_key_and_preserves_old_file(self):
        settings.update_audio_settings(self.payload())
        original = self.path.read_bytes()
        for override in ({"baseUrl": "https://another.example/v1"}, {"provider": "gemini", "baseUrl": "https://api.openai.com/v1"}):
            with self.subTest(override=override), self.assertRaisesRegex(settings.SettingsError, "重新填写"):
                settings.update_audio_settings(self.payload(apiKey="", **override))
            self.assertEqual(self.path.read_bytes(), original)
        changed = settings.update_audio_settings(self.payload(baseUrl="https://another.example/v1", apiKey="fake-new-endpoint-key"))
        self.assertEqual(changed["baseUrl"], "https://another.example/v1")
        self.assertEqual(settings.get_audio_configuration_snapshot()["key"], "fake-new-endpoint-key")

    def test_revision_conflict_preserves_latest_configuration(self):
        stale_payload = self.payload()
        current = settings.update_audio_settings(stale_payload)
        original = self.path.read_bytes()
        with self.assertRaises(settings.SettingsConflictError):
            settings.update_audio_settings(stale_payload)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(settings.get_audio_settings()["revision"], current["revision"])

    def test_clear_and_none_override_environment_keys_without_deleting_file(self):
        os.environ.update({"SYNTHV_AUDIO_PROVIDER": "openai", "OPENAI_API_KEY": "fake-environment-key"})
        settings.update_audio_settings(self.payload())
        cleared = settings.clear_audio_settings()
        self.assertEqual(cleared["provider"], "none")
        self.assertFalse(cleared["keyConfigured"])
        self.assertEqual(cleared["source"], "local")
        self.assertTrue(self.path.exists())
        self.assertEqual(settings.get_audio_configuration_snapshot()["key"], "")
        settings.update_audio_settings(self.payload())
        disabled = settings.update_audio_settings(self.payload(provider="none", apiKey=""))
        self.assertEqual(disabled["provider"], "none")
        self.assertFalse(settings.get_audio_settings()["configured"])

    def test_environment_key_can_be_migrated_only_to_same_endpoint(self):
        os.environ.update({"SYNTHV_AUDIO_PROVIDER": "openai", "OPENAI_API_KEY": "fake-environment-key"})
        migrated = settings.update_audio_settings(self.payload(apiKey=""))
        self.assertEqual(migrated["source"], "local")
        self.assertEqual(settings.get_audio_configuration_snapshot()["key"], "fake-environment-key")

    def test_validation_and_encryption_failure_leave_original_unchanged(self):
        settings.update_audio_settings(self.payload())
        original = self.path.read_bytes()
        with self.assertRaises(settings.SettingsError):
            settings.update_audio_settings(self.payload(timeoutSeconds=-1))
        with patch("synthv_assistant.settings._encrypt", side_effect=settings.SettingsError("模拟 DPAPI 失败")):
            with self.assertRaises(settings.SettingsError):
                settings.update_audio_settings(self.payload(apiKey="fake-rotated-key"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_corrupted_ciphertext_fails_closed_then_can_be_replaced(self):
        settings.update_audio_settings(self.payload())
        os.environ.update({"SYNTHV_AUDIO_PROVIDER": "openai", "OPENAI_API_KEY": "fake-environment-key"})
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        envelope["encrypted"] = "broken-ciphertext"
        self.path.write_text(json.dumps(envelope), encoding="utf-8")
        self.assertFalse(settings.get_audio_settings()["configured"])
        repaired = settings.update_audio_settings(self.payload(apiKey="fake-repair-key"))
        self.assertTrue(repaired["configured"])
        self.assertEqual(settings.get_audio_configuration_snapshot()["key"], "fake-repair-key")

    def test_engineering_operation_lock_does_not_block_settings_updates(self):
        with OperationLock(self.root / "operation.lock"):
            saved = settings.update_audio_settings(self.payload())
        self.assertTrue(saved["configured"])


if __name__ == "__main__":
    unittest.main()
