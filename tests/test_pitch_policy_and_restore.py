"""验证音高授权及指定批次撤销的端到端边界，全部使用隔离资料与替身。

模型替身故意返回不符合用户策略的动作，用于检查持久层的第二道校验；
Lua 用例执行真实桥接分发器，但所有宿主对象仅存在于独立测试虚拟机。
"""

import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from synthv_assistant.conversations import ConversationError, normalize_pitch_policy
from tests import test_batches as batch_fixture
from tests import test_conversations as conversation_fixture
from tests import test_lua_bridge as lua_fixture
from tests import test_server as server_fixture
from tests import test_service as service_fixture


class PitchPolicyTests(unittest.TestCase):
    """策略必须来自本次请求，不能由模型输出或上一轮消息补充授权。"""

    read_saved = conversation_fixture.ConversationTests.read_saved

    def setUp(self):
        conversation_fixture.ConversationTests.setUp(self)
        self.selection["capabilities"] = {"curves": True, "nativePitch": True}
        self.selection["parameters"]["pitchDelta"]["range"] = [-1200, 1200]
        self.selection["parameters"]["pitchCurve"] = {
            "kind": "pitch", "available": True, "range": [0, 127], "pointCount": 0}

    def send_policy(self, policy):
        return self.manager.send_message(self.identifier, "保留原演唱并调整细节", True, [],
                                         pitch_policy=policy)

    def test_strategy_matrix_is_checked_after_planner_before_any_executable_action(self):
        """即使规划器忽略标志，也不能把微调授权扩展成重绘或反向替换。"""
        actions = {
            "breathiness": {"parameter": "breathiness", "delta": 0.1, "reason": "轻微调整气声"},
            "pitchDelta": {"parameter": "pitchDelta", "curve": [[0, 0], [0.5, 8], [1, 0]], "reason": "局部音高微调"},
            "pitchCurve": {"parameter": "pitchCurve", "curve": [[0, 60], [0.5, 60.08], [1, 60]], "reason": "重绘原生音高"},
        }
        allowed = {"preserve": {"breathiness"}, "local": {"breathiness", "pitchDelta"},
                   "redraw": {"breathiness", "pitchCurve"}}
        for policy, permitted in allowed.items():
            for parameter, action in actions.items():
                with self.subTest(policy=policy, parameter=parameter):
                    # 每种组合独立存档，避免上一轮动作让错误的空结果断言意外通过。
                    self.identifier = self.manager.create_conversation()["id"]
                    self.plan["actions"] = [copy.deepcopy(action)]
                    result = self.send_policy(policy)
                    kwargs = self.planner.call_args.kwargs
                    self.assertEqual(kwargs["allow_pitch_adjustments"], policy != "preserve")
                    self.assertEqual(kwargs["allow_native_pitch"], policy == "redraw")
                    self.assertEqual(result["messages"][-2]["pitchPolicy"], policy)
                    reply = result["messages"][-1]
                    if parameter in permitted:
                        self.assertEqual(reply["role"], "assistant")
                        self.assertEqual(reply["pitchPolicy"], policy)
                        self.assertEqual([item["parameter"] for item in reply["actions"]], [parameter])
                        self.assertEqual(reply["actions"][0]["status"], "proposed")
                        self.assertEqual(len(self.read_saved()["_private"]["actions"]), 1)
                    else:
                        self.assertEqual(reply["role"], "error")
                        self.assertIn("音高策略不同", reply["text"])
                        self.assertEqual(reply["actions"], [])
                        self.assertEqual(self.read_saved()["_private"]["actions"], {})
        self.service.preview.assert_not_called()
        self.service.edit.assert_not_called()

    def test_default_preserve_does_not_inherit_previous_redraw_permission(self):
        self.plan["actions"] = [{"parameter": "pitchCurve", "curve": [[0, 60], [1, 60]], "reason": "保留旋律"}]
        self.assertEqual(self.send_policy("redraw")["messages"][-1]["role"], "assistant")
        result = self.manager.send_message(self.identifier, "接下来只改气声", True, [])
        self.assertEqual(result["messages"][-2]["pitchPolicy"], "preserve")
        self.assertEqual(result["messages"][-1]["role"], "error")
        self.assertFalse(self.planner.call_args.kwargs["allow_pitch_adjustments"])
        self.assertFalse(self.planner.call_args.kwargs["allow_native_pitch"])
        # 历史授权仍如实展示，但不能被复用为当前请求的隐式授权。
        self.assertEqual(result["messages"][0]["pitchPolicy"], "redraw")
        self.assertEqual(len(self.read_saved()["_private"]["actions"]), 1)

    def test_invalid_policy_is_rejected_before_selection_save_or_planner(self):
        original = self.read_saved()
        for invalid in (None, True, False, 1, [], {}, "", "native", "REDRAW", " redraw "):
            with self.subTest(policy=invalid):
                with self.assertRaises(ConversationError):
                    normalize_pitch_policy(invalid)
                with self.assertRaises(ConversationError):
                    self.send_policy(invalid)
        self.assertEqual(self.read_saved(), original)
        self.planner.assert_not_called()
        self.service.get_selection.assert_not_called()
        self.service.edit.assert_not_called()

    def test_mixed_plan_is_rejected_atomically_instead_of_silently_dropping_pitch(self):
        """公开说明和真正动作必须一致，不能悄悄只保存未经完整解释的半份计划。"""
        self.plan["actions"].append({"parameter": "pitchDelta", "delta": 5, "reason": "未授权的音高修改"})
        result = self.send_policy("preserve")
        self.assertEqual(result["messages"][-1]["role"], "error")
        self.assertEqual(result["messages"][-1]["actions"], [])
        self.assertEqual(self.read_saved()["_private"]["actions"], {})


