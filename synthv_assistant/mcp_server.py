"""供 AI 客户端调用的本地 stdio MCP 入口。

所有操作复用 AssistantService 的校验、备份和任务逻辑。本模块不提供任意
Python/Lua 执行工具，也不接受本机任意文件路径。stdout 专用于 MCP 协议。
"""

from __future__ import annotations

import base64
import io
import re
import wave

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import AudioContent, TextContent, ToolAnnotations
from pydantic import StrictFloat, StrictInt

from .service import AssistantService
from .config import DATA
from .metadata import LibraryMetadata
from .parameters import ParameterError, validate_preview_payload


MAX_MCP_AUDIO_BYTES = 12_000_000


class ParameterMCPServer(FastMCP):
    """仅收紧两个编辑预览工具的原始参数边界，其余 MCP 行为保持不变。

    FastMCP 默认会解析字符串内嵌的 JSON，并忽略模型未声明的额外字段；参数
    操作不能依赖这种宽松转换。先检查原始形状，再交给正常的工具类型及业务校验。
    """

    async def call_tool(self, name, arguments):
        if name in {"preview_parameter", "preview_curve"}:
            try:
                if (not isinstance(arguments, dict)
                        or set(arguments) - ({"parameter", "curve", "render_mode"}
                                             if name == "preview_curve" else {"parameter", "delta", "curve", "render_mode"})):
                    raise ParameterError("参数预览工具包含未知或无效字段。")
                payload = {("renderMode" if key == "render_mode" else key): value for key, value in arguments.items()}
                validate_preview_payload(payload)
            except ParameterError as exc:
                # 不沿用依赖库含原始 input_value 的错误文案，避免把输入全文回显给客户端。
                raise ToolError(str(exc)) from None
        return await super().call_tool(name, arguments)


