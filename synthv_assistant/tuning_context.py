"""为模型提供确定的乐句坐标，避免让语言模型自行估算音符的时间比例。

这些信息只描述乐谱结构，并非对实际演唱的测量。短音和分句阈值是规划参考，
不能据此断言发音、气口或唱法；音频证据仍由用户明确附加。
"""

from .parameters import ParameterError, finite, selection_preview_notes, supports_curves
from .note_curves import _note_times


def musical_context(selection: dict | None) -> dict:
    """返回有界、无路径和歌词的逐音符规划资料；资料缺失时不猜测时间。

    noteIndex 是按起点排序后的选区内序号，与工程内部 index 无关。使用宿主
    返回的秒坐标，因此跨速度变化时也与 noteCurves 编译器采用相同的时间轴。
    """
    unavailable = {"noteCurvesAvailable": False}
    if not supports_curves(selection):
        return unavailable
    try:
        # 可用声明与编译共用秒坐标校验；不能用归一化比例容差或预览层的
        # 边缘裁剪把一个实际有重叠/不完整的选区描述成可编译。
        start, end, times = _note_times(selection)
    except ParameterError:
        return unavailable
    duration = end - start
    notes = selection_preview_notes(selection)
    if not notes or not finite(duration, 0, 30) or duration <= 0:
        return unavailable
    layout = []
    phrase = 1
    for index, note in enumerate(notes):
        note = {**note, "startPosition": (times[index][0] - start) / duration,
                "endPosition": (times[index][1] - start) / duration}
        gap = ((note["startPosition"] - notes[index - 1]["endPosition"]) * duration
               if index else 0)
        # 150 ms 仅用于标记值得检查的气口候选；不等价于歌词分句或声学静音。
        if index and gap >= 0.15 - 1e-9:
            phrase += 1
        milliseconds = (note["endPosition"] - note["startPosition"]) * duration * 1000
        layout.append({"noteIndex": index + 1, **note, "durationMs": round(milliseconds, 3),
                       "gapBeforeMs": round(max(0, gap) * 1000, 3), "phraseHint": phrase})
    return {"noteCurvesAvailable": True, "noteLayout": layout,
            "phraseGapMs": 150, "shortNoteMs": 180,
            "pitchReference": "score-not-measured", "offsetReference": "existing-automation"}
