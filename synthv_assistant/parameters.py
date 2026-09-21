"""参数提案的共同契约：只接受宿主支持的有限数值，不解释任意代码。

模型、会话持久层、HTTP/MCP 和业务层均复用这里的校验。宿主仍需在真正写入
前再次验证能力、选区和曲线快照；Python 校验不能替代宿主的并发冲突保护。
旧桥接的五种整体增量继续兼容，新增参数、原生音高与绘制曲线必须显式协商能力。
"""

from __future__ import annotations

import math


LEGACY_PARAMETERS = frozenset({"breathiness", "tension", "gender", "loudness", "pitchDelta"})
PARAMETER_LIMITS = {"breathiness": 0.3, "tension": 0.3, "gender": 0.3,
                    "loudness": 6.0, "pitchDelta": 100.0, "toneShift": 200.0,
                    "vibratoEnv": 0.3}
MAX_CURVE_POINTS = 64
MAX_PREVIEW_POINTS = 256
MAX_PREVIEW_CONTROL_POINTS = 4000


def normalize_render_mode(value="smooth") -> str:
    """统一会话绘制偏好；仅接受明确的枚举，不能把 null 或布尔值当默认值。"""
    if not isinstance(value, str) or value not in {"smooth", "points"}:
        raise ParameterError("请选择绘制模式或控制点模式。")
    return value


class ParameterError(ValueError):
    """固定中文验证错误，绝不把模型原文、工程路径或密钥拼进异常。"""


def finite(value, low: float, high: float) -> bool:
    """先比较范围再转浮点，避免超大 JSON 整数在 isfinite 中溢出。"""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and low <= value <= high and math.isfinite(value))


def supports_curves(selection: dict | None) -> bool:
    capabilities = selection.get("capabilities") if isinstance(selection, dict) else None
    return isinstance(capabilities, dict) and capabilities.get("curves") is True


def _definition(parameter: str, selection: dict | None) -> dict | None:
    catalog = selection.get("parameters") if isinstance(selection, dict) else None
    value = catalog.get(parameter) if isinstance(catalog, dict) else None
    return value if isinstance(value, dict) else None


def native_pitch_range(selection: dict | None) -> tuple[float, float]:
    """原生音高的保守音域：当前音符加组移调后，整体上下最多扩展两个半音。"""
    if not isinstance(selection, dict):
        raise ParameterError("原生音高缺少当前音符资料，请重新读取选区。")
    offset, notes = selection.get("groupPitchOffset", 0), selection.get("notes")
    if (not finite(offset, -127, 127) or not isinstance(notes, list) or not 1 <= len(notes) <= 128
            or any(not isinstance(note, dict) or not finite(note.get("pitch"), 0, 127) for note in notes)):
        raise ParameterError("原生音高的音符或组移调资料无效，请重新读取选区。")
    pitches = [float(note["pitch"]) + float(offset) for note in notes]
    low, high = max(0.0, min(pitches) - 2), min(127.0, max(pitches) + 2)
    if low > high:
        raise ParameterError("当前音符的工程音高超出可用范围，不能生成原生音高曲线。")
    return low, high


