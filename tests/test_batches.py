"""组合预览的会话与真实 Lua 状态机回归；完全隔离工程、配置和网络。

重点覆盖部分失败、跨消息拒绝、整批 unknown 落盘、单条撤销和回滚失败恢复。
替身仅提供宿主 API，所有预览/应用/恢复逻辑均执行生产源文件。
"""

import json
import unittest
from unittest.mock import patch

from synthv_assistant.conversations import ConversationError
from tests import test_conversations as conversation_fixture
from tests import test_lua_bridge as lua_fixture
from tests import test_pitch_lua as pitch_fixture
from tests import test_service as service_fixture
from tests import test_server as server_fixture
from tests.test_parameters import modern_selection


class ConversationBatchTests(unittest.TestCase):
    send = conversation_fixture.ConversationTests.send
    read_saved = conversation_fixture.ConversationTests.read_saved

    def setUp(self):
        conversation_fixture.ConversationTests.setUp(self)
        self.selection["capabilities"] = {"curves": True, "batchPreview": True}
        for definition in self.selection["parameters"].values():
            definition.update(available=True, fingerprint="0123456789abcdef:100")
        self.plan["actions"].append({"parameter": "tension", "delta": 0.1, "reason": "调整张力"})
        self.actions = self.send()["messages"][-1]["actions"]
        self.ids = [action["id"] for action in self.actions]
        self.fail_ids = set()

        def preview_batch(batch_id, actions):
            return {"previews": [{"actionId": item["id"], "preview": {
                "previewId": "host-" + item["id"], "parameter": item["parameter"], "pointCount": 12,
                "startSeconds": 1, "endSeconds": 2}}
                for item in actions if item["id"] not in self.fail_ids],
                "errors": [{"actionId": item["id"], "message": "固定的安全失败提示"}
                           for item in actions if item["id"] in self.fail_ids]}

        self.service.preview_batch.side_effect = preview_batch

    def preview(self):
        return self.manager.preview_batch(self.ids)

    def test_partial_failure_keeps_all_public_actions_but_only_successful_confirmation(self):
        self.fail_ids.add(self.ids[0])
        result = self.preview()
        self.assertEqual([item["status"] for item in result["actions"]], ["proposed", "previewed"])
        self.assertEqual(result["errors"][0]["actionId"], self.ids[0])
        self.assertEqual(self.read_saved()["_private"]["batch"]["actionIds"], [self.ids[1]])
        self.assertNotIn("preview", result["actions"][0])
        self.assertEqual(result["actions"][1]["previewBatchId"], result["batchId"])
        self.assertNotIn("projectFile", json.dumps(result))
        self.service.edit.assert_not_called()

    def test_same_count_manual_edit_repreviews_current_baseline_but_rejects_later_apply(self):
        """新预览可以接受用户先前编辑；生成候选后再改值必须阻止整批确认。"""
        self.selection["parameters"]["breathiness"]["fingerprint"] = "0123456789abcdef:101"
        result = self.preview()
        self.assertEqual(result["errors"], [])
        self.assertEqual([item["id"] for item in self.service.preview_batch.call_args.args[1]], self.ids)
        self.assertTrue(all(item["status"] == "previewed" for item in result["actions"]))
        self.selection["parameters"]["breathiness"]["fingerprint"] = "0123456789abcdef:102"
        with self.assertRaisesRegex(ConversationError, "参数摘要"):
            self.manager.apply_batch(result["batchId"])
        self.service.edit.assert_not_called()

    def test_cross_message_duplicate_and_cross_conversation_requests_never_reach_host(self):
        second = self.send()["messages"][-1]["actions"][0]["id"]
        another = self.manager.create_conversation()["id"]
        other = self.manager.send_message(another, "另一个会话", True, [])["messages"][-1]["actions"][0]["id"]
        for invalid in ([], self.ids * 3, [self.ids[0]] * 2, [self.ids[0], second], [self.ids[0], other]):
            with self.subTest(invalid=invalid), self.assertRaises(ConversationError):
                self.manager.preview_batch(invalid)
        self.service.preview_batch.assert_not_called()

    def test_new_single_and_batch_previews_invalidate_previous_credentials(self):
        first = self.preview()
        with self.assertRaisesRegex(ConversationError, "组合预览"):
            self.manager.apply_action(self.ids[0])
        self.manager.preview_action(self.ids[0])
        with self.assertRaisesRegex(ConversationError, "失效"):
            self.manager.apply_batch(first["batchId"])
        second = self.preview()
        self.assertNotEqual(first["batchId"], second["batchId"])
        self.fail_ids.update(self.ids)
        failed = self.preview()
        self.assertIsNone(failed["batchId"])
        self.assertTrue(all(item["status"] == "proposed" and "preview" not in item for item in failed["actions"]))
        with self.assertRaises(ConversationError):
            self.manager.apply_batch(second["batchId"])

    def test_all_unknown_are_durable_before_one_host_write_and_cannot_be_replayed(self):
        result = self.preview()

        def inspect_write(command, args):
            self.assertEqual(command, "apply_batch")
            saved = self.read_saved()
            self.assertNotIn("batch", saved["_private"])
            self.assertTrue(all(item["status"] == "unknown" for item in saved["messages"][-1]["actions"]))
            self.assertEqual(len(args["previewIds"]), 2)
            raise RuntimeError("private-host-path")

        self.service.edit.side_effect = inspect_write
        applied = self.manager.apply_batch(result["batchId"])
        self.assertTrue(all(item["status"] == "unknown" for item in applied["actions"]))
        self.assertNotIn("private-host-path", json.dumps(applied))
        with self.assertRaises(ConversationError):
            self.manager.apply_batch(result["batchId"])
        with self.assertRaises(ConversationError):
            self.preview()
        self.assertEqual(self.service.edit.call_count, 1)

    def test_guard_and_save_failures_do_not_begin_host_write(self):
        result = self.preview()
        self.selection["parameters"]["tension"]["fingerprint"] = "fedcba9876543210:100"
        with self.assertRaises(ConversationError):
            self.manager.apply_batch(result["batchId"])
        self.selection["parameters"]["tension"]["fingerprint"] = "0123456789abcdef:100"
        with patch.object(self.manager, "_save", side_effect=OSError("private")), self.assertRaises(ConversationError):
            self.manager.apply_batch(result["batchId"])
        self.service.edit.assert_not_called()
        self.assertTrue(all(item["status"] == "previewed" for item in self.read_saved()["messages"][-1]["actions"]))

    def test_successful_batch_can_repreview_only_after_confirmed_undo(self):
        result = self.preview()
        self.service.edit.return_value = {"verified": True, "results": [
            {"verified": True, "parameter": action["parameter"], "pointCount": 12, "undoRecords": 1}
            for action in self.actions]}
        applied = self.manager.apply_batch(result["batchId"])
        self.assertTrue(all(item["status"] == "applied" for item in applied["actions"]))
        # 完整原指纹重现模拟宿主已经撤销；仍必须新预览与再次确认。
        again = self.preview()
        self.assertNotEqual(result["batchId"], again["batchId"])
        self.assertTrue(all(item["status"] == "previewed" for item in again["actions"]))
        self.assertEqual(self.service.edit.call_count, 1)


