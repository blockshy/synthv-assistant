"""模型规划专用的局部表达编译与幅度预算，不扩展宿主写入能力。

模型只选择目标音符、位置和原单位增量；时间窗、平滑节点和保留原演唱的
加法语义由本地确定。这里的预算是工作台的温和试调策略，不是声库音质规则。
手工编辑、MCP 与历史预览继续使用 parameters.py 的原有硬边界。
"""

from __future__ import annotations

from bisect import bisect_right
import copy

from .note_curves import _note_times, compile_note_curves
from .parameters import (LEGACY_PARAMETERS, ParameterError, finite, parameter_policy,
                         _public_current_values)


# 生成预算故意小于工程写入硬上限，避免把“可写”误当作“适合每次使用”。
# 声线采用百分点、音高/音色采用音分、响度采用 dB，其余使用宿主原参数单位。
AI_STARTING_LIMITS = {
    "breathiness": .12, "tension": .12, "gender": .08, "loudness": 2.0,
    "pitchDelta": 25.0, "toneShift": 60.0, "vibratoEnv": .15, "voicing": .10,
}


def generation_limits(selection: dict | None) -> dict[str, float]:
    """公开本次真实可用参数的生成预算；不从声库名称或版本号猜参数。"""
    catalog = selection.get("parameters") if isinstance(selection, dict) else None
    # 旧桥接可能只返回部分目录或空目录，仍允许原来的五个参数。把候选交给
    # 同一个 parameter_policy 决定：新目录缺项会被拒绝，旧目录正确保留回退。
    names = set(LEGACY_PARAMETERS)
    if isinstance(catalog, dict):
        names.update(catalog)
    result = {}
    for name in names:
        try:
            hard, native = parameter_policy(name, selection)
        except ParameterError:
            continue
        if native:
            continue
        result[name] = min(hard, 10.0 if name.startswith("vocalMode_") else AI_STARTING_LIMITS[name])
    return result


def compile_note_edits(parameter: str, edits: object, selection: dict) -> tuple[list[dict], list[list[float]]]:
    """把明确的起音、主体、尾音或渐强弱意图变成局部零端点加法包络。

    onset/release 的窗口分别最多 120/180ms，且不超过音符时长的 45%；
    body 在两侧保留实际时间淡入淡出，swell 只用于非音高参数。节点采用
    半余弦窗口的少量关键采样，不使用随机扰动，也不从计算音高反推应修正量。
    返回既有 noteCurves 来源，复用不同节奏时仍可按新音符秒数重新编译。
    """
    limit, native = parameter_policy(parameter, selection)
    if native:
        raise ParameterError("局部表达只叠加现有参数；重绘原生音高请使用已授权的 pitchShape。")
    _, _, times = _note_times(selection)
    if not isinstance(edits, list) or not 1 <= len(edits) <= 12:
        raise ParameterError("局部表达 noteEdits 必须包含 1 至 12 项明确音符目标。")
    sources, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"noteIndex", "gesture", "amount"}:
            raise ParameterError("局部表达只允许 noteIndex、gesture 和 amount。")
        index, gesture, amount = edit["noteIndex"], edit["gesture"], edit["amount"]
        if (not isinstance(index, int) or isinstance(index, bool)
                or not 1 <= index <= len(times) or index in seen):
            raise ParameterError("局部表达的音符序号必须有效且不重复。")
        if not isinstance(gesture, str) or gesture not in {"onset", "body", "release", "swell"}:
            raise ParameterError("局部表达位置只能是 onset、body、release 或 swell。")
        if not finite(amount, -limit, limit) or amount == 0:
            raise ParameterError("局部表达增量必须使用当前参数原单位的有限非零数字。")
        duration = times[index - 1][1] - times[index - 1][0]
        if parameter == "pitchDelta" and (duration < .12 - 1e-9 or gesture == "swell"):
            raise ParameterError("局部音高不自动装饰短于 120ms 的音符或生成整音波峰，请选择明确的起音、主体或尾音目标。")
        if duration < .04 - 1e-9:
            raise ParameterError("短于 40ms 的音符不自动添加表达包络，请缩小目标或在宿主手工处理。")
        seen.add(index)
        value = float(amount)
        if gesture in {"onset", "release"}:
            window = min(.45, (.12 if gesture == "onset" else .18) / duration)
            curve = [[0., 0.], [window / 4, value / 2], [window / 2, value],
                     [window * .75, value / 2], [window, 0.], [1., 0.]]
            if gesture == "release":
                curve = [[1 - position, gain] for position, gain in reversed(curve)]
        elif gesture == "body":
            edge = min(.25, .08 / duration)
            curve = [[0., 0.], [edge / 2, value / 2], [edge, value],
                     [1 - edge, value], [1 - edge / 2, value / 2], [1., 0.]]
        else:
            curve = [[0., 0.], [.25, value / 2], [.5, value], [.75, value / 2], [1., 0.]]
        sources.append({"noteIndex": index, "curve": curve})
    return sources, compile_note_curves(parameter, sources, selection)


