"""逐音符加法包络的合成测试，不读取工程、不请求模型、不改变宿主。"""

import copy
import unittest
from bisect import bisect_right

from synthv_assistant.note_curves import compile_note_curves
from synthv_assistant.parameters import ParameterError


def selection_for(notes):
    """从秒范围生成最小 Automation 选区，不提供原生音高能力。"""
    return {"startSeconds": min(start for start, _ in notes),
            "endSeconds": max(start + duration for start, duration in notes),
            "notes": [{"onsetSeconds": start, "durationSeconds": duration}
                      for start, duration in notes],
            "capabilities": {"curves": True, "nativePitch": False},
            "parameters": {"breathiness": {"kind": "automation"},
                           "pitchDelta": {"kind": "automation"},
                           "vocalMode_Airy": {"kind": "vocalMode", "modeName": "Airy"}}}


def envelope(index, value=0.1):
    """构造首尾回零、中央存在表达增量的最小局部包络。"""
    return {"noteIndex": index, "curve": [[0, 0], [0.5, value], [1, 0]]}


def value_at(curve, position):
    """按协议的线性增量插值检查休止与未目标音符，不模拟宿主的立方插值。"""
    right = min(max(bisect_right([point[0] for point in curve], position), 1), len(curve) - 1)
    a, b = curve[right - 1], curve[right]
    return a[1] + (b[1] - a[1]) * (position - a[0]) / (b[0] - a[0])


