"""本地 WAV 技术分析：只报告可测量的信号事实，不冒充音乐听感评价。"""

from __future__ import annotations

import math
from pathlib import Path
import wave

import numpy as np


# 限制单次内存使用；路径是否属于允许目录由调用入口统一检查。
MAX_WAV_BYTES = 128 * 1024 * 1024
MAX_ENVELOPE_POINTS = 300
SILENCE_PEAK_THRESHOLD = 10 ** (-80 / 20)


def _dbfs(amplitude: float) -> float | None:
    """把归一化振幅转换为 dBFS；数字零用 null 表示，避免输出非法 JSON 无穷值。"""
    return round(20 * math.log10(amplitude), 6) if amplitude > 0 else None


def _decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
    """解码小端有符号 PCM，并使用负满刻度作为 0 dBFS 的统一基准。"""
    if sample_width == 2:
        integers = np.frombuffer(raw, dtype="<i2")
    elif sample_width == 3:
        # NumPy 没有 int24；先拼接三个字节，再对最高位进行符号扩展。
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        integers = (integers ^ 0x800000) - 0x800000
    elif sample_width == 4:
        integers = np.frombuffer(raw, dtype="<i4")
    else:
        raise ValueError("仅支持 16、24、32 位整数 PCM WAV；请将浮点或压缩音频转为 PCM。")
    return integers.astype(np.float64) / float(1 << (sample_width * 8 - 1))


def analyze_wav(path: Path | str) -> dict:
    """分析 PCM WAV 的电平与包络，格式无效时抛出带中文说明的 ValueError。

    RMS 对所有声道的平方采样值求平均，不先混合声道，以免反相信号抵消。
    clippingRatio 是触及整数 PCM 正／负轨值的采样比例，仅提示潜在削波。
    envelope 的 peak/rms 为 0..1 线性振幅，time 为窗口中心的秒数。
    """
    source = Path(path)
    if source.stat().st_size > MAX_WAV_BYTES:
        raise ValueError("WAV 超过 128 MiB 分析上限，请先截取需要评价的短片段。")
    try:
        with wave.open(str(source), "rb") as reader:
            channels = reader.getnchannels()
            sample_rate = reader.getframerate()
            sample_width = reader.getsampwidth()
            frames = reader.getnframes()
            if reader.getcomptype() != "NONE" or sample_width not in (2, 3, 4):
                raise ValueError("仅支持 16、24、32 位整数 PCM WAV。")
            if channels < 1 or sample_rate < 1:
                raise ValueError("WAV 声道数或采样率无效。")
            expected_bytes = frames * channels * sample_width
            if expected_bytes > MAX_WAV_BYTES:
                raise ValueError("WAV 声明的音频数据超过 128 MiB 分析上限。")
            raw = reader.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ValueError("无法读取整数 PCM WAV；请确认文件完整且不是浮点 WAV。") from exc
    if len(raw) != expected_bytes:
        raise ValueError("WAV 音频数据不完整，文件可能仍在录制或已被截断。")

    samples = _decode_pcm(raw, sample_width).reshape(frames, channels)
    absolute = np.abs(samples)
    peak = float(np.max(absolute)) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    positive_rail = 1.0 - 1.0 / (1 << (sample_width * 8 - 1))
    clipped = (samples >= positive_rail) | (samples <= -1.0)
    clipping_ratio = float(np.mean(clipped)) if samples.size else 0.0
    silent = peak <= SILENCE_PEAK_THRESHOLD

    warnings = []
    if frames == 0:
        warnings.append("文件不含采样帧，无法进行试听评价。")
    elif silent:
        warnings.append("音频为静音或接近静音（峰值不高于 -80 dBFS），请检查捕获设备与播放状态。")
    if clipping_ratio > 0:
        warnings.append("存在触及 PCM 满刻度的采样；这提示潜在削波，但不单独证明音频已经失真。")

    envelope = []
    # 按连续窗口汇总，保留局部峰值；不会因为抽样跳过瞬时峰值。
    if frames:
        boundaries = np.linspace(0, frames, min(MAX_ENVELOPE_POINTS, frames) + 1, dtype=int)
        for start, end in zip(boundaries[:-1], boundaries[1:]):
            window = samples[start:end]
            envelope.append({
                "time": round((int(start) + int(end)) / (2 * sample_rate), 6),
                "peak": round(float(np.max(np.abs(window))), 7),
                "rms": round(float(np.sqrt(np.mean(window * window))), 7),
            })

    return {
        "duration": round(frames / sample_rate, 6),
        "sampleRate": sample_rate,
        "channels": channels,
        "sampleWidth": sample_width,
        "frames": frames,
        "peakDbfs": _dbfs(peak),
        "rmsDbfs": _dbfs(rms),
        "clippingRatio": round(clipping_ratio, 9),
        "silent": silent,
        "warnings": warnings,
        "envelope": envelope,
    }


def compare_wavs(before: Path | str, after: Path | str) -> dict:
    """比较两个音频的技术指标；不归一化、不对齐，也不输出虚构的质量评分。"""
    first, second = analyze_wav(before), analyze_wav(after)

    def difference(key: str) -> float | None:
        # 静音的分贝没有有限值，因此涉及静音的电平差也必须返回 null。
        if first[key] is None or second[key] is None:
            return None
        return round(second[key] - first[key], 6)

    warnings = []
    if abs(first["duration"] - second["duration"]) > 0.05:
        warnings.append("两个文件时长不同；请使用相同起止点的片段进行听感对比。")
    if first["sampleRate"] != second["sampleRate"]:
        warnings.append("两个文件采样率不同；本次仅比较汇总指标，未进行重采样。")
    if first["channels"] != second["channels"]:
        warnings.append("两个文件声道数不同，电平变化可能来自声道路由。")
    if first["silent"] or second["silent"]:
        warnings.append("至少一个文件接近静音，不能据此评价调教改善。")
    return {
        "before": first,
        "after": second,
        "delta": {
            "duration": difference("duration"),
            "peakDb": difference("peakDbfs"),
            "rmsDb": difference("rmsDbfs"),
            "clippingRatio": round(second["clippingRatio"] - first["clippingRatio"], 9),
        },
        "warnings": warnings,
        "interpretation": "RMS 是未经频率加权和门限处理的采样均方根，不是 LUFS。电平增大不代表音质或调教变好；本结果不评价音准、咬字、自然度或情感。",
    }
