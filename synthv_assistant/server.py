"""仅绑定回环地址的本地控制台 HTTP 服务。

校验 Host、Origin 和每次启动的随机令牌，拒绝跨站网页静默触发工程编辑。
静态资源与录音都从固定白名单读取，不提供任意文件浏览或脚本执行接口。
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
import re
import secrets
from urllib.parse import urlsplit, unquote

from .config import ROOT
from .service import AssistantService
from .settings import SettingsConflictError
from .metadata import validate_purge_confirmation


def _unique_json_object(pairs):
    """拒绝重复字段，避免永久删除确认在同一 JSON 中出现互相矛盾的值。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("请求不能包含重复的 JSON 字段。")
        result[key] = value
    return result


def make_server(port: int = 8765, service: AssistantService | None = None) -> ThreadingHTTPServer:
    service = service or AssistantService()
    token = secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + host for host in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            """安静运行；stdout 可供 CLI 展示必要诊断，不输出请求参数。"""

        def response_json(self, value, status=200):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def allowed_request(self):
            if self.headers.get("Host", "") not in hosts:
                self.response_json({"error": "仅允许本机回环地址访问。"}, 403)
                return False
            if self.headers.get("Origin") and self.headers["Origin"] not in origins:
                self.response_json({"error": "拒绝跨站请求。"}, 403)
                return False
            return True

        def allowed_token(self):
            """修改入口和配置读取入口均要求当前本机会话令牌。

            读取设置虽然不返回密钥，仍含用户的服务地址与模型偏好；
            因此不能只依赖 Host 校验。令牌不放入 URL，避免进入历史记录。
            """
            supplied_token = self.headers.get("X-SV-Token", "")
            if not supplied_token.isascii() or not secrets.compare_digest(supplied_token, token):
                self.response_json({"error": "本机会话已变化，请刷新页面。"}, 403)
                return False
            return True

        def settings_response(self, operation, *args):
            """配置请求使用独立错误边界，绝不回显底层异常或请求内容。

            配置模块的 ValueError 是固定中文验证信息，可以直接展示；
            文件系统和系统加密等意外异常只返回通用提示，不打印 traceback。
            """
            try:
                return self.response_json(operation(*args))
            except SettingsConflictError as error:
                # 独立的冲突状态让网页要求重新读取，避免重复提交旧版本。
                return self.response_json({"error": str(error)}, 409)
            except ValueError as error:
                return self.response_json({"error": str(error)}, 400)
            except Exception:
                return self.response_json({"error": "无法读取或保存模型设置，请检查本机配置文件和 Windows 账户权限。"}, 500)

        def workbench_response(self, operation, *args):
            """会话和上传使用独立错误边界，不回显模型内部响应或磁盘路径。"""
            try:
                return self.response_json(operation(*args))
            except (ValueError, RuntimeError) as error:
                return self.response_json({"error": str(error)}, 400)
            except Exception:
                return self.response_json({"error": "工作台操作未完成，请检查本机服务状态后重试。"}, 500)

        def do_GET(self):
            if not self.allowed_request():
                return
            path = urlsplit(self.path).path
            try:
                if path == "/api/bootstrap":
                    return self.response_json({"token": token})
                if path == "/api/audio-settings":
                    if not self.allowed_token():
                        return
                    return self.settings_response(service.get_audio_settings)
                if path == "/api/model-platforms" or path.startswith("/api/model-platforms/"):
                    if not self.allowed_token():
                        return
                    if path == "/api/model-platforms":
                        return self.settings_response(service.list_model_platforms)
                    match = re.fullmatch(r"/api/model-platforms/(default|[0-9a-f]{32})/models", path)
                    if match:
                        return self.settings_response(service.cached_platform_models, match[1])
                    match = re.fullmatch(r"/api/model-platforms/(default|[0-9a-f]{32})", path)
                    if match:
                        return self.settings_response(service.get_model_platform, match[1])
                    return self.response_json({"error": "模型平台不存在。"}, 404)
                if path == "/api/trash":
                    if not self.allowed_token():
                        return
                    return self.workbench_response(service.list_trash)
                if path == "/api/conversations" or path == "/api/uploads" or path.startswith("/api/conversations/"):
                    # 会话含用户描述和歌词，读取时同样要求当前页面的令牌。
                    if not self.allowed_token():
                        return
                    if path == "/api/conversations":
                        return self.workbench_response(service.list_conversations)
                    if path == "/api/uploads":
                        return self.workbench_response(service.list_uploads)
                    match = re.fullmatch(r"/api/conversations/([0-9a-f]{32})", path)
                    if match:
                        return self.workbench_response(service.get_conversation, match[1])
                    return self.response_json({"error": "会话不存在。"}, 404)
                if path == "/api/status":
                    return self.response_json(service.status())
                if path == "/api/project":
                    return self.response_json(service.get_project())
                if path == "/api/selection":
                    return self.response_json(service.get_selection())
                if path == "/api/recordings":
                    if not self.allowed_token():
                        return
                    return self.response_json(service.list_recordings())
                if path.startswith("/api/jobs/"):
                    # 聊天任务的结果包含完整会话，不能从轮询接口绕过会话读取认证。
                    if not self.allowed_token():
                        return
                    job = service.jobs.get(path.rsplit("/", 1)[-1])
                    return self.response_json(job or {"error": "任务不存在。"}, 200 if job else 404)
                if path.startswith("/audio/") and path.endswith(".wav"):
                    file, data = service.read_audio("recording", path[7:-4])
                elif path.startswith("/uploads/") and path.endswith(".wav"):
                    file, data = service.read_audio("upload", path[9:-4])
                else:
                    # 静态资源使用显式白名单；共享 UI 模块和页面脚本遵循同源 CSP，不开放任意文件读取。
                    name = {"/": "index.html", "/index.html": "index.html", "/ui.js": "ui.js", "/curves.js": "curves.js", "/app.js": "app.js", "/chat.js": "chat.js", "/models.js": "models.js", "/layout.js": "layout.js", "/pages.js": "pages.js", "/style.css": "style.css"}.get(path)
                    if not name:
                        return self.response_json({"error": "页面不存在。"}, 404)
                    file = ROOT / "web" / name
                    data = file.read_bytes()
                mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                # 支持音频 Range 请求，确保浏览器 A/B 播放器可拖动进度。
                start, end, status = 0, len(data)-1, 200
                range_header = self.headers.get("Range", "")
                if file.suffix == ".wav" and range_header.startswith("bytes="):
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
                    if not match:
                        return self.response_json({"error": "不支持的音频范围。"}, 416)
                    start = int(match[1]); end = min(int(match[2]) if match[2] else end, end)
                    if start > end:
                        return self.response_json({"error": "音频范围越界。"}, 416)
                    status = 206
                self.send_response(status)
                self.send_header("Content-Type", mime + ("; charset=utf-8" if file.suffix != ".wav" else ""))
                self.send_header("Content-Length", str(end-start+1))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
                if file.suffix == ".wav":
                    self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.end_headers(); self.wfile.write(data[start:end+1])
            except OSError:
                self.response_json({"error": "无法读取本地文件，请刷新列表后重试。"}, 400)
            except (ValueError, RuntimeError) as error:
                self.response_json({"error": str(error)}, 400)

        def do_POST(self):
            if not self.allowed_request():
                return
            if not self.allowed_token():
                return
            try:
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                if path == "/api/uploads":
                    # 原始音频不做 Base64 封装；认证和大小检查必须发生在读文件之前。
                    from .assets import MAX_UPLOAD_BYTES
                    if self.headers.get("Transfer-Encoding") or not 44 < length <= MAX_UPLOAD_BYTES:
                        raise ValueError("上传文件大小无效，单个文件不得超过 12 MB。")
                    if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/octet-stream":
                        raise ValueError("请通过工作台上传 WAV 或 MP3 文件。")
                    filename = unquote(self.headers.get("X-File-Name", ""), errors="strict")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("音频上传不完整，请重新上传。")
                    return self.workbench_response(service.save_upload, filename, raw)
                if not 0 < length <= 65536:
                    raise ValueError("请求大小无效。")
                args = json.loads(self.rfile.read(length), object_pairs_hook=_unique_json_object)
                if not isinstance(args, dict):
                    raise ValueError("请求必须是 JSON 对象。")
                if path == "/api/audio-settings":
                    return self.settings_response(service.update_audio_settings, args)
                elif path == "/api/model-platforms":
                    return self.settings_response(service.save_model_platform, args)
                elif path == "/api/model-platforms/default-selection":
                    return self.settings_response(service.set_default_model_platform, args)
                elif path == "/api/model-capabilities":
                    return self.workbench_response(service.model_capabilities, args)
                elif re.fullmatch(r"/api/model-platforms/(?:default|[0-9a-f]{32})/models", path):
                    if args:
                        raise ValueError("获取模型目录不接受额外参数。")
                    return self.settings_response(service.list_platform_models, path.split("/")[3])
                elif path == "/api/audio-settings/clear":
                    return self.settings_response(service.clear_audio_settings)
                elif path == "/api/conversations":
                    return self.workbench_response(service.create_conversation, args.get("title", "新调教会话"))
                elif re.fullmatch(r"/api/conversations/[0-9a-f]{32}/metadata", path):
                    return self.workbench_response(service.update_conversation_metadata, path.split("/")[3], args)
                elif re.fullmatch(r"/api/conversations/[0-9a-f]{32}/model-options", path):
                    return self.workbench_response(service.update_conversation_model_options, path.split("/")[3], args)
                elif re.fullmatch(r"/api/conversations/[0-9a-f]{32}/render-mode", path):
                    return self.workbench_response(service.update_conversation_render_mode, path.split("/")[3], args)
                elif re.fullmatch(r"/api/conversations/[0-9a-f]{32}/reuse", path):
                    # 浏览器只能指定已有消息；曲线、目标及新凭据全部由本机重新校验。
                    if set(args) != {"messageId"}:
                        raise ValueError("复用方案只接受 messageId 字段。")
                    return self.workbench_response(service.reuse_message, path.split("/")[3], args["messageId"])
                elif re.fullmatch(r"/api/conversations/[0-9a-f]{32}/delete", path):
                    if args:
                        raise ValueError("移入回收站不接受额外字段。")
                    return self.workbench_response(service.delete_conversation, path.split("/")[3])
                elif re.fullmatch(r"/api/conversations/[0-9a-f]{32}/purge", path):
                    validate_purge_confirmation(args)
                    return self.workbench_response(service.purge_conversation, path.split("/")[3], args)
                elif re.fullmatch(r"/api/assets/(?:upload|recording)/[0-9a-f]{32}/metadata", path):
                    return self.workbench_response(service.update_asset_metadata, path.split("/")[3], path.split("/")[4], args)
                elif re.fullmatch(r"/api/assets/(?:upload|recording)/[0-9a-f]{32}/delete", path):
                    if args:
                        raise ValueError("移入回收站不接受额外字段。")
                    return self.workbench_response(service.delete_asset, path.split("/")[3], path.split("/")[4])
                elif re.fullmatch(r"/api/assets/(?:upload|recording)/[0-9a-f]{32}/purge", path):
                    validate_purge_confirmation(args)
                    return self.workbench_response(service.purge_asset, path.split("/")[3], path.split("/")[4], args)
                elif re.fullmatch(r"/api/trash/(?:conversation|upload|recording)/[0-9a-f]{32}/purge", path):
                    validate_purge_confirmation(args)
                    return self.workbench_response(service.purge_trash, path.split("/")[3], path.split("/")[4], args)
                elif re.fullmatch(r"/api/trash/(?:conversation|upload|recording)/[0-9a-f]{32}/restore", path):
                    if args:
                        raise ValueError("恢复资料不接受额外字段。")
                    return self.workbench_response(service.restore_resource, path.split("/")[3], path.split("/")[4])
                elif re.fullmatch(r"/api/conversations/[0-9a-f]{32}/messages", path):
                    # 排队只返回任务编号；纯文字和音频请求都通过同一有界任务入口。
                    identifier = path.split("/")[3]
                    optional = [args["modelOptions"]] if "modelOptions" in args else []
                    if "renderMode" in args:
                        # 入队前验证，非法模式不能被 null 回退成默认，也不能触发付费请求。
                        from .parameters import normalize_render_mode
                        optional = [args.get("modelOptions"), normalize_render_mode(args["renderMode"])]
                    result = {"jobId": service.submit(service.send_message, identifier, args.get("text"),
                                                      args.get("includeSelection", True), args.get("attachments", []), *optional)}
                elif re.fullmatch(r"/api/assistant/actions/[0-9a-f]{32}/(?:preview|apply)", path):
                    operation = service.preview_action if path.endswith("/preview") else service.apply_action
                    return self.workbench_response(operation, path.split("/")[4])
                elif path == "/api/assistant/batches/preview":
                    if set(args) != {"actionIds"}:
                        raise ValueError("组合预览只接受 actionIds 字段。")
                    return self.workbench_response(service.preview_action_batch, args["actionIds"])
                elif path == "/api/assistant/batches/apply":
                    if set(args) != {"batchId"}:
                        raise ValueError("组合确认只接受 batchId 字段。")
                    return self.workbench_response(service.apply_action_batch, args["batchId"])
                elif path == "/api/write-mode":
                    result = service.write_mode(args.get("enabled"))
                elif path == "/api/parameters/vocal-mode":
                    # 仅用户界面提供此目录补充入口；模型/MCP没有自动注册权限。
                    # 与所有 POST 共用会话令牌，业务层按原选区身份向宿主确认。
                    return self.workbench_response(service.register_vocal_mode, args)
                elif path == "/api/preview":
                    # 精确校验动作入口的键，未知对象不能被静默忽略为另一个预览。
                    # 完整数值及宿主能力校验统一留在 service/parameters，不能由网页绕过。
                    from .parameters import validate_preview_payload
                    validate_preview_payload(args)
                    if "curve" in args:
                        result = service.preview(args["parameter"], curve=args["curve"],
                                                 render_mode=args.get("renderMode", "smooth"))
                    elif "renderMode" in args:
                        result = service.preview(args["parameter"], args["delta"], render_mode=args["renderMode"])
                    else:
                        result = service.preview(args["parameter"], args["delta"])
                elif path == "/api/apply":
                    result = service.edit("apply", {"previewId": args.get("previewId")})
                elif path == "/api/restore":
                    result = service.edit("restore")
                elif path == "/api/record":
                    result = {"jobId": service.submit(service.record, args.get("startSeconds"), args.get("durationSeconds"), args.get("label", "片段"))}
                elif path == "/api/compare":
                    result = service.compare(args.get("beforeId"), args.get("afterId"))
                elif path == "/api/review":
                    result = {"jobId": service.submit(service.review, args.get("recordingIds"), args.get("prompt"))}
                else:
                    return self.response_json({"error": "接口不存在。"}, 404)
                self.response_json(result)
            except (ValueError, RuntimeError, OSError, TypeError) as error:
                self.response_json({"error": str(error)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
