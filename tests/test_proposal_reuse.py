"""复用调教提案与桥接重连回归：隔离真实工程、音频、配置和模型调用。

复用仅复制参数意图，并为用户当前选区建立新的保护信息。测试重点验证其
不会复制旧确认凭据、不会隐式写入宿主，也不会把相同音高误认成相同旋律。
"""

from __future__ import annotations

import copy
import json
import unittest

from synthv_assistant.conversations import ConversationError
from synthv_assistant.pitch_shapes import compile_pitch_shape
from tests import test_conversations as conversation_fixture
from tests import test_server as server_fixture
from tests.test_parameters import modern_selection


class ProposalReuseTests(unittest.TestCase):
    # 仅借用隔离环境的构造和读写辅助函数；不继承测试类，避免重复执行整套测试。
    send = conversation_fixture.ConversationTests.send
    read_saved = conversation_fixture.ConversationTests.read_saved

    def setUp(self):
        conversation_fixture.ConversationTests.setUp(self)
        self.selection.update(modern_selection())
        self.selection.update(
            groupPitchOffset=0, startSeconds=1, endSeconds=3, noteCount=2,
            notes=[
                {"index": 1, "pitch": 60, "lyrics": "啦", "onset": 1000, "duration": 1000,
                 "onsetSeconds": 1, "durationSeconds": 1},
                {"index": 2, "pitch": 64, "lyrics": "啊", "onset": 2000, "duration": 1000,
                 "onsetSeconds": 2, "durationSeconds": 1},
            ],
        )
        self.selection["capabilities"]["batchPreview"] = True
        self.selection["voiceFingerprint"] = "private-voice-original"
        for definition in self.selection["parameters"].values():
            definition.update(available=True, fingerprint="0123456789abcdef:100")
        self.service.preview.side_effect = self.host_preview
        self.service.preview_batch.side_effect = self.host_batch

    def host_preview(self, parameter, delta=None, *, curve=None, render_mode="smooth"):
        """返回当前目标的只读宿主预览；不允许真实连接或任何隐式工程修改。"""
        self.preview_serial += 1
        result = {"previewId": "isolated-preview-" + str(self.preview_serial),
                  "parameter": parameter, "startSeconds": self.selection["startSeconds"],
                  "endSeconds": self.selection["endSeconds"], "pointCount": 4}
        if curve is not None:
            result.update(curve=copy.deepcopy(curve), renderMode=render_mode)
        return result

    def host_batch(self, batch_id, actions):
        return {"previews": [{"actionId": action["id"], "preview": self.host_preview(action["parameter"])}
                             for action in actions], "errors": []}

    def proposed_message(self, actions=None):
        if actions is not None:
            self.plan["actions"] = actions
        return self.send()["messages"][-1]

    def reuse(self, source):
        result = self.manager.reuse_message(self.identifier, source["id"])
        message = next(item for item in result["conversation"]["messages"] if item["id"] == result["messageId"])
        self.assertEqual(message["origin"], "reuse")
        self.assertEqual(message["role"], "assistant")
        return message, result

    def move_target(self, *, start=11, scale=1, group="another-private-group"):
        """把旋律移到新目标，也可整体拉伸时长；保留归一化节奏与实际音高。"""
        previous_start = self.selection["startSeconds"]
        previous_duration = self.selection["endSeconds"] - previous_start
        for note in self.selection["notes"]:
            note["onsetSeconds"] = start + (note["onsetSeconds"] - previous_start) * scale
            note["durationSeconds"] *= scale
            note["onset"] = round(note["onsetSeconds"] * 1000)
            note["duration"] = round(note["durationSeconds"] * 1000)
        self.selection.update(groupUUID=group, startSeconds=start,
                              endSeconds=start + previous_duration * scale)

    def pitch_action(self, parameter="pitchCurve"):
        if parameter == "pitchCurve":
            curve = compile_pitch_shape({"curve": [[0, 0], [0.5, 10], [1, 0]]}, self.selection)
        else:
            curve = [[0, 0], [0.25, 20], [0.75, -10], [1, 0]]
        return {"parameter": parameter, "curve": curve, "reason": "保留旋律并调整音符内的演唱起伏。"}

    def mark_status(self, message, status, *, verified=None):
        """模拟已持久化的执行状态，不通过写入真实宿主制造测试前提。"""
        document = self.read_saved()
        stored = next(item for item in document["messages"] if item["id"] == message["id"])
        stored["actions"][0]["status"] = status
        if verified is not None:
            stored["actions"][0]["result"] = {"verified": verified}
        self.manager._save(document)

    def assert_no_external_execution(self, *, planner_calls=1):
        self.assertEqual(self.planner.call_count, planner_calls)
        self.service.edit.assert_not_called()
        self.service.write_mode.assert_not_called()

    def test_new_target_and_new_baseline_create_fresh_actions_without_cloud_or_host_write(self):
        source = self.proposed_message()
        original = self.read_saved()
        self.move_target(start=21, scale=2)
        self.selection["projectFile"] = "D:/private-target/another.svp"
        self.selection["parameters"]["breathiness"]["fingerprint"] = "fedcba9876543210:101"
        message, result = self.reuse(source)
        self.assertNotEqual(message["id"], source["id"])
        self.assertNotEqual(message["actions"][0]["id"], source["actions"][0]["id"])
        self.assertEqual(message["selection"]["startSeconds"], 21)
        self.assertEqual(message["selection"]["endSeconds"], 25)
        self.assertEqual(message["actions"][0]["delta"], source["actions"][0]["delta"])
        self.assertEqual(message["actions"][0]["status"], "proposed")
        self.assertFalse(message.get("attachments"))
        self.assertNotIn("preview", message["actions"][0])
        self.assertNotIn("result", message["actions"][0])
        self.assertEqual(self.read_saved()["messages"][:len(original["messages"])], original["messages"])
        for private in ("private-target", "private-group", "private-voice-original", "fedcba9876543210", "_private"):
            self.assertNotIn(private, json.dumps(result))
        self.service.preview.assert_not_called()
        self.service.preview_batch.assert_not_called()
        self.assert_no_external_execution()
        # 生成新宿主预览时必须使用复用目标的新守卫，而非沿用原提案基线。
        preview = self.manager.preview_action(message["actions"][0]["id"])
        self.assertEqual(preview["status"], "previewed")
        self.assertEqual(preview["preview"]["startSeconds"], 21)
        self.assert_no_external_execution()

    def test_previewed_source_never_copies_confirmation_or_old_audio(self):
        source = self.proposed_message()
        action = source["actions"][0]
        preview = self.manager.preview_action(action["id"])
        document = self.read_saved()
        document["messages"][-1]["attachments"] = [{"name": "old-private-recording.wav", "url": "/old-audio"}]
        self.manager._save(document)
        self.move_target()
        message, _ = self.reuse(source)
        cloned = message["actions"][0]
        self.assertEqual(cloned["status"], "proposed")
        for key in ("preview", "previewBatchId", "result"):
            self.assertNotIn(key, cloned)
        self.assertNotIn(preview["preview"]["previewId"], json.dumps(message))
        self.assertFalse(message.get("attachments"))
        with self.assertRaises(ConversationError):
            self.manager.apply_action(cloned["id"])
        self.assert_no_external_execution()

    def test_unknown_or_unverified_applied_source_refuses_entire_message(self):
        source = self.proposed_message([
            {"parameter": "breathiness", "delta": 0.1, "reason": "调整气声。"},
            {"parameter": "tension", "delta": 0.1, "reason": "调整张力。"},
        ])
        self.move_target()
        for status, verified in (("unknown", False), ("applied", False)):
            with self.subTest(status=status):
                self.mark_status(source, status, verified=verified)
                before = self.read_saved()
                with self.assertRaises(ConversationError):
                    self.reuse(source)
                self.assertEqual(self.read_saved(), before)
        self.assert_no_external_execution()

    def test_verified_applied_cannot_clone_on_original_or_overlapping_target(self):
        source = self.proposed_message()
        self.mark_status(source, "applied", verified=True)
        self.selection["parameters"]["breathiness"]["fingerprint"] = "fedcba9876543210:101"
        for start in (1, 2):
            with self.subTest(start=start):
                self.move_target(start=start, group="private-group")
                before = self.read_saved()
                with self.assertRaises(ConversationError):
                    self.reuse(source)
                self.assertEqual(self.read_saved(), before)
        self.assert_no_external_execution()

    def test_verified_applied_may_be_reused_in_nonoverlapping_target(self):
        source = self.proposed_message()
        self.mark_status(source, "applied", verified=True)
        self.move_target(start=11, group="private-group")
        message, _ = self.reuse(source)
        self.assertEqual(message["actions"][0]["status"], "proposed")
        self.assert_no_external_execution()

    def test_moving_entire_group_cannot_bypass_applied_curve_overlap_protection(self):
        """组实例平移不改变底层 NoteGroup 曲线，不能按新秒范围当作另一个目标。"""
        source = self.proposed_message()
        self.mark_status(source, "applied", verified=True)
        old_note_times = [(note["onset"], note["duration"]) for note in self.selection["notes"]]
        self.selection["groupOffset"] += 10000
        self.selection["startSeconds"] += 10
        self.selection["endSeconds"] += 10
        for note in self.selection["notes"]:
            note["onsetSeconds"] += 10
        self.selection["parameters"]["breathiness"]["fingerprint"] = "fedcba9876543210:101"
        self.assertEqual([(note["onset"], note["duration"]) for note in self.selection["notes"]], old_note_times)
        before = self.read_saved()
        with self.assertRaisesRegex(ConversationError, "重叠"):
            self.reuse(source)
        self.assertEqual(self.read_saved(), before)
        self.assert_no_external_execution()

    def test_partial_capability_loss_preserves_only_usable_actions(self):
        source = self.proposed_message([
            {"parameter": "vocalMode_Soft", "delta": 10, "reason": "增加柔和模式。"},
            {"parameter": "tension", "delta": 0.1, "reason": "调整张力。"},
        ])
        self.move_target()
        del self.selection["parameters"]["vocalMode_Soft"]
        message, _ = self.reuse(source)
        self.assertEqual([action["parameter"] for action in message["actions"]], ["tension"])
        self.assert_no_external_execution()

    def test_all_unavailable_actions_reject_without_empty_reuse_message(self):
        source = self.proposed_message()
        self.selection["parameters"]["breathiness"]["available"] = False
        before = self.read_saved()
        with self.assertRaises(ConversationError):
            self.reuse(source)
        self.assertEqual(self.read_saved(), before)
        self.assert_no_external_execution()

    def test_generic_curve_can_scale_to_different_melody_and_duration(self):
        curve = [[0, 0], [0.25, 0.1], [1, -0.05]]
        source = self.proposed_message([{"parameter": "tension", "curve": curve, "reason": "前强后弱。"}])
        self.move_target(scale=3)
        self.selection["notes"][0]["pitch"] = 71
        self.selection["notes"][1]["durationSeconds"] -= 0.5
        message, _ = self.reuse(source)
        self.assertEqual(message["actions"][0]["curve"], curve)
        self.assertEqual(message["selection"]["endSeconds"], 17)
        self.assert_no_external_execution()

    def test_pitch_curve_reuse_accepts_same_effective_pitch_and_proportional_rhythm(self):
        source = self.proposed_message([self.pitch_action()])
        self.move_target(scale=2)
        # 组移调和原始 MIDI 可以不同；实际发声音高相同才是旋律匹配条件。
        self.selection["groupPitchOffset"] = 12
        for note in self.selection["notes"]:
            note["pitch"] -= 12
            note["index"] += 8
            note["lyrics"] = "换词"
        self.selection["notes"].reverse()
        message, _ = self.reuse(source)
        self.assertEqual(message["actions"][0]["curve"], source["actions"][0]["curve"])
        self.assertEqual(message["selection"]["groupPitchOffset"], 12)
        self.assert_no_external_execution()

    def test_pitch_curves_reject_different_note_count_pitch_or_rhythm(self):
        for parameter in ("pitchCurve", "pitchDelta"):
            for change in ("pitch", "rhythm", "count"):
                with self.subTest(parameter=parameter, change=change):
                    base = copy.deepcopy(self.selection)
                    source = self.proposed_message([self.pitch_action(parameter)])
                    self.move_target()
                    if change == "pitch":
                        self.selection["notes"][1]["pitch"] += 1
                    elif change == "rhythm":
                        self.selection["notes"][0]["durationSeconds"] -= 0.25
                        self.selection["notes"][1]["onsetSeconds"] -= 0.25
                        self.selection["notes"][1]["durationSeconds"] += 0.25
                    else:
                        self.selection["notes"].pop()
                        self.selection["noteCount"] = 1
                    before = self.read_saved()
                    with self.assertRaises(ConversationError):
                        self.reuse(source)
                    self.assertEqual(self.read_saved(), before)
                    self.selection = base
        self.assert_no_external_execution(planner_calls=6)

    def test_mismatched_pitch_can_be_excluded_while_generic_parameter_remains(self):
        source = self.proposed_message([
            self.pitch_action("pitchDelta"),
            {"parameter": "breathiness", "delta": 0.1, "reason": "调整气声。"},
        ])
        self.move_target()
        self.selection["notes"][0]["pitch"] += 2
        message, _ = self.reuse(source)
        self.assertEqual([action["parameter"] for action in message["actions"]], ["breathiness"])
        self.assert_no_external_execution()

    def test_native_pitch_reuse_revalidates_curve_against_each_note(self):
        source = self.proposed_message([self.pitch_action()])
        document = self.read_saved()
        # 历史文件可能由旧版模型生成粗略长滑音；旋律相同不能绕过逐音符校验。
        document["messages"][-1]["actions"][0]["curve"] = [[0, 60], [1, 64]]
        self.manager._save(document)
        self.move_target()
        with self.assertRaises(ConversationError):
            self.reuse(source)
        self.assert_no_external_execution()

    def test_reusing_a_reused_message_uses_its_own_selection_snapshot(self):
        source = self.proposed_message([self.pitch_action("pitchDelta")])
        self.move_target(scale=2)
        first, _ = self.reuse(source)
        document = self.read_saved()
        # 人为损坏更早的消息快照，用于证明再次复用读取的是本条提案自己的选区。
        for message in document["messages"]:
            if message["id"] != first["id"] and isinstance(message.get("selection"), dict):
                message["selection"]["notes"][0]["pitch"] = 10
        self.manager._save(document)
        self.move_target(start=31, scale=0.5)
        second, _ = self.reuse(first)
        self.assertEqual(second["selection"]["startSeconds"], 31)
        self.assertEqual(second["actions"][0]["curve"], first["actions"][0]["curve"])
        self.assertNotEqual(second["actions"][0]["id"], first["actions"][0]["id"])
        self.assert_no_external_execution()

    def test_uniform_pitch_delta_does_not_require_matching_melody(self):
        source = self.proposed_message([{"parameter": "pitchDelta", "delta": 5, "reason": "整体微调五音分。"}])
        self.move_target()
        self.selection["notes"][0]["pitch"] = 70
        message, _ = self.reuse(source)
        self.assertEqual(message["actions"][0]["delta"], 5)
        self.assert_no_external_execution()

    def test_session_only_change_can_create_new_preview_but_never_apply_old_preview(self):
        source = self.proposed_message()
        action_id = source["actions"][0]["id"]
        first = self.manager.preview_action(action_id)
        self.service.bridge.status.return_value["session"] = "session-2"
        with self.assertRaises(ConversationError):
            self.manager.apply_action(action_id)
        self.service.edit.assert_not_called()
        second = self.manager.preview_action(action_id)
        self.assertNotEqual(first["preview"]["previewId"], second["preview"]["previewId"])
        self.assertEqual(second["status"], "previewed")
        self.assert_no_external_execution()

    def test_reconnected_legacy_guard_can_repreview_with_complete_unchanged_fingerprint(self):
        source = self.proposed_message()
        action_id = source["actions"][0]["id"]
        document = self.read_saved()
        guard = document["_private"]["actions"][action_id]
        document["_private"]["actions"][action_id] = {
            key: guard[key] for key in ("selection", "session", "parameter")}
        self.manager._save(document)
        self.service.bridge.status.return_value["session"] = "session-2"
        preview = self.manager.preview_action(action_id)
        self.assertEqual(preview["status"], "previewed")
        self.assert_no_external_execution()

    def test_legacy_guard_recovers_after_reconnect_and_note_reindexing(self):
        source = self.proposed_message()
        action_id = source["actions"][0]["id"]
        document = self.read_saved()
        guard = document["_private"]["actions"][action_id]
        document["_private"]["actions"][action_id] = {
            key: guard[key] for key in ("selection", "session", "parameter")}
        # 旧回复没有自己的选区字段，需要原用户消息的快照重建旧指纹再做稳定比较。
        document["messages"][-1].pop("selection", None)
        self.manager._save(document)
        self.selection["notes"].reverse()
        for note in self.selection["notes"]:
            note["index"] += 9
        self.service.bridge.status.return_value["session"] = "session-2"
        preview = self.manager.preview_action(action_id)
        self.assertEqual(preview["status"], "previewed")
        self.assert_no_external_execution()

    def test_refreshed_preview_persists_new_guard_and_requires_new_confirmation(self):
        source = self.proposed_message()
        action_id = source["actions"][0]["id"]
        self.manager.preview_action(action_id)
        self.service.bridge.status.return_value["session"] = "session-2"
        refreshed = self.manager.preview_action(action_id)
        self.service.edit.assert_not_called()
        # 重新构造管理器，验证新保护信息已落盘，而非仅留在本次请求的内存对象中。
        manager = type(self.manager)(self.service)
        applied = manager.apply_action(action_id)
        self.assertEqual(applied["status"], "applied")
        self.service.edit.assert_called_once_with("apply", {"previewId": refreshed["preview"]["previewId"]})
        self.assertEqual(self.planner.call_count, 1)
        self.service.write_mode.assert_not_called()

    def test_reused_proposal_does_not_follow_later_selection_changes_implicitly(self):
        source = self.proposed_message()
        self.move_target(start=11)
        copied, _ = self.reuse(source)
        self.move_target(start=21)
        with self.assertRaises(ConversationError):
            self.manager.preview_action(copied["actions"][0]["id"])
        self.service.preview.assert_not_called()
        self.assert_no_external_execution()

    def test_reconnect_without_full_fingerprint_cannot_refresh_preview(self):
        del self.selection["parameters"]["breathiness"]["fingerprint"]
        source = self.proposed_message()
        self.service.bridge.status.return_value["session"] = "session-2"
        with self.assertRaises(ConversationError):
            self.manager.preview_action(source["actions"][0]["id"])
        self.service.preview.assert_not_called()
        self.assert_no_external_execution()

    def test_reconnect_with_changed_parameter_still_requires_explicit_reuse(self):
        source = self.proposed_message()
        self.service.bridge.status.return_value["session"] = "session-2"
        self.selection["parameters"]["breathiness"]["fingerprint"] = "fedcba9876543210:100"
        with self.assertRaises(ConversationError):
            self.manager.preview_action(source["actions"][0]["id"])
        self.service.preview.assert_not_called()
        message, _ = self.reuse(source)
        self.assertEqual(self.manager.preview_action(message["actions"][0]["id"])["status"], "previewed")
        self.assert_no_external_execution()

    def test_stable_identity_ignores_note_indexes_and_input_order_but_keeps_lyrics(self):
        source = self.proposed_message()
        action_id = source["actions"][0]["id"]
        self.selection["notes"].reverse()
        for note in self.selection["notes"]:
            note["index"] += 20
        self.assertEqual(self.manager.preview_action(action_id)["status"], "previewed")
        self.selection["notes"][0]["lyrics"] = "另一歌词"
        with self.assertRaises(ConversationError):
            self.manager.preview_action(action_id)
        self.assertEqual(self.service.preview.call_count, 1)
        self.assert_no_external_execution()

    def test_reconnect_during_host_preview_cannot_issue_confirmable_result(self):
        source = self.proposed_message()
        self.service.bridge.status.return_value["session"] = "session-2"

        def changed_session(*args, **kwargs):
            result = self.host_preview(*args, **kwargs)
            self.service.bridge.status.return_value["session"] = "session-3"
            return result

        self.service.preview.side_effect = changed_session
        with self.assertRaises(ConversationError):
            self.manager.preview_action(source["actions"][0]["id"])
        saved = self.read_saved()["messages"][-1]["actions"][0]
        self.assertNotEqual(saved["status"], "previewed")
        self.assert_no_external_execution()

    def test_batch_reconnect_repreviews_all_members_and_rejects_old_confirmation(self):
        source = self.proposed_message([
            {"parameter": "breathiness", "delta": 0.1, "reason": "调整气声。"},
            {"parameter": "tension", "delta": 0.1, "reason": "调整张力。"},
        ])
        ids = [action["id"] for action in source["actions"]]
        first = self.manager.preview_batch(ids)
        self.service.bridge.status.return_value["session"] = "session-2"
        with self.assertRaises(ConversationError):
            self.manager.apply_batch(first["batchId"])
        self.service.edit.assert_not_called()
        second = self.manager.preview_batch(ids)
        self.assertNotEqual(first["batchId"], second["batchId"])
        self.assertEqual(second["errors"], [])
        self.assertTrue(all(action["status"] == "previewed" for action in second["actions"]))
        self.assert_no_external_execution()


