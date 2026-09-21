"""文件桥接回归测试：只操作临时 IPC 目录，不连接真实 SynthV 工程。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from synthv_assistant.bridge import BridgeClient, BridgeError, atomic_json


class BridgeTests(unittest.TestCase):
    """验证请求相关性、锁互斥和错误传播，避免失败被误报为成功。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.client = BridgeClient(self.directory)
        atomic_json(self.directory / "heartbeat.json", {
            "session": "test-session", "timestamp": time.time(), "protocol": 1,
        })

    def respond(self, payload):
        """模拟宿主消费请求并回写同一编号；不启动脚本或后台进程。"""
        def exchange(path, request):
            atomic_json(path, request)
            path.unlink()
            atomic_json(self.directory / "response.json", {"id": request["id"], **payload})
        return patch("synthv_assistant.bridge.atomic_json", side_effect=exchange)

    def test_matching_response_returns_result_and_releases_lock(self):
        with self.respond({"ok": True, "result": {"noteCount": 3}}):
            self.assertEqual(self.client.call("get_selection"), {"noteCount": 3})
        self.assertFalse((self.directory / "client.lock").exists())

    def test_host_rejection_preserves_original_error(self):
        # 宿主拒绝写入应立即保留原始原因，不能被轮询循环吞成超时。
        with self.respond({"ok": False, "error": "选区已经变化"}):
            with self.assertRaisesRegex(BridgeError, "选区已经变化"):
                self.client.call("apply", {"previewId": "old-preview"})
        self.assertFalse((self.directory / "client.lock").exists())

    def test_preview_rejection_exposes_only_a_fixed_public_message(self):
        """旧协议的 Lua 位置前缀留在本地，公开提示只取自代码白名单。"""
        message = "已有原生音高曲线跨越选区边界，已拒绝预览；请扩大选区或手动处理。"
        for raw in (message, "C:/private-project/bridge.lua:409: " + message):
            with self.subTest(raw=raw), self.respond({"ok": False, "error": raw}):
                with self.assertRaises(BridgeError) as raised:
                    self.client.call("preview", {"parameter": "pitchCurve"})
                self.assertEqual(str(raised.exception), raw)
                self.assertEqual(raised.exception.public_message, message)

    def test_public_preview_error_compatibility_never_exposes_arbitrary_response(self):
        """动态宿主异常、尾随秘密和伪造 publicMessage 字段均不能公开。"""
        message = "候选曲线会影响选区以外的插值，已拒绝预览；请扩大选区或手动调整边界。"
        suffix = " 未写入；可缩短选区或选择控制点模式。"
        self.assertEqual(BridgeError("private.lua:485: private.lua:304: " + message + suffix).public_message,
                         message)
        for raw in ("private-api-key", message + " private-api-key", {"secret": "private-api-key"},
                    "x" * 16_385 + message):
            with self.subTest(raw_type=type(raw).__name__), self.respond({
                    "ok": False, "error": raw, "publicMessage": "private-api-key"}):
                with self.assertRaises(BridgeError) as raised:
                    self.client.call("preview")
                self.assertIsNone(raised.exception.public_message)

    def test_existing_client_lock_blocks_second_client_without_removing_lock(self):
        lock = self.directory / "client.lock"
        lock.write_text("another-client", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "占用"):
            self.client.call("get_project")
        self.assertEqual(lock.read_text(encoding="utf-8"), "another-client")
        self.assertFalse((self.directory / "request.json").exists())

    def test_pending_request_is_not_overwritten(self):
        # 上一条写入结果未知时，保留原请求供人工排查，禁止覆盖或自动重试。
        request = self.directory / "request.json"
        request.write_text("original request", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "尚未处理"):
            self.client.call("apply")
        self.assertEqual(request.read_text(encoding="utf-8"), "original request")
        self.assertFalse((self.directory / "client.lock").exists())

    def test_unrelated_response_cannot_satisfy_new_request(self):
        atomic_json(self.directory / "response.json", {
            "id": "an-older-request", "ok": True, "result": {"verified": True},
        })
        with patch("synthv_assistant.bridge.time.monotonic", side_effect=[0, 0, 1]), \
                patch("synthv_assistant.bridge.time.sleep"):
            with self.assertRaisesRegex(BridgeError, "超时"):
                self.client.call("apply", timeout=0.5)
        # 超时后不能删除可能即将被宿主消费的写请求，否则调用者会错误重试。
        self.assertTrue((self.directory / "request.json").exists())
        self.assertFalse((self.directory / "client.lock").exists())

    def test_stale_heartbeat_does_not_send_request(self):
        atomic_json(self.directory / "heartbeat.json", {"session": "old", "timestamp": time.time() - 60})
        with self.assertRaisesRegex(BridgeError, "未连接"):
            self.client.call("get_project")
        self.assertFalse((self.directory / "request.json").exists())

    def test_nonfinite_json_never_replaces_existing_payload(self):
        path = self.directory / "saved.json"
        atomic_json(path, {"value": 1})
        with self.assertRaises(ValueError):
            atomic_json(path, {"value": float("nan")})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 1})


if __name__ == "__main__":
    unittest.main()