class PitchPolicyBoundaryTests(unittest.TestCase):
    """非法策略在 HTTP 入队前拒绝，不能占用任务或触发付费请求。"""

    setUp = server_fixture.ServerTests.setUp
    request = server_fixture.ServerTests.request

    def test_invalid_policy_or_render_mode_never_enters_job_queue(self):
        route = "/api/conversations/" + "a" * 32 + "/messages"
        bodies = [{"text": "调整", "pitchPolicy": value}
                  for value in (None, True, 1, [], {}, "native", " preserve ")]
        bodies += [{"text": "调整", "pitchPolicy": "redraw", "renderMode": value}
                   for value in (None, True, "unknown")]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", route, body=body,
                                              headers={"X-SV-Token": "test-token"})[0], 400)
        self.service.submit.assert_not_called()
        self.service.send_message.assert_not_called()

    def test_valid_policy_keeps_model_and_drawing_arguments_in_correct_positions(self):
        identifier = "a" * 32
        self.service.submit.return_value = "queued-test-job"
        options = {"platformId": "default", "model": "synthetic-model"}
        for policy in ("preserve", "local", "redraw"):
            with self.subTest(policy=policy):
                status, _, body = self.request("POST", f"/api/conversations/{identifier}/messages",
                    headers={"X-SV-Token": "test-token"}, body={"text": "调整", "includeSelection": True,
                        "pitchPolicy": policy, "renderMode": "points", "modelOptions": options})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["jobId"], "queued-test-job")
                self.service.submit.assert_called_with(self.service.send_message, identifier, "调整", True, [],
                                                       options, "points", policy)
        # 无策略的旧客户端沿用服务默认 preserve，不从历史设置自动推导授权。
        self.request("POST", f"/api/conversations/{identifier}/messages", body={"text": "旧客户端"},
                     headers={"X-SV-Token": "test-token"})
        self.service.submit.assert_called_with(self.service.send_message, identifier, "旧客户端", True, [])


class PolicyServiceTests(unittest.TestCase):
    """服务包装层必须原样传递用户策略，缺省值也要明确传入会话层。"""

    setUp = service_fixture.ServiceTests.setUp

    def test_service_forwards_explicit_and_default_pitch_policy(self):
        manager = MagicMock()
        with patch.object(self.service, "conversations", return_value=manager):
            self.service.send_message("a" * 32, "讨论", False, [])
            self.assertEqual(manager.send_message.call_args.kwargs["pitch_policy"], "preserve")
            for policy in ("local", "redraw"):
                self.service.send_message("a" * 32, "调整", True, [], render_mode="points", pitch_policy=policy)
                self.assertEqual(manager.send_message.call_args.kwargs["pitch_policy"], policy)
                self.assertEqual(manager.send_message.call_args.kwargs["render_mode"], "points")
            self.service.restore_action_batch("b" * 32)
            manager.restore_batch.assert_called_once_with("b" * 32)


