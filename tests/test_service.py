"""业务层失败恢复测试：模拟桥接和采集器，不播放或读取用户工程。"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from synthv_assistant.bridge import BridgeError
from synthv_assistant.service import AssistantService, finite_number
from synthv_assistant.metadata import LibraryMetadata
from synthv_assistant.operations import OperationLock
from tests.test_parameters import modern_selection


class ServiceTests(unittest.TestCase):
    """重点验证录音异常后的停止、状态恢复和修改互斥。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bridge = MagicMock()
        with patch("synthv_assistant.service.ensure_directories"), \
                patch("synthv_assistant.service.BridgeClient", return_value=self.bridge):
            self.service = AssistantService()
        self.addCleanup(self.service.executor.shutdown, wait=True)
        # 使用专门生成的临时目录，确保测试不会访问项目运行数据或真实 SVP。
        for name in ("RECORDINGS", "DATA"):
            patcher = patch("synthv_assistant.service." + name, self.directory)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record_dependencies(self):
        """为录音过程注入可控替身，避免设备、进程和音频分析的外部副作用。"""
        capture = MagicMock()
        capture.start.return_value = {"status": "ready"}
        capture.wait.return_value = {"status": "complete"}
        project = {"playbackStatus": "stopped", "projectFile": "synthetic-test-project.svp"}
        patches = [
            patch.object(self.service, "get_project", return_value=project),
            patch("synthv_assistant.service.find_synthv_pid", return_value=123),
            patch("synthv_assistant.service.prepare_capture", return_value=capture),
            patch("synthv_assistant.service.time.sleep"),
            patch("synthv_assistant.analysis.analyze_wav", return_value={"silent": False}),
            patch("synthv_assistant.service.atomic_json"),
            patch.object(self.service, "_log"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return capture

    def test_invalid_times_are_rejected_before_contacting_host(self):
        for value in (True, None, "1", float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finite_number(value, "时长", 0, 30)
        self.bridge.call.assert_not_called()

    def test_edit_is_blocked_while_recording(self):
        self.service.recording = True
        for operation in (
            lambda: self.service.preview("tension", 0.1),
            lambda: self.service.edit("apply", {"previewId": "test"}),
            lambda: self.service.write_mode(False),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.bridge.call.assert_not_called()

    def test_legacy_preview_keeps_original_ipc_without_extra_catalog_read(self):
        self.bridge.call.return_value = {"previewId": "legacy-preview", "parameter": "tension", "delta": 0.1}
        result = self.service.preview("tension", 0.1)
        self.assertEqual(result["previewId"], "legacy-preview")
        self.bridge.call.assert_called_once_with("preview", {"parameter": "tension", "delta": 0.1})
        self.bridge.call.reset_mock()
        with self.assertRaises(ValueError):
            self.service.preview("tension", 0.31)
        self.bridge.call.assert_not_called()

    def test_curve_preview_reads_capability_and_forwards_no_write(self):
        selection = modern_selection()
        curve = [[0, 0], [0.5, 25], [1, 0]]
        self.bridge.call.side_effect = [selection, {"previewId": "curve-preview", "curve": curve,
            "renderMode": "points", "curvePreview": [{"position": 0, "after": 0}], "projectFile": "private-path"}]
        result = self.service.preview("pitchDelta", curve=curve, render_mode="points")
        self.assertEqual(self.bridge.call.call_args_list[0].args[0], "get_selection")
        self.assertEqual(self.bridge.call.call_args_list[1].args,
                         ("preview", {"parameter": "pitchDelta", "curve": curve, "renderMode": "points"}))
        self.assertIsNone(result["curvePreview"][0]["before"])
        self.assertNotIn("projectFile", result)
        self.assertNotIn("apply", [call.args[0] for call in self.bridge.call.call_args_list])

    def test_unavailable_new_parameter_cannot_reach_host_preview(self):
        self.bridge.call.return_value = {"parameters": {}, "capabilities": {"curves": True}}
        with self.assertRaises(ValueError):
            self.service.preview("vocalMode_Soft", 10)
        self.assertEqual([call.args[0] for call in self.bridge.call.call_args_list], ["get_selection"])

    def test_register_vocal_mode_uses_trimmed_original_name_and_only_expected_identity(self):
        selection = {"projectFile": "", "groupUUID": "group-1", "groupOffset": 123,
                     "groupPitchOffset": 12, "voiceFingerprint": "voice-1", "notes": [{"pitch": 60}],
                     "parameters": {"private": "not-forwarded"}}
        self.bridge.call.return_value = {"parameters": {"vocalMode_柔和": {"source": "user"}}}
        result = self.service.register_vocal_mode({"name": "  柔和  ", "selection": selection})
        self.assertIn("vocalMode_柔和", result["parameters"])
        expected = {key: selection[key] for key in ("projectFile", "groupUUID", "groupOffset", "groupPitchOffset", "voiceFingerprint")}
        self.bridge.call.assert_called_once_with("register_vocal_mode", {"name": "柔和", "expected": expected})

    def test_register_vocal_mode_rejects_names_or_incomplete_identity_before_host(self):
        selection = {"projectFile": "", "groupUUID": "group-1", "groupOffset": 0,
                     "groupPitchOffset": 0, "voiceFingerprint": "voice-1"}
        bad = [{"name": value, "selection": selection} for value in (None, "", "  ", "A\nB", "A\x00B", "\ud800", "柔" * 27)]
        bad.extend(({"name": "Soft", "selection": {key: value for key, value in selection.items() if key != "voiceFingerprint"}},
                    {"name": "Soft", "selection": {**selection, "groupOffset": True}},
                    {"name": "Soft", "selection": {**selection, "groupPitchOffset": 10 ** 1000}},
                    {"name": "Soft", "selection": {**selection, "voiceFingerprint": ""}},
                    {"name": "Soft", "selection": selection, "code": "unsafe"}))
        for payload in bad:
            with self.assertRaises(ValueError):
                self.service.register_vocal_mode(payload)
        self.bridge.call.assert_not_called()
        self.bridge.call.return_value = {"parameters": {}}
        self.service.register_vocal_mode({"name": "柔" * 26, "selection": selection})
        self.assertEqual(self.bridge.call.call_args.args[1]["name"], "柔" * 26)

    def test_register_vocal_mode_respects_recording_and_cross_process_operation_lock(self):
        payload = {"name": "Soft", "selection": {"projectFile": "", "groupUUID": "group-1", "groupOffset": 0,
                   "groupPitchOffset": 0, "voiceFingerprint": "voice-1"}}
        self.service.recording = True
        with self.assertRaises(ValueError):
            self.service.register_vocal_mode(payload)
        self.service.recording = False
        with OperationLock(self.directory / "operation.lock"):
            with self.assertRaises(ValueError):
                self.service.register_vocal_mode(payload)
        self.bridge.call.assert_not_called()

    def test_successful_recording_releases_recording_flag(self):
        capture = self.record_dependencies()
        self.bridge.call.return_value = {"started": True}
        result = self.service.record(2, 3, "测试片段")
        self.assertEqual(result["label"], "测试片段")
        self.assertEqual(result["durationSeconds"], 3)
        self.assertFalse(self.service.recording)
        capture.cancel.assert_not_called()

    def test_capture_failure_after_playback_stops_host(self):
        capture = self.record_dependencies()
        capture.wait.side_effect = RuntimeError("采集失败")
        self.bridge.call.return_value = {"started": True}
        with self.assertRaisesRegex(RuntimeError, "采集失败"):
            self.service.record(2, 3)
        capture.cancel.assert_called_once()
        self.assertIn("stop_playback", [call.args[0] for call in self.bridge.call.call_args_list])
        self.assertFalse(self.service.recording)

    def test_playback_timeout_still_requests_stop_for_unknown_execution_result(self):
        # IPC 超时不能证明播放未开始；异常恢复必须停止可能已经启动的播放。
        capture = self.record_dependencies()
        self.bridge.call.side_effect = [BridgeError("请求超时，执行结果未知"), {"stopped": True}]
        with self.assertRaisesRegex(BridgeError, "超时"):
            self.service.record(2, 3)
        capture.cancel.assert_called_once()
        self.assertIn("stop_playback", [call.args[0] for call in self.bridge.call.call_args_list])
        self.assertFalse(self.service.recording)

    def test_recording_ids_cannot_escape_fixed_directory(self):
        for identifier in ("../secret", "A" * 32, "a" * 31, "a" * 33, None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.service.recording_path(identifier)

    def test_second_background_job_is_rejected_while_first_is_running(self):
        entered, release = threading.Event(), threading.Event()

        def waiting_job():
            """有限等待保证断言失败时测试也不会永久挂起。"""
            entered.set()
            release.wait(2)
            return {"done": True}

        identifier = self.service.submit(waiting_job)
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.service.jobs[identifier]["state"], "running")
            with self.assertRaisesRegex(ValueError, "当前还有"):
                self.service.submit(lambda: None)
        finally:
            release.set()

    def test_send_message_job_exposes_live_progress_and_retains_it_after_completion(self):
        """真实 submit/线程池注入回调；事件屏障保证在任务仍运行时读取进度。

        只替换会话管理器的模型工作，不替换 submit 或 send_message，因而覆盖
        operation == self.send_message 的真实分支，避免测试绕过进度注入条件。
        """
        entered, emit_reasoning = threading.Event(), threading.Event()
        reasoning_ready, finish = threading.Event(), threading.Event()
        callbacks = []
        expected = {"id": "d" * 32, "messages": [{"role": "assistant", "text": "模型模拟回复"}]}
        manager = MagicMock()

        def simulated_conversation(identifier, text, include_selection, attachments,
                                   model_options=None, on_progress=None):
            self.assertEqual(identifier, expected["id"])
            self.assertEqual(text, "解释当前调教思路")
            self.assertFalse(include_selection)
            self.assertEqual(attachments, [])
            self.assertEqual(model_options, {"model": "mock-model"})
            self.assertTrue(callable(on_progress))
            callbacks.append(on_progress)
            entered.set()
            if not emit_reasoning.wait(2):
                raise RuntimeError("测试未释放思考阶段")
            on_progress({"stage": "正在接收思考摘要", "reasoning": "这是模拟服务明确返回的摘要。",
                         "text": "", "receivedCharacters": 0})
            reasoning_ready.set()
            if not finish.wait(2):
                raise RuntimeError("测试未释放完成阶段")
            on_progress({"stage": "正在校验计划", "text": "模型模拟回复", "receivedCharacters": 6})
            return expected

        manager.send_message.side_effect = simulated_conversation
        with patch.object(self.service, "conversations", return_value=manager):
            identifier = self.service.submit(self.service.send_message, expected["id"],
                                             "解释当前调教思路", False, [], {"model": "mock-model"})
            try:
                self.assertTrue(entered.wait(1))
                preparing = self.service.jobs[identifier]
                self.assertEqual(preparing["state"], "running")
                self.assertEqual(preparing["progress"]["stage"], "正在准备请求")
                self.assertFalse(preparing["progress"]["reasoningAvailable"])
                emit_reasoning.set()
                self.assertTrue(reasoning_ready.wait(1))
                live = self.service.jobs[identifier]
                self.assertEqual(live["state"], "running")
                self.assertNotIn("result", live)
                self.assertEqual(live["progress"]["stage"], "正在接收思考摘要")
                self.assertEqual(live["progress"]["reasoning"], "这是模拟服务明确返回的摘要。")
                self.assertTrue(live["progress"]["reasoningAvailable"])
                self.assertGreaterEqual(live["progress"]["elapsedSeconds"], 0)
                # 已返回的快照不得在后台被原地改成新阶段，保证轮询读取一致。
                self.assertEqual(preparing["progress"]["stage"], "正在准备请求")
            finally:
                emit_reasoning.set()
                finish.set()
                # 同一单线程执行器上的屏障在 worker 完整退出之后才返回，不使用轮询 sleep。
                self.service.executor.submit(lambda: None).result(timeout=2)
        completed = self.service.jobs[identifier]
        self.assertEqual(completed["state"], "done")
        self.assertEqual(completed["result"], expected)
        self.assertEqual(completed["progress"]["stage"], "正在校验计划")
        self.assertEqual(completed["progress"]["reasoning"], "这是模拟服务明确返回的摘要。")
        self.assertEqual(completed["progress"]["text"], "模型模拟回复")
        self.assertEqual(live["progress"]["stage"], "正在接收思考摘要")
        # 迟到的回调不能重新打开已结束任务或覆盖完成时保留的摘要。
        callbacks[0]({"stage": "迟到事件", "reasoning": "不应覆盖"})
        self.assertIs(self.service.jobs[identifier], completed)
        self.bridge.call.assert_not_called()

    def test_send_message_progress_whitelist_and_size_limits_apply_while_running(self):
        """供应商进度只保留界面需要的有限字段，额外对象不能透传到任务查询。"""
        ready, finish = threading.Event(), threading.Event()
        manager = MagicMock()

        def simulated_conversation(*_args, on_progress=None, **_kwargs):
            on_progress({"stage": "阶" * 150, "reasoning": "思" * 13_000,
                         "text": "文" * 7_000, "receivedCharacters": 30_000,
                         "rawResponse": {"authorization": "fake-only-test-key"}})
            ready.set()
            if not finish.wait(2):
                raise RuntimeError("测试未释放完成阶段")
            return {"messages": []}

        manager.send_message.side_effect = simulated_conversation
        with patch.object(self.service, "conversations", return_value=manager):
            identifier = self.service.submit(self.service.send_message, "e" * 32, "模拟需求", False, [])
            try:
                self.assertTrue(ready.wait(1))
                job = self.service.jobs[identifier]
                self.assertEqual(job["state"], "running")
                progress = job["progress"]
                self.assertEqual(len(progress["stage"]), 120)
                self.assertEqual(len(progress["reasoning"]), 12_000)
                self.assertEqual(len(progress["text"]), 6_000)
                self.assertEqual(progress["receivedCharacters"], 24_000)
                self.assertNotIn("rawResponse", progress)
                self.assertNotIn("fake-only-test-key", json.dumps(job))
            finally:
                finish.set()
                self.service.executor.submit(lambda: None).result(timeout=2)
        self.assertEqual(self.service.jobs[identifier]["progress"], progress)
        self.bridge.call.assert_not_called()

    def test_deleted_recording_cannot_play_or_review_and_original_stays(self):
        identifier = "a" * 32
        audio = self.directory / (identifier + ".wav")
        audio.write_bytes(b"fake-complete-audio")
        (self.directory / (identifier + ".json")).write_text(json.dumps({"id": identifier, "label": "录音", "note": "原采集技术说明"}), encoding="utf-8")
        library = LibraryMetadata(self.directory)
        library.update("recording", identifier, {"note": "私有备注"})
        item = self.service.list_recordings()["items"][0]
        self.assertEqual((item["note"], item["captureNote"]), ("私有备注", "原采集技术说明"))
        library.delete("recording", identifier, "录音")
        self.assertEqual(self.service.list_recordings()["items"], [])
        with self.assertRaises(ValueError):
            self.service.recording_path(identifier)
        with patch("synthv_assistant.review.review_audio") as model:
            with self.assertRaises(ValueError):
                self.service.review([identifier], "评价")
            model.assert_not_called()
        self.assertEqual(audio.read_bytes(), b"fake-complete-audio")

    def test_recording_holds_asset_lock_until_task_finishes(self):
        capture = self.record_dependencies()
        self.bridge.call.return_value = {"started": True}
        fixed_id = "b" * 32

        def during_capture():
            with self.assertRaises(ValueError):
                LibraryMetadata(self.directory).delete("recording", fixed_id, "运行中")
            with self.assertRaises(ValueError):
                LibraryMetadata(self.directory).purge("recording", fixed_id, {"confirm": True})
            return {"status": "complete"}

        capture.wait.side_effect = during_capture
        with patch("synthv_assistant.service.uuid.uuid4") as random_id:
            random_id.return_value.hex = fixed_id
            self.service.record(2, 3)
        self.assertIsNone(LibraryMetadata(self.directory).read("recording", fixed_id)["deletedAt"])

    def test_review_does_not_include_local_note_and_prevents_delete(self):
        identifier = "c" * 32
        (self.directory / (identifier + ".wav")).write_bytes(b"fake-complete-audio")
        (self.directory / (identifier + ".json")).write_text(json.dumps({"id": identifier, "label": "录音", "note": "legacy-technical-note"}), encoding="utf-8")
        library = LibraryMetadata(self.directory)
        library.update("recording", identifier, {"note": "private-user-note"})

        def inspect_request(paths, prompt, context):
            self.assertNotIn("private-user-note", str(context))
            self.assertNotIn("note", context["recordings"][0])
            with self.assertRaises(ValueError):
                library.delete("recording", identifier, "使用中")
            with self.assertRaises(ValueError):
                library.purge("recording", identifier, {"confirm": True})
            return {"status": "ok"}

        with patch("synthv_assistant.review.review_audio", side_effect=inspect_request), patch("synthv_assistant.service.atomic_json"):
            self.assertEqual(self.service.review([identifier], "评价")["status"], "ok")

    def test_playback_read_holds_asset_lock_until_all_bytes_are_loaded(self):
        """读取播放器字节期间永久删除必须明确拒绝，不能只保护路径查找。"""
        identifier = "d" * 32
        audio = self.directory / (identifier + ".wav")
        audio.write_bytes(b"synthetic-player-bytes")
        read_bytes = Path.read_bytes

        def during_read(path):
            if path == audio:
                with self.assertRaisesRegex(ValueError, "正在处理"):
                    LibraryMetadata(self.directory).purge("recording", identifier, {"confirm": True})
            return read_bytes(path)

        with patch.object(Path, "read_bytes", during_read):
            path, content = self.service.read_audio("recording", identifier)
        self.assertEqual((path, content), (audio, b"synthetic-player-bytes"))
        # 读取完成必须释放锁，否则后续永久删除会永久显示“正在处理”。
        with LibraryMetadata(self.directory).resource_lock("recording", identifier):
            pass

    def test_playback_read_failure_is_fixed_and_releases_resource_lock(self):
        identifier = "e" * 32
        audio = self.directory / (identifier + ".wav")
        audio.write_bytes(b"synthetic-player-bytes")
        with patch.object(Path, "read_bytes", side_effect=OSError("private-user-file-path")):
            with self.assertRaises(ValueError) as caught:
                self.service.read_audio("recording", identifier)
        self.assertNotIn("private-user-file-path", str(caught.exception))
        with LibraryMetadata(self.directory).resource_lock("recording", identifier):
            pass


if __name__ == "__main__":
    unittest.main()
