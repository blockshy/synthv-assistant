"""将模型提出的音分包络编译为逐音符音高，避免稀疏折线改写旋律。

模型只控制每个音符内部的小幅演唱变化；音符基准、实际秒数、组移调和
相邻音符的过渡由本地确定。这里不读取宿主、不执行写入，也不改变原始选区。
输出仍使用既有 pitchCurve 的 0..1 时间比例及工程绝对 MIDI 半音契约。
"""

from __future__ import annotations

from bisect import bisect_right

from .parameters import MAX_CURVE_POINTS, MAX_PREVIEW_POINTS, ParameterError, finite, native_pitch_range


# 宿主先计算起止秒数，再返回 durationSeconds；加回起点时可能相差一个
# 浮点末位。这里只消除亚纳秒的计算噪声，绝不把可听的重叠当作相邻音符。
TIME_EPSILON = 1e-9


def _note_layout(selection: dict) -> tuple[float, float, list[tuple[float, float, float]]]:
    """核对真实秒数并返回按时间排序的 (起点、终点、工程音高)。

    onset/duration 是 blick，跨速度变化时不能用于网页曲线比例。桥接提供的
    onsetSeconds/durationSeconds 已经过速度图换算，必须使用这两个字段。
    """
    native_pitch_range(selection)
    start, end = selection.get("startSeconds"), selection.get("endSeconds")
    if (not finite(start, -1_000_000_000, 1_000_000_000)
            or not finite(end, -1_000_000_000, 1_000_000_000) or end <= start):
        raise ParameterError("音高包络缺少有效的选区秒数范围，请重新读取选区。")
    offset = float(selection.get("groupPitchOffset", 0))
    notes = []
    for note in selection["notes"]:
        onset, duration = note.get("onsetSeconds"), note.get("durationSeconds")
        pitch = float(note["pitch"]) + offset
        if (not finite(onset, -1_000_000_000, 1_000_000_000)
                or not finite(duration, 0, 1_000_000_000) or duration <= 0
                or not finite(pitch, 0, 127)):
            raise ParameterError("音高包络的音符秒数或移调后音高无效，请重新读取选区。")
        finish = float(onset) + float(duration)
        if not finite(finish, -1_000_000_000, 1_000_000_000) or finish <= onset:
            raise ParameterError("音符时长过短或无效，无法可靠生成音高包络。")
        notes.append((float(onset), finish, pitch))
    notes.sort(key=lambda note: note[0])
    if abs(notes[0][0] - start) > TIME_EPSILON or abs(notes[-1][1] - end) > TIME_EPSILON:
        raise ParameterError("音符秒数与选区范围不一致，请重新读取选区。")
    for index in range(1, len(notes)):
        previous_end = notes[index - 1][1]
        onset, finish, pitch = notes[index]
        if onset < previous_end - TIME_EPSILON:
            raise ParameterError("所选音符存在时间重叠，不能为单条音高曲线确定唯一旋律；请缩小选区。")
        if onset < previous_end:
            # 仅规范化浮点末位的负间隔；保留音符终点，不能整体平移后续音符。
            if finish <= previous_end:
                raise ParameterError("音符时长过短或无效，无法可靠生成音高包络。")
            notes[index] = (previous_end, finish, pitch)
    return float(start), float(end), notes


def _shape_data(shape: object) -> tuple[list[list[float]], float]:
    """严格校验模型的局部音分包络，不猜测单位、不补点、不裁剪越界值。"""
    if (not isinstance(shape, dict) or "curve" not in shape
            or set(shape) - {"curve", "transitionMs"}):
        raise ParameterError("音高包络只能包含 curve 和可选 transitionMs。")
    curve, transition = shape["curve"], shape.get("transitionMs", 40)
    if not isinstance(curve, list) or not 2 <= len(curve) <= 8:
        raise ParameterError("每个音符的音高包络必须包含 2 至 8 个控制点。")
    if not finite(transition, 5, 120):
        raise ParameterError("音符过渡时间必须是 5 至 120 毫秒的有限数字。")
    checked, previous = [], -1.0
    for point in curve:
        if (not isinstance(point, list) or len(point) != 2
                or not finite(point[0], 0, 1) or point[0] <= previous
                or not finite(point[1], -50, 50)):
            raise ParameterError("音高包络位置必须严格递增且在 0 到 1 之间，偏移必须在 -50 至 50 音分之间。")
        previous = float(point[0])
        checked.append([previous, float(point[1])])
    if checked[0][0] != 0 or checked[-1][0] != 1:
        raise ParameterError("音高包络的首尾位置必须分别为 0 和 1。")
    return checked, float(transition) / 2000


def _remove_collinear(points: list[list[float]]) -> list[list[float]]:
    """仅去掉单个音符内部严格共线的点，永远保留其主体起点和终点。

    不在整条旋律上做简化，避免同音重复或很短的经过音在压缩后被跨过去。
    不设有损误差容限；不能确认完全冗余的点宁可保留并触发明确的点数限制。
    """
    kept = []
    for point in points:
        while len(kept) >= 2:
            left, middle = kept[-2], kept[-1]
            cross = ((middle[1] - left[1]) * (point[0] - left[0])
                     - (point[1] - left[1]) * (middle[0] - left[0]))
            if cross != 0:
                break
            kept.pop()
        kept.append(point)
    return kept


