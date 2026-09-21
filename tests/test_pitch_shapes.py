"""逐音符音高编译与兼容校验测试；只构造资料，不访问真实工程或模型。"""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from synthv_assistant.parameters import ParameterError
from synthv_assistant.pitch_shapes import (compile_pitch_shape, validate_note_alignment,
                                           validate_preview_note_alignment)


def selection_for(notes, *, offset=0):
    """从 (秒起点、秒时长、音符音高) 构造桥接实际返回的时间字段。"""
    items = [{"onsetSeconds": start, "durationSeconds": duration, "pitch": pitch}
             for start, duration, pitch in notes]
    return {"startSeconds": min(note[0] for note in notes),
            "endSeconds": max(note[0] + note[1] for note in notes),
            "groupPitchOffset": offset, "notes": items}


class PitchShapeTests(unittest.TestCase):
    def setUp(self):
        self.flat = {"curve": [[0, 0], [1, 0]]}
        self.selection = selection_for([(10, 1, 60), (11, 1, 64)])

    def test_default_transition_preserves_both_note_bodies(self):
        curve = compile_pitch_shape(self.flat, self.selection)
        self.assertEqual(len(curve), 4)
        self.assertEqual(curve[0], [0, 60])
        self.assertEqual(curve[-1], [1, 64])
        self.assertAlmostEqual(curve[1][0], 0.49)
        self.assertAlmostEqual(curve[2][0], 0.51)
        validate_note_alignment(curve, self.selection)

    def test_unsorted_notes_are_sorted_without_changing_source(self):
        self.selection["notes"].reverse()
        original = copy.deepcopy(self.selection)
        curve = compile_pitch_shape(self.flat, self.selection)
        self.assertEqual([point[1] for point in curve], [60, 60, 64, 64])
        self.assertEqual(self.selection, original)
        validate_note_alignment(curve, self.selection)

    def test_real_seconds_override_blick_proportions_across_tempo_change(self):
        selection = selection_for([(5, 1, 60), (6, 3, 64)])
        # 两个音符具有相同 blick 时长，但速度变化令实际持续秒数不同。
        selection["notes"][0].update(onset=0, duration=705600000)
        selection["notes"][1].update(onset=705600000, duration=705600000)
        curve = compile_pitch_shape(self.flat, selection)
        self.assertAlmostEqual(curve[1][0], 0.245)
        self.assertAlmostEqual(curve[2][0], 0.255)
        validate_note_alignment(curve, selection)

    def test_transpose_and_cents_are_added_in_semitones(self):
        selection = selection_for([(0, 1, 48)], offset=12)
        curve = compile_pitch_shape({"curve": [[0, -20], [0.5, 30], [1, 0]]}, selection)
        self.assertEqual(curve, [[0, 59.8], [0.5, 60.3], [1, 60]])
        validate_note_alignment(curve, selection)

    def test_short_note_keeps_eighty_percent_body_and_is_not_skipped(self):
        selection = selection_for([(0, 1, 60), (1, 0.01, 67), (1.01, 1, 60)])
        curve = compile_pitch_shape({**self.flat, "transitionMs": 120}, selection)
        middle = [point for point in curve if point[1] == 67]
        self.assertEqual(len(middle), 2)
        self.assertAlmostEqual((middle[1][0] - middle[0][0]) * 2.01, 0.008)
        validate_note_alignment(curve, selection)
        with self.assertRaisesRegex(ParameterError, "未贴合"):
            validate_note_alignment([[0, 60], [1, 60]], selection)

    def test_rest_only_connects_note_boundaries(self):
        selection = selection_for([(0, 0.5, 60), (1, 0.5, 67)])
        curve = compile_pitch_shape(self.flat, selection)
        self.assertEqual(curve, [[0, 60], [1 / 3, 60], [2 / 3, 67], [1, 67]])
        validate_note_alignment(curve, selection)

    def test_transition_width_is_bounded_by_requested_milliseconds(self):
        curve = compile_pitch_shape({**self.flat, "transitionMs": 10}, self.selection)
        self.assertAlmostEqual((curve[2][0] - curve[1][0]) * 2, 0.010)

    def test_overlapping_notes_are_rejected_in_both_entry_points(self):
        selection = selection_for([(0, 1.1, 60), (1, 1, 64)])
        for operation in (lambda: compile_pitch_shape(self.flat, selection),
                          lambda: validate_note_alignment([[0, 60], [1, 64]], selection)):
            with self.assertRaisesRegex(ParameterError, "重叠"):
                operation()

    def test_roundoff_at_join_does_not_turn_adjacent_notes_into_polyphony(self):
        selection = selection_for([(0, 0.1 + 0.2, 60), (0.3, 0.7, 64)])
        curve = compile_pitch_shape(self.flat, selection)
        validate_note_alignment(curve, selection)
        self.assertTrue(all(curve[index][0] > curve[index - 1][0] for index in range(1, len(curve))))

    def test_invalid_shape_fields_points_and_transition_are_rejected(self):
        bad = [None, [], {}, {"curve": None}, {**self.flat, "unknown": 1},
               {"curve": [[0, 0]]}, {"curve": [[index / 8, 0] for index in range(9)]},
               {"curve": [[0.1, 0], [1, 0]]}, {"curve": [[0, 0], [0.9, 0]]},
               {"curve": [[0, 0], [0.5, 0], [0.5, 10], [1, 0]]},
               {"curve": [[0, 51], [1, 0]]}, {"curve": [[0, -51], [1, 0]]},
               {"curve": [[0, True], [1, 0]]}, {"curve": [[False, 0], [1, 0]]},
               {"curve": [[0, float("nan")], [1, 0]]}, {"curve": [[0, 0], [float("inf"), 0]]},
               {"curve": [[0, "10"], [1, 0]]}, {"curve": [[0, 0, 1], [1, 0]]},
               {"curve": [(0, 0), (1, 0)]}]
        bad.extend({**self.flat, "transitionMs": value}
                   for value in (None, True, "40", 0, 4.9, 120.1, float("inf"), float("nan")))
        for shape in bad:
            with self.subTest(shape=repr(shape)), self.assertRaises(ParameterError):
                compile_pitch_shape(shape, self.selection)

    def test_invalid_time_layout_and_pitch_are_rejected(self):
        changes = [("startSeconds", None), ("endSeconds", 10), ("startSeconds", 9),
                   ("endSeconds", 13), ("groupPitchOffset", True), ("groupPitchOffset", 100)]
        for field, value in changes:
            selection = {**self.selection, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ParameterError):
                compile_pitch_shape(self.flat, selection)
        for field, value in (("onsetSeconds", None), ("onsetSeconds", True), ("durationSeconds", 0),
                             ("durationSeconds", None), ("durationSeconds", -1),
                             ("durationSeconds", float("nan")), ("pitch", True), ("pitch", 128)):
            selection = copy.deepcopy(self.selection)
            selection["notes"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ParameterError):
                compile_pitch_shape(self.flat, selection)

    def test_extreme_midi_boundary_rejects_offsets_instead_of_clipping(self):
        for pitch, cents in ((0, -1), (127, 1)):
            with self.subTest(pitch=pitch), self.assertRaisesRegex(ParameterError, "音域"):
                compile_pitch_shape({"curve": [[0, cents], [1, cents]]}, selection_for([(0, 1, pitch)]))

    def test_same_pitch_notes_keep_individual_bodies_and_limit_is_explicit(self):
        selection = selection_for([(index, 1, 60) for index in range(32)])
        curve = compile_pitch_shape(self.flat, selection)
        self.assertEqual(len(curve), 64)
        validate_note_alignment(curve, selection)
        with self.assertRaisesRegex(ParameterError, "缩短选区或减少包络点"):
            compile_pitch_shape(self.flat, selection_for([(index, 1, 60) for index in range(33)]))

    def test_only_local_strictly_collinear_points_can_reduce_count(self):
        selection = selection_for([(index, 1, 60 + index % 2) for index in range(9)])
        constant = {"curve": [[index / 7, 20] for index in range(8)]}
        curve = compile_pitch_shape(constant, selection)
        self.assertEqual(len(curve), 18)
        validate_note_alignment(curve, selection)
        alternating = {"curve": [[index / 7, 20 if index % 2 else -20] for index in range(8)]}
        with self.assertRaisesRegex(ParameterError, "不能省略音符"):
            compile_pitch_shape(alternating, selection)

    def test_absolute_compatibility_check_rejects_long_glide_across_distinct_notes(self):
        with self.assertRaisesRegex(ParameterError, "未贴合"):
            validate_note_alignment([[0, 60], [1, 64]], self.selection)
        # 不能只检查整句的最高/最低音域或某一个采样点；后半音符也逐一检查。
        with self.assertRaisesRegex(ParameterError, "未贴合"):
            validate_note_alignment([[0, 60], [0.5, 60], [1, 64]], self.selection)

    def test_absolute_alignment_tolerance_is_per_note_not_global_range(self):
        selection = selection_for([(0, 1, 60)])
        validate_note_alignment([[0, 60.75], [1, 60.75]], selection)
        with self.assertRaisesRegex(ParameterError, "未贴合"):
            validate_note_alignment([[0, 60.75001], [1, 60.75001]], selection)

    def test_absolute_compatibility_check_validates_raw_curve_structure(self):
        for curve in (None, [], [[0, 60]], [[0, 60], [0.9, 64]], [[0.1, 60], [1, 64]],
                      [[0, 60], [0, 60], [1, 64]], [[0, True], [1, 64]],
                      [[0, 60], [True, 64]], [[0, float("nan")], [1, 64]]):
            with self.subTest(curve=repr(curve)), self.assertRaises(ParameterError):
                validate_note_alignment(curve, self.selection)

    def test_actual_host_samples_detect_interpolation_error_inside_note_body(self):
        selection = selection_for([(0, 1, 48)], offset=12)
        preview = {"parameter": "pitchCurve", "curvePreview": [
            {"position": index / 96, "after": 60} for index in range(97)]}
        validate_preview_note_alignment(preview, selection)
        preview["curvePreview"][48]["after"] = 60.75001
        with self.assertRaisesRegex(ParameterError, "宿主实际音高预览"):
            validate_preview_note_alignment(preview, selection)

    def test_actual_samples_skip_edge_transition_and_rest(self):
        selection = selection_for([(0, 1, 60), (2, 1, 64)])
        preview = {"parameter": "pitchCurve", "curvePreview": [
            {"position": 0, "after": 61}, {"position": 1 / 6, "after": 60},
            {"position": 0.5, "after": 62}, {"position": 5 / 6, "after": 64},
            {"position": 1, "after": 63}]}
        # 端点可以保留演唱过渡，休止不应被误当作需要匹配某个音符的主体。
        validate_preview_note_alignment(preview, selection)

    def test_actual_samples_never_interpolate_across_an_unsampled_short_note(self):
        selection = selection_for([(0, 0.501, 60), (0.501, 0.002, 67), (0.503, 0.497, 60)])
        curve = compile_pitch_shape(self.flat, selection)
        validate_note_alignment(curve, selection)
        preview = {"parameter": "pitchCurve", "curvePreview": [
            {"position": index / 96, "after": 60} for index in range(97)]}
        # 0.501..0.503 音符没有真实采样命中，不能拿邻近显示采样连线推导其音高。
        validate_preview_note_alignment(preview, selection)
        preview["curvePreview"].append({"position": 0.502, "after": 60})
        preview["curvePreview"].sort(key=lambda item: item["position"])
        with self.assertRaisesRegex(ParameterError, "宿主实际音高预览"):
            validate_preview_note_alignment(preview, selection)

    def test_actual_preview_samples_require_finite_ordered_complete_host_values(self):
        base = {"parameter": "pitchCurve", "curvePreview": [
            {"position": 0, "after": 60}, {"position": 1, "after": 64}]}
        invalid = [None, {}, {**base, "parameter": "pitchDelta"}, {**base, "curvePreview": []},
                   {**base, "curvePreview": base["curvePreview"] * 129}]
        for position, value in ((True, 60), (0, None), (0, True), (0, float("nan")), (0, 128), (0.1, 60)):
            invalid.append({**base, "curvePreview": [{"position": position, "after": value}, base["curvePreview"][1]]})
        invalid.append({**base, "curvePreview": [base["curvePreview"][0], base["curvePreview"][0], base["curvePreview"][1]]})
        for preview in invalid:
            with self.subTest(preview=repr(preview)[:150]), self.assertRaises(ParameterError):
                validate_preview_note_alignment(preview, self.selection)


