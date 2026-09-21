"""SynthV Assistant 命令行：本机控制台、MCP、脚本生成和采集器构建。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _port(value: str) -> int:
    """将端口限制为可显式访问的 TCP 端口，拒绝随机端口 0。"""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须为整数。") from None
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("端口必须位于 1～65535。")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """集中定义 CLI，使入口行为可以脱离真实 SynthV 进行测试。"""
    parser = argparse.ArgumentParser(prog="synthv-assistant", description="Synthesizer V Studio 2 本地调教与听音助手")
    commands = parser.add_subparsers(dest="command", title="子命令")
    parser.set_defaults(command="serve")
    serve = commands.add_parser("serve", help="启动本地网页控制台（默认）")
    # HTTP 服务现有 Host/Origin 校验只面向本机，不开放局域网绑定。
    serve.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1", help="仅允许本机回环地址")
    serve.add_argument("--port", type=_port, default=8765, help="本机控制台端口，默认 8765")
    commands.add_parser("mcp", help="以 stdio 方式提供 MCP 工具，stdout 仅包含协议消息")
    script = commands.add_parser("build-script", help="生成单文件 Lua 桥接，不自动安装到 SynthV")
    script.add_argument("--output", type=Path, help="可选的生成文件位置；默认 data/install/SynthVAssistant.lua")
    commands.add_parser("build-capture", help="使用本机 .NET SDK 构建 Windows 进程音频采集器")
    commands.add_parser("status", help="以 JSON 输出连接、采集和听评配置状态")
    return parser


def main(argv: list[str] | None = None) -> int:
    """按子命令延迟导入依赖；缺少 MCP 依赖不会影响脚本生成等离线操作。"""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "mcp":
            from .mcp_server import run_mcp
            run_mcp()
        elif args.command == "build-script":
            from .bridge import build_script
            generated = build_script(args.output)
            print(json.dumps({"script": str(generated.resolve()), "installed": False,
                              "message": "脚本已生成；请复制到 SynthV 的脚本文件夹并从脚本菜单启动。"}, ensure_ascii=False))
        elif args.command == "build-capture":
            from .capture import build_capture_helper
            executable = build_capture_helper()
            print(json.dumps({"captureExecutable": str(executable)}, ensure_ascii=False))
        elif args.command == "status":
            from .service import AssistantService
            service = AssistantService()
            try:
                print(json.dumps(service.status(), ensure_ascii=False, indent=2, allow_nan=False))
            finally:
                service.executor.shutdown(wait=True, cancel_futures=True)
        else:
            from .server import make_server
            from .service import AssistantService
            service = AssistantService()
            http_server = None
            try:
                port = getattr(args, "port", 8765)
                http_server = make_server(port=port, service=service)
                print(f"SynthV Assistant 已启动：http://127.0.0.1:{port}/", flush=True)
                print("按 Ctrl+C 停止。音频听评默认不启用，网页可直接进行本地录音和技术分析。", flush=True)
                http_server.serve_forever(poll_interval=0.25)
            finally:
                if http_server is not None:
                    http_server.server_close()
                service.executor.shutdown(wait=True, cancel_futures=True)
        return 0
    except KeyboardInterrupt:
        print("助手已停止。", file=sys.stderr)
        return 0
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        # 不打印 traceback 和环境变量；stdio MCP 的错误也只写 stderr。
        print(f"无法完成操作：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
