"""会话持久化和提案状态机测试：全部使用临时目录、模型替身及宿主替身。

这些测试不会读取真实工程、配置或录音，也不会发起云请求。重点验证人工确认
之前无写入、执行结果未知时不可重放、跨进程文件锁和公开上下文的隐私边界。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from synthv_assistant.conversations import ConversationError, ConversationManager, MAX_FILE_BYTES
from synthv_assistant.operations import OperationLock
from synthv_assistant.planner import PlannerError
from tests.test_parameters import modern_selection


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        data_patch = patch("synthv_assistant.conversations.DATA", self.directory)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        # 新会话默认平台也必须隔离；测试不能读取工作区真实的加密配置。
        platform_patch = patch("synthv_assistant.conversations._default_platform_id", return_value="default")
        self.default_platform = platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.service = MagicMock()
        self.service.operation_lock = threading.RLock()
        self.service.bridge.status.return_value = {"connected": True, "session": "session-1"}
        self.selection = {
            "projectFile": "C:/private-do-not-disclose/song.svp", "groupUUID": "private-group", "groupOffset": 0,
            "startSeconds": 1, "endSeconds": 2, "noteCount": 1,
            "notes": [{"index": 1, "pitch": 60, "lyrics": "啦", "onset": 1000, "duration": 1000,
                       "onsetSeconds": 1, "durationSeconds": 1}],
            "parameters": {name: {"range": [-1, 1], "defaultValue": 0, "pointCount": 0}
                           for name in ("breathiness", "tension", "loudness", "gender", "pitchDelta")},
        }
        self.service.get_selection.side_effect = lambda: copy.deepcopy(self.selection)
        self.preview_serial = 0

        def host_preview(parameter, delta):
            self.preview_serial += 1
            return {"previewId": "host-preview-" + str(self.preview_serial), "parameter": parameter,
                    "delta": delta, "noteCount": 1, "startSeconds": 1, "endSeconds": 2,
                    "pointCount": 32, "summary": "真实宿主替身预览", "projectFile": "must-not-return"}

        self.service.preview.side_effect = host_preview
        self.service.edit.return_value = {"verified": True, "undoRecords": 1, "pointCount": 32,
                                          "message": "完成", "projectFile": "must-not-return"}
        self.plan = {"text": "建议先少量增加气声，再试听。", "provider": "test-provider", "model": "test-model",
                     "inputMode": "text", "actions": [{"parameter": "breathiness", "delta": 0.1, "reason": "减轻起音压力"}]}
        planner_patch = patch("synthv_assistant.conversations._plan_tuning", return_value=self.plan)
        self.planner = planner_patch.start()
        self.addCleanup(planner_patch.stop)
        assets_patch = patch("synthv_assistant.conversations._resolve_attachments", return_value=([], []))
        self.assets = assets_patch.start()
        self.addCleanup(assets_patch.stop)
        self.manager = ConversationManager(self.service)
        self.conversation = self.manager.create_conversation()
        self.identifier = self.conversation["id"]

    def send(self, text="请让这句轻柔一些", include_selection=True, attachments=None):
        return self.manager.send_message(self.identifier, text, include_selection, attachments or [])

    def proposed(self):
        return self.send()["messages"][-1]["actions"][0]

    def read_saved(self):
        return json.loads((self.directory / "conversations" / (self.identifier + ".json")).read_text(encoding="utf-8"))

    def test_curve_plan_preview_and_confirm_preserve_shape_and_private_guards(self):
        """曲线仅在预览后获得确认入口；声库、指纹不进入模型和公开消息。"""
        self.selection.update(modern_selection())
        self.selection["voiceFingerprint"] = "private-voice-fingerprint"
        self.selection["parameters"]["vocalMode_Soft"]["fingerprint"] = "private-parameter-fingerprint"
        curve = [[0, 0], [0.5, 20], [1, 0]]
        self.plan["actions"] = [{"parameter": "vocalMode_Soft", "curve": curve, "reason": "逐渐增加柔和模式。"}]
        self.service.preview.side_effect = None
        self.service.preview.return_value = {"previewId": "curve-preview", "parameter": "vocalMode_Soft",
            "curve": curve, "renderMode": "smooth", "representation": "automation-simplified",
            "label": "柔和", "unit": "%", "beforePointCount": 20, "pointCount": 8, "pointReduction": 12,
            "curvePreview": [{"position": 0, "before": 0, "after": 0}, {"position": 1, "before": 0, "after": 0}],
            "capabilityWarnings": ["目录仅包含当前已返回的模式"], "voiceFile": "private-path"}
        action = self.proposed()
        self.assertEqual(action["curve"], curve)
        self.assertEqual(action["label"], "柔和")
        self.assertEqual(action["modeName"], "Soft")
        model_selection = self.planner.call_args.args[1]
        self.assertTrue(model_selection["capabilities"]["curves"])
        self.assertEqual(model_selection["groupPitchOffset"], 12)
        self.assertNotIn("fingerprint", model_selection["parameters"]["vocalMode_Soft"])
        self.assertNotIn("voiceFingerprint", model_selection)
        preview = self.manager.preview_action(action["id"])
        self.service.preview.assert_called_once_with("vocalMode_Soft", curve=curve, render_mode="smooth")
        self.assertEqual(preview["preview"]["pointReduction"], 12)
        self.assertNotIn("voiceFile", preview["preview"])
        self.service.edit.assert_not_called()
        applied = self.manager.apply_action(action["id"])
        self.assertEqual(applied["status"], "applied")
        self.service.edit.assert_called_once_with("apply", {"previewId": "curve-preview"})

    def test_voice_or_same_point_count_fingerprint_change_blocks_new_preview(self):
        self.selection.update(modern_selection())
        self.selection["voiceFingerprint"] = "voice-original"
        self.selection["parameters"]["breathiness"]["fingerprint"] = "curve-original"
        action = self.proposed()
        self.selection["voiceFingerprint"] = "voice-other"
        with self.assertRaises(ConversationError):
            self.manager.preview_action(action["id"])
        self.selection["voiceFingerprint"] = "voice-original"
        self.selection["parameters"]["breathiness"]["fingerprint"] = "same-point-count-different-values"
        with self.assertRaises(ConversationError):
            self.manager.preview_action(action["id"])
        self.service.preview.assert_not_called()

    def test_curve_capability_loss_and_invalid_preview_cannot_expose_confirmation(self):
        self.selection.update(modern_selection())
        self.plan["actions"] = [{"parameter": "tension", "curve": [[0, 0], [1, 0.1]], "reason": "逐渐变化。"}]
        action = self.proposed()
        self.selection["capabilities"]["curves"] = False
        with self.assertRaises(ConversationError):
            self.manager.preview_action(action["id"])
        self.service.preview.assert_not_called()
        self.selection["capabilities"]["curves"] = True
        self.service.preview.side_effect = None
        self.service.preview.return_value = {"previewId": "bad", "curvePreview": [{"position": 0, "after": True}]}
        with self.assertRaises(ConversationError):
            self.manager.preview_action(action["id"])
        saved = self.read_saved()["messages"][-1]["actions"][0]
        self.assertEqual(saved["status"], "proposed")
        self.assertNotIn("preview", saved)

    def test_second_validation_rejects_mocked_duplicate_or_unknown_curve_fields(self):
        self.selection.update(modern_selection())
        action = {"parameter": "tension", "curve": [[0, 0], [1, 0.1]], "reason": "变化。"}
        for actions in ([action, action], [{**action, "command": "unsafe"}]):
            self.plan["actions"] = actions
            result = self.send()
            self.assertEqual(result["messages"][-1]["role"], "error")
            self.assertEqual(result["messages"][-1]["actions"], [])
        self.service.preview.assert_not_called()

    def test_model_choice_persists_without_entering_prompt_history(self):
        """会话记住选择，旧会话缺省兼容，摘要不被当作下一轮模型指令。"""
        options = {"platformId": "default", "model": "gpt-6-astra", "reasoningEffort": "high"}
        with patch("synthv_assistant.platforms.get_model_platform", return_value={"provider": "openai", "model": "gpt-6-astra"}):
            changed = self.manager.update_model_options(self.identifier, options)
        self.assertEqual(changed["modelOptions"], options)
        self.plan["reasoningSummary"] = "供应商实际返回的摘要。"
        result = self.send(include_selection=False)
        self.assertEqual(self.planner.call_args.kwargs["model_options"], options)
        self.assertEqual(result["messages"][-1]["reasoningSummary"], self.plan["reasoningSummary"])
        self.assertNotIn("reasoningSummary", json.dumps(self.manager._history(self.read_saved())))

    def test_model_option_changes_are_rejected_during_send_lock(self):
        with patch("synthv_assistant.platforms.get_model_platform", return_value={"provider": "openai", "model": "gpt-6-astra"}), \
             OperationLock(self.directory / "conversation-locks" / (self.identifier + ".lock")):
            with self.assertRaises(ConversationError):
                self.manager.update_model_options(self.identifier, {"reasoningEffort": "high"})

    def test_failed_generation_keeps_already_published_summary(self):
        def fail(*_args, **kwargs):
            kwargs["on_progress"]({"stage": "正在接收思考摘要", "reasoning": "已返回的公开摘要"})
            raise PlannerError("测试供应商中断")
        self.planner.side_effect = fail
        seen = []
        result = self.manager.send_message(self.identifier, "讨论", False, [], on_progress=seen.append)
        self.assertEqual(result["messages"][-1]["reasoningSummary"], "已返回的公开摘要")
        self.assertEqual(result["messages"][-1]["role"], "error")

    def test_conversations_persist_and_are_sorted_by_latest_update(self):
        second = self.manager.create_conversation("第二个会话")
        result = self.send("这是自动标题的第一条请求", include_selection=False)
        reloaded = ConversationManager(self.service).get_conversation(self.identifier)
        self.assertEqual(reloaded, result)
        self.assertEqual(result["title"], "这是自动标题的第一条请求")
        self.assertEqual(len(result["messages"]), 2)
        items = self.manager.list_conversations()["items"]
        self.assertEqual([item["id"] for item in items], [self.identifier, second["id"]])
        self.assertEqual(items[0]["messageCount"], 2)

    def test_model_receives_whitelisted_selection_without_internal_identity(self):
        result = self.send()
        model_selection = self.planner.call_args.args[1]
        self.assertEqual(model_selection["notes"][0]["lyrics"], "啦")
        encoded = json.dumps({"public": result, "selection": model_selection}, ensure_ascii=False)
        for private in ("private-do-not-disclose", "private-group", "session-1", "_private", "projectFile"):
            self.assertNotIn(private, encoded)
        self.assertIn("selection", self.read_saved()["_private"]["actions"][result["messages"][-1]["actions"][0]["id"]])

    def test_send_and_preview_never_apply_or_enable_writes(self):
        action = self.proposed()
        self.assertEqual(action["status"], "proposed")
        previewed = self.manager.preview_action(action["id"])
        self.assertEqual(previewed["status"], "previewed")
        self.assertEqual(previewed["preview"]["previewId"], "host-preview-1")
        self.assertNotIn("projectFile", previewed["preview"])
        self.service.edit.assert_not_called()
        self.service.write_mode.assert_not_called()

    def test_apply_requires_preview_and_persists_unknown_before_host_call(self):
        action = self.proposed()
        with self.assertRaisesRegex(ConversationError, "预览"):
            self.manager.apply_action(action["id"])
        self.service.edit.assert_not_called()
        self.manager.preview_action(action["id"])

        def verified_write(command, args):
            saved_action = self.read_saved()["messages"][-1]["actions"][0]
            self.assertEqual(saved_action["status"], "unknown")
            self.assertEqual(command, "apply")
            self.assertEqual(args, {"previewId": "host-preview-1"})
            return {"verified": True, "undoRecords": 1}

        self.service.edit.side_effect = verified_write
        applied = self.manager.apply_action(action["id"])
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(applied["result"]["verified"])
        with self.assertRaises(ConversationError):
            self.manager.apply_action(action["id"])
        self.service.edit.assert_called_once()

    def test_timeout_stays_unknown_after_restart_and_cannot_be_replayed(self):
        action = self.proposed()
        self.manager.preview_action(action["id"])
        self.service.edit.side_effect = TimeoutError("private key must never be visible")
        result = self.manager.apply_action(action["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("private key", json.dumps(result))
        restarted = ConversationManager(self.service)
        with self.assertRaises(ConversationError):
            restarted.apply_action(action["id"])
        with self.assertRaises(ConversationError):
            restarted.preview_action(action["id"])
        self.service.edit.assert_called_once()

    def test_process_interruption_leaves_durable_unknown(self):
        action = self.proposed()
        self.manager.preview_action(action["id"])
        self.service.edit.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.manager.apply_action(action["id"])
        self.assertEqual(self.read_saved()["messages"][-1]["actions"][0]["status"], "unknown")

    def test_failed_unknown_save_prevents_host_write(self):
        action = self.proposed()
        self.manager.preview_action(action["id"])
        with patch.object(self.manager, "_save", side_effect=ConversationError("磁盘不可写")):
            with self.assertRaises(ConversationError):
                self.manager.apply_action(action["id"])
        self.service.edit.assert_not_called()

    def test_explicit_read_only_preserves_preview_until_user_enables_write(self):
        self.service.bridge.status.return_value = {"connected": True, "session": "session-1", "writeEnabled": False}
        action = self.proposed()
        # 只读模式允许生成预览，但在明确得知未开启写入时，不能把提案标为 unknown。
        self.manager.preview_action(action["id"])
        with self.assertRaisesRegex(ConversationError, "尚未开启写入"):
            self.manager.apply_action(action["id"])
        self.assertEqual(self.read_saved()["messages"][-1]["actions"][0]["status"], "previewed")
        self.service.edit.assert_not_called()
        self.service.write_mode.assert_not_called()
        self.service.bridge.status.return_value["writeEnabled"] = True
        self.assertEqual(self.manager.apply_action(action["id"])["status"], "applied")

    def test_write_success_followed_by_save_failure_remains_non_replayable(self):
        action = self.proposed()
        self.manager.preview_action(action["id"])
        save = self.manager._save
        calls = []

        def interrupted_save(document):
            calls.append(None)
            if len(calls) == 2:
                raise ConversationError("写后保存失败")
            return save(document)

        with patch.object(self.manager, "_save", side_effect=interrupted_save):
            with self.assertRaisesRegex(ConversationError, "写后"):
                self.manager.apply_action(action["id"])
        # 宿主已经成功，但磁盘上的 unknown 仍是恢复后的权威状态，绝不能再次写入。
        self.assertEqual(self.read_saved()["messages"][-1]["actions"][0]["status"], "unknown")
        with self.assertRaises(ConversationError):
            ConversationManager(self.service).apply_action(action["id"])
        self.service.edit.assert_called_once()

    def test_changed_selection_or_session_blocks_preview_before_host(self):
        action = self.proposed()
        self.selection["notes"][0]["pitch"] = 61
        with self.assertRaisesRegex(ConversationError, "选区"):
            self.manager.preview_action(action["id"])
        self.selection["notes"][0]["pitch"] = 60
        self.service.bridge.status.return_value = {"connected": True, "session": "replacement-session"}
        with self.assertRaisesRegex(ConversationError, "会话"):
            self.manager.preview_action(action["id"])
        self.service.preview.assert_not_called()

    def test_changed_parameter_summary_blocks_preview(self):
        action = self.proposed()
        self.selection["parameters"]["breathiness"]["pointCount"] = 2
        with self.assertRaisesRegex(ConversationError, "参数摘要"):
            self.manager.preview_action(action["id"])
        self.service.preview.assert_not_called()

    def test_selection_changed_during_host_preview_never_exposes_apply_button(self):
        action = self.proposed()

        def switching_preview(parameter, delta):
            self.selection["notes"][0]["duration"] += 1
            return {"previewId": "unexpected-target-preview"}

        self.service.preview.side_effect = switching_preview
        with self.assertRaisesRegex(ConversationError, "选区"):
            self.manager.preview_action(action["id"])
        saved_action = self.manager.get_conversation(self.identifier)["messages"][-1]["actions"][0]
        self.assertEqual(saved_action["status"], "proposed")
        self.assertNotIn("preview", saved_action)
        self.service.edit.assert_not_called()

    def test_selection_changed_after_preview_blocks_apply_before_unknown(self):
        action = self.proposed()
        self.manager.preview_action(action["id"])
        self.selection["notes"][0]["lyrics"] = "另一段"
        with self.assertRaisesRegex(ConversationError, "选区"):
            self.manager.apply_action(action["id"])
        self.service.edit.assert_not_called()
        self.assertEqual(self.read_saved()["messages"][-1]["actions"][0]["status"], "previewed")

    def test_different_parameter_remains_actionable_after_first_apply(self):
        self.plan["actions"].append({"parameter": "tension", "delta": -0.05, "reason": "减少紧张感"})
        actions = self.send()["messages"][-1]["actions"]
        self.manager.preview_action(actions[0]["id"])
        self.manager.apply_action(actions[0]["id"])
        self.selection["parameters"]["breathiness"]["pointCount"] = 32
        second = self.manager.preview_action(actions[1]["id"])
        self.assertEqual(second["status"], "previewed")

    def test_new_preview_invalidates_previous_confirmation_across_conversations(self):
        first = self.proposed()
        self.manager.preview_action(first["id"])
        second_conversation = self.manager.create_conversation()
        second_result = self.manager.send_message(second_conversation["id"], "另一个方案", True, [])
        second = second_result["messages"][-1]["actions"][0]
        self.manager.preview_action(second["id"])
        original = self.manager.get_conversation(self.identifier)["messages"][-1]["actions"][0]
        self.assertEqual(original["status"], "proposed")
        self.assertNotIn("preview", original)
        with self.assertRaises(ConversationError):
            self.manager.apply_action(first["id"])
        self.service.edit.assert_not_called()

    def test_consultation_cannot_return_executable_actions_or_read_selection(self):
        result = self.send(include_selection=False)
        self.assertEqual(result["messages"][-1]["actions"], [])
        self.assertIsNone(self.planner.call_args.args[1])
        self.service.get_selection.assert_not_called()

    def test_missing_selection_explicitly_rejects_without_cloud_fallback(self):
        self.selection["notes"] = []
        with self.assertRaisesRegex(ConversationError, "选中"):
            self.send()
        self.planner.assert_not_called()
        self.assertEqual(self.manager.get_conversation(self.identifier)["messages"], [])

    def test_disconnected_bridge_rejects_selected_request(self):
        self.service.bridge.status.return_value = {"connected": False}
        with self.assertRaisesRegex(ConversationError, "连接"):
            self.send()
        self.planner.assert_not_called()

    def test_attachments_use_resolved_paths_but_persist_only_public_metadata(self):
        identifier = "a" * 32
        path = self.directory / "not-read.wav"
        self.assets.return_value = ([path], [{"kind": "upload", "id": identifier, "name": "示例.wav",
                                            "url": "/uploads/" + identifier + ".wav", "projectFile": "do-not-leak"}])
        result = self.send(include_selection=False, attachments=[{"kind": "upload", "id": identifier}])
        self.assertEqual(self.planner.call_args.args[3], [path])
        self.assertNotIn("projectFile", self.planner.call_args.args[4][0])
        self.assertEqual(result["messages"][0]["attachments"][0]["name"], "示例.wav")
        self.assertNotIn("do-not-leak", json.dumps(result))

    def test_model_failure_is_persisted_as_redacted_error_message(self):
        self.planner.side_effect = RuntimeError("apiKey=sk-fictional-secret https://private.example")
        result = self.send(include_selection=False)
        self.assertEqual([message["role"] for message in result["messages"]], ["user", "error"])
        self.assertNotIn("sk-fictional", json.dumps(result))
        self.assertNotIn("private.example", json.dumps(result))
        self.assertEqual(result, self.manager.get_conversation(self.identifier))

    def test_safe_planner_error_preserves_actionable_fixed_message(self):
        message = "听评服务认证失败，请检查 API key。"
        self.planner.side_effect = PlannerError(message)
        result = self.send(include_selection=False)
        self.assertEqual(result["messages"][-1]["role"], "error")
        self.assertEqual(result["messages"][-1]["text"], message)
        self.assertEqual(result["messages"][-1]["actions"], [])

    def test_message_over_planner_limit_is_rejected_before_persisting_or_sending(self):
        with self.assertRaisesRegex(ConversationError, "4000"):
            self.send("字" * 4001, include_selection=False)
        self.planner.assert_not_called()
        self.assertEqual(self.manager.get_conversation(self.identifier)["messages"], [])

    def test_unhashable_attachment_kind_has_fixed_validation_error(self):
        for kind in (["upload"], {"kind": "upload"}):
            with self.subTest(kind=kind), self.assertRaisesRegex(ConversationError, "附件"):
                self.send(include_selection=False, attachments=[{"kind": kind, "id": "a" * 32}])
        self.assets.assert_not_called()
        self.planner.assert_not_called()

    def test_invalid_model_action_is_not_silently_executed(self):
        self.plan["actions"] = [{"parameter": "breathiness", "delta": 50, "reason": "越界"}]
        result = self.send()
        self.assertEqual(result["messages"][-1]["role"], "error")
        self.assertEqual(result["messages"][-1]["actions"], [])
        self.service.preview.assert_not_called()
        self.service.edit.assert_not_called()

    def test_history_has_eight_text_only_messages_and_bounded_context(self):
        for index in range(6):
            self.send("第" + str(index) + "次咨询" + "字" * 2500, include_selection=False)
        history = self.planner.call_args.args[2]
        self.assertEqual(len(history), 8)
        self.assertTrue(all(set(message) == {"role", "text"} for message in history))
        self.assertLessEqual(sum(len(message["text"]) for message in history), 16000)

    def test_capacity_limit_rejects_before_model_or_user_message_append(self):
        document = self.read_saved()
        document["messages"] = [self.manager._message("user", "历史") for _ in range(99)]
        self.manager._save(document)
        with self.assertRaisesRegex(ConversationError, "消息上限"):
            self.send(include_selection=False)
        self.assertEqual(len(self.read_saved()["messages"]), 99)
        self.planner.assert_not_called()

    def test_oversized_document_and_path_traversal_are_rejected(self):
        path = self.directory / "conversations" / (self.identifier + ".json")
        path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ConversationError, "2 MB"):
            self.manager.get_conversation(self.identifier)
        for identifier in ("../secret", "A" * 32, None):
            with self.subTest(identifier=identifier), self.assertRaises(ConversationError):
                self.manager.get_conversation(identifier)

    def test_same_conversation_lock_rejects_concurrent_send(self):
        # 使用真实文件锁模拟另一个 HTTP/MCP 实例，不能依赖单进程 threading.Lock。
        with OperationLock(self.directory / "conversation-locks" / (self.identifier + ".lock")):
            with self.assertRaisesRegex(ConversationError, "正在处理"):
                self.send(include_selection=False)
        self.planner.assert_not_called()

    def test_assistant_lock_blocks_parallel_action_state_transitions(self):
        action = self.proposed()
        with OperationLock(self.directory / "assistant.lock"):
            with self.assertRaisesRegex(ConversationError, "正在处理"):
                self.manager.preview_action(action["id"])
        self.service.preview.assert_not_called()

    def test_atomic_replace_failure_keeps_original_file_and_removes_temporary(self):
        path = self.directory / "conversations" / (self.identifier + ".json")
        original = path.read_bytes()
        document = self.read_saved()
        document["title"] = "不能写入的新版"
        with patch("synthv_assistant.conversations.os.replace", side_effect=OSError("private filesystem error")):
            with self.assertRaises(ConversationError) as caught:
                self.manager._save(document)
        self.assertNotIn("private filesystem", str(caught.exception))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.manager.directory.glob("*.tmp")), [])

    def test_local_conversation_metadata_never_enters_model_context(self):
        updated = self.manager.update_metadata(self.identifier, {"title": "手工命名", "note": "private-note-never-send", "starred": True})
        self.assertEqual((updated["title"], updated["note"], updated["starred"]), ("手工命名", "private-note-never-send", True))
        result = self.send(include_selection=False)
        self.assertEqual(result["title"], "手工命名")
        self.assertNotIn("private-note-never-send", str(self.planner.call_args))
        self.assertEqual(self.manager.list_conversations()["items"][0]["note"], "private-note-never-send")

    def test_deleted_conversation_blocks_read_send_preview_apply_and_preserves_original(self):
        action = self.proposed()
        original = (self.manager.directory / (self.identifier + ".json")).read_bytes()
        self.assertTrue(self.manager.delete_conversation(self.identifier)["deleted"])
        self.assertEqual(self.manager.list_conversations()["items"], [])
        operations = (lambda: self.manager.get_conversation(self.identifier), lambda: self.send(include_selection=False),
                      lambda: self.manager.preview_action(action["id"]), lambda: self.manager.apply_action(action["id"]))
        for operation in operations:
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual((self.manager.directory / (self.identifier + ".json")).read_bytes(), original)
        self.assertTrue(self.manager.restore_conversation(self.identifier)["restored"])
        restored = self.manager.get_conversation(self.identifier)
        self.assertEqual(restored["messages"][-1]["actions"][0]["id"], action["id"])
        self.assertEqual((self.manager.directory / (self.identifier + ".json")).read_bytes(), original)

    def test_delete_releases_active_conversation_capacity_restore_checks_limit(self):
        with patch("synthv_assistant.conversations.MAX_CONVERSATIONS", 1):
            self.manager.delete_conversation(self.identifier)
            replacement = self.manager.create_conversation("替代会话")
            with self.assertRaises(ConversationError):
                self.manager.restore_conversation(self.identifier)
            self.manager.delete_conversation(replacement["id"])
            self.manager.restore_conversation(self.identifier)
            self.assertEqual(self.manager.list_conversations()["items"][0]["id"], self.identifier)

    def test_model_request_holds_conversation_and_audio_delete_locks(self):
        audio_id = "e" * 32
        attachment = {"kind": "upload", "id": audio_id}
        self.assets.return_value = ([self.directory / "fake.wav"], [{**attachment, "label": "音频", "note": "private-audio-note"}])

        def planning(*args, **_options):
            # 同步调用替身代表模型正在运行；删除与备注修改必须非阻塞拒绝。
            with self.assertRaises(ValueError):
                self.manager.delete_conversation(self.identifier)
            with self.assertRaises(ValueError):
                self.manager.library.delete("upload", audio_id, "使用中")
            with self.assertRaises(ValueError):
                self.manager.purge_conversation(self.identifier, {"confirm": True})
            with self.assertRaises(ValueError):
                self.manager.library.purge("upload", audio_id, {"confirm": True})
            self.assertNotIn("private-audio-note", str(args))
            return self.plan

        self.planner.side_effect = planning
        self.send(include_selection=False, attachments=[attachment])
        self.assertIsNone(self.manager.library.read("upload", audio_id)["deletedAt"])

    def test_history_attachment_is_unavailable_after_delete_and_available_after_restore(self):
        identifier = "f" * 32
        directory = self.directory / "uploads"
        directory.mkdir()
        (directory / (identifier + ".wav")).write_bytes(b"fake-audio-only-used-as-file-marker")
        (directory / (identifier + ".json")).write_text("{}", encoding="utf-8")
        attachment = {"kind": "upload", "id": identifier}
        self.assets.return_value = ([directory / (identifier + ".wav")], [{**attachment, "name": "历史片段"}])
        first = self.send(include_selection=False, attachments=[attachment])
        self.assertTrue(first["messages"][0]["attachments"][0]["available"])
        self.manager.library.delete("upload", identifier, "历史片段")
        history = self.manager.get_conversation(self.identifier)["messages"][0]["attachments"][0]
        self.assertFalse(history["available"])
        self.assertTrue(history["deleted"])
        self.assertEqual(history["name"], "历史片段")
        self.manager.library.restore("upload", identifier)
        self.assertTrue(self.manager.get_conversation(self.identifier)["messages"][0]["attachments"][0]["available"])
        self.manager.library.purge("upload", identifier, {"confirm": True})
        history = self.manager.get_conversation(self.identifier)["messages"][0]["attachments"][0]
        self.assertEqual((history["available"], history["deleted"], history["permanent"]), (False, True, True))
        self.assertEqual(history["name"], "历史片段")

    def test_new_conversation_stores_default_platform_without_rewriting_existing(self):
        """修改全局偏好仅影响新建会话；旧缺省数据仍保持 default 兼容行为。"""
        self.default_platform.return_value = "d" * 32
        created = self.manager.create_conversation()
        self.assertEqual(created["modelOptions"], {"platformId": "d" * 32, "model": "", "reasoningEffort": "default"})
        self.assertEqual(self.manager.get_conversation(self.identifier)["modelOptions"]["platformId"], "default")
        legacy = self.read_saved()
        legacy.pop("modelOptions")
        self.manager._save(legacy)
        self.assertEqual(self.manager.get_conversation(self.identifier)["modelOptions"]["platformId"], "default")

    def test_purge_conversation_does_not_delete_other_conversations_or_audio(self):
        second = self.manager.create_conversation("保留会话")
        audio = self.directory / "uploads" / ("b" * 32 + ".wav")
        audio.parent.mkdir()
        audio.write_bytes(b"independent-user-audio")
        self.manager.update_metadata(self.identifier, {"note": "会话备注"})
        self.manager.purge_conversation(self.identifier, {"confirm": True})
        self.assertFalse((self.manager.directory / (self.identifier + ".json")).exists())
        self.assertEqual(self.manager.list_conversations()["items"][0]["id"], second["id"])
        self.assertEqual(audio.read_bytes(), b"independent-user-audio")
        with self.assertRaises(ValueError):
            self.manager.get_conversation(self.identifier)
        self.assertEqual(self.manager.library.trash()["items"], [])

    def test_purge_shares_both_conversation_and_global_action_locks(self):
        """其他会话预览也可能改本会话，两个锁都必须阻止删除。"""
        for path in (self.directory / "assistant.lock", self.manager.locks / (self.identifier + ".lock")):
            with self.subTest(lock=path.name), OperationLock(path):
                with self.assertRaisesRegex(ConversationError, "正在处理"):
                    self.manager.purge_conversation(self.identifier, {"confirm": True})
            self.assertTrue((self.manager.directory / (self.identifier + ".json")).exists())

    def test_purged_conversation_cannot_replay_preview_or_apply(self):
        action = self.proposed()
        self.manager.preview_action(action["id"])
        self.manager.purge_conversation(self.identifier, {"confirm": True})
        for operation in (lambda: self.manager.preview_action(action["id"]),
                          lambda: self.manager.apply_action(action["id"]),
                          lambda: self.send(include_selection=False)):
            with self.assertRaises(ValueError):
                operation()
        self.service.edit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
