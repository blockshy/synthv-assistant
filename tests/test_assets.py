"""音频素材边界验证：全部使用临时合成波形，不接触真实歌曲或云服务。"""

import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from synthv_assistant import assets


def pcm_bytes(seconds=1, rate=8000):
    """生成可重复的整数 PCM 测试片段，不依赖外部音频资源。"""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\x10\x00" * int(seconds * rate))
    return stream.getvalue()


class AssetsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        for name, value in (("DATA", self.root), ("RECORDINGS", self.recordings)):
            replacement = patch.object(assets, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_upload_is_local_and_filename_never_controls_path(self):
        item = assets.save_upload("../../我的片段.wav", pcm_bytes())
        self.assertEqual(item["name"], "我的片段.wav")
        self.assertEqual(assets.upload_path(item["id"]).parent, self.root / "uploads")
        self.assertEqual(assets.list_uploads()["items"], [item])
        self.assertNotIn(str(self.root), json.dumps(item))
        paths, metadata = assets.resolve_audio_attachments([{"kind": "upload", "id": item["id"]}])
        self.assertEqual(paths, [assets.upload_path(item["id"])])
        self.assertEqual(metadata, [item])

    def test_empty_attachments_do_not_read_or_load_audio(self):
        with patch.object(assets, "upload_path") as upload, patch.object(assets, "get_upload") as metadata:
            self.assertEqual(assets.resolve_audio_attachments([]), ([], []))
        upload.assert_not_called()
        metadata.assert_not_called()

    def test_invalid_or_too_long_files_leave_no_public_asset(self):
        for name, data in (("song.exe", pcm_bytes()), ("bad.wav", b"invalid" * 20),
                           ("long.wav", pcm_bytes(61)), ("zero.wav", pcm_bytes(0)),
                           ("huge.wav", b"a" * (assets.MAX_UPLOAD_BYTES + 1))):
            with self.subTest(name=name), self.assertRaises(ValueError):
                assets.save_upload(name, data)
        self.assertEqual(assets.list_uploads(), {"items": []})
        self.assertEqual(list((self.root / "uploads").glob("*")), [])

    def test_attachment_identifiers_types_and_duplicates_are_checked(self):
        item = assets.save_upload("sample.wav", pcm_bytes())
        valid = {"kind": "upload", "id": item["id"]}
        for attachments in ([valid, valid], [valid] * 3, [{"kind": [], "id": item["id"]}],
                            [{"kind": "upload", "id": "../../secrets"}], [{**valid, "path": "secret"}], None):
            with self.subTest(attachments=attachments), self.assertRaises(ValueError):
                assets.resolve_audio_attachments(attachments)

    def test_recording_metadata_does_not_export_project_paths(self):
        identifier = "a" * 32
        (self.recordings / (identifier + ".wav")).write_bytes(pcm_bytes())
        (self.recordings / (identifier + ".json")).write_text(json.dumps({
            "id": identifier, "label": "工程录音", "durationSeconds": 1,
            "projectFile": "private-song.svp", "capture": {"pid": 123}}), encoding="utf-8")
        _, metadata = assets.resolve_audio_attachments([{"kind": "recording", "id": identifier}])
        self.assertNotIn("private-song", json.dumps(metadata))
        self.assertNotIn("pid", json.dumps(metadata))

    def test_mp3_without_ffmpeg_has_actionable_error(self):
        with patch.object(assets.shutil, "which", return_value=None), self.assertRaisesRegex(ValueError, "FFmpeg"):
            assets.save_upload("sample.mp3", b"fake mp3" * 20)
        self.assertEqual(assets.list_uploads()["items"], [])

    def test_audio_metadata_soft_delete_restore_and_path_rejection(self):
        item = assets.save_upload("sample.wav", pcm_bytes())
        path = assets.upload_path(item["id"])
        original = path.read_bytes()
        updated = assets.update_asset_metadata("upload", item["id"], {"label": "副歌", "note": "本地备注", "starred": True})
        self.assertEqual((updated["label"], updated["note"], updated["starred"]), ("副歌", "本地备注", True))
        assets.delete_asset("upload", item["id"])
        self.assertEqual(assets.list_uploads()["items"], [])
        self.assertEqual(path.read_bytes(), original)
        for operation in (lambda: assets.upload_path(item["id"]),
                          lambda: assets.resolve_audio_attachments([{"kind": "upload", "id": item["id"]}]),
                          lambda: assets.update_asset_metadata("upload", item["id"], {"starred": False})):
            with self.assertRaises(ValueError):
                operation()
        assets.restore_asset("upload", item["id"])
        self.assertEqual(assets.get_upload(item["id"])["note"], "本地备注")
        self.assertEqual(path.read_bytes(), original)

    def test_deleted_upload_releases_active_capacity_and_restore_checks_limit(self):
        with patch.object(assets, "MAX_UPLOADS", 1):
            first = assets.save_upload("first.wav", pcm_bytes())
            assets.delete_asset("upload", first["id"])
            second = assets.save_upload("second.wav", pcm_bytes())
            with self.assertRaises(ValueError):
                assets.restore_asset("upload", first["id"])
            assets.delete_asset("upload", second["id"])
            assets.restore_asset("upload", first["id"])
            self.assertEqual([item["id"] for item in assets.list_uploads()["items"]], [first["id"]])

    def test_deleted_recording_cannot_be_attached_but_can_be_restored(self):
        identifier = "b" * 32
        (self.recordings / (identifier + ".wav")).write_bytes(pcm_bytes())
        (self.recordings / (identifier + ".json")).write_text(json.dumps({"id": identifier, "label": "录音", "note": "采集说明"}), encoding="utf-8")
        updated = assets.update_asset_metadata("recording", identifier, {"note": "本地私有备注"})
        self.assertEqual(updated["captureNote"], "采集说明")
        self.assertNotIn(str(self.recordings), json.dumps(updated))
        assets.delete_asset("recording", identifier)
        with self.assertRaises(ValueError):
            assets.resolve_audio_attachments([{"kind": "recording", "id": identifier}])
        assets.restore_asset("recording", identifier)
        self.assertEqual(assets.resolve_audio_attachments([{"kind": "recording", "id": identifier}])[1][0]["note"], "本地私有备注")

    @unittest.skipUnless(shutil.which("ffmpeg"), "未安装 FFmpeg，跳过真实转码验证")
    def test_real_mp3_converts_to_playable_pcm_wav(self):
        # 编码与解码均只针对本用例创建的合成音频，验证实际 Windows 子进程参数。
        source, encoded = self.root / "source.wav", self.root / "source.mp3"
        source.write_bytes(pcm_bytes())
        subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-y", "-i", str(source), str(encoded)], check=True, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        item = assets.save_upload("合成样本.mp3", encoded.read_bytes())
        with wave.open(str(assets.upload_path(item["id"])), "rb") as audio:
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.getnchannels(), 2)
            self.assertEqual(audio.getframerate(), 44100)
        self.assertAlmostEqual(item["durationSeconds"], 1, places=1)

    def test_direct_purge_upload_removes_wav_index_and_local_notes(self):
        item = assets.save_upload("临时合成音频.wav", pcm_bytes())
        assets.update_asset_metadata("upload", item["id"], {"note": "本地备注"})
        result = assets.purge_asset("upload", item["id"], {"confirm": True})
        self.assertTrue(result["permanent"])
        self.assertEqual(assets.list_uploads()["items"], [])
        self.assertFalse((self.root / "uploads" / (item["id"] + ".wav")).exists())
        self.assertFalse((self.root / "uploads" / (item["id"] + ".json")).exists())
        with self.assertRaises(ValueError):
            assets.resolve_audio_attachments([{"kind": "upload", "id": item["id"]}])

    def test_asset_purge_rejects_conversation_kind_and_unknown_id(self):
        for kind, identifier in (("conversation", "b" * 32), ("upload", "../escape"), ("recording", "b" * 32)):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                assets.purge_asset(kind, identifier, {"confirm": True})


if __name__ == "__main__":
    unittest.main()
