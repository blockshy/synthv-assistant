"""用合成选区验证生成策略；不连接模型、宿主或用户工程，不评价实际听感。"""

import copy
import json
import unittest
from unittest.mock import patch

from synthv_assistant import planner
from synthv_assistant.generation_policy import (calibrate_generated_action,
                                                compile_note_edits, generation_limits)
from synthv_assistant.note_curves import compile_note_curves
from synthv_assistant.parameters import ParameterError, validate_action
from synthv_assistant.pitch_shapes import compile_pitch_shape, validate_note_alignment
from tests.test_tuning_quality import phrase


class GenerationPolicyTests(unittest.TestCase):
    """策略隔离、实际秒数和幅度保持是可重复校验的条件，区别于主观音质。"""

    def parse(self, actions, *, selection=None, **permissions):
        return planner._parse_plan(json.dumps({"text": "按明确目标提出修改。", "actions": actions}),
                                   has_selection=True, has_audio=False, key="",
                                   selection=phrase() if selection is None else selection, **permissions)

    def action(self, parameter="pitchDelta", *, index=3, gesture="release", amount=-12):
        return {"parameter": parameter, "noteEdits": [
            {"noteIndex": index, "gesture": gesture, "amount": amount}], "reason": "按要求轻微下压指定尾音。"}

    def test_preserve_pitch_is_default_even_if_model_claims_authorization(self):
        """宿主能力和模型文字都不能代替会话独立的音高策略。"""
        native = {"parameter": "pitchCurve", "pitchShape": {"curve": [[0, 0], [1, 0]]},
                  "reason": "用户已明确授权重绘。"}
        for action in (native, self.action()):
            with self.subTest(parameter=action["parameter"]), self.assertRaises(planner.PlannerError):
                self.parse([action])
        self.assertEqual(self.parse([])["actions"], [])

    def test_local_and_redraw_cannot_silently_substitute_each_other(self):
        native = {"parameter": "pitchCurve", "pitchShape": {"notes": [
            {"noteIndex": 3, "curve": [[0, 0], [0.8, 0], [1, -15]]}]}, "reason": "明确重绘末音。"}
        with self.assertRaisesRegex(planner.PlannerError, "未允许重绘"):
            self.parse([native], allow_pitch_adjustments=True)
        with self.assertRaisesRegex(planner.PlannerError, "模型却返回音高偏差"):
            self.parse([self.action()], allow_pitch_adjustments=True, allow_native_pitch=True)
        result = self.parse([native], allow_pitch_adjustments=True, allow_native_pitch=True)
        self.assertEqual(result["actions"][0]["parameter"], "pitchCurve")
        self.assertIn("替换", result["actions"][0]["reason"])
        self.assertEqual(result["actions"][0]["curve"][-1][1], 66.85)

    def test_invalid_permission_types_fail_before_configuration_or_network(self):
        invalid = [("true", False), (1, False), (False, "false"), (False, True), (None, False)]
        with patch.object(planner, "get_audio_configuration_snapshot") as config, \
                patch.object(planner, "_send_json") as send:
            for local, redraw in invalid:
                with self.subTest(local=local, redraw=redraw), self.assertRaises(planner.PlannerError):
                    planner.plan_tuning("测试", phrase(), [], [], [],
                                        allow_pitch_adjustments=local, allow_native_pitch=redraw)
            config.assert_not_called()
            send.assert_not_called()

    def test_new_native_generation_requires_per_note_shape_but_manual_curves_remain_supported(self):
        """不能靠三个对齐检查点来批准模型任意长折线；手工契约独立保留。"""
        selection = phrase()
        curve = compile_pitch_shape({"curve": [[0, 0], [1, 0]]}, selection)
        for numeric in ({"curve": curve}, {"pitchShape": {"curve": [[0, 0], [1, 0]]}}):
            with self.subTest(numeric=numeric), self.assertRaisesRegex(planner.PlannerError, "pitchShape.notes"):
                self.parse([{"parameter": "pitchCurve", "reason": "用户明确重绘。", **numeric}],
                           allow_pitch_adjustments=True, allow_native_pitch=True)
        manual = validate_action({"parameter": "pitchCurve", "curve": curve, "reason": "手工编辑"}, selection)
        self.assertEqual(manual["curve"], curve)

    def test_local_pitch_requires_structured_target_instead_of_arbitrary_humps(self):
        variants = [{"delta": 10}, {"curve": [[0, 0], [.3, 8], [.7, 8], [1, 0]]},
                    {"noteCurves": [{"noteIndex": 3, "curve": [[0, 0], [.5, 8], [1, 0]]}]}]
        for numeric in variants:
            with self.subTest(numeric=numeric), self.assertRaisesRegex(planner.PlannerError, "noteEdits"):
                self.parse([{"parameter": "pitchDelta", "reason": "使它更自然。", **numeric}],
                           allow_pitch_adjustments=True)

    def test_local_release_window_is_actual_milliseconds_and_keeps_unselected_targets_zero(self):
        selection = phrase()
        selection["notes"].reverse()
        before = copy.deepcopy(selection)
        result = self.parse([self.action()], selection=selection, allow_pitch_adjustments=True)
        action = result["actions"][0]
        sources = result["noteCurveSources"]["pitchDelta"]
        self.assertEqual(selection, before)
        self.assertEqual(sources[0]["noteIndex"], 3)
        self.assertEqual(action["curve"], compile_note_curves("pitchDelta", sources, selection))
        # 第三个音符是 1.2 秒；尾音窗固定为 180ms，不能按整句比例拉成半个音。
        local = sources[0]["curve"]
        self.assertAlmostEqual((1 - local[1][0]) * 1.2, .18)
        self.assertTrue(all(value == 0 for position, value in action["curve"] if position <= .91))
        self.assertEqual(action["curve"][-1], [1, 0])
        self.assertEqual(min(value for _, value in action["curve"]), -12)
        self.assertNotIn("noteEdits", action)
        validate_action(action, selection)

    def test_pitch_short_notes_and_swell_are_not_automatically_decorated(self):
        for action in (self.action(index=1), self.action(gesture="swell")):
            with self.subTest(action=action), self.assertRaisesRegex(planner.PlannerError, "不自动装饰"):
                self.parse([action], allow_pitch_adjustments=True)
        # 非音高的明确渐强渐弱仍保留，不能把生成约束误扩展到所有音乐表达。
        result = self.parse([self.action("loudness", gesture="swell", amount=1)])
        self.assertEqual(max(value for _, value in result["actions"][0]["curve"]), 1)

    def test_structured_edit_contract_rejects_conflicts_and_unknown_fields(self):
        base = self.action("tension", amount=-.05)
        variants = [{**base, "delta": .02}, {**base, "curve": [[0, 0], [1, .01]]},
                    {**base, "noteEdits": [{**base["noteEdits"][0], "command": "invalid"}]},
                    {**base, "noteEdits": base["noteEdits"] * 2}]
        for action in variants:
            with self.subTest(action=action), self.assertRaises(planner.PlannerError):
                self.parse([action])

    def test_soft_budget_uniformly_scales_shape_and_private_reuse_source(self):
        result = self.parse([self.action("tension", gesture="body", amount=-.3)])
        action, sources = result["actions"][0], result["noteCurveSources"]["tension"]
        self.assertEqual(min(value for _, value in action["curve"]), -.12)
        self.assertEqual(action["curve"], compile_note_curves("tension", sources, phrase()))
        self.assertIn(-.06, [value for _, value in sources[0]["curve"]])
        self.assertIn("按温和试调预算", action["reason"])
        # 手工/MCP 的硬限制仍允许 .3；新生成预算不能改写公共参数契约。
        self.assertEqual(validate_action({"parameter": "tension", "delta": -.3, "reason": "手动"}, phrase())["delta"], -.3)

    def test_known_sample_headroom_reduces_whole_shape_without_flat_topping(self):
        selection = phrase()
        definition = selection["parameters"]["tension"]
        definition.update(range=[-1, 1], currentValues={"samples": [[0, .98], [.5, .2], [1, .4]],
                                                     "minimum": .2, "maximum": .98})
        raw = {"parameter": "tension", "curve": [[0, .1], [.5, .05], [1, 0]], "reason": "明确目标"}
        before = copy.deepcopy(selection)
        result = self.parse([raw], selection=selection)["actions"][0]
        self.assertAlmostEqual(result["curve"][0][1], .02)
        self.assertAlmostEqual(result["curve"][1][1], .01)
        self.assertEqual(result["curve"][-1][1], 0)
        self.assertEqual(selection, before)
        self.assertIn("已读样本", result["reason"])

    def test_missing_samples_never_use_default_or_computed_pitch_as_baseline(self):
        selection = phrase()
        selection["parameters"]["tension"].update(defaultValue=1, range=[-1, 1], pointCount=50)
        selection["computedPitch"] = {"status": "ready", "unit": "MIDI", "samples": [[0, 127], [1, 0]]}
        action = {"parameter": "tension", "delta": .05, "reason": "明确增加"}
        calibrated, factor, reasons = calibrate_generated_action(action, selection)
        self.assertEqual(calibrated, action)
        self.assertEqual(factor, 1)
        self.assertEqual(reasons, [])

    def test_saturated_direction_is_rejected_instead_of_a_zero_effect_preview(self):
        selection = phrase()
        selection["parameters"]["tension"].update(range=[-1, 1], currentValues={
            "samples": [[0, 1], [1, 1]], "minimum": 1, "maximum": 1})
        with self.assertRaisesRegex(planner.PlannerError, "没有可用余量"):
            self.parse([{"parameter": "tension", "delta": .05, "reason": "增加"}], selection=selection)

    def test_budget_units_and_catalog_presence_remain_explicit(self):
        selection = phrase()
        limits = generation_limits(selection)
        self.assertEqual(limits["pitchDelta"], 25)
        self.assertEqual(limits["loudness"], 2)
        self.assertEqual(limits["vocalMode_Soft"], 10)
        self.assertNotIn("pitchCurve", limits)
        selection["parameters"]["tension"]["maxDelta"] = .02
        self.assertEqual(generation_limits(selection)["tension"], .02)
        del selection["parameters"]["vocalMode_Soft"]
        self.assertNotIn("vocalMode_Soft", generation_limits(selection))
        # 无能力目录的旧桥接允许稀疏/空参数摘要，不应在校准阶段抛 KeyError。
        legacy = {"noteCount": 1, "notes": [{"pitch": 60}], "parameters": {}}
        action = self.parse([{"parameter": "tension", "delta": -.05, "reason": "明确降低"}],
                            selection=legacy)["actions"][0]
        self.assertEqual(action["delta"], -.05)

    def test_node_budget_does_not_silently_drop_targets(self):
        selection = phrase()
        # 12 个互不相接音符各有 6 个包络节点，超过执行契约的 64 点预算。
        selection.update(startSeconds=0, endSeconds=23, notes=[
            {"pitch": 60, "onsetSeconds": index * 2, "durationSeconds": 1} for index in range(12)])
        edits = [{"noteIndex": index + 1, "gesture": "body", "amount": .05} for index in range(12)]
        with self.assertRaisesRegex(ParameterError, "64"):
            compile_note_edits("tension", edits, selection)

    def test_native_per_note_shape_does_not_repeat_one_decoration_on_every_note(self):
        selection = phrase()
        shape = {"notes": [{"noteIndex": 3, "curve": [[0, 0], [.8, 0], [1, -20]]}]}
        curve = compile_pitch_shape(shape, selection)
        self.assertTrue(all(value == 60 for position, value in curve if position < .05))
        self.assertEqual(curve[-1], [1, 66.8])
        validate_note_alignment(curve, selection)
        with self.assertRaisesRegex(ParameterError, "180ms"):
            compile_pitch_shape({"notes": [{"noteIndex": 1, "curve": [[0, -10], [1, 0]]}]}, selection)
        for invalid in ({**shape, "curve": [[0, 0], [1, 0]]}, {"notes": shape["notes"] * 2},
                        {"notes": [{"noteIndex": True, "curve": [[0, 0], [1, 0]]}]}):
            with self.subTest(shape=invalid), self.assertRaises(ParameterError):
                compile_pitch_shape(invalid, selection)


if __name__ == "__main__":
    unittest.main()
