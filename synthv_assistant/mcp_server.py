"""供 AI 客户端调用的本地 stdio MCP 入口。

所有操作复用 AssistantService 的校验、备份和任务逻辑。本模块不提供任意
Python/Lua 执行工具，也不接受本机任意文件路径。stdout 专用于 MCP 协议。
"""

from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import StrictFloat, StrictInt

from .service import AssistantService
from .parameters import ParameterError, validate_preview_payload, public_parameter_catalog, public_computed_pitch


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
    # MCP 使用工具协议，不套用网页会话 JSON 格式；音乐证据和确认原则与其它
    # AI 入口集中维护，避免工具说明与网页规划出现不同的参数单位或音频假设。
    from .prompts import MCP_INSTRUCTION
    server = ParameterMCPServer(
        "SynthV Assistant",
        instructions=MCP_INSTRUCTION,
        log_level="WARNING",
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    local_change = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    @server.tool(annotations=readonly)
    def status() -> dict:
        """读取桥接与写入模式；不暴露已退役的录音、音频目录或供应商配置。"""
        state = assistant.status()
        # 与业务层保持字段一致；锁状态为未知或读取失败时也必须保留，避免客户端
        # 把缺失的跨进程占用信息误判成空闲。音频状态仍不进入 MCP 返回值。
        return {key: state[key] for key in ("bridge", "writeEnabled", "operationBusy", "operationStatusError")
                if key in state}

    @server.tool(annotations=readonly)
    def get_project() -> dict:
        """读取当前 SynthV 工程结构与播放状态，要求宿主 Lua 桥接已启动。"""
        return assistant.get_project()

    @server.tool(annotations=readonly)
    def get_selection() -> dict:
        """读取当前选中音符、时间范围及参数；此工具不改变选区。"""
        return assistant.get_selection()

    @server.tool(annotations=readonly)
    def get_parameter_catalog() -> dict:
        """读取当前选区的可编辑参数、声线默认项及宿主已计算音高，不改动工程。

        先依据目录选择参数名称、单位与 maxDelta，再调用预览工具。声线目录只含
        宿主设置中出现或用户手动核对的名称，complete=false；voiceValues 的
        pitch/timbre/pronunciation 为只读默认项，不是三个独立自动化通道。
        pitchCurve 写入真正的 PitchControlCurve；computedPitch 只是只读参考。
        """
        selection = assistant.get_selection()
        if not isinstance(selection, dict):
            raise ValueError("宿主未返回有效选区，请重新读取。")
        catalog = public_parameter_catalog(selection)
        modes = []
        for parameter, definition in catalog.items():
            if definition.get("kind") == "vocalMode":
                modes.append({"name": definition["modeName"], "parameter": parameter,
                              "source": definition.get("source", "host"),
                              "values": definition.get("voiceValues", {})})
        # 仅使用经过公共契约筛选的有界字段，不将工程路径、曲线指纹或宿主原始
        # getVoice 对象传给调用模型；缺少计算音高时保留未知状态，不补造基线。
        result = {"parameters": catalog, "vocalModes": {"complete": False, "items": modes}}
        pitch = public_computed_pitch(selection.get("computedPitch"))
        if pitch is not None:
            result["computedPitch"] = pitch
        return result

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

    return server


def run_mcp() -> None:
    """以标准输入输出运行；任何诊断应写到 stderr，不能污染协议流。"""
    service = AssistantService()
    try:
        create_mcp_server(service).run(transport="stdio")
    finally:
        # 等待已经开始的受控后台任务收尾，避免 stdio 退出留下未完成工作。
        service.executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    run_mcp()