class ConversationBatchRestoreTests(unittest.TestCase):
    """撤销绑定原批次身份，并把未知状态先持久化再接触宿主。"""

    send = conversation_fixture.ConversationTests.send
    read_saved = conversation_fixture.ConversationTests.read_saved

    def setUp(self):
        batch_fixture.ConversationBatchTests.setUp(self)
        self.service.bridge.status.return_value.update(writeEnabled=True, capabilities={"batchRestore": True})
        preview = self.manager.preview_batch(self.ids)
        self.batch_id = preview["batchId"]
        self.service.edit.return_value = {"verified": True, "results": [
            {"verified": True, "parameter": action["parameter"], "pointCount": 12}
            for action in self.actions]}
        applied = self.manager.apply_batch(self.batch_id)
        self.assertTrue(all(item["status"] == "applied" for item in applied["actions"]))
        self.applied_document = self.read_saved()
        self.service.edit.reset_mock()

    def test_confirmed_restore_clears_old_credentials_and_requires_fresh_preview(self):
        def restore(command, args):
            self.assertEqual((command, args), ("restore_batch", {"batchId": self.batch_id}))
            self.assertTrue(all(item["status"] == "unknown" for item in self.read_saved()["messages"][-1]["actions"]))
            return {"verified": True, "batchId": self.batch_id}

        self.service.edit.side_effect = restore
        result = self.manager.restore_batch(self.batch_id)
        self.assertTrue(result["verified"])
        self.assertFalse(result["rejected"])
        for action in result["actions"]:
            self.assertEqual(action["status"], "proposed")
            for field in ("preview", "previewBatchId", "result"):
                self.assertNotIn(field, action)
        saved = self.read_saved()
        self.assertNotIn("batch", saved["_private"])
        self.assertEqual(saved["_private"]["actions"], self.applied_document["_private"]["actions"])
        with self.assertRaises(ConversationError):
            self.manager.apply_batch(self.batch_id)
        with self.assertRaises(ConversationError):
            self.manager.restore_batch(self.batch_id)
        again = self.manager.preview_batch(self.ids)
        self.assertNotEqual(again["batchId"], self.batch_id)
        self.assertTrue(all(item["status"] == "previewed" for item in again["actions"]))
        self.service.edit.assert_called_once()

    def test_explicit_no_write_rejections_preserve_applied_records(self):
        for reason in ("not_latest_batch", "wrong_project", "wrong_group"):
            with self.subTest(reason=reason):
                self.service.edit.return_value = {"verified": False, "rejected": True, "reason": reason}
                result = self.manager.restore_batch(self.batch_id)
                self.assertFalse(result["verified"])
                self.assertTrue(result["rejected"])
                self.assertIn("未执行撤销", result["message"])
                self.assertEqual(self.read_saved()["messages"][-1]["actions"],
                                 self.applied_document["messages"][-1]["actions"])

    def test_unconfirmed_or_wrong_batch_receipt_remains_unknown_and_cannot_replay(self):
        receipts = [None, {}, {"verified": True}, {"verified": True, "batchId": "b" * 32},
                    {"verified": False, "rejected": True, "reason": "unrecognized-private-error"}]
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                self.manager._save(copy.deepcopy(self.applied_document))
                self.service.edit.reset_mock()
                self.service.edit.return_value = receipt
                result = self.manager.restore_batch(self.batch_id)
                self.assertFalse(result["verified"])
                self.assertFalse(result["rejected"])
                self.assertTrue(all(item["status"] == "unknown" for item in result["actions"]))
                self.assertNotIn("unrecognized-private-error", json.dumps(result))
                with self.assertRaises(ConversationError):
                    self.manager.restore_batch(self.batch_id)
                with self.assertRaises(ConversationError):
                    self.manager.preview_batch(self.ids)
                self.service.edit.assert_called_once()

    def test_host_exception_is_private_and_unknown_state_is_durable(self):
        self.service.edit.side_effect = TimeoutError("C:/private/project-and-token")
        result = self.manager.restore_batch(self.batch_id)
        self.assertTrue(all(item["status"] == "unknown" for item in result["actions"]))
        self.assertTrue(all(item["status"] == "unknown" for item in self.read_saved()["messages"][-1]["actions"]))
        self.assertNotIn("project-and-token", json.dumps(result))
        with self.assertRaises(ConversationError):
            self.manager.restore_batch(self.batch_id)
        self.service.edit.assert_called_once()

    def test_missing_capability_permission_connection_or_record_cannot_send_restore(self):
        states = [{"connected": False, "writeEnabled": True, "capabilities": {"batchRestore": True}},
                  {"connected": True, "writeEnabled": True},
                  {"connected": True, "writeEnabled": True, "capabilities": {"batchRestore": False}},
                  {"connected": True, "writeEnabled": False, "capabilities": {"batchRestore": True}}]
        for state in states:
            with self.subTest(state=state):
                self.service.bridge.status.return_value = state
                with self.assertRaises(ConversationError):
                    self.manager.restore_batch(self.batch_id)
                self.assertEqual(self.read_saved(), self.applied_document)
        for invalid in ("b" * 32, "invalid", None):
            with self.subTest(batch_id=invalid), self.assertRaises(ConversationError):
                self.manager.restore_batch(invalid)
        self.service.edit.assert_not_called()

    def test_unverified_applied_record_and_failed_initial_save_never_touch_host(self):
        broken = copy.deepcopy(self.applied_document)
        broken["messages"][-1]["actions"][0]["result"]["verified"] = False
        self.manager._save(broken)
        with self.assertRaises(ConversationError):
            self.manager.restore_batch(self.batch_id)
        self.manager._save(copy.deepcopy(self.applied_document))
        with patch.object(self.manager, "_save", side_effect=ConversationError("测试保存失败")):
            with self.assertRaisesRegex(ConversationError, "保存失败"):
                self.manager.restore_batch(self.batch_id)
        self.assertTrue(all(item["status"] == "applied" for item in self.read_saved()["messages"][-1]["actions"]))
        self.service.edit.assert_not_called()


