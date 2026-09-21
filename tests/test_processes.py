"""目标进程选择测试，模拟进程列表，不访问其他程序内存。"""

import os
import unittest
from unittest.mock import patch

from synthv_assistant.capture import CaptureError
from synthv_assistant.service import find_synthv_pid


@unittest.skipUnless(os.name == "nt", "Windows 进程选择逻辑")
class ProcessSelectionTests(unittest.TestCase):
    def test_unique_process_is_selected(self):
        """单实例自动选择，不要求用户手动查找 PID。"""
        with patch.dict(os.environ, {"SYNTHV_ASSISTANT_PID": ""}), \
                patch("synthv_assistant.processes.synthv_process_ids", return_value=[123]):
            self.assertEqual(find_synthv_pid(), 123)

    def test_multiple_instances_require_explicit_pid(self):
        """多实例不能凭排序猜测目标，避免录制与桥接不同的实例。"""
        with patch.dict(os.environ, {"SYNTHV_ASSISTANT_PID": ""}), \
                patch("synthv_assistant.processes.synthv_process_ids", return_value=[123, 456]):
            with self.assertRaises(CaptureError):
                find_synthv_pid()

    def test_explicit_pid_must_belong_to_synthv(self):
        """拒绝已退出、已复用或指向其他应用的进程号。"""
        with patch.dict(os.environ, {"SYNTHV_ASSISTANT_PID": "789"}), \
                patch("synthv_assistant.processes.synthv_process_ids", return_value=[123]):
            with self.assertRaises(CaptureError):
                find_synthv_pid()

    def test_explicit_pid_selects_existing_instance(self):
        with patch.dict(os.environ, {"SYNTHV_ASSISTANT_PID": "456"}), \
                patch("synthv_assistant.processes.synthv_process_ids", return_value=[123, 456]):
            self.assertEqual(find_synthv_pid(), 456)
