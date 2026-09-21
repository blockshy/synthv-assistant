"""多平台配置测试：临时 DATA、虚构密钥及加密替身，不接触用户真实凭据。"""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from synthv_assistant import platforms, settings
from synthv_assistant.operations import OperationLock


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = "sk-fictional-platform-unit-test-not-real"
        self.encrypted = {}
        self.real_encrypt, self.real_decrypt = settings._encrypt, settings._decrypt

        def protect(data):
            # 这是测试替身而非生产加密：磁盘仅存随机令牌，明文保留在本用例内存。
            token = b"test-protected-" + uuid.uuid4().hex.encode("ascii")
            self.encrypted[token] = data
            return token

        def unprotect(data):
            return self.encrypted[data]

        system_environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
        system_environment["SYNTHV_AUDIO_PROVIDER"] = "none"
        patches = [patch.object(platforms, "DATA", self.root), patch.object(settings, "DATA", self.root),
                   patch.dict(os.environ, system_environment, clear=True),
                   patch.object(settings, "_encrypt", side_effect=protect), patch.object(settings, "_decrypt", side_effect=unprotect)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def payload(self, **changes):
        value = {"name": "测试平台", "provider": "openai", "model": "vendor/model-v1",
                 "baseUrl": "https://MOCK.example:443/v1/", "timeoutSeconds": 45,
                 "revision": platforms.list_model_platforms()["revision"], "apiKey": self.key}
        value.update(changes)
        return value

    def create(self, **changes):
        return platforms.save_model_platform(self.payload(**changes))

    def test_empty_list_contains_unchanged_default_and_does_not_create_registry(self):
        result = platforms.list_model_platforms()
        self.assertEqual(result["defaultPlatformId"], "default")
        self.assertEqual(result["revision"], "empty")
        self.assertEqual([item["id"] for item in result["items"]], ["default"])
        self.assertEqual(result["items"][0]["revision"], "environment")
        self.assertEqual(platforms.get_default_model_platform_id(), "default")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_new_conversation_default_is_persisted_and_public_result_contains_no_key(self):
        # 偏好必须来自加密注册表而非进程全局状态；保存后再次读取仍能获得同一 ID。
        created = self.create()
        result = platforms.set_default_model_platform({"id": created["id"], "revision": created["revision"]})
        self.assertEqual(result["defaultPlatformId"], created["id"])
        self.assertNotEqual(result["revision"], created["revision"])
        self.assertEqual(platforms.get_default_model_platform_id(), created["id"])
        self.assertEqual(result, platforms.list_model_platforms())
        self.assertNotIn(self.key, json.dumps(result))
        self.assertNotIn(self.key.encode(), (self.root / "model-platforms.json").read_bytes())
        self.assertFalse((self.root / "audio-settings.json").exists())

    def test_default_selection_and_platform_edits_share_revision_conflicts(self):
        # 两个方向均验证 CAS，避免旧设置页覆盖最新偏好，或旧偏好页覆盖最新平台。
        created = self.create()
        stale_edit = self.payload(id=created["id"], name="不应覆盖")
        first_selection = {"id": created["id"], "revision": created["revision"]}
        selected = platforms.set_default_model_platform(first_selection)
        with self.assertRaises(platforms.PlatformConflictError):
            platforms.save_model_platform(stale_edit)
        with self.assertRaises(platforms.PlatformConflictError):
            platforms.set_default_model_platform(first_selection)
        self.create(name="新增平台")
        with self.assertRaises(platforms.PlatformConflictError):
            platforms.set_default_model_platform({"id": created["id"], "revision": selected["revision"]})
        self.assertEqual(platforms.get_default_model_platform_id(), created["id"])

    def test_ordinary_edits_preserve_preference_and_disabling_selected_platform_resets_it(self):
        chosen = self.create(name="新会话默认")
        platforms.set_default_model_platform({"id": chosen["id"], "revision": chosen["revision"]})
        other = self.create(name="其他平台")
        platforms.save_model_platform(self.payload(id=other["id"], provider="none"))
        platforms.save_model_platform(self.payload(id=chosen["id"], name="默认改名", model="another-model", apiKey=""))
        self.assertEqual(platforms.get_default_model_platform_id(), chosen["id"])
        # 回退和停用必须出现在同一个注册表版本中；即使旧默认未配置，也不改选其他平台。
        platforms.save_model_platform(self.payload(id=chosen["id"], provider="none"))
        result = platforms.list_model_platforms()
        self.assertEqual(result["defaultPlatformId"], "default")
        self.assertFalse(next(item for item in result["items"] if item["id"] == chosen["id"])["configured"])
        self.assertEqual(platforms.get_default_model_platform_id(), "default")

    def test_selecting_legacy_default_uses_registry_revision_without_changing_audio_settings(self):
        # 原设置拥有自己的版本；新会话偏好的 CAS 必须始终使用注册表版本。
        legacy = platforms.get_model_platform("default")
        configured = platforms.save_model_platform(self.payload(id="default", revision=legacy["revision"]))
        original_settings = (self.root / "audio-settings.json").read_bytes()
        chosen = self.create()
        result = platforms.set_default_model_platform({"id": chosen["id"], "revision": chosen["revision"]})
        with self.assertRaises(platforms.PlatformConflictError):
            platforms.set_default_model_platform({"id": "default", "revision": configured["revision"]})
        result = platforms.set_default_model_platform({"id": "default", "revision": result["revision"]})
        self.assertEqual(result["defaultPlatformId"], "default")
        self.assertEqual((self.root / "audio-settings.json").read_bytes(), original_settings)
        self.assertEqual(platforms.get_model_platform_snapshot("default")["key"], self.key)

    def test_default_selection_rejects_unknown_or_unconfigured_targets_and_invalid_payloads(self):
        disabled = self.create(provider="none")
        original = (self.root / "model-platforms.json").read_bytes()
        # 包含错误容器类型及额外凭据字段，确保验证既严格，也不会反射原始输入。
        invalid = [None, [], {}, {"id": "default"}, {"id": [], "revision": disabled["revision"]},
                   {"id": "default", "revision": False}, {"id": "default", "revision": disabled["revision"], "apiKey": self.key}]
        invalid.extend({"id": target, "revision": disabled["revision"]} for target in ("default", disabled["id"], "f" * 32))
        for payload in invalid:
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(platforms.PlatformError) as caught:
                platforms.set_default_model_platform(payload)
            self.assertNotIn(self.key, str(caught.exception))
            self.assertEqual((self.root / "model-platforms.json").read_bytes(), original)

    def test_old_registry_without_preference_is_read_only_compatible_then_upgrades_on_save(self):
        self.create()
        path = self.root / "model-platforms.json"
        envelope = json.loads(path.read_bytes())
        token = base64.b64decode(envelope["encrypted"])
        old_payload = json.loads(self.encrypted[token])
        del old_payload["defaultPlatformId"]
        self.encrypted[token] = json.dumps(old_payload).encode("utf-8")
        original = path.read_bytes()
        self.assertEqual(platforms.get_default_model_platform_id(), "default")
        self.assertEqual(platforms.list_model_platforms()["defaultPlatformId"], "default")
        self.assertEqual(path.read_bytes(), original)
        created = self.create(name="升级后保存")
        token = base64.b64decode(json.loads(path.read_bytes())["encrypted"])
        self.assertEqual(json.loads(self.encrypted[token])["defaultPlatformId"], "default")
        platforms.set_default_model_platform({"id": created["id"], "revision": created["revision"]})
        self.assertEqual(platforms.get_default_model_platform_id(), created["id"])

    def test_invalid_persisted_preference_fails_closed_without_silent_fallback(self):
        disabled = self.create(provider="none")
        path = self.root / "model-platforms.json"
        token = base64.b64decode(json.loads(path.read_bytes())["encrypted"])
        original = json.loads(self.encrypted[token])
        # 无效类型、悬空 ID 和停用目标都不是允许的历史格式，不可自动选中另一个供应商。
        for bad_id in (None, [], "unknown", "f" * 32, disabled["id"]):
            self.encrypted[token] = json.dumps({**original, "defaultPlatformId": bad_id}).encode("utf-8")
            for call in (platforms.get_default_model_platform_id, platforms.list_model_platforms,
                         lambda: platforms.set_default_model_platform({"id": "default", "revision": disabled["revision"]})):
                with self.subTest(identifier=bad_id), self.assertRaisesRegex(platforms.PlatformError, "损坏"):
                    call()

    def test_default_selection_uses_registry_lock_and_preserves_file_on_failed_atomic_replace(self):
        created = self.create()
        payload = {"id": created["id"], "revision": created["revision"]}
        original = (self.root / "model-platforms.json").read_bytes()
        with OperationLock(self.root / "model-platforms.lock"):
            with self.assertRaisesRegex(platforms.PlatformError, "正在保存"):
                platforms.set_default_model_platform(payload)
        for target in ("synthv_assistant.platforms.os.replace", "synthv_assistant.settings._encrypt"):
            with patch(target, side_effect=OSError(self.key)), self.assertRaises(platforms.PlatformError) as caught:
                platforms.set_default_model_platform(payload)
            self.assertNotIn(self.key, str(caught.exception))
            self.assertEqual((self.root / "model-platforms.json").read_bytes(), original)
        self.assertEqual(platforms.get_default_model_platform_id(), "default")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_create_get_snapshot_and_public_views_never_return_key(self):
        created = self.create()
        self.assertEqual(created["baseUrl"], "https://mock.example/v1")
        self.assertTrue(created["keyConfigured"])
        self.assertEqual(platforms.get_model_platform(created["id"]), created)
        self.assertEqual(platforms.get_model_platform_snapshot(created["id"])["key"], self.key)
        self.assertNotIn(self.key, json.dumps(platforms.list_model_platforms()))
        self.assertNotIn("key", created)
        self.assertNotIn(self.key.encode(), (self.root / "model-platforms.json").read_bytes())

    def test_default_edits_only_original_settings_file(self):
        existing = platforms.get_model_platform("default")
        public = platforms.save_model_platform(self.payload(id="default", revision=existing["revision"], name="会被忽略"))
        self.assertEqual(public["name"], "默认平台")
        self.assertEqual(public["revision"], settings.get_audio_settings()["revision"])
        original = (self.root / "audio-settings.json").read_bytes()
        self.create()
        self.assertEqual((self.root / "audio-settings.json").read_bytes(), original)
        self.assertEqual(platforms.get_model_platform_snapshot("default")["key"], self.key)

    def test_blank_key_retained_only_for_same_platform_provider_and_normalized_address(self):
        created = self.create()
        updated = platforms.save_model_platform(self.payload(id=created["id"], apiKey="", baseUrl="https://mock.example/v1", model="new-model"))
        self.assertEqual(platforms.get_model_platform_snapshot(created["id"])["key"], self.key)
        original = (self.root / "model-platforms.json").read_bytes()
        for changes in ({"baseUrl": "https://other.example/v1"}, {"provider": "gemini", "model": "gemini-test"}):
            with self.subTest(changes=changes), self.assertRaises(settings.SettingsError):
                platforms.save_model_platform(self.payload(id=created["id"], apiKey="", **changes))
            self.assertEqual((self.root / "model-platforms.json").read_bytes(), original)
        self.assertEqual(updated["model"], "new-model")

    def test_none_clears_key_without_affecting_other_platform(self):
        first, second = self.create(), self.create(name="第二平台", apiKey="fake-second-key")
        platforms.save_model_platform(self.payload(id=first["id"], provider="none"))
        self.assertEqual(platforms.get_model_platform_snapshot(first["id"])["key"], "")
        self.assertFalse(platforms.get_model_platform(first["id"])["configured"])
        self.assertEqual(platforms.get_model_platform_snapshot(second["id"])["key"], "fake-second-key")

    def test_registry_revision_is_shared_and_stale_save_does_not_overwrite(self):
        first = self.create()
        stale = self.payload(id=first["id"], name="过期修改")
        second = self.create(name="另一个平台")
        self.assertEqual(platforms.get_model_platform(first["id"])["revision"], second["revision"])
        with self.assertRaises(platforms.PlatformConflictError):
            platforms.save_model_platform(stale)
        self.assertEqual(platforms.get_model_platform(first["id"])["name"], "测试平台")

    def test_strict_fields_identifiers_names_and_numbers_preserve_registry(self):
        self.create()
        original = (self.root / "model-platforms.json").read_bytes()
        for change in ({"name": " "}, {"name": "a" * 81}, {"name": "bad\nname"}, {"timeoutSeconds": True},
                       {"timeoutSeconds": 10**1000}, {"unknown": "never-echo"}, {"id": "../escape"},
                       {"baseUrl": "http://mock.example/v1"}, {"apiKey": "key\nnewline"}, {"provider": []}):
            with self.subTest(change=str(change)[:40]), self.assertRaises(settings.SettingsError) as caught:
                platforms.save_model_platform(self.payload(**change))
            self.assertNotIn(self.key, str(caught.exception))
            self.assertEqual((self.root / "model-platforms.json").read_bytes(), original)
        for identifier in (None, [], "unknown", "f" * 32):
            with self.assertRaises(platforms.PlatformError):
                platforms.get_model_platform(identifier)

    def test_twenty_platform_limit_includes_default(self):
        with patch.object(platforms, "MAX_PLATFORMS", 3):
            first, second = self.create(), self.create(name="第二")
            with self.assertRaisesRegex(platforms.PlatformError, "20"):
                self.create(name="超出上限")
            platforms.save_model_platform(self.payload(id=first["id"], name="仍能修改", apiKey=""))
            self.assertEqual(len(platforms.list_model_platforms()["items"]), 3)

    def test_atomic_replace_and_encryption_failure_keep_original_file(self):
        created = self.create()
        original = (self.root / "model-platforms.json").read_bytes()
        for target, failure in (("synthv_assistant.platforms.os.replace", OSError("private-secret-path")),
                                ("synthv_assistant.settings._encrypt", settings.SettingsError("private-secret-key"))):
            with patch(target, side_effect=failure), self.assertRaises(settings.SettingsError) as caught:
                platforms.save_model_platform(self.payload(id=created["id"], name="失败修改"))
            self.assertNotIn("private-secret", str(caught.exception))
            self.assertEqual((self.root / "model-platforms.json").read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_corrupt_registry_fails_closed_but_default_remains_accessible(self):
        self.create()
        (self.root / "model-platforms.json").write_text("broken", encoding="utf-8")
        for call in (platforms.list_model_platforms, platforms.get_default_model_platform_id,
                     lambda: platforms.get_model_platform("a" * 32)):
            with self.assertRaises(platforms.PlatformError):
                call()
        self.assertEqual(platforms.get_model_platform("default")["id"], "default")
        with self.assertRaises(platforms.PlatformError):
            platforms.save_model_platform({"name": "不可覆盖", "provider": "none", "model": "", "baseUrl": "",
                                           "timeoutSeconds": 60, "revision": "empty"})
        self.assertEqual((self.root / "model-platforms.json").read_text(), "broken")

    def test_envelope_revision_and_protected_kind_are_verified(self):
        self.create()
        path = self.root / "model-platforms.json"
        envelope = json.loads(path.read_bytes())
        envelope["revision"] = "f" * 32
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(platforms.PlatformError):
            platforms.list_model_platforms()

    def test_registry_lock_is_independent_and_nonblocking(self):
        payload = self.payload()
        with OperationLock(self.root / "model-platforms.lock"):
            with self.assertRaisesRegex(platforms.PlatformError, "正在保存"):
                platforms.save_model_platform(payload)
        with OperationLock(self.root / "settings.lock"), OperationLock(self.root / "operation.lock"):
            self.assertTrue(platforms.save_model_platform(payload)["configured"])

    @unittest.skipUnless(os.name == "nt", "DPAPI 仅支持 Windows")
    def test_real_dpapi_ciphertext_can_be_read_by_another_process(self):
        # 唯一真实系统加密用例仍只处理本测试的虚构 key 和临时文件，不访问实际 DATA。
        with patch.object(settings, "_encrypt", side_effect=self.real_encrypt), patch.object(settings, "_decrypt", side_effect=self.real_decrypt):
            item = self.create()
            platforms.set_default_model_platform({"id": item["id"], "revision": item["revision"]})
            self.assertNotIn(self.key.encode(), (self.root / "model-platforms.json").read_bytes())
            self.assertEqual(platforms.get_model_platform_snapshot(item["id"])["key"], self.key)
            code = ("import json,sys; from synthv_assistant.platforms import get_model_platform_snapshot,get_default_model_platform_id; "
                    "s=get_model_platform_snapshot(sys.argv[1]); print(json.dumps({'name':s['name'],'configured':s['configured'],"
                    "'defaultPlatformId':get_default_model_platform_id()}))")
            environment = {**os.environ, "SYNTHV_ASSISTANT_DATA": str(self.root)}
            child = subprocess.run([sys.executable, "-c", code, item["id"]], cwd=str(Path(__file__).resolve().parent.parent),
                                   env=environment, capture_output=True, text=True, timeout=15, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(child.returncode, 0, "隔离子进程无法读取测试平台注册表。")
            self.assertTrue(json.loads(child.stdout)["configured"])
            self.assertEqual(json.loads(child.stdout)["defaultPlatformId"], item["id"])


if __name__ == "__main__":
    unittest.main()