@unittest.skipIf(lua_fixture.LuaRuntime is None, "未安装可选 Lua 测试依赖 lupa")
class LuaBoundBatchRestoreTests(unittest.TestCase):
    """桥接自身必须证明批次是最近修改，不能只信任页面上的历史卡片。"""

    setUp = batch_fixture.LuaBatchTests.setUp
    call = lua_fixture.LuaBridgeTests.call
    enable = lua_fixture.LuaBridgeTests.enable
    preview_id = lua_fixture.LuaBridgeTests.preview_id
    preview = batch_fixture.LuaBatchTests.preview
    apply = batch_fixture.LuaBatchTests.apply

    def test_current_batch_restores_once_and_advertises_identity_check_capability(self):
        self.assertTrue(self.call("get_selection")["result"]["capabilities"]["batchRestore"])
        self.assertTrue(self.apply(self.preview())["ok"])
        response = self.call("restore_batch", batchId=self.batch)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["result"]["batchId"], self.batch)
        self.assertTrue(response["result"]["verified"])
        self.assertEqual(self.host.undos, 2)
        writes = self.host.mutations
        again = self.call("restore_batch", batchId=self.batch)
        self.assertEqual(again["result"], {"verified": False, "rejected": True, "reason": "not_latest_batch"})
        self.assertEqual(self.host.mutations, writes)
        self.assertEqual(self.host.undos, 2)

    def test_old_batch_cannot_restore_a_later_batch_or_single_action(self):
        self.assertTrue(self.apply(self.preview())["ok"])
        original = self.batch
        self.batch = "b" * 32
        self.assertTrue(self.apply(self.preview())["ok"])
        writes = self.host.mutations
        undos = self.host.undos
        rejected = self.call("restore_batch", batchId=original)
        self.assertEqual(rejected["result"]["reason"], "not_latest_batch")
        self.assertEqual(self.host.mutations, writes)
        self.assertEqual(self.host.undos, undos)
        restored = self.call("restore_batch", batchId=self.batch)
        self.assertTrue(restored["result"]["verified"])
        self.assertEqual(restored["result"]["batchId"], self.batch)
        self.assertEqual(self.host.undos, undos + 1)
        # 恢复后旧票据已失效，但同一组参数仍可重新生成只读候选。
        fresh = self.preview()
        self.assertTrue(all(item["ok"] for item in fresh["items"]))
        single = self.preview_id()
        self.assertTrue(self.call("apply", previewId=single)["ok"])
        writes = self.host.mutations
        undos = self.host.undos
        rejected = self.call("restore_batch", batchId=original)
        self.assertEqual(rejected["result"]["reason"], "not_latest_batch")
        self.assertEqual(self.host.mutations, writes)
        self.assertEqual(self.host.undos, undos)

    def test_wrong_group_and_invalid_identifier_are_rejected_before_writes(self):
        self.assertTrue(self.apply(self.preview())["ok"])
        writes = self.host.mutations
        self.lua.execute('local group=SV:getMainEditor():getCurrentGroup():getTarget(); function group:getUUID() return "other-group" end')
        rejected = self.call("restore_batch", batchId=self.batch)
        self.assertTrue(rejected["ok"])
        self.assertEqual(rejected["result"]["reason"], "wrong_group")
        for invalid in ("", "b" * 31, "g" * 32, True):
            with self.subTest(batch_id=invalid):
                self.assertFalse(self.call("restore_batch", batchId=invalid)["ok"])
        self.assertEqual(self.host.mutations, writes)
        self.assertEqual(self.host.undos, 1)


if __name__ == "__main__":
    unittest.main()
