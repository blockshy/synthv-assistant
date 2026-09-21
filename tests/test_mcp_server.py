"""验证 MCP 工具调用与真正的 AudioContent 输出，不连接或编辑真实宿主。"""

import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from unittest.mock import MagicMock, patch
import wave

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from synthv_assistant.__main__ import build_parser, main
from synthv_assistant.mcp_server import create_mcp_server


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    """使用注入的业务服务，检查工具参数和异步任务编号的传递。"""

    def setUp(self):
        # MCP 素材锁与删除标记全部落入测试目录，不读取真实工作台的资料状态。
        self.temporary_data = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_data.cleanup)
        data_patch = patch("synthv_assistant.mcp_server.DATA", Path(self.temporary_data.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)
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
        self.assertEqual(names, {"status", "get_project", "get_selection", "set_write_mode", "preview_parameter",
                                 "apply_preview", "restore_last_edit", "start_recording", "get_job", "list_recordings",
                                 "compare_recordings", "review_recordings", "get_recording_audio", "preview_curve"})
        review = next(tool for tool in tools if tool.name == "review_recordings")
        self.assertTrue(review.annotations.openWorldHint)
        self.assertIn("上传", review.description)

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

    async def test_long_tasks_return_job_id_without_waiting(self):
        result = await self.server.call_tool("start_recording", {"start_seconds": 2, "duration_seconds": 5})
        self.service.submit.assert_called_with(self.service.record, 2.0, 5.0, "片段")
        # 普通 dict 返回值由 FastMCP 编码为 JSON TextContent。
        self.assertEqual(json.loads(result[0].text)["jobId"], "a" * 32)
        state = await self.server.call_tool("get_job", {"job_id": "a" * 32})
        self.assertEqual(json.loads(state[0].text)["state"], "done")

    async def test_audio_tool_returns_wav_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(44100)
                writer.writeframes(b"\x00\x10" * 200)
            self.service.recording_path.return_value = path
            result = await self.server.call_tool("get_recording_audio", {"recording_id": "b" * 32})
            audio = next(item for item in result if item.type == "audio")
            self.assertEqual(audio.mimeType, "audio/wav")
            self.assertEqual(base64.b64decode(audio.data), path.read_bytes())
            self.service.recording_path.assert_called_once_with("b" * 32)

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
                self.assertIn("get_recording_audio", {tool.name for tool in result.tools})

    async def test_audio_read_respects_resource_lock(self):
        from synthv_assistant.metadata import LibraryMetadata
        # 正在使用的音频不能同时被另一个读取/永久删除流程抢占。
        with LibraryMetadata(Path(self.temporary_data.name)).resource_lock("recording", "b" * 32):
            with self.assertRaisesRegex(Exception, "正在处理|正在使用|被 AI 请求使用"):
                await self.server.call_tool("get_recording_audio", {"recording_id": "b" * 32})
        self.service.recording_path.assert_not_called()

    async def test_missing_audio_error_does_not_expose_local_path(self):
        self.service.recording_path.return_value = Path(self.temporary_data.name) / "private-missing.wav"
        with self.assertRaises(Exception) as caught:
            await self.server.call_tool("get_recording_audio", {"recording_id": "b" * 32})
        self.assertNotIn("private-missing.wav", str(caught.exception))
        self.assertIn("无法读取录音文件", str(caught.exception))


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
