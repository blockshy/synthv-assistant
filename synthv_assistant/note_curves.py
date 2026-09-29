"""把针对指定音符的加法包络编译为现有选区曲线，不覆盖原始演唱曲线。

模型只负责选择音符以及每个音符的少量表达节点；秒坐标、音符边界和参数
上限均由本地确定。输出仍是既有 Automation 的 0..1 时间比例与参数原单位，
因此无需给宿主增加新的写入接口，也不会把音高偏移错误解释为绝对 MIDI。
"""

from __future__ import annotations

from .parameters import (MAX_CURVE_POINTS, ParameterError, finite, parameter_policy,
                         supports_curves, validate_change)


# 宿主的 onsetSeconds + durationSeconds 可能有一个浮点末位的差异。
# 只允许亚纳秒误差，不能用宽松容差把真实重叠音符拼成单声部旋律。
TIME_EPSILON = 1e-9


def _note_times(selection: object) -> tuple[float, float, list[tuple[float, float]]]:
    """提取按实际时间排序的音符范围，刻意不依赖音高和原生音高能力。

    Automation 的偏移量与组移调无关；特别是 pitchDelta 的单位始终是音分。
    blick 时长跨速度变化时不能直接换算比例，只接受桥接已换算的实际秒数。
    """
    if not isinstance(selection, dict):
        raise ParameterError("逐音符包络缺少有效选区，请重新读取选区。")
    start, end, raw_notes = (selection.get("startSeconds"), selection.get("endSeconds"),
                             selection.get("notes"))
    if (not finite(start, -1e9, 1e9) or not finite(end, -1e9, 1e9) or end <= start
            or not isinstance(raw_notes, list) or not 1 <= len(raw_notes) <= 128):
        raise ParameterError("逐音符包络缺少有效音符或选区秒数范围，请重新读取选区。")
    notes = []
    for note in raw_notes:
        if not isinstance(note, dict):
            raise ParameterError("逐音符包络的音符资料无效，请重新读取选区。")
        onset, duration = note.get("onsetSeconds"), note.get("durationSeconds")
        if (not finite(onset, -1e9, 1e9) or not finite(duration, 0, 1e9) or duration <= 0):
            raise ParameterError("逐音符包络的音符秒数无效，请重新读取选区。")
        finish = float(onset) + float(duration)
        if not finite(finish, -1e9, 1e9) or finish <= onset:
            raise ParameterError("音符时长过短或无效，无法可靠生成逐音符包络。")
        notes.append((float(onset), finish))
    notes.sort(key=lambda note: note[0])
    if abs(notes[0][0] - start) > TIME_EPSILON or abs(notes[-1][1] - end) > TIME_EPSILON:
        raise ParameterError("音符秒数与选区范围不一致，请重新读取选区。")
    # 仅对来自同一个边界的浮点末位做规范化，不改变内部音符的实际起止关系。
    notes[0] = (float(start), notes[0][1])
    notes[-1] = (notes[-1][0], float(end))
    for index, (onset, finish) in enumerate(notes):
        if index and onset < notes[index - 1][1] - TIME_EPSILON:
            raise ParameterError("所选音符存在时间重叠，无法确定逐音符参数包络；请缩小选区。")
        if index and onset < notes[index - 1][1]:
            onset = notes[index - 1][1]
            notes[index] = (onset, finish)
        if onset < start or finish > end or finish <= onset:
            raise ParameterError("音符范围无效或时长过短，无法可靠生成逐音符包络。")
    return float(start), float(end), notes


def compile_note_curves(parameter: str, note_curves: object, selection: dict) -> list[list[float]]:
    """将不同音符的局部偏移编译为一条完整、有限、可预览的加法曲线。

    noteIndex 是当前选中音符按实际起始秒数排序后的 1-based 序号，绝不是工程
    中的音符 index。每条包络严格从 [0, 0] 到 [1, 0]；未指定的音符和休止由
    零增量直线连接，保留其原有演唱。只合并同一时刻的零端点，不裁剪超限值、
    不删减内部节点、不略过短音符，点数超过既有协议上限时明确拒绝。

    本函数生成的是请求中的线性加法包络。宿主原曲线的插值、参数上下界及真实
    写入结果仍须经过现有只读预览和确认流程，不能将编译成功视为听感保证。
    """
    if parameter == "pitchCurve":
        raise ParameterError("逐音符加法包络不支持原生音高曲线，请使用音高偏移 pitchDelta。")
    limit, absolute_pitch = parameter_policy(parameter, selection)
    if absolute_pitch or not supports_curves(selection):
        raise ParameterError("逐音符包络需要当前桥接支持 Automation 曲线，请重新读取选区。")
    start, end, notes = _note_times(selection)
    if not isinstance(note_curves, list) or not 1 <= len(note_curves) <= 32:
        raise ParameterError("逐音符包络必须包含 1 至 32 项音符调整。")
    targets = {}
    for item in note_curves:
        if not isinstance(item, dict) or set(item) != {"noteIndex", "curve"}:
            raise ParameterError("逐音符包络只允许 noteIndex 和 curve，不能包含未知或冲突字段。")
        index, envelope = item["noteIndex"], item["curve"]
        if (not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= len(notes)):
            raise ParameterError("noteIndex 必须是按时间排序的选中音符序号，从 1 开始。")
        if index in targets:
            raise ParameterError("同一个音符不能重复指定包络，请合并该音符的调整。")
        if not isinstance(envelope, list) or not 3 <= len(envelope) <= 8:
            raise ParameterError("每个音符的参数包络必须包含 3 至 8 个控制点。")
        checked, previous = [], -1.0
        for point in envelope:
            if (not isinstance(point, list) or len(point) != 2
                    or not finite(point[0], 0, 1) or point[0] <= previous
                    or not finite(point[1], -limit, limit)):
                raise ParameterError("音符包络位置必须严格递增且在 0 到 1 之间，参数增量不得超过当前允许上限。")
            previous = float(point[0])
            checked.append([previous, float(point[1])])
        if checked[0] != [0, 0] or checked[-1] != [1, 0]:
            raise ParameterError("音符包络必须以 [0, 0] 开始、以 [1, 0] 结束，避免改动相邻音符和休止。")
        targets[index] = checked

    points = [[0.0, 0.0]]
    for index in sorted(targets):
        onset, finish = notes[index - 1]
        for position, value in targets[index]:
            # 先减去选区起点再计算局部位置，减少绝对秒数很大时的浮点精度损失。
            normalized = ((onset - start) + position * (finish - onset)) / (end - start)
            if position == 0 and index == 1:
                normalized = 0.0
            elif position == 1 and index == len(notes):
                normalized = 1.0
            point = [normalized, value]
            if point == points[-1] and value == 0:
                # 两个相接音符共用的零端点没有信息差，可精确合并；其他点一律保留。
                continue
            if normalized <= points[-1][0]:
                raise ParameterError("音符时间精度不足，无法生成严格递增的参数包络；请缩小选区。")
            points.append(point)
    if points[-1][0] != 1:
        points.append([1.0, 0.0])
    if len(points) > MAX_CURVE_POINTS:
        raise ParameterError("逐音符参数曲线超过 64 个控制点；请减少目标音符或包络点，不能自动省略调整。")
    # 最后复用公共契约，防止该模型专用入口绕过非零增量、数值范围或目录权限。
    return validate_change(parameter, curve=points, selection=selection)["curve"]