@unittest.skipIf(lua_fixture.LuaRuntime is None, "未安装可选 Lua 测试依赖 lupa")
class LuaBatchTests(unittest.TestCase):
    call = lua_fixture.LuaBridgeTests.call
    enable = lua_fixture.LuaBridgeTests.enable
    preview_id = lua_fixture.LuaBridgeTests.preview_id

    def setUp(self):
        lua_fixture.LuaBridgeTests.setUp(self)
        # 每个参数独立对象，避免测试替身把张力与气声错误映射到同一条曲线。
        self.lua.execute('''
local group=SV:getMainEditor():getCurrentGroup():getTarget()
host.curves={tension=host.curve,breathiness=host.curve:clone()}
host.curves.breathiness.cloned=false
function group:getParameter(name) return host.curves[name] or host.curve end
''')
        self.batch = "a" * 32
        self.requests = [{"actionId": "1" * 32, "change": {"parameter": "tension", "delta": 0.1}},
                         {"actionId": "2" * 32, "change": {"parameter": "breathiness", "delta": 0.08}}]

    def preview(self):
        result = self.call("preview_batch", batchId=self.batch, actions=self.requests)
        self.assertTrue(result["ok"], result.get("error"))
        return result["result"]

    def apply(self, preview):
        self.enable()
        ids = [item["preview"]["previewId"] for item in preview["items"] if item["ok"]]
        return self.call("apply_batch", batchId=self.batch, previewIds=ids)

    def test_one_readonly_preview_one_undo_and_full_restore(self):
        preview = self.preview()
        self.assertTrue(all(item["ok"] for item in preview["items"]))
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.undos, 0)
        result = self.apply(preview)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["result"]["results"]), 2)
        self.assertEqual(self.host.undos, 1)
        restored = self.call("restore")
        self.assertTrue(restored["ok"], restored.get("error"))
        self.assertEqual(self.host.undos, 2)
        self.assertEqual(self.host.curves.tension.get(self.host.curves.tension, 1500), 0)
        self.assertEqual(self.host.curves.breathiness.get(self.host.curves.breathiness, 1500), 0)

    def test_partial_preview_failure_and_confirmation_of_successful_subset(self):
        self.requests[1]["change"]["delta"] = 50
        preview = self.preview()
        self.assertEqual([item["ok"] for item in preview["items"]], [True, False])
        result = self.apply(preview)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["result"]["results"]), 1)
        self.assertEqual(self.host.curves.breathiness.get(self.host.curves.breathiness, 1500), 0)

    def test_all_guards_precede_writes_and_batch_cannot_be_applied_as_single(self):
        preview = self.preview()
        self.enable()
        single = self.call("apply", previewId=preview["items"][0]["preview"]["previewId"])
        self.assertFalse(single["ok"])
        self.lua.execute("host.curves.breathiness.points[1][2]=0.03")
        result = self.apply(preview)
        self.assertFalse(result["ok"])
        self.assertEqual(self.host.mutations, 0)
        self.assertEqual(self.host.undos, 0)

    def test_new_preview_invalidates_batch_and_successful_batch_cannot_replay(self):
        preview = self.preview()
        self.preview_id()
        self.assertFalse(self.apply(preview)["ok"])
        preview = self.preview()
        self.assertTrue(self.apply(preview)["ok"])
        writes = self.host.mutations
        self.assertFalse(self.apply(preview)["ok"])
        self.assertEqual(self.host.mutations, writes)

    def test_failed_second_write_restores_both_parameters(self):
        preview = self.preview()
        self.lua.execute('''
local target=host.curves.breathiness
local add=target.add
function target:add(x,y) if self.writeRound==1 then error("simulated second write failure") end return add(self,x,y) end
''')
        result = self.apply(preview)
        self.assertFalse(result["ok"])
        self.assertIn("已恢复", result["error"])
        self.assertEqual(self.host.curves.tension.get(self.host.curves.tension, 1500), 0)
        self.assertEqual(self.host.curves.breathiness.get(self.host.curves.breathiness, 1500), 0)
        self.assertEqual(self.host.undos, 1)

    def test_failed_rollback_retains_whole_batch_for_guarded_restore(self):
        preview = self.preview()
        self.host.corruptRounds = self.lua.table_from({1: True, 2: True})
        result = self.apply(preview)
        self.assertFalse(result["ok"])
        self.assertIn("恢复记录", result["error"])
        self.host.corruptRounds = None
        restored = self.call("restore")
        self.assertTrue(restored["ok"], restored.get("error"))
        self.assertEqual(self.host.curves.tension.get(self.host.curves.tension, 1500), 0)
        self.assertEqual(self.host.curves.breathiness.get(self.host.curves.breathiness, 1500), 0)

    def test_restore_checks_all_members_before_touching_any(self):
        preview = self.preview()
        self.assertTrue(self.apply(preview)["ok"])
        self.lua.execute("host.curves.breathiness.points[1][2]=0.03")
        writes = self.host.mutations
        self.assertFalse(self.call("restore")["ok"])
        self.assertEqual(self.host.mutations, writes)


