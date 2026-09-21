"""资料覆盖层测试：全部存储在临时目录，不读取或改变用户真实资料。"""

import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from synthv_assistant.metadata import LibraryMetadata, MetadataError


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = LibraryMetadata(self.root)
        self.identifier = "a" * 32

    def test_legacy_defaults_and_updates_are_immediately_visible_across_instances(self):
        first = self.library.read("upload", self.identifier)
        self.assertEqual((first["note"], first["starred"]), ("", False))
        self.library.update("upload", self.identifier, {"note": "仅本地备注", "starred": True})
        other = LibraryMetadata(self.root)
        self.assertEqual(other.read("upload", self.identifier)["note"], "仅本地备注")
        other.update("upload", self.identifier, {"label": "新的片段名称"})
        state = self.library.read("upload", self.identifier)
        self.assertTrue(state["starred"])
        self.assertEqual(state["note"], "仅本地备注")
        self.assertEqual(state["label"], "新的片段名称")

    def test_soft_delete_restore_retains_notes_and_does_not_touch_original(self):
        original = self.root / "original.wav"
        original.write_bytes(b"immutable-original")
        self.library.update("recording", self.identifier, {"note": "保留备注", "starred": True})
        self.assertTrue(self.library.delete("recording", self.identifier, "录音片段")["deleted"])
        item = self.library.trash()["items"][0]
        self.assertEqual((item["note"], item["label"], item["kind"]), ("保留备注", "录音片段", "recording"))
        with self.assertRaises(MetadataError):
            self.library.assert_available("recording", self.identifier)
        stamp = item["deletedAt"]
        self.library.delete("recording", self.identifier, "重复删除")
        self.assertEqual(self.library.trash()["items"][0]["deletedAt"], stamp)
        self.assertTrue(self.library.restore("recording", self.identifier)["restored"])
        self.assertEqual(self.library.trash()["items"], [])
        self.assertTrue(self.library.read("recording", self.identifier)["starred"])
        self.assertEqual(original.read_bytes(), b"immutable-original")

    def test_all_invalid_patches_leave_previous_snapshot_unchanged(self):
        self.library.update("upload", self.identifier, {"label": "原名称", "note": "原备注"})
        original = self.library.read("upload", self.identifier)
        payloads = ({}, None, [], {"title": "会话字段不能用于音频"}, {"starred": 1},
                    {"starred": "false"}, {"label": " "}, {"label": "a" * 101},
                    {"label": "含\n换行"}, {"note": "a" * 2001}, {"note": None},
                    {"note": "\x00"}, {"label": "部分不能先保存", "unknown": True})
        for payload in payloads:
            with self.subTest(payload=str(payload)[:30]), self.assertRaises(MetadataError):
                self.library.update("upload", self.identifier, payload)
            self.assertEqual(self.library.read("upload", self.identifier), original)
        for payload in ({"title": "a" * 81}, {"label": "错误字段"}):
            with self.assertRaises(MetadataError):
                self.library.update("conversation", self.identifier, payload)

    def test_identifier_kind_and_asset_lists_are_strict(self):
        for kind, identifier in (("unknown", self.identifier), ([], self.identifier),
                                 ("upload", "../escape"), ("upload", None)):
            with self.subTest(kind=kind), self.assertRaises(MetadataError):
                self.library.read(kind, identifier)
        for attachments in (None, [{"kind": "conversation", "id": self.identifier}],
                            [{"kind": "upload", "id": self.identifier, "path": "forbidden"}],
                            [{"kind": "upload", "id": self.identifier}] * 2):
            with self.assertRaises(MetadataError), self.library.use_assets(attachments):
                pass

    def test_running_audio_request_rejects_delete_and_update_without_waiting(self):
        attachment = {"kind": "upload", "id": self.identifier}
        with self.library.use_assets([attachment]):
            other = LibraryMetadata(self.root)
            with self.assertRaisesRegex(MetadataError, "正在处理"):
                other.delete("upload", self.identifier, "使用中")
            with self.assertRaises(MetadataError):
                other.update("upload", self.identifier, {"note": "不得保存"})
        self.assertIsNone(self.library.read("upload", self.identifier)["deletedAt"])
        self.library.delete("upload", self.identifier, "结束后可删除")

    def test_atomic_replace_failure_hides_paths_and_preserves_existing_bytes(self):
        self.library.update("upload", self.identifier, {"note": "旧版"})
        path = self.root / "metadata" / ("upload-" + self.identifier + ".json")
        original = path.read_bytes()
        with patch("synthv_assistant.metadata.os.replace", side_effect=OSError("private-secret-path")):
            with self.assertRaises(MetadataError) as caught:
                self.library.update("upload", self.identifier, {"note": "新版"})
        self.assertNotIn("private-secret-path", str(caught.exception))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_corrupt_metadata_fails_closed(self):
        self.library.delete("upload", self.identifier, "已删除")
        path = self.root / "metadata" / ("upload-" + self.identifier + ".json")
        path.write_text("{broken", encoding="utf-8")
        for operation in (lambda: self.library.assert_available("upload", self.identifier),
                          lambda: self.library.trash(),
                          lambda: self.library.update("upload", self.identifier, {"starred": True})):
            with self.assertRaises(MetadataError):
                operation()

    def test_recording_legacy_technical_note_is_separate_from_user_note(self):
        item = {"id": self.identifier, "note": "旧采集技术说明", "label": "录音"}
        public = self.library.decorate("recording", item)
        self.assertEqual(public["note"], "")
        self.assertEqual(public["captureNote"], "旧采集技术说明")
        self.library.update("recording", self.identifier, {"note": "用户的私有备注"})
        public = self.library.decorate("recording", item)
        self.assertEqual(public["note"], "用户的私有备注")
        self.assertEqual(public["captureNote"], "旧采集技术说明")

    def resource_files(self, kind="upload"):
        """只创建临时 UUID 资源，旁边的哨兵文件用于证明删除没有级联。"""
        folder = self.root / {"upload": "uploads", "recording": "recordings", "conversation": "conversations"}[kind]
        folder.mkdir(exist_ok=True)
        extensions = (".json",) if kind == "conversation" else (".wav", ".json")
        paths = [folder / (self.identifier + suffix) for suffix in extensions]
        for path in paths:
            path.write_bytes(b"private-user-content")
        self.library.update(kind, self.identifier, {"note": "private-local-note", "starred": True})
        return paths

    def test_purge_removes_only_owned_files_and_keeps_content_free_marker(self):
        for kind in ("upload", "recording", "conversation"):
            with self.subTest(kind=kind):
                files = self.resource_files(kind)
                sentinel = files[0].parent / ("b" * 32 + ".json")
                sentinel.write_bytes(b"other-user-content")
                result = self.library.purge(kind, self.identifier, {"confirm": True})
                self.assertEqual(result, {"deleted": True, "permanent": True, "kind": kind, "id": self.identifier})
                self.assertTrue(all(not path.exists() for path in files))
                self.assertFalse(self.library._path(kind, self.identifier).exists())
                self.assertEqual(sentinel.read_bytes(), b"other-user-content")
                marker = self.library._purge_path(kind, self.identifier).read_bytes()
                self.assertNotIn(b"private", marker)
                self.assertLess(len(marker), 400)
                self.assertTrue((self.root / "asset-locks" / (kind + "-" + self.identifier + ".lock")).is_file())
                self.assertEqual(self.library.trash(), {"items": []})
                # 成功后的相同请求可以安全重试，不会访问其他资源。
                self.assertEqual(self.library.purge(kind, self.identifier, {"confirm": True}), result)
                for operation in (lambda: self.library.restore(kind, self.identifier),
                                  lambda: self.library.assert_available(kind, self.identifier),
                                  lambda: self.library.update(kind, self.identifier, {"note": "复活"}),
                                  lambda: self.library.delete(kind, self.identifier, "复活")):
                    with self.assertRaisesRegex(MetadataError, "永久"):
                        operation()

    def test_purge_requires_exact_confirmation_before_touching_content(self):
        files = self.resource_files()
        for payload in ({}, None, [], {"confirm": False}, {"confirm": 1}, {"confirm": "true"},
                        {"confirm": True, "path": "arbitrary"}):
            with self.subTest(payload=payload), self.assertRaises(MetadataError):
                self.library.purge("upload", self.identifier, payload)
            self.assertTrue(all(path.exists() for path in files))
        self.assertFalse(self.library.purges.exists())

    def test_trash_purge_refuses_active_resource_and_accepts_deleted_resource(self):
        files = self.resource_files()
        with self.assertRaisesRegex(MetadataError, "不在回收站"):
            self.library.purge("upload", self.identifier, {"confirm": True}, require_trash=True)
        self.assertTrue(all(path.exists() for path in files))
        self.library.delete("upload", self.identifier, "回收站音频")
        self.library.purge("upload", self.identifier, {"confirm": True}, require_trash=True)
        self.assertEqual(self.library.trash()["items"], [])

    def test_failed_unlink_is_unrestorable_and_retry_removes_remaining_content(self):
        files = self.resource_files()
        unlink = Path.unlink

        def fail_index(path, *args, **kwargs):
            if path == files[1]:
                raise PermissionError("private-absolute-path")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_index):
            with self.assertRaisesRegex(MetadataError, "未完成.*不能恢复") as caught:
                self.library.purge("upload", self.identifier, {"confirm": True})
        self.assertNotIn("private-absolute-path", str(caught.exception))
        self.assertFalse(files[0].exists())
        self.assertTrue(files[1].exists())
        pending = self.library.trash()["items"][0]
        self.assertEqual((pending["permanent"], pending["purgePending"], pending["restorable"]), (True, True, False))
        self.assertNotIn("private", json.dumps(pending))
        with self.assertRaisesRegex(MetadataError, "无法恢复"):
            self.library.restore("upload", self.identifier)
        self.library.purge("upload", self.identifier, {"confirm": True}, require_trash=True)
        self.assertTrue(all(not path.exists() for path in files))
        self.assertEqual(self.library.trash()["items"], [])

    def test_marker_commit_failure_deletes_nothing(self):
        files = self.resource_files()
        with patch("synthv_assistant.metadata.os.replace", side_effect=PermissionError("private-error")):
            with self.assertRaisesRegex(MetadataError, "尚未删除"):
                self.library.purge("upload", self.identifier, {"confirm": True})
        self.assertTrue(all(path.exists() for path in files))
        self.assertFalse(self.library.read("upload", self.identifier).get("permanent"))

    def test_final_marker_failure_remains_pending_until_retry(self):
        files = self.resource_files()
        write = self.library._write_purge

        def fail_complete(marker):
            if marker["state"] == "complete":
                raise OSError("private-error")
            write(marker)

        with patch.object(self.library, "_write_purge", side_effect=fail_complete):
            with self.assertRaisesRegex(MetadataError, "不能恢复"):
                self.library.purge("upload", self.identifier, {"confirm": True})
        self.assertTrue(all(not path.exists() for path in files))
        self.assertTrue(self.library.read("upload", self.identifier)["purgePending"])
        self.library.purge("upload", self.identifier, {"confirm": True})
        self.assertFalse(self.library.read("upload", self.identifier)["purgePending"])

    def test_asset_lock_blocks_purge_until_model_or_capture_finishes(self):
        files = self.resource_files("recording")
        with self.library.use_assets([{"kind": "recording", "id": self.identifier}]):
            with self.assertRaisesRegex(MetadataError, "正在处理"):
                LibraryMetadata(self.root).purge("recording", self.identifier, {"confirm": True})
        self.assertTrue(all(path.exists() for path in files))

    def test_reparse_directory_or_target_is_rejected_before_any_deletion(self):
        files = self.resource_files()
        lstat = Path.lstat
        for unsafe in (files[0].parent, files[0], self.root / "metadata", self.root / "asset-locks"):
            def fake_lstat(path, *args, **kwargs):
                if path == unsafe:
                    return SimpleNamespace(st_mode=stat.S_IFDIR if path.suffix == "" else stat.S_IFREG,
                                           st_file_attributes=0x400)
                return lstat(path, *args, **kwargs)
            with self.subTest(unsafe=unsafe.name), patch.object(Path, "lstat", fake_lstat):
                with self.assertRaises(MetadataError):
                    self.library.purge("upload", self.identifier, {"confirm": True})
            self.assertTrue(all(path.exists() for path in files))
        with self.assertRaises(MetadataError):
            self.library.checked_path(self.root.parent / "outside.wav")

    def test_real_symlink_target_is_never_followed(self):
        files = self.resource_files()
        outside = self.root / "unrelated-original.wav"
        outside.write_bytes(b"must-remain")
        files[0].unlink()
        try:
            os.symlink(outside, files[0])
        except OSError:
            self.skipTest("当前账户没有创建符号链接的权限；reparse 属性校验另有无条件测试。")
        with self.assertRaises(MetadataError):
            self.library.purge("upload", self.identifier, {"confirm": True})
        self.assertEqual(outside.read_bytes(), b"must-remain")
        self.assertTrue(files[1].exists())


if __name__ == "__main__":
    unittest.main()
