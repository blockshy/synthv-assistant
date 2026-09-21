"""验证实际文件锁的跨线程、跨进程竞争；所有锁文件位于测试临时目录。"""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from synthv_assistant.operations import OperationBusyError, OperationLock, operation_busy
from synthv_assistant.service import AssistantService


class OperationLockTests(unittest.TestCase):
    """真实锁测试不读取用户工程，不控制 SynthV，也不依赖音频设备。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "operation.lock"

    def test_competing_instances_fail_immediately_and_file_remains(self):
        self.assertFalse(operation_busy(self.path))
        with OperationLock(self.path):
            started = time.monotonic()
            with self.assertRaisesRegex(OperationBusyError, "其他控制台或 MCP"):
                with OperationLock(self.path):
                    self.fail("第二个锁实例不应进入临界区。")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(operation_busy(self.path))
            # 状态探测不能意外释放原锁。
            self.assertTrue(operation_busy(self.path))
        self.assertTrue(self.path.exists())
        self.assertFalse(operation_busy(self.path))

    def test_exception_releases_lock_without_deleting_file(self):
        with self.assertRaisesRegex(RuntimeError, "模拟失败"):
            with OperationLock(self.path):
                raise RuntimeError("模拟失败")
        self.assertTrue(self.path.exists())
        with OperationLock(self.path):
            self.assertTrue(operation_busy(self.path))

    def test_other_thread_cannot_acquire_same_file(self):
        findings = []
        with OperationLock(self.path):
            thread = threading.Thread(target=lambda: findings.append(operation_busy(self.path)))
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(findings, [True])

    def start_holder_process(self, terminate=False):
        """启动一个短寿命子进程持锁，并用文件通知就绪，避免无限等待管道。"""
        ready = self.root / "ready.txt"
        release = self.root / "release.txt"
        script = """
import os
from pathlib import Path
import sys
import time
from synthv_assistant.operations import OperationLock
lock_path, ready, release = map(Path, sys.argv[1:4])
with OperationLock(lock_path):
    ready.write_text('ready', encoding='utf-8')
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if sys.argv[4] == 'exit':
        os._exit(0)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.path), str(ready), str(release), "exit" if terminate else "normal"],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def clean_process():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=3)

        self.addCleanup(clean_process)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            process.terminate()
            _output, failure = process.communicate(timeout=3)
            self.fail("子进程未持锁就绪：" + failure.decode("utf-8", errors="replace"))
        return process, release

    def test_real_process_collision_and_normal_exit(self):
        process, release = self.start_holder_process()
        self.assertTrue(operation_busy(self.path))
        with self.assertRaises(OperationBusyError):
            with OperationLock(self.path):
                self.fail("其他进程持锁时不得操作。")
        release.write_text("release", encoding="utf-8")
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertTrue(self.path.exists())
        self.assertFalse(operation_busy(self.path))

    def test_process_abrupt_exit_releases_kernel_lock(self):
        # os._exit 跳过上下文清理，证明恢复不依赖 finally 或手动删除锁文件。
        process, release = self.start_holder_process(terminate=True)
        self.assertTrue(operation_busy(self.path))
        release.write_text("exit", encoding="utf-8")
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertTrue(self.path.exists())
        self.assertFalse(operation_busy(self.path))


class ServiceOperationTests(unittest.TestCase):
    """模拟慢录音，验证另一个 AssistantService 无法穿透服务级互斥。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data_patch = patch("synthv_assistant.service.DATA", self.root)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        with patch("synthv_assistant.service.ensure_directories"), patch("synthv_assistant.service.BridgeClient") as bridge_class:
            bridge_class.side_effect = [MagicMock(), MagicMock()]
            self.recorder, self.editor = AssistantService(), AssistantService()
        self.addCleanup(self.recorder.executor.shutdown, wait=True)
        self.addCleanup(self.editor.executor.shutdown, wait=True)

    def test_all_mutation_entries_are_blocked_but_reads_remain_available(self):
        self.editor.bridge.call.return_value = {"read": True}
        with OperationLock(self.root / "operation.lock"):
            operations = [
                lambda: self.editor.write_mode(False),
                lambda: self.editor.preview("tension", 0.1),
                lambda: self.editor.edit("apply", {"previewId": "sample"}),
                lambda: self.editor.record(0, 1),
            ]
            for operation in operations:
                with self.assertRaises(OperationBusyError):
                    operation()
            self.editor.bridge.call.assert_not_called()
            self.assertEqual(self.editor.get_project(), {"read": True})
            self.assertEqual(self.editor.get_selection(), {"read": True})

    def test_entire_recording_and_failure_cleanup_hold_exclusive_lock(self):
        entered, release = threading.Event(), threading.Event()
        observed = []
        session = MagicMock()

        def wait_for_release():
            entered.set()
            release.wait(3)
            raise RuntimeError("模拟录音结束失败")

        def cancellation():
            # 录音失败后取消采集也必须在临界区内，不能允许新编辑提前进入。
            observed.append(operation_busy(self.root / "operation.lock"))

        session.wait.side_effect = wait_for_release
        session.cancel.side_effect = cancellation
        self.recorder.bridge.call.return_value = {"ok": True}
        project = {"playbackStatus": "stopped", "projectFile": "mock.svp"}
        failures = []

        def recording_worker():
            try:
                self.recorder.record(0, 1)
            except RuntimeError as exc:
                failures.append(str(exc))

        with patch.object(self.recorder, "get_project", return_value=project), \
                patch("synthv_assistant.service.find_synthv_pid", return_value=123), \
                patch("synthv_assistant.service.prepare_capture", return_value=session):
            thread = threading.Thread(target=recording_worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(OperationBusyError):
                    self.editor.preview("tension", 0.1)
                self.editor.bridge.call.assert_not_called()
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [True])
        self.assertEqual(failures, ["模拟录音结束失败"])
        self.assertFalse(self.recorder.recording)
        self.assertFalse(operation_busy(self.root / "operation.lock"))

    def test_status_does_not_label_short_edit_lock_as_recording(self):
        self.editor.bridge.status.return_value = {"connected": True, "writeEnabled": False}
        with patch("synthv_assistant.service.find_capture_executable", return_value=Path("mock.exe")), \
                patch("synthv_assistant.review.provider_status", return_value={"configured": False}), \
                OperationLock(self.root / "operation.lock"):
            status = self.editor.status()
        self.assertTrue(status["operationBusy"])
        self.assertFalse(status["capture"]["recording"])
        self.assertEqual(status["capture"]["recordingStatusScope"], "this_service")


if __name__ == "__main__":
    unittest.main()