def create_mcp_server(service: AssistantService | None = None) -> FastMCP:
    """创建可测试的 MCP 服务器；不在导入模块时连接或修改 SynthV。"""
    assistant = service if service is not None else AssistantService()
    server = ParameterMCPServer(
        "SynthV Assistant",
        instructions=(
            "协助调教 Synthesizer V Studio 2。先读取状态和选区，再预览参数变更。"
            "只有用户已授权编辑时才开启写入并应用预览。录音和模型听评返回 jobId，"
            "请用 get_job 查询结果；不要重复启动尚未完成的工作。"
            "review_recordings 会在配置供应商后上传所选音频及片段上下文，默认未配置。"
            "get_recording_audio 返回真实 WAV audio 内容，客户端与模型必须支持音频输入；"
            "只收到音频链接、指标或文件名不能称为已经听过音频。"
            "本地 RMS/峰值指标不能代替听感评价。"
        ),
        log_level="WARNING",
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    local_change = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
    external_call = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    @server.tool(annotations=readonly)
    def status() -> dict:
        """读取桥接、音频采集器、写入模式及听评配置状态；不回显 API key。"""
        return assistant.status()

    @server.tool(annotations=readonly)
    def get_project() -> dict:
        """读取当前 SynthV 工程结构与播放状态，要求宿主 Lua 桥接已启动。"""
        return assistant.get_project()

    @server.tool(annotations=readonly)
    def get_selection() -> dict:
        """读取当前选中音符、时间范围及参数；此工具不改变选区。"""
        return assistant.get_selection()

    @server.tool(annotations=local_change)
    def set_write_mode(enabled: bool) -> dict:
        """开启或关闭参数写入；开启时备份已保存的 SVP，未保存编辑需先另存副本。"""
        return assistant.write_mode(enabled)

    @server.tool(annotations=readonly)
    def preview_parameter(parameter: str, delta: StrictFloat | StrictInt | None = None,
                          curve: list[list[StrictFloat | StrictInt]] | None = None,
                          render_mode: str = "smooth") -> dict:
        """预览当前选区的增量或比例曲线，返回 previewId，不立即修改工程。

        先读取 get_selection 的参数目录和 capabilities。delta 与 curve 二选一；
        curve 为2至64个[position,value]，position严格递增且首尾为0和1。
        通用参数及声库模式的value为偏移；pitchCurve为绝对MIDI、只接受curve，
        只支持smooth，且限制在当前选区实际音域上下2半音内。不得猜测声库模式。
        render_mode 为 smooth（默认精简）或 points。旧的参数增量调用保持兼容。
        """
        if curve is None and render_mode == "smooth":
            return assistant.preview(parameter, delta)
        return assistant.preview(parameter, delta, curve=curve, render_mode=render_mode)

    @server.tool(annotations=readonly)
    def preview_curve(parameter: str, curve: list[list[StrictFloat | StrictInt]], render_mode: str = "smooth") -> dict:
        """预览一条有界稀疏曲线，不自动应用；确认后另调 apply_preview。

        curve 横轴为当前连续选区比例0..1，首尾0和1，2至64点严格递增；纵轴
        通常为参数偏移，pitchCurve专用绝对MIDI半音。需先读取当前宿主参数目录。
        smooth为默认精简表示，points为控制点表示；pitchCurve只能使用smooth。
        """
        return assistant.preview(parameter, curve=curve, render_mode=render_mode)

    @server.tool(annotations=local_change)
    def apply_preview(preview_id: str) -> dict:
        """应用已预览的修改；宿主核对选区与曲线未变化且写入模式已开启。"""
        if not preview_id or len(preview_id) > 128:
            raise ValueError("请传入有效的 previewId。")
        return assistant.edit("apply", {"previewId": preview_id})

    @server.tool(annotations=local_change)
    def restore_last_edit() -> dict:
        """恢复助手最近一次参数修改；宿主检查之后未发生冲突编辑。"""
        return assistant.edit("restore")

    @server.tool(annotations=local_change)
    def start_recording(start_seconds: float, duration_seconds: float, label: str = "片段") -> dict:
        """按进程录制 SynthV 当前混音并自动播放指定片段；时长 1～30 秒。

        不录麦克风，不改变轨道独奏/静音。立即返回 jobId；使用 get_job 等待完成，
        结果包含录音 id、技术分析与实际捕获状态。录音期间不要修改参数。
        """
        identifier = assistant.submit(assistant.record, start_seconds, duration_seconds, label)
        return {"jobId": identifier, "message": "录音任务已提交，请通过 get_job 查询结果。"}

    @server.tool(annotations=readonly)
    def get_job(job_id: str) -> dict:
        """查询本 MCP 进程创建的任务，state 为 running、done 或 error。"""
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("无效的任务编号。")
        with assistant.operation_lock:
            job = assistant.jobs.get(job_id)
            if job is None:
                raise ValueError("任务不存在，可能由其他助手进程创建或当前服务已重启。")
            return {"jobId": job_id, **job}

    @server.tool(annotations=readonly)
    def list_recordings() -> dict:
        """列出本助手生成的录音与分析，返回的录音 id 可用于对比和听评。"""
        return assistant.list_recordings()

    @server.tool(annotations=readonly)
    def compare_recordings(before_id: str, after_id: str) -> dict:
        """本地比较两个录音的电平、时长、潜在削波；不联网，不产生听感质量分。"""
        return assistant.compare(before_id, after_id)

    @server.tool(annotations=external_call)
    def review_recordings(recording_ids: list[str], prompt: str) -> dict:
        """将 1～2 段录音送给已配置的音频模型听评，立即返回 jobId。

        此操作配置供应商后会上传真实音频及片段上下文，可能产生 API 费用。
        默认供应商为 none，未配置时任务结果明确返回 not_configured，不伪造听感。
        两段录音按修改前、修改后排序；模型仅建议，不自动改动 SynthV。
        """
        identifier = assistant.submit(assistant.review, recording_ids, prompt)
        return {"jobId": identifier, "message": "听评任务已提交；配置供应商后会上传所选音频，请用 get_job 获取结果。"}

    @server.tool(annotations=readonly, structured_output=False)
    def get_recording_audio(recording_id: str) -> list[TextContent | AudioContent]:
        """向当前 MCP 客户端返回真实 WAV 音频，最大 12 MB，不调用额外听评服务。

        客户端及其模型必须能接收 MCP AudioContent 并支持音频输入；服务器无法
        保证每个客户端都会把音频转交模型。此工具不接受任意文件路径。
        """
        # 从可用性检查到读取完成持有素材锁，永久删除不能在二者之间移除音频。
        # 该锁只保护本机读取；返回内存中的内容后即可释放，不长期阻止资料管理。
        with LibraryMetadata(DATA).use_assets([{"kind": "recording", "id": recording_id}]):
            try:
                path = assistant.recording_path(recording_id)
                if path.stat().st_size > MAX_MCP_AUDIO_BYTES:
                    raise ValueError("录音超过 MCP 内联音频的 12 MB 上限，请使用更短片段。")
                # 有界读取防止外部程序同时扩展文件导致超量内存分配。
                with path.open("rb") as audio_file:
                    audio = audio_file.read(MAX_MCP_AUDIO_BYTES + 1)
            except OSError:
                raise ValueError("无法读取录音文件，请检查文件是否完整并稍后重试。") from None
        if len(audio) > MAX_MCP_AUDIO_BYTES:
            raise ValueError("录音超过 MCP 内联音频的 12 MB 上限。")
        try:
            with wave.open(io.BytesIO(audio), "rb") as reader:
                expected = reader.getnframes() * reader.getnchannels() * reader.getsampwidth()
                if reader.getnframes() <= 0 or expected > MAX_MCP_AUDIO_BYTES:
                    raise ValueError("音频为空或音频数据大小无效。")
                if len(reader.readframes(reader.getnframes())) != expected:
                    raise ValueError("音频尚未写入完整，请等待录音任务结束。")
        except (wave.Error, EOFError):
            raise ValueError("录音不是可读取的完整 WAV。") from None
        return [
            TextContent(type="text", text="以下是实际录制的 SynthV WAV 音频。只有客户端和模型支持音频输入时才能直接听评；技术指标不能代替试听。"),
            AudioContent(type="audio", mimeType="audio/wav", data=base64.b64encode(audio).decode("ascii")),
        ]

    return server


def run_mcp() -> None:
    """以标准输入输出运行；任何诊断应写到 stderr，不能污染协议流。"""
    service = AssistantService()
    try:
        create_mcp_server(service).run(transport="stdio")
    finally:
        # 等待已开始的短录音收尾，避免服务退出时遗留音频辅助进程。
        service.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    run_mcp()