def parameter_policy(parameter: object, selection: dict | None) -> tuple[float, bool]:
    """返回本次允许的偏移上限及是否为绝对 MIDI 曲线。

    动态声库名称必须逐字匹配本次宿主目录中的 modeName，不能仅凭 vocalMode_
    前缀获得权限。宿主报告的上限只能收紧本地策略，不能扩大安全调整范围。
    """
    if (not isinstance(parameter, str) or not 1 <= len(parameter) <= 160
            or any(ord(character) < 32 for character in parameter)):
        raise ParameterError("参数名称无效，请重新读取当前选区。")
    definition = _definition(parameter, selection)
    capabilities = selection.get("capabilities") if isinstance(selection, dict) else None
    # 兼容回退只属于没有能力目录的旧桥接。新桥接即使把 curves 显式关闭，
    # 也不能把缺失/已过滤的禁用参数重新解释为默认可用的旧五参数。
    has_current_catalog = isinstance(capabilities, dict) and "curves" in capabilities
    if definition is not None:
        if "available" in definition and not isinstance(definition["available"], bool):
            raise ParameterError("宿主参数可用状态无效，请重新读取当前选区。")
        if definition.get("available") is False:
            raise ParameterError("当前声库或选区不支持该参数，请重新读取选区。")
    if parameter in PARAMETER_LIMITS:
        limit = PARAMETER_LIMITS[parameter]
        if parameter not in LEGACY_PARAMETERS:
            if not supports_curves(selection) or definition is None or definition.get("kind") != "automation":
                raise ParameterError("新增参数需要当前桥接明确支持，请更新桥接并重新读取选区。")
        else:
            if has_current_catalog and definition is None:
                raise ParameterError("该参数未出现在当前可用目录中，请重新读取选区。")
            if definition is not None and definition.get("kind", "automation") != "automation":
                raise ParameterError("宿主参数类型不匹配，请重新读取当前选区。")
    elif parameter.startswith("vocalMode_"):
        name = parameter[len("vocalMode_"):]
        if (not name or not supports_curves(selection) or definition is None
                or definition.get("kind") != "vocalMode" or definition.get("modeName") != name):
            raise ParameterError("该声库模式不在当前宿主参数目录中，请重新读取选区。")
        limit = 30.0
    elif parameter == "pitchCurve":
        capabilities = selection.get("capabilities", {}) if isinstance(selection, dict) else {}
        if (not supports_curves(selection) or not isinstance(capabilities, dict)
                or capabilities.get("nativePitch") is not True or definition is None
                or definition.get("kind") != "pitch" or definition.get("available") is not True):
            raise ParameterError("当前宿主未提供可用的原生音高曲线，请重新读取选区。")
        return 127.0, True
    else:
        raise ParameterError("参数不在当前允许的调教目录中。")
    if definition is not None and "maxDelta" in definition:
        advertised = definition["maxDelta"]
        if not finite(advertised, 0, 1_000_000) or advertised <= 0:
            raise ParameterError("宿主参数调整上限无效，请重新读取当前选区。")
        limit = min(limit, float(advertised))
    return limit, False


def validate_change(parameter, delta=None, *, curve=None, render_mode="smooth", selection=None) -> dict:
    """校验一次偏移或曲线请求，返回可安全转发给宿主的规范字典。

    curve 横轴固定为当前连续选区的比例 0..1，首尾必须完整覆盖选区；偏移曲线
    的淡入淡出由宿主执行。pitchCurve 的纵轴为工程绝对 MIDI 半音，不能当 cents。
    只验证有限控制点，不擅自排序、补端点、裁剪值或改变用户要求的表示方式。
    """
    if not isinstance(render_mode, str) or render_mode not in {"smooth", "points"}:
        raise ParameterError("曲线表示方式只能是 smooth 或 points。")
    if (curve is None) == (delta is None):
        raise ParameterError("请只提供一个参数增量或一条曲线。")
    limit, absolute_pitch = parameter_policy(parameter, selection)
    if absolute_pitch and render_mode != "smooth":
        raise ParameterError("原生音高曲线只支持 smooth；控制点表示请使用 pitchDelta。")
    result = {"parameter": parameter}
    if curve is not None:
        if not supports_curves(selection):
            raise ParameterError("当前桥接尚未支持绘制曲线，请更新桥接并重新读取选区。")
        if not isinstance(curve, list) or not 2 <= len(curve) <= MAX_CURVE_POINTS:
            raise ParameterError("曲线必须包含 2 至 64 个控制点。")
        checked = []
        previous = -1.0
        low, high = native_pitch_range(selection) if absolute_pitch else (-limit, limit)
        for point in curve:
            if (not isinstance(point, list) or len(point) != 2 or not finite(point[0], 0, 1)
                    or point[0] <= previous or not finite(point[1], low, high)):
                raise ParameterError("曲线点必须按位置严格递增，位置在 0 到 1 之间且数值不得越界。")
            previous = float(point[0])
            checked.append([previous, float(point[1])])
        if checked[0][0] != 0 or checked[-1][0] != 1:
            raise ParameterError("曲线首尾位置必须分别为 0 和 1。")
        if not absolute_pitch and not any(point[1] != 0 for point in checked):
            raise ParameterError("偏移曲线不能全部为零。")
        result.update(curve=checked, renderMode=render_mode)
    else:
        if absolute_pitch:
            raise ParameterError("原生音高只接受绝对 MIDI 曲线，不能使用整体增量。")
        if not finite(delta, -limit, limit) or delta == 0:
            raise ParameterError("参数增量必须是安全范围内的有限非零数字。")
        result["delta"] = float(delta)
        if render_mode != "smooth":
            if not supports_curves(selection):
                raise ParameterError("当前桥接尚未支持该曲线表示方式，请更新桥接。")
            result["renderMode"] = render_mode
    return result


