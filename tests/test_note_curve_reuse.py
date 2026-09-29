"""逐音符意图持久化及再次复用回归；只使用合成选区和临时会话。

这些检查验证包络在节奏变化后仍落在对应音符，不把“末音”重新解释成整段
时间比例。宿主及模型都是替身，测试不读取真实工程、录音或服务配置。
"""

import copy
import json
import unittest

from synthv_assistant.conversations import ConversationError
from synthv_assistant.note_curves import compile_note_curves
from tests import test_proposal_reuse as reuse_fixture


class NoteCurveReuseTests(unittest.TestCase):
    # 复用隔离环境辅助函数，不继承用例类，避免重复运行旧测试。
    send = reuse_fixture.ProposalReuseTests.send
    read_saved = reuse_fixture.ProposalReuseTests.read_saved
    host_preview = reuse_fixture.ProposalReuseTests.host_preview
    host_batch = reuse_fixture.ProposalReuseTests.host_batch
    proposed_message = reuse_fixture.ProposalReuseTests.proposed_message
    reuse = reuse_fixture.ProposalReuseTests.reuse
    assert_no_external_execution = reuse_fixture.ProposalReuseTests.assert_no_external_execution

    def setUp(self):
        reuse_fixture.ProposalReuseTests.setUp(self)
        self.set_layout([(10, 0.1), (10.1, 0.5), (10.8, 1.2)], group="source-group")
        self.local = [{"noteIndex": 3, "curve": [[0, 0], [0.5, -0.05], [1, 0]]}]

    def set_layout(self, notes, *, group="target-group"):
        """建立可变时值但确定排序的合成乐句，工程 index 与选区序号刻意不同。"""
        self.selection.update(
            groupUUID=group, startSeconds=notes[0][0], endSeconds=notes[-1][0] + notes[-1][1],
            noteCount=len(notes), notes=[
                {"index": index + 80, "pitch": 60 + index * 2, "lyrics": "啦",
                 "onset": round(onset * 1000), "duration": round(duration * 1000),
                 "onsetSeconds": onset, "durationSeconds": duration}
                for index, (onset, duration) in enumerate(notes)])

    def local_plan(self, parameter="tension"):
        """模拟规划器内部结果；公开动作仅有曲线，来源单独保存在内部字典。"""
        self.plan["actions"] = [{"parameter": parameter,
                                 "curve": compile_note_curves(parameter, self.local, self.selection),
                                 "reason": "只修饰第3个选中音符。"}]
        self.plan["noteCurveSources"] = {parameter: copy.deepcopy(self.local)}
        return self.proposed_message()

    def test_reuse_recompiles_last_note_instead_of_scaling_whole_phrase(self):
        source = self.local_plan()
        self.set_layout([(20, 1.7), (21.7, 0.1), (21.8, 0.2)])
        message, _ = self.reuse(source)
        action = message["actions"][0]
        self.assertEqual(action["curve"], compile_note_curves("tension", self.local, self.selection))
        peak = next(position for position, value in action["curve"] if value == -0.05)
        self.assertAlmostEqual(peak, 0.95)
        self.assertNotEqual(action["curve"], source["actions"][0]["curve"])
        self.assertIn("对应音符重新定位", message["text"])
        self.assert_no_external_execution()

    def test_changed_note_count_excludes_local_action_but_keeps_legacy_action(self):
        self.local_plan()
        self.plan["actions"].append({"parameter": "breathiness", "delta": 0.03, "reason": "少量气声。"})
        source = self.proposed_message()
        self.set_layout([(20, 1), (21, 1)])
        message, _ = self.reuse(source)
        self.assertEqual([item["parameter"] for item in message["actions"]], ["breathiness"])
        self.assertIn("音符数量不一致", message["text"])
        self.assert_no_external_execution(planner_calls=2)

    def test_second_reuse_inherits_private_source_and_uses_current_note_times(self):
        source = self.local_plan()
        self.set_layout([(20, 1.7), (21.7, 0.1), (21.8, 0.2)], group="first-target")
        first, _ = self.reuse(source)
        self.set_layout([(30, 0.2), (30.2, 0.3), (30.5, 2)], group="second-target")
        second, _ = self.reuse(first)
        action = second["actions"][0]
        self.assertEqual(action["curve"], compile_note_curves("tension", self.local, self.selection))
        saved = self.read_saved()
        for message in (source, first, second):
            identifier = message["actions"][0]["id"]
            self.assertEqual(saved["_private"]["actions"][identifier]["noteCurves"], self.local)
        self.assert_no_external_execution()

    def test_legacy_curve_without_source_keeps_original_reuse_behavior(self):
        curve = compile_note_curves("tension", self.local, self.selection)
        source = self.proposed_message([{"parameter": "tension", "curve": curve, "reason": "整段包络。"}])
        self.set_layout([(20, 2), (22, 1)])
        message, _ = self.reuse(source)
        self.assertEqual(message["actions"][0]["curve"], curve)
        self.assertNotIn("noteCurves", self.read_saved()["_private"]["actions"][message["actions"][0]["id"]])
        self.assert_no_external_execution()

    def test_pitch_delta_local_source_does_not_relax_melody_or_rhythm_guard(self):
        source = self.local_plan("pitchDelta")
        self.set_layout([(20, 1.7), (21.7, 0.1), (21.8, 0.2)])
        before = self.read_saved()
        with self.assertRaisesRegex(ConversationError, "相对节奏不匹配"):
            self.reuse(source)
        self.assertEqual(self.read_saved(), before)
        self.assert_no_external_execution()

    def test_missing_snapshot_and_corrupt_source_never_fall_back_to_generic_curve(self):
        source = self.local_plan()
        pristine = self.read_saved()
        identifier = source["actions"][0]["id"]
        self.set_layout([(20, 1.7), (21.7, 0.1), (21.8, 0.2)])
        for damage in ("missing-snapshot", "null-source", "changed-source", "changed-curve"):
            with self.subTest(damage=damage):
                document = copy.deepcopy(pristine)
                if damage == "missing-snapshot":
                    for message in document["messages"]:
                        message.pop("selection", None)
                elif damage == "null-source":
                    document["_private"]["actions"][identifier]["noteCurves"] = None
                elif damage == "changed-source":
                    document["_private"]["actions"][identifier]["noteCurves"][0]["curve"][1][1] = -0.02
                else:
                    document["messages"][-1]["actions"][0]["curve"][2][1] = -0.02
                self.manager._save(document)
                before = self.read_saved()
                with self.assertRaises(ConversationError):
                    self.reuse(source)
                self.assertEqual(self.read_saved(), before)
        self.assert_no_external_execution()

    def test_planned_sources_must_match_an_existing_curve_action(self):
        """内部元数据也不能无条件信任，更不能为未对应的参数伪造局部意图。"""
        curve = compile_note_curves("tension", self.local, self.selection)
        base = {"text": "局部建议。", "actions": [{"parameter": "tension", "curve": curve,
                                                "reason": "末音调整。"}],
                "noteCurveSources": {"tension": copy.deepcopy(self.local)}}
        invalid = [None, [], {"breathiness": copy.deepcopy(self.local)}, {"tension": None},
                   {"tension": [{"noteIndex": 2, "curve": [[0, 0], [0.5, -0.05], [1, 0]]}]}]
        for sources in invalid:
            with self.subTest(sources=sources), self.assertRaises(ConversationError):
                self.manager._planned_message({**base, "noteCurveSources": sources}, self.selection, "session-1")
        mismatch = copy.deepcopy(base)
        mismatch["actions"][0] = {"parameter": "tension", "delta": -0.05, "reason": "整段调整。"}
        with self.assertRaises(ConversationError):
            self.manager._planned_message(mismatch, self.selection, "session-1")
        with self.assertRaises(ConversationError):
            self.manager._planned_message(base, None, None)

    def test_private_sources_are_deep_copied_and_not_exposed_to_page(self):
        source = self.local_plan()
        action_id = source["actions"][0]["id"]
        self.plan["noteCurveSources"]["tension"][0]["curve"][1][1] = -0.2
        self.assertEqual(self.read_saved()["_private"]["actions"][action_id]["noteCurves"], self.local)
        public = json.dumps(self.manager.get_conversation(self.identifier))
        self.assertNotIn("noteCurveSources", public)
        self.assertNotIn("noteCurves", public)
        self.assert_no_external_execution()


if __name__ == "__main__":
    unittest.main()
