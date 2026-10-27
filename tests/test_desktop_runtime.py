"""桌面后台的认证、随机端口及安全退出测试；使用隔离目录和模拟业务层。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib import error, request

from synthv_assistant.desktop_runtime import DesktopRuntime, configure_desktop_data
from synthv_assistant.server import make_server


class FakeService:
    """只提供测试需要的只读行为，不连接 SynthV、不读取设置或真实音频。"""

    def __init__(self):
        self.operation_lock = threading.RLock()
        self.jobs = {}
        self.recording = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.get_selection = MagicMock(return_value={"noteCount": 0, "notes": []})
        self.status = MagicMock(return_value={"version": "test", "bridge": {"connected": False}})
        self.read_audio = MagicMock(return_value=(Path("synthetic.wav"), b"RIFF" + bytes(100)))


@contextmanager
def running_server():
    """真实回环 TCP 用于检验 Host、令牌和请求收尾，不开放局域网端口。"""
    with tempfile.TemporaryDirectory() as directory:
        service = FakeService()
        runtime = DesktopRuntime(service, Path(directory), busy_probe=lambda _path: False)
        server = make_server(0, service, desktop_runtime=runtime)
        runtime.attach(server)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        worker.start()
        try:
            yield service, runtime, server
        finally:
            server.shutdown()
            worker.join(timeout=3)
            service.executor.shutdown(wait=True)
            server.server_close()


def call(server, path, *, token=True, body=None, headers=None, method=None):
    supplied = {"X-SV-Token": server.desktop_token} if token else {}
    supplied.update(headers or {})
    encoded = json.dumps(body).encode() if body is not None else None
    req = request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=encoded, headers=supplied, method=method)
    try:
        response = request.urlopen(req, timeout=3)
    except error.HTTPError as response_error:
        response = response_error
    with response:
        return response.status, response.read()


class DesktopServerTests(unittest.TestCase):
    def test_all_read_routes_and_audio_require_handshake_token(self):
        with running_server() as (service, _runtime, server):
            for path in ("/api/status", "/api/selection", "/api/desktop/status", "/api/bootstrap",
                         "/audio/" + "a" * 32 + ".wav", "/uploads/" + "b" * 32 + ".wav"):
                with self.subTest(path=path):
                    self.assertEqual(call(server, path, token=False)[0], 403)
            service.status.assert_not_called()
            service.get_selection.assert_not_called()
            service.read_audio.assert_not_called()

    def test_actual_random_port_and_native_authenticated_routes_work(self):
        with running_server() as (service, _runtime, server):
            self.assertGreater(server.server_port, 0)
            self.assertEqual(call(server, "/api/selection")[0], 200)
            self.assertEqual(call(server, "/api/status", headers={"Host": "127.0.0.1:0"})[0], 403)
            self.assertEqual(call(server, "/api/status", headers={"Origin": "https://untrusted.example"})[0], 403)
            code, data = call(server, "/audio/" + "a" * 32 + ".wav", headers={"Range": "bytes=0-3"})
            self.assertEqual((code, data), (206, b"RIFF"))
            service.get_selection.assert_called_once()

    def test_desktop_does_not_expose_web_assets_or_bootstrap(self):
        with running_server() as (_service, _runtime, server):
            for path in ("/", "/index.html", "/app.js", "/api/bootstrap", "/api/unknown"):
                self.assertEqual(call(server, path)[0], 404)

    def test_shutdown_refuses_running_job_then_exits_without_cancelling(self):
        with running_server() as (service, runtime, server):
            service.jobs["synthetic"] = {"state": "running"}
            code, body = call(server, "/api/desktop/shutdown", body={})
            self.assertEqual(code, 409)
            self.assertTrue(json.loads(body)["busy"])
            self.assertFalse(runtime.status()["closing"])
            service.jobs["synthetic"]["state"] = "done"
            code, body = call(server, "/api/desktop/shutdown", body={})
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["closing"])

    def test_shutdown_rejects_nonempty_or_unauthenticated_request(self):
        with running_server() as (_service, runtime, server):
            self.assertEqual(call(server, "/api/desktop/shutdown", token=False, method="POST")[0], 403)
            self.assertEqual(call(server, "/api/desktop/shutdown", body={"force": True})[0], 400)
            self.assertFalse(runtime.status()["closing"])

    def test_authenticated_status_exposes_only_running_task_identifiers(self):
        with running_server() as (service, _runtime, server):
            service.jobs = {"a" * 32: {"state": "running", "progress": {"text": "private-model-content"}},
                            "b" * 32: {"state": "done", "result": {"private": "project-data"}},
                            "c" * 32: {"state": "error"}}
            code, raw = call(server, "/api/desktop/status")
            self.assertEqual(code, 200)
            result = json.loads(raw)
            self.assertEqual(result["runningJobs"], 1)
            self.assertEqual(result["runningJobIds"], ["a" * 32])
            self.assertNotIn(b"private-model-content", raw)
            self.assertNotIn(b"project-data", raw)
            self.assertNotIn("runningJobIds", json.loads(call(server, "/api/status")[1]))

    def test_draining_rejects_new_business_requests(self):
        with running_server() as (service, runtime, server):
            service.jobs["existing"] = {"state": "running"}
            watcher = threading.Thread(target=runtime.watch_owner, args=(io.StringIO(""),))
            watcher.start()
            try:
                deadline = time.monotonic() + 2
                while not runtime.status()["closing"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(call(server, "/api/selection")[0], 503)
                self.assertEqual(call(server, "/api/desktop/status")[0], 200)
                service.get_selection.assert_not_called()
                self.assertTrue(watcher.is_alive())
            finally:
                service.jobs["existing"]["state"] = "done"
                watcher.join(timeout=3)
            self.assertFalse(watcher.is_alive())


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.addCleanup(self.service.executor.shutdown, wait=True)
        self.runtime = DesktopRuntime(self.service, Path("unused-test-data"), busy_probe=lambda _path: False)

    def test_active_request_prevents_shutdown_and_count_is_released(self):
        self.assertTrue(self.runtime.begin_request())
        self.assertFalse(self.runtime.request_shutdown()[0])
        self.assertEqual(self.runtime.status()["activeRequests"], 1)
        self.runtime.end_request()
        self.assertTrue(self.runtime.request_shutdown()[0])
        self.assertFalse(self.runtime.begin_request())

    def test_cross_process_operation_or_probe_error_is_busy(self):
        for probe in (lambda _: True, MagicMock(side_effect=OSError("private filesystem detail"))):
            runtime = DesktopRuntime(self.service, Path("unused-test-data"), busy_probe=probe)
            self.assertTrue(runtime.status()["busy"])
            self.assertFalse(runtime.request_shutdown()[0])
            self.assertNotIn("private filesystem detail", json.dumps(runtime.status()))

    def test_operation_lock_query_does_not_block_on_long_write(self):
        locked = threading.Event()
        release = threading.Event()

        def writing():
            with self.service.operation_lock:
                locked.set()
                release.wait(3)

        worker = threading.Thread(target=writing)
        worker.start()
        self.assertTrue(locked.wait(2))
        try:
            started = time.monotonic()
            self.assertTrue(self.runtime.status()["busy"])
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join(timeout=3)

    def test_data_configuration_never_migrates_or_rewrites_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "original"
            old.mkdir()
            sample = old / "keep.txt"
            sample.write_text("unchanged")
            with patch.dict(os.environ, {"SYNTHV_ASSISTANT_DATA": str(old)}, clear=True):
                self.assertEqual(configure_desktop_data(), old.resolve())
            self.assertEqual(sample.read_text(), "unchanged")
            with patch.dict(os.environ, {"LOCALAPPDATA": directory}, clear=True):
                expected = Path(directory) / "SynthV Assistant" / "data"
                self.assertEqual(configure_desktop_data(), expected.resolve())
                self.assertFalse(expected.exists())
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
                configure_desktop_data()

    def test_subprocess_handshake_owner_eof_and_single_instance(self):
        """真实子进程验证启动器管道契约，服务资料全部落在临时空目录。"""
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "SYNTHV_ASSISTANT_DATA": directory, "PYTHONIOENCODING": "utf-8"}
            # 不依赖开发终端的 UTF-8 环境变量；后台必须自行保证握手及失败诊断
            # 可由 Dart 的 UTF-8 解码器读取，中文 Windows 上也应保持一致。
            environment.pop("PYTHONIOENCODING", None)
            process = subprocess.Popen([sys.executable, "-m", "synthv_assistant", "desktop"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", env=environment,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                # 启动失败时 readline 也可能没有内容；用守护线程和有界队列，
                # 不能在超时后等待 reader executor 而让整个测试套件卡住。
                output = queue.Queue()
                threading.Thread(target=lambda: output.put(process.stdout.readline()), daemon=True).start()
                handshake = json.loads(output.get(timeout=10))
                self.assertEqual(handshake["protocol"], 1)
                self.assertEqual(handshake["dataDirectory"], str(Path(directory).resolve()))
                # Windows venv 的 python.exe 可为启动转发器，握手 PID 是实际
                # 服务解释器，未必等于 Popen 所持有的转发器进程号。
                self.assertGreater(handshake["pid"], 0)
                self.assertTrue(handshake["token"])
                duplicate = subprocess.run([sys.executable, "-m", "synthv_assistant", "desktop"],
                                           input="", capture_output=True, text=True, encoding="utf-8",
                                           env=environment, timeout=10,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(duplicate.returncode, 1)
                self.assertEqual(duplicate.stdout, "")
                self.assertIn("已有桌面后台", duplicate.stderr)
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual(process.stdout.read(), "")
                self.assertEqual(process.stderr.read(), "")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


if __name__ == "__main__":
    unittest.main()