class BatchBoundaryTests(unittest.TestCase):
    """验证组合路由仍走同一个令牌边界，宿主错误不能带出私有路径。"""
    request = server_fixture.ServerTests.request

    def setUp(self):
        server_fixture.ServerTests.setUp(self)

    def test_routes_require_token_and_reject_extra_fields(self):
        cases = [("preview", {"actionIds": ["a" * 32]}, "preview_action_batch"),
                 ("apply", {"batchId": "b" * 32}, "apply_action_batch")]
        for verb, body, operation in cases:
            callback = getattr(self.service, operation)
            callback.return_value = {"actions": []}
            route = "/api/assistant/batches/" + verb
            self.assertEqual(self.request("POST", route, body=body)[0], 403)
            callback.assert_not_called()
            self.assertEqual(self.request("POST", route, body={**body, "extra": True},
                                          headers={"X-SV-Token": "test-token"})[0], 400)
            callback.assert_not_called()
            self.assertEqual(self.request("POST", route, body=body,
                                          headers={"X-SV-Token": "test-token"})[0], 200)
            callback.assert_called_once_with(next(iter(body.values())))


class BatchServiceTests(unittest.TestCase):
    setUp = service_fixture.ServiceTests.setUp

    def test_private_host_error_is_sanitized_and_success_is_bound_to_action_parameter(self):
        batch_id, action_id = "a" * 32, "b" * 32
        actions = [{"id": action_id, "parameter": "tension", "delta": 0.1}]
        for item in ({"actionId": action_id, "ok": False, "error": "C:/private/token-not-public"},
                     {"actionId": action_id, "ok": True, "preview": {"previewId": "p", "parameter": "gender"}}):
            self.bridge.call.side_effect = [modern_selection(), {"batchId": batch_id, "items": [item]}]
            result = self.service.preview_batch(batch_id, actions)
            self.assertEqual(result["previews"], [])
            self.assertEqual(len(result["errors"]), 1)
            self.assertNotIn("private", json.dumps(result))
            self.assertNotIn("token-not-public", json.dumps(result))

    def test_response_identity_mismatch_and_recording_are_fail_closed(self):
        actions = [{"id": "a" * 32, "parameter": "tension", "delta": 0.1}]
        self.bridge.call.side_effect = [modern_selection(), {"batchId": "b" * 32, "items": []}]
        with self.assertRaises(ValueError):
            self.service.preview_batch("c" * 32, actions)
        self.service.recording = True
        self.bridge.call.reset_mock()
        with self.assertRaises(ValueError):
            self.service.preview_batch("c" * 32, actions)
        self.bridge.call.assert_not_called()


