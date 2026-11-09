"""验证参数 MCP 工具契约及音频入口退役，不连接或编辑真实宿主。"""

import io
import json
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from unittest.mock import MagicMock, patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synthv_assistant.__main__ import build_parser, main
from synthv_assistant.mcp_server import create_mcp_server


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    """使用注入的业务服务，检查工具参数和异步任务编号的传递。"""

    def setUp(self):
        # stdio 握手使用隔离目录，不读取真实工作台资料或凭据。
        self.temporary_data = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_data.cleanup)
        self.service = MagicMock()
        self.service.operation_lock = threading.RLock()
        self.service.jobs = {"a" * 32: {"state": "done", "result": {"id": "b" * 32}}}
        self.service.status.return_value = {"writeEnabled": False}
        self.service.submit.return_value = "a" * 32
        self.service.preview.return_value = {"previewId": "preview-1"}
        self.service.edit.return_value = {"verified": True}
        self.server = create_mcp_server(self.service)

    async def test_expected_tools_are_registered(self):
        tools = await self.server.list_tools()
        names = {tool.name for tool in tools}
        self.assertEqual(names, {"status", "get_project", "get_selection", "get_parameter_catalog",
                                 "set_write_mode", "preview_parameter", "apply_preview",
                                 "restore_last_edit", "get_job", "preview_curve"})
        self.assertTrue(all(tool.annotations.openWorldHint is False for tool in tools))

    async def test_preview_and_apply_use_existing_service(self):
        await self.server.call_tool("preview_parameter", {"parameter": "tension", "delta": 0.1})
        self.service.preview.assert_called_once_with("tension", 0.1)
        await self.server.call_tool("apply_preview", {"preview_id": "preview-1"})
        self.service.edit.assert_called_once_with("apply", {"previewId": "preview-1"})

    async def test_curve_tools_forward_explicit_representation_without_applying(self):
        curve = [[0, 0], [0.5, 25], [1, 0]]
        await self.server.call_tool("preview_curve", {"parameter": "pitchDelta", "curve": curve, "render_mode": "points"})
        self.service.preview.assert_called_once_with("pitchDelta", curve=curve, render_mode="points")
        self.service.preview.reset_mock()
        await self.server.call_tool("preview_parameter", {"parameter": "pitchDelta", "curve": curve})
        self.service.preview.assert_called_once_with("pitchDelta", None, curve=curve, render_mode="smooth")
        self.service.edit.assert_not_called()

    async def test_parameter_tools_do_not_coerce_boolean_or_numeric_string(self):
        # MCP 的 Pydantic 入口也要拒绝 bool/字符串，不能先转换为 float 绕过业务校验。
        from mcp.server.fastmcp.exceptions import ToolError
        for payload in ({"parameter": "loudness", "delta": True}, {"parameter": "loudness", "delta": "1"},
                        {"parameter": "pitchDelta", "curve": [[0, 0], [1, True]]},
                        {"parameter": "tension", "curve": "[[0, 0], [1, 0.1]]"},
                        {"parameter": "tension", "delta": 0.1, "curve": None},
                        {"parameter": "tension", "delta": 0.1, "code": "unsafe"}):
            with self.subTest(fields=list(payload)), self.assertRaises(ToolError):
                await self.server.call_tool("preview_parameter", payload)
        self.service.preview.assert_not_called()

    async def test_existing_job_can_still_be_queried(self):
        state = await self.server.call_tool("get_job", {"job_id": "a" * 32})
        self.assertEqual(json.loads(state[0].text)["state"], "done")
        self.service.submit.assert_not_called()

    async def test_retired_audio_names_cannot_read_files_or_submit_jobs(self):
        from mcp.server.fastmcp.exceptions import ToolError
        for name in ("start_recording", "list_recordings", "compare_recordings",
                     "review_recordings", "get_recording_audio"):
            with self.subTest(name=name), self.assertRaises(ToolError):
                await self.server.call_tool(name, {})
        self.service.recording_path.assert_not_called()
        self.service.submit.assert_not_called()
        self.service.list_recordings.assert_not_called()

    async def test_parameter_catalog_exposes_real_units_without_private_context(self):
        self.service.get_selection.return_value = {
            "projectFile": "private-project", "voiceFingerprint": "private-fingerprint",
            "capabilities": {"curves": True},
            "parameters": {
                "voicing": {"kind": "automation", "label": "浊音 / 清音", "range": [0, 1],
                            "available": True, "maxDelta": 0.3, "fingerprint": "private-curve"},
                "vocalMode_Airy": {"kind": "vocalMode", "modeName": "Airy", "source": "host",
                                   "voiceValues": {"pitch": 20, "timbre": 30, "pronunciation": 40,
                                                   "private": "private-value"}},
            },
            "computedPitch": {"status": "ready", "unit": "MIDI", "samples": [[0, 60.2], [1, None]]},
        }
        result = await self.server.call_tool("get_parameter_catalog", {})
        data = json.loads(result[0].text)
        self.assertEqual(data["parameters"]["voicing"]["range"], [0, 1])
        self.assertEqual(data["vocalModes"], {"complete": False, "items": [
            {"name": "Airy", "parameter": "vocalMode_Airy", "source": "host",
             "values": {"pitch": 20, "timbre": 30, "pronunciation": 40}}]})
        self.assertEqual(data["computedPitch"]["samples"], [[0, 60.2], [1, None]])
        self.assertNotIn("private", json.dumps(data))
        self.service.edit.assert_not_called()

    async def test_status_does_not_advertise_retired_audio_configuration(self):
        # 占用、空闲与锁探测失败均须如实传递；过滤音频状态不能顺带删掉操作互斥状态。
        for busy, error in ((True, None), (False, None), (None, "无法读取跨进程操作锁。")):
            with self.subTest(busy=busy):
                expected = {"bridge": {"connected": True}, "writeEnabled": False,
                            "operationBusy": busy, "operationStatusError": error}
                self.service.status.return_value = {
                    **expected, "capture": {"ready": True}, "review": {"provider": "private"}}
                result = await self.server.call_tool("status", {})
                self.assertEqual(json.loads(result[0].text), expected)

    async def test_real_stdio_initialization_and_tool_listing(self):
        # 仅握手并列举工具，不调用宿主操作，验证 CLI 没有污染 stdout 协议流。
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "synthv_assistant", "mcp"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env={"SYNTHV_ASSISTANT_DATA": str(Path(self.temporary_data.name) / "stdio")},
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                result = await client.list_tools()
                names = {tool.name for tool in result.tools}
                self.assertIn("get_parameter_catalog", names)
                self.assertNotIn("get_recording_audio", names)



class CliTests(unittest.TestCase):
    """检查命令行默认行为，不启动浏览器、HTTP 服务或原生录音程序。"""

    def test_default_and_loopback_options(self):
        self.assertEqual(build_parser().parse_args([]).command, "serve")
        args = build_parser().parse_args(["serve", "--port", "8766", "--host", "localhost"])
        self.assertEqual(args.port, 8766)
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["serve", "--host", "0.0.0.0"])

    def test_status_prints_json_and_closes_service(self):
        service = MagicMock()
        service.status.return_value = {"writeEnabled": False}
        with patch("synthv_assistant.service.AssistantService", return_value=service), patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(main(["status"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"writeEnabled": False})
        service.executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