class NoteCurveTests(unittest.TestCase):
    def setUp(self):
        self.selection = selection_for([(5, 1), (6, 3)])

    def test_real_seconds_override_blick_ratios_across_tempo_change(self):
        # 两个音符的 blick 时长相同，第二个音符实际秒数更长，必须采用秒坐标。
        for index, note in enumerate(self.selection["notes"]):
            note.update(onset=index * 705600000, duration=705600000)
        curve = compile_note_curves("breathiness", [envelope(1), envelope(2, -0.2)], self.selection)
        self.assertEqual(curve, [[0, 0], [0.125, 0.1], [0.25, 0], [0.625, -0.2], [1, 0]])

    def test_index_uses_sorted_selection_order_not_project_index(self):
        self.selection["notes"][0]["index"] = 82
        self.selection["notes"][1]["index"] = 17
        self.selection["notes"].reverse()
        original = copy.deepcopy(self.selection)
        curve = compile_note_curves("breathiness", [envelope(1)], self.selection)
        self.assertEqual(curve, [[0, 0], [0.125, 0.1], [0.25, 0], [1, 0]])
        self.assertEqual(self.selection, original)

    def test_pitch_delta_uses_cents_and_is_independent_of_group_transpose(self):
        expected = compile_note_curves("pitchDelta", [envelope(2, -18)], self.selection)
        self.selection["groupPitchOffset"] = 12
        for note in self.selection["notes"]:
            note["pitch"] = 60
        self.assertEqual(compile_note_curves("pitchDelta", [envelope(2, -18)], self.selection), expected)
        self.assertEqual(expected[2], [0.625, -18])

    def test_different_notes_may_have_different_shapes_and_strengths(self):
        items = [envelope(2, -0.2), {"noteIndex": 1, "curve": [[0, 0], [0.2, 0.05], [0.8, 0.05], [1, 0]]}]
        original = copy.deepcopy(items)
        curve = compile_note_curves("breathiness", items, self.selection)
        self.assertEqual(curve, [[0, 0], [0.05, 0.05], [0.2, 0.05], [0.25, 0], [0.625, -0.2], [1, 0]])
        self.assertEqual(items, original)

    def test_only_last_note_is_targeted_without_interior_nodes_on_other_notes(self):
        selection = selection_for([(0, 1), (1, 1), (2, 1)])
        curve = compile_note_curves("breathiness", [envelope(3)], selection)
        self.assertEqual(curve, [[0, 0], [2 / 3, 0], [5 / 6, 0.1], [1, 0]])
        for position in (0.1, 0.3, 0.5, 2 / 3):
            self.assertEqual(value_at(curve, position), 0)

    def test_rest_and_untargeted_middle_note_keep_zero_addition(self):
        selection = selection_for([(0, 1), (2, 1), (4, 1)])
        curve = compile_note_curves("breathiness", [envelope(1), envelope(3, -0.1)], selection)
        self.assertEqual(curve, [[0, 0], [0.1, 0.1], [0.2, 0], [0.8, 0], [0.9, -0.1], [1, 0]])
        for position in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            self.assertEqual(value_at(curve, position), 0)

    def test_short_note_is_not_dropped(self):
        selection = selection_for([(0, 1), (1, 0.002), (1.002, 1)])
        curve = compile_note_curves("pitchDelta", [envelope(2, 12)], selection)
        self.assertEqual(len(curve), 5)
        self.assertAlmostEqual(curve[1][0] * 2.002, 1)
        self.assertAlmostEqual(curve[2][0] * 2.002, 1.001)
        self.assertAlmostEqual(curve[3][0] * 2.002, 1.002)
        self.assertEqual(curve[2][1], 12)

    def test_only_identical_zero_join_points_are_merged(self):
        selection = selection_for([(0, 1), (1, 1)])
        items = [{"noteIndex": index, "curve": [[0, 0], [0.25, 0.1], [0.5, 0.1], [0.75, 0.1], [1, 0]]}
                 for index in (1, 2)]
        self.assertEqual(len(compile_note_curves("breathiness", items, selection)), 9)

    def test_roundoff_at_note_join_is_normalized(self):
        selection = selection_for([(0, 0.1 + 0.2), (0.3, 0.7)])
        curve = compile_note_curves("breathiness", [envelope(1), envelope(2)], selection)
        self.assertEqual(len(curve), 5)
        self.assertTrue(all(a[0] < b[0] for a, b in zip(curve, curve[1:])))

    def test_output_budget_accepts_64_but_never_discards_65th_point(self):
        # 31 个相接的三点包络共有 63 点；给其中一个音符增加一个节点正好达到 64。
        selection = selection_for([(index, 1) for index in range(31)])
        items = [envelope(index + 1) for index in range(31)]
        items[0]["curve"].insert(1, [0.25, 0.05])
        self.assertEqual(len(compile_note_curves("breathiness", items, selection)), 64)
        items[1]["curve"].insert(1, [0.25, 0.05])
        with self.assertRaisesRegex(ParameterError, "超过 64"):
            compile_note_curves("breathiness", items, selection)

    def test_vocal_mode_and_host_tighter_limit_are_honored(self):
        curve = compile_note_curves("vocalMode_Airy", [envelope(1, 12)], self.selection)
        self.assertEqual(curve[1][1], 12)
        self.selection["parameters"]["vocalMode_Airy"]["maxDelta"] = 10
        with self.assertRaises(ParameterError):
            compile_note_curves("vocalMode_Airy", [envelope(1, 12)], self.selection)
        for parameter in ("pitchCurve", "vocalMode_Unknown", "unknown", True):
            with self.subTest(parameter=parameter), self.assertRaises(ParameterError):
                compile_note_curves(parameter, [envelope(1)], self.selection)

    def test_capability_and_disabled_catalog_cannot_be_bypassed(self):
        self.selection["capabilities"]["curves"] = False
        with self.assertRaises(ParameterError):
            compile_note_curves("breathiness", [envelope(1)], self.selection)
        self.selection["capabilities"]["curves"] = True
        self.selection["parameters"]["breathiness"]["available"] = False
        with self.assertRaises(ParameterError):
            compile_note_curves("breathiness", [envelope(1)], self.selection)

    def test_bad_target_shape_unknown_fields_and_conflicts_are_rejected(self):
        bad = [None, {}, [], [None], [{}], [envelope(1)] * 33,
               [envelope(1), envelope(1)], [{**envelope(1), "delta": 0.1}],
               [{**envelope(1), "pitchShape": {}}], [{**envelope(1), "code": "arbitrary"}],
               [{"noteIndex": 1}], [{"curve": envelope(1)["curve"]}]]
        bad.extend([{**envelope(1), "noteIndex": index}] for index in (0, 3, -1, True, "1", 1.0, None))
        for items in bad:
            with self.subTest(items=repr(items)), self.assertRaises(ParameterError):
                compile_note_curves("breathiness", items, self.selection)

    def test_invalid_local_envelopes_are_rejected_without_repair(self):
        invalid = [None, [], [[0, 0], [1, 0]], [[index / 8, 0] for index in range(9)],
                   [[0.1, 0], [0.5, 0.1], [1, 0]], [[0, 0], [0.5, 0.1], [0.9, 0]],
                   [[0, 0.1], [0.5, 0.1], [1, 0]], [[0, 0], [0.5, 0.1], [1, 0.1]],
                   [[0, 0], [0.5, 0.1], [0.5, 0.1], [1, 0]],
                   [[0, 0], [0.7, 0.1], [0.5, 0.1], [1, 0]],
                   [[0, 0], [0.5, 0], [1, 0]], [(0, 0), (0.5, 0.1), (1, 0)]]
        for bad_value in (0.300001, -0.300001, True, "0.1", None, float("nan"), float("inf")):
            invalid.append([[0, 0], [0.5, bad_value], [1, 0]])
        for bad_position in (-0.1, 1.1, True, "0.5", float("nan"), float("inf")):
            invalid.append([[0, 0], [bad_position, 0.1], [1, 0]])
        for curve in invalid:
            with self.subTest(curve=repr(curve)), self.assertRaises(ParameterError):
                compile_note_curves("breathiness", [{"noteIndex": 1, "curve": curve}], self.selection)

    def test_invalid_selection_and_overlaps_are_rejected(self):
        bad = [None, {}, {**self.selection, "notes": []}, {**self.selection, "notes": [None]},
               {**self.selection, "startSeconds": 4}, {**self.selection, "endSeconds": 10},
               {**self.selection, "startSeconds": True}, {**self.selection, "endSeconds": float("inf")},
               selection_for([(0, 1.1), (1, 1)])]
        for field, value in (("onsetSeconds", None), ("onsetSeconds", True), ("durationSeconds", 0),
                             ("durationSeconds", -1), ("durationSeconds", float("nan"))):
            changed = copy.deepcopy(self.selection)
            changed["notes"][0][field] = value
            bad.append(changed)
        for selection in bad:
            with self.subTest(selection=repr(selection)), self.assertRaises(ParameterError):
                compile_note_curves("breathiness", [envelope(1)], selection)


if __name__ == "__main__":
    unittest.main()
