"""会话级模型选项与协议能力；公开模型目录不等于完整的能力注册表。

这些保守规则依据 2026-09-22 官方文档。未知兼容服务允许显式选择标准
reasoning_effort，但明确标注未验证；绝不在失败后偷偷换模型或降级重试。
"""

import re


DEFAULT_OPTIONS = {"platformId": "default", "model": "", "reasoningEffort": "default"}
EFFORT_LABELS = {"default": "模型默认", "none": "关闭", "minimal": "极低", "low": "低",
                 "medium": "中", "high": "高", "xhigh": "更高", "max": "最高"}


def normalize_model_options(value=None):
    """只保存三个公开选择；禁止请求把地址、密钥或任意供应商参数混进会话。"""
    if value is None:
        return dict(DEFAULT_OPTIONS)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_OPTIONS):
        raise ValueError("模型选项只允许平台、模型名称与推理强度。")
    result = {**DEFAULT_OPTIONS, **value}
    platform = result["platformId"]
    if not isinstance(platform, str) or not re.fullmatch(r"default|[0-9a-f]{32}", platform):
        raise ValueError("模型平台编号无效，请重新选择平台。")
    model = result["model"]
    if not isinstance(model, str) or len(model) > 200:
        raise ValueError("模型名称最长 200 个字符。")
    model = model.strip()
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*", model):
        raise ValueError("请填写有效模型 ID，不能包含 URL、空格或查询参数。")
    result["model"] = model
    if not isinstance(result["reasoningEffort"], str) or result["reasoningEffort"] not in EFFORT_LABELS:
        raise ValueError("推理强度无效，请重新选择。")
    return result


def capabilities(config, model=None):
    """只返回本地能力说明，不联网、不把配置地址或凭据返回到会话。"""
    name = model or config.get("model", "")
    provider = config.get("provider")
    values, supported, audio, kind = [], False, "unknown", None
    note = "该模型未声明可调推理强度，使用模型默认设置。"
    if provider == "openai":
        if re.fullmatch(r"gpt-6-astra(?:-\d{4}-\d{2}-\d{2})?", name):
            values, supported, audio = ["low", "medium", "high", "xhigh", "max"], True, "unsupported"
        elif re.fullmatch(r"gpt-5\.6(?:-sol)?(?:-\d{4}-\d{2}-\d{2})?", name):
            values, supported, audio = ["none", "low", "medium", "high", "xhigh", "max"], True, "unsupported"
        elif re.match(r"(?:gpt-audio|gpt-4o(?:-mini)?-audio)(?:-|$)", name):
            audio = "supported"
        elif re.match(r"(?:gpt-4(?:[.-]|$)|gpt-3\.5)", name):
            audio = "unsupported"
        else:
            values, supported = ["none", "minimal", "low", "medium", "high", "xhigh", "max"], None
        kind = "effort"
        if supported is None:
            note = "模型列表不提供完整推理能力；这里按兼容协议传递所选值，需平台支持。默认不额外传参。"
        elif supported:
            note = "按该模型已公布的推理档位传参；默认不额外传参。兼容平台的实际支持以响应为准。"
    elif provider == "qwen":
        # 百炼 Chat Completions 虽采用兼容消息结构，推理档位仍须按 Qwen
        # 自己的契约校验。qwen3.8-flash / max 支持图像、文字和视频，不支持音频。
        # 其他 Qwen 系列只开放默认文本请求，不擅自复用这些型号的音频或推理能力。
        audio = "unsupported"
        note = "当前 Qwen 接口仅接入文本调教；此型号的推理参数尚未适配，请使用模型默认。"
        if name in {"qwen3.8-flash", "qwen3.8-max", "qwen3.8-omni-flash"}:
            values, supported, kind = ["none", "low", "medium", "xhigh"], True, "qwen-effort"
            audio = "supported" if name == "qwen3.8-omni-flash" else "unsupported"
            note = "Qwen 使用 low / medium / xhigh 推理档位，关闭时传 reasoning_effort=none；默认不覆盖推理档位。"
            # 音频可用性由独立的 audioInput 字段统一展示，避免与前端提示重复。
            if audio == "supported":
                note += "Omni 音频附件须符合本地大小限制。"
    elif provider == "gemini":
        # 不把图像或实时专用模型误当成普通 generateContent 调教模型。
        if re.match(r"gemini-3(?:[.-])", name) and not any(part in name for part in ("image", "live")):
            values, supported, audio, kind = ["low", "medium", "high"], True, "supported", "level"
            if re.match(r"gemini-3(?:\.[1356])?-flash", name):
                values.insert(0, "minimal")
            note = "Gemini 3 使用 thinkingLevel；各系列支持档位不同，默认由模型决定。"
        elif re.match(r"gemini-2\.5-(?:pro|flash)(?:-|$)", name) and not any(part in name for part in ("image", "live")):
            values, supported, audio, kind = ["low", "medium", "high"], True, "supported", "budget"
            if "-pro" not in name:
                values.insert(0, "none")
            note = "Gemini 2.5 使用思考预算：低 1024、中 8192、高 16384 tokens；默认不覆盖预算。"
    return {"provider": provider, "model": name, "audioInput": audio,
            "reasoning": {"supported": supported, "options": [{"value": key, "label": EFFORT_LABELS[key]}
                              for key in ["default", *values]], "note": note}, "reasoningKind": kind}


def validate_for_config(config, options):
    """请求开始时再检查，防止过期界面或手写 HTTP 绕过模型档位限制。"""
    if config.get("provider") == "gemini" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", config.get("model", "")):
        raise ValueError("Gemini 模型名称无效，请勿填写 models/ 前缀。")
    result = capabilities(config)
    allowed = {item["value"] for item in result["reasoning"]["options"]}
    if options["reasoningEffort"] not in allowed:
        raise ValueError("当前模型不支持所选推理强度，请改为模型默认或支持的档位。")
    return result


def apply_reasoning(body, config, effort, *, include_summary=False):
    """映射供应商参数；default 始终省略强度，不用 0 或 none 冒充模型默认。"""
    info = validate_for_config(config, {"reasoningEffort": effort})
    if config["provider"] == "openai":
        if effort != "default":
            body["reasoning_effort"] = effort
    elif config["provider"] == "qwen":
        # 直接发送 HTTP JSON 时扩展字段位于顶层，不能照搬 Python SDK 的
        # extra_body 包装。强度默认时省略该字段；官方 none 本身关闭思考。
        # 本工作台仅保存有限公开摘要，不具备完整思考历史，因此关闭默认开启的
        # preserve_thinking，避免把截断摘要当成下一轮所需的完整推理历史。
        if info["reasoningKind"] == "qwen-effort":
            body["preserve_thinking"] = False
        if effort != "default":
            body["reasoning_effort"] = effort
    elif info["reasoningKind"]:
        thinking = {}
        if effort != "default":
            if info["reasoningKind"] == "budget":
                thinking["thinkingBudget"] = {"none": 0, "low": 1024, "medium": 8192, "high": 16384}[effort]
            else:
                thinking["thinkingLevel"] = effort
        if include_summary:
            thinking["includeThoughts"] = True
        if thinking:
            generation = body.setdefault("generationConfig", {})
            generation["thinkingConfig"] = thinking
            # 输出上限包含思考 tokens；旧 4096 会把高强度请求在输出前截断。
            generation.pop("maxOutputTokens", None)
    return info
