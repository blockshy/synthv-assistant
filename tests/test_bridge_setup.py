"""桥接连接管理回归：只操作临时脚本、心跳和锁，不连接真实 SynthV。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from synthv_assistant.bridge import BridgeClient, BridgeError, atomic_json
from synthv_assistant import bridge_setup
from synthv_assistant.desktop_runtime import DesktopRuntime
from synthv_assistant.operations import OperationLock
from synthv_assistant.service import AssistantService


class BridgeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "desktop-data"
        self.current = self.data / "ipc"
        self.current.mkdir(parents=True)
        self.legacy = self.root / "previous-data" / "ipc"
        self.legacy.mkdir(parents=True)
        self.appdata = self.root / "roaming"
        self.appdata.mkdir()
        environment = patch.dict(os.environ, {"APPDATA": str(self.appdata)})
        environment.start()
        self.addCleanup(environment.stop)
        self.service = SimpleNamespace(bridge=BridgeClient(self.current), jobs={}, recording=False,
                                       operation_lock=threading.RLock(), bridge_binding_lock=threading.RLock(),
                                       _bridge_operation_root=None, _bridge_saved_invalid=False)
        self.script = bridge_setup.script_path()
        self.script.parent.mkdir(parents=True)
        self.write_script(self.legacy)
        self.beat(self.legacy)

    def write_script(self, directory: Path):
        self.script.write_text(bridge_setup.SCRIPT_HEADER + "\nlocal IPC_DIR = [[" +
                               directory.as_posix() + "]]\n-- 隔离测试脚本，不执行\n", encoding="utf-8")

    def beat(self, directory: Path, **extra):
        atomic_json(directory / "heartbeat.json", {"session": "test-session", "timestamp": time.time(),
                                                    "protocol": 2, "writeEnabled": False,
                                                    "projectFile": "不要返回的工程信息", **extra})

    def diagnostic(self):
        return bridge_setup.diagnostics(self.service, self.data)

    def choice(self, target="installed"):
        return {"target": target, "revision": self.diagnostic()["revision"]}

    def installation(self):
        return {"confirm": True, "revision": self.diagnostic()["revision"]}

    def test_diagnosis_identifies_mismatch_without_exposing_project_or_creating_data(self):
        report = self.diagnostic()
        self.assertEqual(report["reason"], "directory_mismatch")
        self.assertEqual(report["dataDirectory"], str(self.data))
        self.assertEqual(report["installedScript"]["ipcDirectory"], str(self.legacy))
        self.assertTrue(report["canConnectInstalled"])
        self.assertNotIn("不要返回", json.dumps(report, ensure_ascii=False))
        self.assertFalse((self.legacy / "request.json").exists())
        missing = self.root / "unknown-data" / "ipc"
        self.write_script(missing)
        self.assertFalse(self.diagnostic()["canConnectInstalled"])
        self.assertFalse(missing.parent.exists())

    def test_connect_persists_only_verified_binding_and_restart_restores_without_migration(self):
        report = bridge_setup.connect(self.service, self.data, self.choice())
        self.assertEqual(report["reason"], "connected")
        self.assertEqual(report["connectionTarget"], "installed")
        self.assertEqual(self.service._bridge_operation_root, self.legacy.parent)
        saved = json.loads((self.data / bridge_setup.CONFIG_NAME).read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"version", "target", "ipcDirectory", "scriptSha256"})
        self.assertFalse((self.legacy / "request.json").exists())
        self.service.bridge = BridgeClient(self.current)
        bridge_setup.restore_choice(self.service, self.data)
        self.assertEqual(self.service.bridge.directory, self.legacy)
        result = bridge_setup.connect(self.service, self.data, self.choice("current"))
        self.assertEqual(result["currentIpcDirectory"], str(self.current))
        self.assertIsNone(self.service._bridge_operation_root)
        self.assertEqual(json.loads((self.data / bridge_setup.CONFIG_NAME).read_text()),
                         {"version": 1, "target": "current"})

    def test_changed_or_unknown_saved_binding_never_silently_redirects(self):
        bridge_setup.connect(self.service, self.data, self.choice())
        other = self.root / "other" / "ipc"
        other.mkdir(parents=True)
        self.write_script(other)
        self.service.bridge = BridgeClient(self.current)
        self.service._bridge_operation_root = None
        bridge_setup.restore_choice(self.service, self.data)
        self.assertEqual(self.service.bridge.directory, self.current)
        self.assertTrue(self.service._bridge_saved_invalid)
        self.assertEqual(self.diagnostic()["reason"], "saved_connection_invalid")

    def test_invalid_saved_binding_blocks_active_default_until_explicit_choice(self):
        """默认目录也有活跃工程时，旧绑定失效不得成为静默回退和读取工程的授权。"""
        bridge_setup.connect(self.service, self.data, self.choice())
        self.beat(self.current, session="other-project-session", writeEnabled=True)
        self.script.write_bytes(self.script.read_bytes() + b"-- changed binding revision\n")
        bridge_setup.restore_choice(self.service, self.data)
        report = self.diagnostic()
        self.assertEqual(report["reason"], "saved_connection_invalid")
        self.assertFalse(report["currentHeartbeat"]["connected"])
        self.assertFalse(report["currentHeartbeat"]["writeEnabled"])
        self.assertTrue(report["canUseCurrent"])
        self.assertFalse(self.service.bridge.status()["connected"])
        for action in ("get_project", "get_selection", "preview", "apply"):
            with self.subTest(action=action), self.assertRaises(BridgeError):
                self.service.bridge.call(action)
        self.assertFalse((self.current / "request.json").exists())
        selected = bridge_setup.connect(self.service, self.data, self.choice("current"))
        self.assertEqual(selected["reason"], "connected")
        self.assertTrue(self.service.bridge.status()["connected"])
        self.assertFalse(self.service._bridge_saved_invalid)

    def test_stale_diagnosis_arbitrary_paths_and_stopped_script_are_rejected(self):
        original = self.choice()
        self.script.write_text(self.script.read_text(encoding="utf-8") + "-- changed\n", encoding="utf-8")
        with self.assertRaises(bridge_setup.BridgeSetupConflict):
            bridge_setup.connect(self.service, self.data, original)
        with self.assertRaises(ValueError):
            bridge_setup.connect(self.service, self.data, {**self.choice(), "path": str(self.root)})
        self.beat(self.legacy, timestamp=time.time() - 60)
        with self.assertRaises(bridge_setup.BridgeSetupConflict):
            bridge_setup.connect(self.service, self.data, self.choice())
        self.assertEqual(self.service.bridge.directory, self.current)

    def test_jobs_recording_pending_requests_and_cross_process_lock_block_switch(self):
        for kind in ("job", "recording", "request", "lock"):
            with self.subTest(kind=kind):
                if kind == "job":
                    self.service.jobs["test"] = {"state": "running"}
                if kind == "recording":
                    self.service.recording = True
                if kind == "request":
                    (self.legacy / "request.json").write_text("不可覆盖的请求", encoding="utf-8")
                lock = OperationLock(self.legacy.parent / "operation.lock") if kind == "lock" else None
                if lock:
                    lock.__enter__()
                try:
                    with self.assertRaises(bridge_setup.BridgeSetupConflict):
                        bridge_setup.connect(self.service, self.data, self.choice())
                finally:
                    if lock:
                        lock.__exit__(None, None, None)
                    self.service.jobs.clear()
                    self.service.recording = False
                    (self.legacy / "request.json").unlink(missing_ok=True)
                self.assertEqual(self.service.bridge.directory, self.current)

    def test_install_backs_up_exact_old_bytes_atomically_and_requires_host_restart(self):
        previous = self.script.read_bytes()
        bridge_setup.connect(self.service, self.data, self.choice())
        result = bridge_setup.install(self.service, self.data, self.installation())
        self.assertTrue(result["restartRequired"])
        self.assertTrue(result["backupCreated"])
        backups = list((self.data / "backups/bridge").glob("*.lua.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), previous)
        self.assertIn("local IPC_DIR = [[" + self.current.as_posix() + "]]", self.script.read_text(encoding="utf-8"))
        self.assertEqual(self.service.bridge.directory, self.current)
        self.assertFalse(list(self.script.parent.glob("*.tmp")))
        self.assertFalse((self.legacy / "request.json").exists())

    def test_install_failure_preserves_old_file_and_cleans_temporary(self):
        previous = self.script.read_bytes()
        with patch("synthv_assistant.bridge_setup.os.replace", side_effect=PermissionError):
            with self.assertRaises(OSError):
                bridge_setup.install(self.service, self.data, self.installation())
        self.assertEqual(self.script.read_bytes(), previous)
        self.assertFalse(list(self.script.parent.glob("*.tmp")))
        self.assertEqual(self.service.bridge.directory, self.current)

    def test_install_config_failure_rolls_back_and_shared_script_lock_blocks_install(self):
        """跨资料目录安装也竞争同一脚本锁；连接配置失败不能留下半成功状态。"""
        previous = self.script.read_bytes()
        with OperationLock(self.script.parent / "SynthVAssistant.install.lock"):
            with self.assertRaises(bridge_setup.BridgeSetupConflict):
                bridge_setup.install(self.service, self.data, self.installation())
        self.assertEqual(self.script.read_bytes(), previous)
        with patch("synthv_assistant.bridge_setup._save_choice", side_effect=PermissionError):
            with self.assertRaises(OSError):
                bridge_setup.install(self.service, self.data, self.installation())
        self.assertEqual(self.script.read_bytes(), previous)
        self.assertEqual(self.service.bridge.directory, self.current)

    def test_unsafe_script_path_cannot_be_used_and_nonfinite_heartbeat_is_hidden(self):
        self.script.write_text(bridge_setup.SCRIPT_HEADER + "\nlocal IPC_DIR = [[//remote/share/ipc]]\n", encoding="utf-8")
        self.assertFalse(self.diagnostic()["installedScript"]["recognized"])
        with self.assertRaises(ValueError):
            bridge_setup.connect(self.service, self.data, self.choice())
        self.write_script(self.legacy)
        (self.legacy / "heartbeat.json").write_text('{"session":"test","timestamp":NaN,"protocol":2}', encoding="utf-8")
        self.assertIsNone(self.diagnostic()["installedHeartbeat"]["heartbeatAge"])
        self.assertFalse(self.diagnostic()["canConnectInstalled"])

    def test_install_requires_literal_confirmation(self):
        previous = self.script.read_bytes()
        with self.assertRaises(ValueError):
            bridge_setup.install(self.service, self.data, {**self.installation(), "confirm": 1})
        self.assertEqual(self.script.read_bytes(), previous)

    def test_binding_guard_blocks_switch_and_old_operation_before_path_can_change(self):
        """模拟另一个线程正在读取宿主；切换必须立即拒绝且不更换现有实例。"""
        ready, finish = threading.Event(), threading.Event()
        def hold():
            with self.service.bridge_binding_lock:
                ready.set()
                finish.wait(3)
        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(ready.wait(2))
        try:
            with self.assertRaises(bridge_setup.BridgeSetupConflict):
                bridge_setup.connect(self.service, self.data, self.choice())
        finally:
            finish.set()
            worker.join(3)
        self.assertEqual(self.service.bridge.directory, self.current)

    def test_crlf_script_is_recognized_and_reparse_path_is_rejected(self):
        """Windows 换行不会阻断正常安装；重解析点不允许成为外部写入跳板。"""
        original = self.script.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.script.write_bytes(original)
        self.assertTrue(self.diagnostic()["installedScript"]["recognized"])
        original_lstat = Path.lstat
        def inspect(path):
            if path == self.legacy:
                return SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
            return original_lstat(path)
        with patch.object(Path, "lstat", inspect):
            self.assertFalse(self.diagnostic()["installedScript"]["recognized"])
            with self.assertRaises(ValueError):
                bridge_setup.connect(self.service, self.data, self.choice())

    def test_service_and_desktop_shutdown_probe_follow_selected_ipc_lock(self):
        with patch("synthv_assistant.service.DATA", self.data), \
                patch("synthv_assistant.service.ensure_directories"), \
                patch("synthv_assistant.service.BridgeClient", return_value=BridgeClient(self.current)):
            service = AssistantService()
            self.addCleanup(service.executor.shutdown, wait=True)
            service.connect_bridge({"target": "installed", "revision": service.bridge_diagnostics()["revision"]})
            self.assertEqual(service.operation_path(), self.legacy.parent / "operation.lock")
            runtime = DesktopRuntime(service, self.data)
            with OperationLock(self.legacy.parent / "operation.lock"):
                self.assertTrue(runtime.status()["operationBusy"])
                self.assertFalse(runtime.request_shutdown()[0])
                with self.assertRaises(ValueError):
                    service.edit("apply", {})
            self.assertFalse(runtime.status()["operationBusy"])
            # 普通状态不新增路径；桌面认证状态包含当前绑定，供 UI 清空旧选区。
            with patch("synthv_assistant.review.provider_status", return_value={}), \
                    patch("synthv_assistant.service.find_capture_executable", return_value=Path("test.exe")):
                self.assertNotIn("ipcDirectory", service.status()["bridge"])
                self.assertEqual(service.status(include_bridge_location=True)["bridge"]["ipcDirectory"], str(self.legacy))


if __name__ == "__main__":
    unittest.main()