def compile_pitch_shape(shape: object, selection: dict) -> list[list[float]]:
    """将同一音分包络映射至每个音符，返回既有契约的绝对 MIDI 曲线。

    相接音符在两侧各留最多 transitionMs/2、且不超过本音符时长 10% 的过渡，
    因而任何短音符至少保留 80% 主体。遇到休止则直接连接休止两端，不在空白
    中创造音符，也不为了跨休止而挤占实际音符。整段首尾严格覆盖比例 0 和 1。
    """
    envelope, half_transition = _shape_data(shape)
    start, end, notes = _note_layout(selection)
    low, high = native_pitch_range(selection)
    points = []
    for index, (onset, finish, pitch) in enumerate(notes):
        margin = min(half_transition, (finish - onset) * 0.1)
        touches_previous = index > 0 and abs(onset - notes[index - 1][1]) <= TIME_EPSILON
        touches_next = index + 1 < len(notes) and abs(notes[index + 1][0] - finish) <= TIME_EPSILON
        body_start = onset + margin if touches_previous else onset
        body_end = finish - margin if touches_next else finish
        if body_end <= body_start:
            raise ParameterError("音符时长过短，无法保留可靠的音高主体；请缩小选区。")
        local = []
        for position, cents in envelope:
            value = pitch + cents / 100
            if not finite(value, low, high):
                raise ParameterError("音高包络超出当前音符允许的工程音域，请减小音分偏移。")
            seconds = body_start + position * (body_end - body_start)
            local.append([(seconds - start) / (end - start), value])
        points.extend(_remove_collinear(local))
        if len(points) > MAX_CURVE_POINTS:
            raise ParameterError("逐音符音高曲线超过 64 个控制点；请缩短选区或减少包络点，不能省略音符。")
    # 首尾对应实际第一/最后音符，固定精确值只消除宿主时长相加的浮点末位。
    points[0][0], points[-1][0] = 0.0, 1.0
    if any(points[index][0] <= points[index - 1][0] for index in range(1, len(points))):
        raise ParameterError("音符时间精度不足，无法生成严格递增的音高曲线；请缩小选区。")
    return points


def validate_note_alignment(curve: object, selection: dict) -> None:
    """校验旧模型直接返回的绝对音高，拒绝跨过音符的粗略长折线。

    每个音符的 25%/50%/75% 都处于最长 10% 边缘过渡之外，逐点线性求值
    后必须距其实际工程音高不超过 0.75 半音。只检查旋律对应关系，不把这一
    有界校验解释为演唱听感或宿主渲染结果的保证；真实预览仍由宿主完成。
    """
    start, end, notes = _note_layout(selection)
    low, high = native_pitch_range(selection)
    if not isinstance(curve, list) or not 2 <= len(curve) <= MAX_CURVE_POINTS:
        raise ParameterError("音高曲线必须包含 2 至 64 个控制点。")
    previous = -1.0
    for point in curve:
        if (not isinstance(point, list) or len(point) != 2 or not finite(point[0], 0, 1)
                or point[0] <= previous or not finite(point[1], low, high)):
            raise ParameterError("音高曲线的控制点位置或工程音高无效。")
        previous = float(point[0])
    if curve[0][0] != 0 or curve[-1][0] != 1:
        raise ParameterError("音高曲线的首尾位置必须分别为 0 和 1。")
    positions = [point[0] for point in curve]
    for onset, finish, pitch in notes:
        for fraction in (0.25, 0.5, 0.75):
            position = (onset + fraction * (finish - onset) - start) / (end - start)
            right_index = min(max(bisect_right(positions, position), 1), len(curve) - 1)
            left, right = curve[right_index - 1], curve[right_index]
            ratio = (position - left[0]) / (right[0] - left[0])
            value = left[1] + ratio * (right[1] - left[1])
            if abs(value - pitch) > 0.75 + 1e-9:
                raise ParameterError("音高曲线未贴合所选音符，可能遗漏短音符或把换音画成长滑音；请改用逐音符音分包络。")


def validate_preview_note_alignment(preview: object, selection: dict) -> None:
    """补验宿主实际插值采样，拒绝节点正确但主体明显偏音的原生曲线。

    原生曲线的插值未公开，输入折线吻合音符不代表宿主渲染也吻合。因此只用
    宿主 getValueAt 返回的真实 after 值检查音符中间 25%..75% 区间，避免把
    允许的边缘过渡误判成偏音。这里绝不把 97 点显示采样再次连成折线求值：
    那样会跨过没有采样命中的短音符，制造出宿主从未返回的错误音高。

    若短音符主体未被采样命中，本函数不会声称已验证该主体；输入端的逐音符
    检查仍然有效，但有限采样不能代替试听，也不能证明所有插值极值均正确。
    """
    start, end, notes = _note_layout(selection)
    samples = preview.get("curvePreview") if isinstance(preview, dict) else None
    if (not isinstance(preview, dict) or preview.get("parameter") != "pitchCurve"
            or not isinstance(samples, list) or not 2 <= len(samples) <= MAX_PREVIEW_POINTS):
        raise ParameterError("宿主未返回完整的原生音高采样，无法确认音符对应关系。")
    previous = -1.0
    for sample in samples:
        if (not isinstance(sample, dict) or not finite(sample.get("position"), 0, 1)
                or sample["position"] <= previous or not finite(sample.get("after"), 0, 127)):
            raise ParameterError("宿主原生音高采样的位置或数值无效，未生成可确认预览。")
        previous = float(sample["position"])
    if samples[0]["position"] != 0 or samples[-1]["position"] != 1:
        raise ParameterError("宿主原生音高采样没有覆盖整个选区，未生成可确认预览。")
    for onset, finish, pitch in notes:
        body_start = (onset + 0.25 * (finish - onset) - start) / (end - start)
        body_end = (onset + 0.75 * (finish - onset) - start) / (end - start)
        for sample in samples:
            if body_start <= sample["position"] <= body_end and abs(sample["after"] - pitch) > 0.75 + 1e-9:
                raise ParameterError("宿主实际音高预览偏离音符主体超过 0.75 半音，已拒绝确认；请减小音分包络或缩短选区后重试。")
