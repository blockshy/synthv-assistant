"""参数共同契约的纯数据测试，不读取配置、连接宿主或发送云请求。"""

import copy
import unittest

from synthv_assistant.parameters import (PARAMETER_LIMITS, ParameterError, public_parameter_catalog,
                                         public_preview, validate_action, validate_change, selection_preview_notes)


def modern_selection():
    """模拟完整的新桥接目录，声库模式名称与目录严格对应。"""
    return {"capabilities": {"curves": True, "nativePitch": True}, "groupPitchOffset": 12,
            "notes": [{"pitch": 48}, {"pitch": 52}],
            "parameters": {
                **{name: {"kind": "automation", "maxDelta": limit, "range": [-1000, 1000],
                          "defaultValue": 0, "pointCount": 0, "label": name, "unit": "test"}
                   for name, limit in PARAMETER_LIMITS.items()},
                "vocalMode_Soft": {"kind": "vocalMode", "modeName": "Soft", "label": "柔和",
                                   "unit": "%", "maxDelta": 30, "range": [0, 100]},
                "pitchCurve": {"kind": "pitch", "available": True, "range": [0, 127], "unit": "MIDI"}}}


class ParameterTests(unittest.TestCase):
    def setUp(self):
        self.selection = modern_selection()

    def test_preview_notes_use_actual_seconds_and_transposition_without_private_fields(self):
        """预览参考层只传乐谱时间及音高，不能泄漏歌词或把它伪装成 before 基频。"""
        selection = {"startSeconds": 10, "endSeconds": 14, "groupPitchOffset": 12, "notes": [
            {"pitch": 48, "onsetSeconds": 10, "durationSeconds": 0.5, "lyrics": "private"},
            {"pitch": 52, "onsetSeconds": 12, "durationSeconds": 2}]}
        notes = selection_preview_notes(selection)
        self.assertEqual(notes, [{"startPosition": 0, "endPosition": 0.125, "pitch": 60},
                                 {"startPosition": 0.5, "endPosition": 1, "pitch": 64}])
        preview = public_preview({"previewId": "test", "notes": notes})
        self.assertEqual(public_preview(preview), preview)
        self.assertNotIn("before", preview)
        self.assertEqual(selection_preview_notes(self.selection), [])
        for invalid in ([{**notes[0], "lyrics": "private"}], [{**notes[0], "pitch": True}],
                        [{**notes[0], "endPosition": 0}], [notes[0]] * 129):
            with self.assertRaises(ParameterError):
                public_preview({"previewId": "test", "notes": invalid})

    def test_legacy_delta_retains_original_shape_and_bounds(self):
        self.assertEqual(validate_change("tension", 0.1), {"parameter": "tension", "delta": 0.1})
        for value in (True, "0.1", None, 0, float("nan"), float("inf"), 10 ** 1000, 0.300001):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ParameterError):
                validate_change("tension", value)

    def test_extended_parameters_require_advertised_current_capability(self):
        for name in ("toneShift", "vibratoEnv", "vocalMode_Soft"):
            with self.subTest(name=name):
                self.assertEqual(validate_change(name, 0.1, selection=self.selection)["delta"], 0.1)
                for changed in ({}, {**self.selection, "capabilities": {"curves": False}}):
                    with self.assertRaises(ParameterError):
                        validate_change(name, 0.1, selection=changed)
        self.selection["parameters"]["vocalMode_Soft"]["modeName"] = "Power"
        with self.assertRaises(ParameterError):
            validate_change("vocalMode_Soft", 1, selection=self.selection)
        with self.assertRaises(ParameterError):
            validate_change("vocalMode_NotListed", 1, selection=self.selection)

    def test_disabled_or_missing_legacy_parameter_cannot_fallback_in_current_catalog(self):
        """新目录过滤禁用参数后，不能因参数原本属于旧五种而重新授予可用性。"""
        self.selection["parameters"]["tension"]["available"] = False
        public = {**self.selection, "parameters": public_parameter_catalog(self.selection)}
        self.assertNotIn("tension", public["parameters"])
        for curves in (True, False):
            public["capabilities"] = {"curves": curves}
            with self.subTest(curves=curves), self.assertRaises(ParameterError):
                validate_change("tension", 0.1, selection=public)
        with self.assertRaises(ParameterError):
            validate_change("tension", 0.1, selection=self.selection)
        # 真正旧桥接没有能力目录，仍接受历史的增量语义和不完整参数摘要。
        for legacy in ({}, {"parameters": {}}, {"parameters": {"tension": {"range": [-1, 1], "pointCount": 0}}}):
            self.assertEqual(validate_change("tension", 0.1, selection=legacy)["delta"], 0.1)

    def test_native_pitch_unavailability_reports_fixed_reason_and_is_excluded_from_model(self):
        """原因只按代码映射，不能回显任意宿主字符串；模型仍可使用已有偏移参数。"""
        self.selection["capabilities"]["nativePitch"] = False
        definition = self.selection["parameters"]["pitchCurve"]
        definition.update(available=False, unavailableCode="pitch-delta-nonzero", unavailableReason="private host details")
        with self.assertRaisesRegex(ParameterError, "非零音高偏移") as caught:
            validate_change("pitchCurve", curve=[[0, 60], [1, 64]], selection=self.selection)
        self.assertNotIn("private", str(caught.exception))
        public = public_parameter_catalog(self.selection)
        self.assertNotIn("pitchCurve", public)
        self.assertIn("pitchDelta", public)
        self.assertEqual(validate_change("pitchDelta", 5, selection=self.selection)["delta"], 5)
        definition["unavailableCode"] = "pitch-delta-unknown"
        with self.assertRaisesRegex(ParameterError, "兼容状态"):
            validate_change("pitchCurve", curve=[[0, 60], [1, 64]], selection=self.selection)

    def test_host_limit_can_tighten_but_cannot_expand_local_policy(self):
        self.selection["parameters"]["toneShift"]["maxDelta"] = 1000
        with self.assertRaises(ParameterError):
            validate_change("toneShift", 201, selection=self.selection)
        self.selection["parameters"]["toneShift"]["maxDelta"] = 20
        self.assertEqual(validate_change("toneShift", 20, selection=self.selection)["delta"], 20)
        with self.assertRaises(ParameterError):
            validate_change("toneShift", 20.01, selection=self.selection)

    def test_relative_curve_has_explicit_default_representation(self):
        curve = [[0, 0], [0.4, 0.2], [1, -0.1]]
        value = validate_change("tension", curve=curve, selection=self.selection)
        self.assertEqual(value, {"parameter": "tension", "curve": curve, "renderMode": "smooth"})
        self.assertIsNot(value["curve"], curve)
        self.assertEqual(validate_change("pitchDelta", curve=[[0, -100], [1, 100]], render_mode="points",
                                        selection=self.selection)["renderMode"], "points")
        with self.assertRaises(ParameterError):
            validate_change("tension", curve=curve)

    def test_curve_rejects_invalid_positions_shapes_values_and_excess_points(self):
        examples = ([], [[0, 0]], [[0, 0], [1, 0]], [[0.1, 0], [1, 0]], [[0, 0], [0.9, 0]],
                    [[0, 0], [0, 0], [1, 0]], [[0, 0], [0.8, 0], [0.7, 0], [1, 0]],
                    [[0, 0], [1, True]], [[False, 0], [1, 0]], [[0, 0], [1, "0.1"]],
                    [[0, 0], [1, float("nan")]], [[0, 0], [1, 10 ** 1000]],
                    [[0, 0], [1, 0.31]], [[0, 0, "extra"], [1, 0]],
                    [[index / 64, 0] for index in range(65)])
        for curve in examples:
            with self.subTest(points=len(curve)), self.assertRaises(ParameterError):
                validate_change("tension", curve=curve, selection=self.selection)
        valid = [[index / 63, 0.1] for index in range(64)]
        self.assertEqual(len(validate_change("tension", curve=valid, selection=self.selection)["curve"]), 64)

    def test_native_pitch_uses_actual_midi_and_conservative_note_range(self):
        value = validate_change("pitchCurve", curve=[[0, 58], [1, 66]], selection=self.selection)
        self.assertEqual(value["curve"], [[0, 58], [1, 66]])
        for curve in ([[0, 57.99], [1, 64]], [[0, 60], [1, 66.01]], [[0, 0], [1, 127]]):
            with self.assertRaises(ParameterError):
                validate_change("pitchCurve", curve=curve, selection=self.selection)
        for kwargs in ({"delta": 1}, {"curve": [[0, 60], [1, 64]], "render_mode": "points"}):
            with self.assertRaises(ParameterError):
                validate_change("pitchCurve", selection=self.selection, **kwargs)
        for change in ({"nativePitch": False}, {"nativePitch": True, "curves": False}):
            broken = {**self.selection, "capabilities": change}
            with self.assertRaises(ParameterError):
                validate_change("pitchCurve", curve=[[0, 60], [1, 64]], selection=broken)
        self.selection["parameters"]["pitchCurve"]["available"] = False
        with self.assertRaises(ParameterError):
            validate_change("pitchCurve", curve=[[0, 60], [1, 64]], selection=self.selection)

    def test_native_pitch_rejects_invalid_group_offset_and_clamps_midi_edges(self):
        for value in (True, "12", float("nan"), 10 ** 1000):
            self.selection["groupPitchOffset"] = value
            with self.assertRaises(ParameterError):
                validate_change("pitchCurve", curve=[[0, 60], [1, 64]], selection=self.selection)
        self.selection.update(groupPitchOffset=0, notes=[{"pitch": 0}])
        self.assertEqual(validate_change("pitchCurve", curve=[[0, 0], [1, 2]], selection=self.selection)["curve"][-1][1], 2)
        with self.assertRaises(ParameterError):
            validate_change("pitchCurve", curve=[[0, -0.01], [1, 2]], selection=self.selection)

    def test_action_contract_rejects_unknown_and_conflicting_fields(self):
        action = {"parameter": "tension", "delta": 0.1, "reason": "略微放松"}
        for extra in ({"curve": [[0, 0], [1, 0.1]]}, {"curve": None}, {"code": "unsafe"},
                      {"renderMode": True}, {"renderMode": "unknown"}, {"delta": None}, {"reason": " "}):
            with self.subTest(fields=list(extra)), self.assertRaises(ParameterError):
                validate_action({**action, **extra}, self.selection)

    def test_public_catalog_does_not_copy_private_or_nested_arbitrary_values(self):
        self.selection["voiceFingerprint"] = "private-voice"
        entry = self.selection["parameters"]["tension"]
        entry.update(fingerprint="private-hash", voiceFile="private-path", defaultValue={"path": "private-path"})
        self.selection["parameters"]["vocalMode_Soft"]["source"] = "user"
        entry["source"] = "private-path"
        public = public_parameter_catalog(self.selection)
        self.assertNotIn("fingerprint", public["tension"])
        self.assertNotIn("voiceFile", public["tension"])
        self.assertNotIn("defaultValue", public["tension"])
        self.assertEqual(public["vocalMode_Soft"]["modeName"], "Soft")
        self.assertEqual(public["vocalMode_Soft"]["source"], "user")
        self.assertNotIn("source", public["tension"])

    def test_public_preview_normalizes_unavailable_before_and_strips_private_fields(self):
        source = {"previewId": "test", "curvePreview": [{"position": 0, "after": 60},
                  {"position": 1, "before": 61, "after": 62}], "curve": [[0, 60], [1, 62]],
                  "pointReduction": 4, "capabilityWarnings": ["尚未取得修改前生成音高"], "voiceFile": "private-path"}
        self.assertEqual(public_preview(source)["curvePreview"][0]["before"], None)
        self.assertNotIn("voiceFile", public_preview(source))
        for bad in ({"curvePreview": [{"position": 0, "after": True}]}, {"pointReduction": 0.5},
                    {"curvePreview": [{"position": index / 256, "after": 60} for index in range(257)]},
                    {"curvePreview": [{"position": 0, "after": 60, "path": "private-path"}]}):
            with self.subTest(fields=list(bad)), self.assertRaises(ParameterError):
                public_preview({**copy.deepcopy(source), **bad})

    def test_public_preview_keeps_real_nodes_separate_and_is_backward_compatible(self):
        """真实节点保留自己的位置和单位；二次白名单过滤不丢点，也不为旧桥接补造节点。"""
        legacy = {"previewId": "test", "curvePreview": [{"position": 0, "after": 60}]}
        self.assertNotIn("controlPoints", public_preview(legacy))
        source = {**legacy, "controlPoints": [{"position": 0.125, "value": 60.25},
                                               {"position": 0.625, "value": 61.5}]}
        public = public_preview(source)
        self.assertEqual(public["controlPoints"], source["controlPoints"])
        self.assertEqual(public_preview(public), public)
        # 控制点和曲线采样采用不同上限，不能误用 256 点采样限制截掉真实节点。
        boundary = [{"position": index / 3999, "value": 0.1} for index in range(4000)]
        self.assertEqual(len(public_preview({**legacy, "controlPoints": boundary})["controlPoints"]), 4000)

    def test_public_preview_rejects_invalid_real_nodes_without_partial_output(self):
        """重复、倒序、越界、非有限值、未知字段及超量节点均整体拒绝。"""
        invalid = [None, {}, [{"position": 0, "value": True}],
                   [{"position": True, "value": 1}], [{"position": -0.1, "value": 1}],
                   [{"position": 1.1, "value": 1}], [{"position": 0, "value": float("nan")}],
                   [{"position": 0, "value": float("inf")}],
                   [{"position": 0, "value": 1, "path": "private-path"}],
                   [{"position": 0, "value": 1}, {"position": 0, "value": 2}],
                   [{"position": 0.7, "value": 1}, {"position": 0.3, "value": 2}],
                   [{"position": index / 4000, "value": 0} for index in range(4001)]]
        for index, points in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(ParameterError):
                public_preview({"previewId": "test", "controlPoints": points})


if __name__ == "__main__":
    unittest.main()
