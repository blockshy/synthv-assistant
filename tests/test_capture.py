"""采集进程生命周期测试：验证错误不会伪装为就绪、不会扩大录音范围。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from synthv_assistant.capture import CaptureError, CaptureSession, prepare_capture


class FakeProcess:
    """可控子进程替身，用于验证失败、超时以及终止行为，不接触实际音频设备。"""

    def __init__(self, *, returncode=None, stdout="", stderr="", timeout=False):
        self.returncode = returncode
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.timeout = timeout
        self.terminated = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.timeout and not self.terminated:
            raise subprocess.TimeoutExpired("capture", timeout)
        return self.stdout_text, self.stderr_text

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


class CaptureTests(unittest.TestCase):
    """无需安装音频驱动或播放用户工程即可执行的回归测试。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.executable = self.root / "capture.exe"
        self.executable.write_bytes(b"test executable placeholder")

    def session(self):
        return prepare_capture(123, 1, self.root / "capture.wav", executable=self.executable)

    @staticmethod
    def payload(session, status="ready", scope="process_tree"):
        return {"status": status, "scope": scope, "pid": session.pid, "output": str(session.output)}

    def test_rejects_unbounded_duration_and_invalid_pid(self):
        for seconds in (0, 32.001, 33, float("nan"), float("inf"), True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                prepare_capture(123, seconds, self.root / "x.wav", executable=self.executable)
        for pid in (0, -1, True, 1.5):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                prepare_capture(pid, 1, self.root / "x.wav", executable=self.executable)

    def test_full_phrase_has_two_seconds_of_capture_headroom(self):
        session = prepare_capture(123, 32, self.root / "long.wav", executable=self.executable)
        self.assertEqual(session.seconds, 32)

    def test_existing_recording_is_never_overwritten(self):
        target = self.root / "existing.wav"
        target.write_bytes(b"original recording")
        with self.assertRaises(CaptureError):
            prepare_capture(123, 1, target, executable=self.executable)
        self.assertEqual(target.read_bytes(), b"original recording")

    def test_start_requires_actual_ready_signal(self):
        session = self.session()
        process = FakeProcess(returncode=1, stderr="接口激活失败")
        with patch("synthv_assistant.capture.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(CaptureError, "接口激活失败"):
                session.start()
        self.assertIsNone(session.ready)

    def test_start_timeout_terminates_only_helper(self):
        session = self.session()
        process = FakeProcess()
        with patch("synthv_assistant.capture.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(CaptureError, "未就绪"):
                session.start(timeout=0.01)
        self.assertTrue(process.terminated)

    def test_wrong_capture_scope_is_rejected(self):
        session = self.session()
        process = FakeProcess()
        session.ready_file.write_text(json.dumps(self.payload(session, scope="system")), encoding="utf-8")
        with patch("synthv_assistant.capture.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(CaptureError, "采集范围"):
                session.start()
        self.assertTrue(process.terminated)
        self.assertFalse(session.ready_file.exists())

    def test_wait_checks_wave_and_allows_repeated_result(self):
        session = self.session()
        with wave.open(str(session.output), "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(44100)
            writer.writeframes(bytes(44100 * 4))
        session.ready = self.payload(session)
        expected = self.payload(session, status="complete")
        session.process = FakeProcess(returncode=0, stdout=json.dumps(expected))
        self.assertEqual(session.wait(), expected)
        self.assertEqual(session.wait(), expected)

    def test_wait_timeout_terminates_helper(self):
        session = self.session()
        session.ready = self.payload(session)
        session.process = FakeProcess(timeout=True)
        with self.assertRaisesRegex(CaptureError, "超时"):
            session.wait(timeout=0.01)
        self.assertTrue(session.process.terminated)

    def test_truncated_wave_is_not_reported_as_success(self):
        session = self.session()
        with wave.open(str(session.output), "wb") as writer:
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(44100)
            writer.writeframes(bytes(100 * 4))
        session.ready = self.payload(session)
        session.process = FakeProcess(returncode=0, stdout=json.dumps(self.payload(session, status="complete")))
        with self.assertRaisesRegex(CaptureError, "时长"):
            session.wait()


if __name__ == "__main__":
    unittest.main()