def _delta_at(action: dict, position: float) -> float:
    """只计算请求自身的线性偏移，不插值或重建宿主原始参数与计算音高。"""
    if "delta" in action:
        return action["delta"]
    curve = action["curve"]
    at = min(max(bisect_right([p[0] for p in curve], position), 1), len(curve) - 1)
    left, right = curve[at - 1], curve[at]
    fraction = (position - left[0]) / (right[0] - left[0])
    return left[1] + fraction * (right[1] - left[1])


def calibrate_generated_action(action: dict, selection: dict | None) -> tuple[dict, float, list[str]]:
    """按温和起步预算及已知样本余量等比收敛，保留目标、符号和包络形状。

    调整会返回明确说明，由规划器显示实际幅度；不逐点裁剪造成削顶。样本余量
    只检查宿主实际返回的位置，不能证明采样之间不越界，正式宿主预览仍必需。
    未知资料不补零；样本已无余量时拒绝该方向，不生成无效的零增量动作。
    """
    parameter = action["parameter"]
    if parameter == "pitchCurve":
        return action, 1., []
    budget = generation_limits(selection)[parameter]
    peak = abs(action["delta"]) if "delta" in action else max(abs(p[1]) for p in action["curve"])
    factor = min(1., budget / peak)
    reasons = ["已按温和试调预算等比减小幅度。"] if factor < 1 else []
    catalog = selection.get("parameters") if isinstance(selection, dict) else None
    definition = catalog.get(parameter) if isinstance(catalog, dict) else None
    if isinstance(definition, dict):
        current = _public_current_values(definition.get("currentValues"))
        bounds = definition.get("range")
        if (current and isinstance(bounds, list) and len(bounds) == 2
                and finite(bounds[0], -1e9, 1e9) and finite(bounds[1], bounds[0], 1e9)):
            sampled_factor = factor
            for position, baseline in current["samples"]:
                # 已越界的宿主数据不用于反向造一个修复动作；留给实际预览核验。
                if not bounds[0] <= baseline <= bounds[1]:
                    continue
                delta = _delta_at(action, position)
                if delta > 0:
                    sampled_factor = min(sampled_factor, max(0., bounds[1] - baseline) / delta)
                elif delta < 0:
                    sampled_factor = min(sampled_factor, max(0., baseline - bounds[0]) / -delta)
            if sampled_factor < factor:
                reasons.append("已按宿主已读样本的剩余范围等比减小幅度。")
                factor = sampled_factor
    if factor <= 1e-9:
        raise ParameterError("当前已读参数样本在该方向没有可用余量，请改用其他方向或目标；未生成零效果方案。")
    calibrated = copy.deepcopy(action)
    if factor < 1:
        if "delta" in calibrated:
            calibrated["delta"] *= factor
        else:
            calibrated["curve"] = [[p, v * factor] for p, v in calibrated["curve"]]
    return calibrated, factor, reasons
