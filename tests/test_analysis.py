"""使用可重复的合成信号验证 WAV 解码与指标，不依赖声库或外部录音。"""

from pathlib import Path
import math
import tempfile
import unittest
import wave

import numpy as np

from synthv_assistant.analysis import analyze_wav, compare_wavs


class AudioAnalysisTests(unittest.TestCase):
    """覆盖位深、相位、静音及损坏文件等真实音频边界。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def write_signal(self, name, samples, width=2, rate=8000):
        """把归一化测试信号写为小端 PCM；三字节写法独立于被测解码实现。"""
        values = np.asarray(samples)
        channels = values.shape[1] if values.ndim == 2 else 1
        full_scale = 1 << (width * 8 - 1)
        integers = np.clip(np.rint(values * full_scale), -full_scale, full_scale - 1).astype(np.int64)
        if width == 3:
            raw = b"".join(int(value).to_bytes(3, "little", signed=True) for value in integers.ravel())
        else:
            raw = integers.astype("<i" + str(width)).tobytes()
        destination = self.root / name
        with wave.open(str(destination), "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(width)
            target.setframerate(rate)
            target.writeframes(raw)
        return destination

    def test_pcm_bit_depths_and_sine_levels(self):
        # 整周期的半幅正弦：峰值约 -6.02 dBFS，RMS 约 -9.03 dBFS。
        signal = 0.5 * np.sin(2 * math.pi * 1000 * np.arange(8000) / 8000)
        for width in (2, 3, 4):
            with self.subTest(width=width):
                result = analyze_wav(self.write_signal(f"sine{width}.wav", signal, width))
                self.assertAlmostEqual(result["peakDbfs"], -6.0206, places=3)
                self.assertAlmostEqual(result["rmsDbfs"], -9.0309, places=3)
                self.assertEqual(result["duration"], 1)
                self.assertEqual(len(result["envelope"]), 300)
                self.assertEqual(result["clippingRatio"], 0)

    def test_antiphase_stereo_is_not_silence(self):
        # 相位相反不应导致技术指标把确实有声的立体声误报为静音。
        signal = np.column_stack((np.full(100, 0.5), np.full(100, -0.5)))
        result = analyze_wav(self.write_signal("antiphase.wav", signal))
        self.assertFalse(result["silent"])
        self.assertEqual(result["channels"], 2)
        self.assertAlmostEqual(result["rmsDbfs"], -6.0206, places=3)

    def test_silence_has_json_safe_decibels(self):
        result = analyze_wav(self.write_signal("silence.wav", np.zeros(800)))
        self.assertTrue(result["silent"])
        self.assertIsNone(result["rmsDbfs"])
        self.assertIsNone(result["peakDbfs"])
        self.assertTrue(result["warnings"])

    def test_negative_24bit_and_exact_rails(self):
        result = analyze_wav(self.write_signal("rail.wav", [-1, 1, -0.5, 0.5], 3))
        self.assertEqual(result["clippingRatio"], 0.5)
        self.assertEqual(result["peakDbfs"], 0)
        self.assertAlmostEqual(result["envelope"][2]["rms"], 0.5)

    def test_rejects_truncated_data(self):
        path = self.write_signal("truncated.wav", np.ones(100) * 0.1)
        path.write_bytes(path.read_bytes()[:-10])
        with self.assertRaisesRegex(ValueError, "不完整"):
            analyze_wav(path)

    def test_empty_wav_and_unsupported_8bit(self):
        result = analyze_wav(self.write_signal("empty.wav", []))
        self.assertEqual(result["envelope"], [])
        self.assertEqual(result["duration"], 0)
        path = self.root / "8bit.wav"
        with wave.open(str(path), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(1)
            target.setframerate(8000)
            target.writeframes(bytes([128] * 100))
        with self.assertRaisesRegex(ValueError, "整数 PCM"):
            analyze_wav(path)

    def test_comparison_reports_level_delta_not_quality(self):
        before = self.write_signal("before.wav", np.ones(100) * 0.5)
        after = self.write_signal("after.wav", np.ones(100) * 0.25)
        result = compare_wavs(before, after)
        self.assertAlmostEqual(result["delta"]["rmsDb"], -6.0206, places=3)
        self.assertIn("不是 LUFS", result["interpretation"])
        self.assertNotIn("qualityScore", result)


if __name__ == "__main__":
    unittest.main()