def validate_action(action: object, selection: dict | None, *, reason_limit: int = 2000) -> dict:
    """模型及持久层共用精确字段校验，拒绝未知键和同时提供 delta/curve。"""
    if not isinstance(action, dict):
        raise ParameterError("参数动作必须是有效的对象。")
    fields = set(action)
    if (not {"parameter", "reason"} <= fields or fields - {"parameter", "reason", "delta", "curve", "renderMode"}
            or (("delta" in fields) == ("curve" in fields))):
        raise ParameterError("参数动作包含缺失、冲突或未知字段。")
    reason = action["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > reason_limit:
        raise ParameterError("参数动作缺少有效的调整原因。")
    # 显式 null 不能被当作“未传入该字段”退回另一条路径。
    if action.get("curve" if "curve" in action else "delta") is None:
        raise ParameterError("参数动作的增量或曲线不能为空。")
    changed = validate_change(action["parameter"], action.get("delta"), curve=action.get("curve"),
                              render_mode=action.get("renderMode", "smooth"), selection=selection)
    return {**changed, "reason": reason.strip()}


def validate_preview_payload(payload: object) -> None:
    """在 HTTP/MCP 自动类型转换之前拒绝未知字段及字符串伪装的数值/数组。

    本层只确认请求的原始 JSON 形状，不提前读取宿主；实际参数目录、每项数值
    上限和完整曲线规则由 validate_change 在工程操作锁内统一检查。
    """
    if (not isinstance(payload, dict) or set(payload) - {"parameter", "delta", "curve", "renderMode"}
            or not isinstance(payload.get("parameter"), str)
            or (("delta" in payload) == ("curve" in payload))):
        raise ParameterError("预览必须只含参数、增量或曲线，以及可选表示方式。")
    mode = payload.get("renderMode", "smooth")
    if not isinstance(mode, str) or mode not in {"smooth", "points"}:
        raise ParameterError("曲线表示方式只能是 smooth 或 points。")
    if "delta" in payload:
        if not finite(payload["delta"], -1_000_000, 1_000_000):
            raise ParameterError("参数增量必须是有限数字，不能使用布尔值或数字字符串。")
    else:
        curve = payload["curve"]
        if (not isinstance(curve, list) or not 2 <= len(curve) <= MAX_CURVE_POINTS
                or any(not isinstance(point, list) or len(point) != 2
                       or not finite(point[0], 0, 1) or not finite(point[1], -1_000_000, 1_000_000)
                       for point in curve)):
            raise ParameterError("曲线必须是包含有限数字的控制点数组，不能使用字符串或布尔值。")


def public_parameter_catalog(selection: dict) -> dict:
    """仅公开经过目录策略检查的参数字段，私有 fingerprint 不发送给云模型。"""
    catalog = selection.get("parameters")
    if not isinstance(catalog, dict):
        return {}
    result = {}
    # 参数数量本身也有界，避免损坏桥接把任意庞大对象送往模型上下文。
    if len(catalog) > 128:
        raise ParameterError("宿主参数目录过大，请重新读取选区。")
    for name, definition in catalog.items():
        if not isinstance(definition, dict):
            continue
        try:
            parameter_policy(name, selection)
        except ParameterError:
            continue
        public = {}
        for key in ("label", "unit", "kind", "modeName"):
            value = definition.get(key)
            if isinstance(value, str) and len(value) <= 200:
                public[key] = value
        for key in ("defaultValue", "maxDelta"):
            if key in definition and finite(definition[key], -1_000_000_000, 1_000_000_000):
                public[key] = definition[key]
        count = definition.get("pointCount")
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1_000_000:
            public["pointCount"] = count
        bounds = definition.get("range")
        if (isinstance(bounds, list) and len(bounds) == 2
                and all(finite(value, -1_000_000_000, 1_000_000_000) for value in bounds) and bounds[0] <= bounds[1]):
            public["range"] = list(bounds)
        if isinstance(definition.get("available"), bool):
            public["available"] = definition["available"]
        if definition.get("source") in ("host", "user"):
            public["source"] = definition["source"]
        result[name] = public
    return result


def selection_preview_notes(selection: dict | None) -> list[dict]:
    """提取不含歌词、文件路径或指纹的音符参考，时间使用宿主已换算的秒。

    旧桥接可能没有音符秒坐标，此时省略参考层，不能以等宽方块冒充实际时值。
    音符矩形只表示乐谱音高，绝不当作修改前的实际合成音高测量。
    """
    if not isinstance(selection, dict):
        return []
    start, end = selection.get("startSeconds"), selection.get("endSeconds")
    notes, offset = selection.get("notes"), selection.get("groupPitchOffset", 0)
    if (not finite(start, -1e9, 1e9) or not finite(end, -1e9, 1e9) or end <= start
            or not finite(offset, -127, 127) or not isinstance(notes, list) or not 1 <= len(notes) <= 128):
        return []
    result = []
    for note in notes:
        if not isinstance(note, dict):
            return []
        onset, duration, pitch = note.get("onsetSeconds"), note.get("durationSeconds"), note.get("pitch")
        if (not finite(onset, start - 1e-6, end) or not finite(duration, 0, end - start + 1e-6)
                or duration <= 0 or onset + duration > end + 1e-6 or not finite(pitch, 0, 127)
                or not 0 <= pitch + offset <= 127):
            return []
        result.append({"startPosition": max(0.0, (onset - start) / (end - start)),
                       "endPosition": min(1.0, (onset + duration - start) / (end - start)),
                       "pitch": float(pitch + offset)})
    return sorted(result, key=lambda note: note["startPosition"])


def public_preview(preview: object) -> dict:
    """公开宿主预览的有限字段；采样和真实控制点采用独立的数量限制。

    before 为 null 表示宿主没有可靠取得修改前的原生音高，不补造测量数据。
    controlPoints 为可选字段，仅含选区内经过宿主读回的候选节点，最多 4000 个；
    它与最多 256 个 curvePreview 插值样本不可混用，旧桥接缺省时不伪造节点。
    畸形曲线整体拒绝，防止界面显示看似有效的预览却确认了不同内容。
    """
    if not isinstance(preview, dict) or not isinstance(preview.get("previewId"), str) or not 1 <= len(preview["previewId"]) <= 128:
        raise ParameterError("宿主没有返回有效的预览编号。")
    result = {"previewId": preview["previewId"]}
    if "notes" in preview:
        notes = preview["notes"]
        if (not isinstance(notes, list) or len(notes) > 128 or any(
                not isinstance(note, dict) or set(note) != {"startPosition", "endPosition", "pitch"}
                or not finite(note.get("startPosition"), 0, 1) or not finite(note.get("endPosition"), 0, 1)
                or note["startPosition"] >= note["endPosition"] or not finite(note.get("pitch"), 0, 127)
                for note in notes)):
            raise ParameterError("预览的音符参考资料无效。")
        result["notes"] = [dict(note) for note in notes]
    for name in ("parameter", "renderMode", "representation", "unit", "label", "summary"):
        if name in preview:
            value = preview[name]
            if not isinstance(value, str) or len(value) > (2000 if name == "summary" else 200):
                raise ParameterError("宿主预览的说明字段无效。")
            result[name] = value
    for name in ("delta", "startSeconds", "endSeconds"):
        if name in preview:
            if not finite(preview[name], -1_000_000_000, 1_000_000_000):
                raise ParameterError("宿主预览的数值字段无效。")
            result[name] = preview[name]
    for name in ("noteCount", "pointCount", "beforePointCount", "pointReduction"):
        if name in preview:
            value = preview[name]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
                raise ParameterError("宿主预览的点数无效。")
            result[name] = value
    if "curve" in preview:
        curve = preview["curve"]
        if (not isinstance(curve, list) or not 2 <= len(curve) <= MAX_CURVE_POINTS
                or any(not isinstance(point, list) or len(point) != 2 or not finite(point[0], 0, 1)
                       or not finite(point[1], -1_000_000, 1_000_000) for point in curve)
                or curve[0][0] != 0 or curve[-1][0] != 1
                or any(left[0] >= right[0] for left, right in zip(curve, curve[1:]))):
            raise ParameterError("宿主预览的控制点无效。")
        result["curve"] = [[float(position), float(value)] for position, value in curve]
    if "curvePreview" in preview:
        samples = preview["curvePreview"]
        if not isinstance(samples, list) or len(samples) > MAX_PREVIEW_POINTS:
            raise ParameterError("宿主曲线预览超过允许的采样上限。")
        checked = []
        previous = -1.0
        for sample in samples:
            if (not isinstance(sample, dict) or not {"position", "after"} <= set(sample)
                    or set(sample) - {"position", "before", "after"}
                    or not finite(sample["position"], 0, 1) or sample["position"] <= previous
                    or (sample.get("before") is not None and not finite(sample["before"], -1_000_000, 1_000_000))
                    or not finite(sample["after"], -1_000_000, 1_000_000)):
                raise ParameterError("宿主曲线预览包含无效采样。")
            previous = float(sample["position"])
            checked.append({"position": previous, "before": sample.get("before"), "after": sample["after"]})
        result["curvePreview"] = checked
    if "controlPoints" in preview:
        # 单独白名单化真实候选节点，禁止透传原始宿主对象、脚本元数据或区外控制点。
        # position 统一为选区实际秒数的比例；value 保留参数单位，原生音高为绝对 MIDI。
        points = preview["controlPoints"]
        if not isinstance(points, list) or len(points) > MAX_PREVIEW_CONTROL_POINTS:
            raise ParameterError("宿主预览的真实控制点超过允许上限。")
        checked_points = []
        previous = -1.0
        for point in points:
            if (not isinstance(point, dict) or set(point) != {"position", "value"}
                    or not finite(point["position"], 0, 1) or point["position"] <= previous
                    or not finite(point["value"], -1_000_000, 1_000_000)):
                raise ParameterError("宿主预览包含无效的真实控制点。")
            previous = float(point["position"])
            checked_points.append({"position": previous, "value": float(point["value"])})
        result["controlPoints"] = checked_points
    if "capabilityWarnings" in preview:
        warnings = preview["capabilityWarnings"]
        if (not isinstance(warnings, list) or len(warnings) > 16
                or any(not isinstance(item, str) or len(item) > 1000 for item in warnings)):
            raise ParameterError("宿主能力提示格式无效。")
        result["capabilityWarnings"] = list(warnings)
    return result