@unittest.skipIf(lua_fixture.LuaRuntime is None, "未安装可选 Lua 测试依赖 lupa")
class MixedNativeBatchTests(unittest.TestCase):
    """原生音高与独立自动化共用一条撤销、读回校验及恢复边界。"""
    call = pitch_fixture.NativePitchBridgeTests.call

    def setUp(self):
        pitch_fixture.NativePitchBridgeTests.setUp(self)
        curve_source = lua_fixture.STUB.split("local function copy(points)", 1)[1].split("host.curve=setmetatable", 1)[0]
        self.lua.execute("local function copy(points)" + curve_source + '''
host.method="Linear"
host.automation=setmetatable({points={{0,0},{3000,0}}},Curve)
local original=group.getParameter
function group:getParameter(name) if name=="tension" then return host.automation end return original(self,name) end
''')

    def test_native_and_automation_are_applied_and_restored_together(self):
        original = self.native.snapshot(self.group)
        result = self.call("preview_batch", batchId="c" * 32, actions=[
            {"actionId": "1" * 32, "change": {"parameter": "pitchCurve", "curve": [[0, 60], [0.5, 60.2], [1, 60]]}},
            {"actionId": "2" * 32, "change": {"parameter": "tension", "delta": 0.1}}])
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(all(item["ok"] for item in result["result"]["items"]), result)
        ids = [item["preview"]["previewId"] for item in result["result"]["items"]]
        self.assertTrue(self.call("write_mode", enabled=True, expectedProject="isolated-test.svp")["ok"])
        applied = self.call("apply_batch", batchId="c" * 32, previewIds=ids)
        self.assertTrue(applied["ok"], applied.get("error"))
        self.assertEqual(self.host.undos, 1)
        self.assertFalse(self.native.same(self.group, original))
        restored = self.call("restore")
        self.assertTrue(restored["ok"], restored.get("error"))
        self.assertTrue(self.native.same(self.group, original))
        self.assertEqual(self.host.automation.get(self.host.automation, 1500), 0)
        self.assertEqual(self.host.undos, 2)


if __name__ == "__main__":
    unittest.main()