class PitchPreviewConversationTests(unittest.TestCase):
    """独立覆盖会话入口，确保旧提案与实际宿主采样都不能绕过新旋律校验。"""

    def setUp(self):
        from synthv_assistant.conversations import ConversationManager
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target, value in (("synthv_assistant.conversations.DATA", Path(temporary.name)),
                              ("synthv_assistant.conversations._default_platform_id", lambda: "default")):
            patched = patch(target, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.selection = selection_for([(0, 1, 60), (1, 1, 64)])
        self.selection.update(projectFile="private-test-only.svp", groupUUID="test-group", groupOffset=0,
                              capabilities={"curves": True, "nativePitch": True}, noteCount=2,
                              parameters={"pitchCurve": {"kind": "pitch", "available": True,
                                          "range": [0, 127], "defaultValue": 0, "pointCount": 0}})
        self.curve = compile_pitch_shape({"curve": [[0, 0], [1, 0]]}, self.selection)
        self.plan = {"text": "按音符提出小幅变化。", "actions": [
            {"parameter": "pitchCurve", "curve": self.curve, "reason": "保持旋律。"}]}
        planned = patch("synthv_assistant.conversations._plan_tuning", return_value=self.plan)
        planned.start()
        self.addCleanup(planned.stop)
        self.service = MagicMock()
        self.service.operation_lock = threading.RLock()
        self.service.bridge.status.return_value = {"connected": True, "session": "test-session"}
        self.service.get_selection.side_effect = lambda: copy.deepcopy(self.selection)
        self.preview = {"previewId": "native-test-only", "parameter": "pitchCurve", "curvePreview": [
            {"position": position, "after": value} for position, value in
            ((0, 60), (0.25, 60), (0.75, 64), (1, 64))]}
        self.service.preview.return_value = self.preview
        self.manager = ConversationManager(self.service)
        self.identifier = self.manager.create_conversation()["id"]

    def propose(self):
        return self.manager.send_message(self.identifier, "测试音高", True, [])["messages"][-1]["actions"][0]

    def test_old_rough_pitch_proposal_is_rejected_before_host_preview(self):
        from synthv_assistant.conversations import ConversationError
        self.plan["actions"][0]["curve"] = [[0, 60], [1, 64]]
        action = self.propose()
        with self.assertRaisesRegex(ConversationError, "未贴合"):
            self.manager.preview_action(action["id"])
        self.service.preview.assert_not_called()
        self.service.edit.assert_not_called()

    def test_host_interpolation_error_removes_previous_confirmation(self):
        from synthv_assistant.conversations import ConversationError
        action = self.propose()
        self.assertEqual(self.manager.preview_action(action["id"])["status"], "previewed")
        self.preview["curvePreview"][1]["after"] = 61
        with self.assertRaisesRegex(ConversationError, "宿主实际音高预览"):
            self.manager.preview_action(action["id"])
        saved = self.manager.get_conversation(self.identifier)["messages"][-1]["actions"][0]
        self.assertEqual(saved["status"], "proposed")
        self.assertNotIn("preview", saved)
        with self.assertRaises(ConversationError):
            self.manager.apply_action(action["id"])
        self.service.edit.assert_not_called()

    def test_planner_shape_fields_and_point_mode_cannot_bypass_native_contract(self):
        from synthv_assistant.planner import _parse_plan, PlannerError
        shape = {"parameter": "pitchCurve", "pitchShape": {"curve": [[0, 0], [1, 0]]}, "reason": "保持旋律。"}
        bad = [{**shape, "delta": 0.1}, {**shape, "curve": self.curve}, {**shape, "code": "untrusted"},
               {**shape, "pitchShape": None}, {**shape, "pitchShape": True}, {**shape, "renderMode": None},
               {**shape, "renderMode": True}, {**shape, "renderMode": "invalid"}, {**shape, "parameter": "pitchDelta"}]
        for action in bad:
            with self.subTest(action=repr(action)), self.assertRaises(PlannerError):
                _parse_plan(json.dumps({"text": "测试", "actions": [action]}),
                            has_selection=True, has_audio=False, key="", selection=self.selection)
        for action in (shape, {"parameter": "pitchCurve", "curve": self.curve, "renderMode": "smooth", "reason": "保持旋律。"}):
            with self.subTest(action=repr(action)), self.assertRaises(PlannerError):
                _parse_plan(json.dumps({"text": "测试", "actions": [action]}),
                            has_selection=True, has_audio=False, key="", selection=self.selection, render_mode="points")


if __name__ == "__main__":
    unittest.main()