class ServerReuseTests(unittest.TestCase):
    """只驱动内存 HTTP 处理器，验证复用入口认证及固定消息 ID 契约。"""

    request = server_fixture.ServerTests.request

    def setUp(self):
        server_fixture.ServerTests.setUp(self)
        self.conversation_id = "a" * 32
        self.message_id = "b" * 32
        self.route = "/api/conversations/" + self.conversation_id + "/reuse"
        self.service.reuse_message.return_value = {
            "conversation": {"id": self.conversation_id, "messages": []}, "messageId": "c" * 32}

    def assert_only_local_reuse(self):
        self.service.submit.assert_not_called()
        self.service.edit.assert_not_called()
        self.service.preview.assert_not_called()
        self.service.apply_action.assert_not_called()

    def test_reuse_requires_current_token_and_matching_origin(self):
        for headers in ({}, {"X-SV-Token": "old-token"},
                        {"X-SV-Token": "test-token", "Origin": "https://untrusted.example"},
                        {"X-SV-Token": "test-token", "Host": "untrusted.example:8765"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", self.route, headers=headers,
                                             body={"messageId": self.message_id})
                self.assertEqual(status, 403)
        self.service.reuse_message.assert_not_called()
        self.assert_only_local_reuse()

    def test_reuse_rejects_missing_or_extra_payload_fields(self):
        for body in ({}, {"messageId": self.message_id, "curve": [[0, 0], [1, 0.1]]},
                     {"messageId": self.message_id, "selection": {}},
                     {"messageId": self.message_id, "previewId": "stale-preview"},
                     {"messageId": self.message_id, "apply": True}):
            with self.subTest(fields=list(body)):
                status, _, _ = self.request("POST", self.route, headers={"X-SV-Token": "test-token"}, body=body)
                self.assertEqual(status, 400)
        self.service.reuse_message.assert_not_called()
        self.assert_only_local_reuse()

    def test_authorized_reuse_routes_only_existing_message_identity(self):
        status, _, body = self.request("POST", self.route, headers={"X-SV-Token": "test-token"},
                                       body={"messageId": self.message_id})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.service.reuse_message.return_value)
        self.service.reuse_message.assert_called_once_with(self.conversation_id, self.message_id)
        self.assert_only_local_reuse()


if __name__ == "__main__":
    unittest.main()
