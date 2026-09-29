"""Gateway/journal integration with mocked dispatch and real temporary SQLite.

The real adapter, guardian, COM, and Windows UI are never imported or executed.
"""

import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from wxbg.journal import Journal


def load_isolated_gateway(state_directory):
    source = Path(__file__).parents[1] / "src" / "wxbg" / "gateway.py"
    spec = importlib.util.spec_from_file_location("wxbg._gateway_contract_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"WXBG_STATE_DIR": str(state_directory)}):
        spec.loader.exec_module(module)
    return module


def worker_success(summary):
    return {
        "ok": True,
        "worker_started": True,
        "result": deepcopy(summary),
        "cleanup": {"status": "restored", "restored": True, "errors": []},
    }


def worker_failure(code, *, started, unknown=False):
    return {
        "ok": False,
        "worker_started": started,
        "error": {"code": code, "outcome_unknown": unknown},
        "cleanup": {"status": "restored", "restored": True, "errors": []},
    }


class GatewayJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.gateway = load_isolated_gateway(self.directory)
        self.execute_patch = patch.object(self.gateway, "execute")
        self.dispatch = self.execute_patch.start()
        self.addCleanup(self.execute_patch.stop)
        self.dispatch.side_effect = AssertionError("dispatch requires an explicit mock response")
        self.summary = {
            "ok": True,
            "status": "submitted",
            "verification_level": "new_local_bubble_and_empty_draft",
            "counts": {"submitted": 1},
            "refs": {"message": "0123456789abcdef0123456789abcdef"},
            "background_mode": "minimized",
        }

    def response(self, value):
        self.dispatch.side_effect = lambda *args, **kwargs: deepcopy(value)

    def send(self, text="private synthetic chat text", operation_id="send-1"):
        return self.gateway.wechat_send_text("session-opaque-ref", text, operation_id)

    def test_send_at_username_returns_handoff_without_dispatch_or_journal(self):
        first = self.gateway.wechat_send_at_username('寧', operation_id='at-1')
        second = self.gateway.wechat_send_at_username('寧', operation_id='at-1')
        self.dispatch.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(first['result']['status'], 'computer_use_handoff_required')
        self.assertIs(first['sent'], False)
        self.assertIs(first['dispatch_performed'], False)
        self.assertIsNone(first['result']['unverified_target_hints']['session_ref'])
        self.assertFalse((self.directory / 'operations.sqlite3').exists())

    def test_send_at_username_invalid_name_never_reserves_or_dispatches(self):
        with self.assertRaises(ToolError):
            self.gateway.wechat_send_at_username('@寧', operation_id='at-invalid')
        self.assertFalse((self.directory / 'operations.sqlite3').exists())
        self.assertEqual(self.dispatch.call_count, 0)

    def test_send_at_username_operation_id_is_only_a_hint(self):
        first = self.gateway.wechat_send_at_username('寧', operation_id='at-2')
        second = self.gateway.wechat_send_at_username('另一人', operation_id='at-2')
        self.assertNotEqual(first['result']['requested_username'],
                            second['result']['requested_username'])
        self.assertIs(second['result']['operation_id_is_not_a_send_or_idempotency_record'], True)
        self.dispatch.assert_not_called()
        self.assertFalse((self.directory / 'operations.sqlite3').exists())

    def test_send_at_username_schema_has_no_remark_fallback(self):
        tool = self.gateway.SERVER._tool_manager.get_tool('wechat_send_at_username')
        self.assertEqual(tool.parameters.get('additionalProperties'), False)
        self.assertEqual(set(tool.parameters['properties']), {
            'username', 'text', 'session_ref', 'operation_id',
            'conversation_title_hint', 'conversation_key_hint'})
        self.assertEqual(tool.parameters['required'], ['username'])

    def row(self, operation_id="send-1"):
        return Journal(self.directory / "operations.sqlite3").get(operation_id)

    def test_success_replay_does_not_dispatch_again(self):
        self.response(worker_success(self.summary))
        original = self.send()
        replay = self.send()
        self.assertEqual(self.dispatch.call_count, 1)
        self.assertEqual(original["result"], self.summary)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], original["result"])
        for response in (original, replay):
            self.assertEqual(
                {key: response[key] for key in (
                    "sender_role", "sender_role_verified", "sender_role_evidence")},
                {
                    "sender_role": "self",
                    "sender_role_verified": True,
                    "sender_role_evidence": "local_outgoing_operation",
                },
            )
            self.assertNotIn("sender_role", response["result"])
        self.assertEqual(self.row()["state"], "complete")

    def test_conflicting_inner_result_cannot_be_journaled_as_success(self):
        self.response(worker_success({"ok": False, "status": "rejected",
                                      "error_code": "draft_conflict"}))
        with self.assertRaises(ToolError) as caught:
            self.send(operation_id="send-inner-rejected")
        self.assertEqual(json.loads(str(caught.exception))["code"], "outcome_unknown")
        self.assertEqual(self.row("send-inner-rejected")["state"], "outcome_unknown")
        with self.assertRaises(ToolError):
            self.send(operation_id="send-inner-rejected")
        self.assertEqual(self.dispatch.call_count, 1)

    def test_wrong_action_success_summary_cannot_complete_send(self):
        self.response(worker_success({"ok": True, "status": "draft_updated",
                                      "verification_level": "client_readback"}))
        with self.assertRaises(ToolError) as caught:
            self.send(operation_id="send-wrong-summary")
        self.assertEqual(json.loads(str(caught.exception))["code"], "outcome_unknown")
        self.assertEqual(self.row("send-wrong-summary")["state"], "outcome_unknown")

    def test_invalid_text_is_rejected_before_reserving_operation(self):
        for text, operation_id in (("", "send-empty"), ("\x00", "send-nul"),
                                   ("x" * 10001, "send-too-long")):
            with self.subTest(operation_id=operation_id):
                with self.assertRaises(ToolError) as caught:
                    self.send(text=text, operation_id=operation_id)
                self.assertEqual(json.loads(str(caught.exception)), {
                    "code": "invalid_text", "outcome_unknown": False, "retry": False})
                self.assertIsNone(self.row(operation_id))
        self.dispatch.assert_not_called()

    def test_invalid_new_draft_is_rejected_before_reserving_operation(self):
        with self.assertRaises(ToolError) as caught:
            self.gateway.wechat_set_draft("session-ref", "\x00", "", "draft-nul")
        self.assertEqual(json.loads(str(caught.exception)), {
            "code": "invalid_text", "outcome_unknown": False, "retry": False})
        self.assertIsNone(self.row("draft-nul"))
        self.dispatch.assert_not_called()

    def test_same_id_with_changed_text_is_rejected_before_dispatch(self):
        self.response(worker_success(self.summary))
        self.send()
        with self.assertRaises(ToolError) as caught:
            self.send(text="different text")
        self.assertEqual(json.loads(str(caught.exception))["code"], "operation_conflict")
        self.assertEqual(self.dispatch.call_count, 1)

    def test_same_id_with_changed_target_is_rejected_before_dispatch(self):
        self.response(worker_success(self.summary))
        self.send()
        with self.assertRaises(ToolError) as caught:
            self.gateway.wechat_send_text("different-session-ref", "private synthetic chat text", "send-1")
        self.assertEqual(json.loads(str(caught.exception))["code"], "operation_conflict")
        self.assertEqual(self.dispatch.call_count, 1)

    def test_worker_timeout_is_sticky_unknown(self):
        self.response(worker_failure("TIMEOUT", started=True, unknown=True))
        with self.assertRaises(ToolError):
            self.send()
        self.assertEqual(self.row()["state"], "outcome_unknown")
        with self.assertRaises(ToolError) as caught:
            self.send()
        error = json.loads(str(caught.exception))
        self.assertEqual(error["code"], "outcome_unknown")
        self.assertTrue(error["outcome_unknown"])
        self.assertFalse(error["retry"])
        self.assertEqual(self.dispatch.call_count, 1)

    def test_non_object_worker_error_is_sticky_unknown_with_fixed_envelope(self):
        self.response({"ok": False, "worker_started": True,
                       "error": "private unstructured failure"})
        with self.assertRaises(ToolError) as caught:
            self.send(operation_id="malformed-worker-error")
        self.assertEqual(json.loads(str(caught.exception)), {
            "code": "outcome_unknown", "reason_code": "guardian_protocol_error",
            "outcome_unknown": True, "retry": False})
        self.assertEqual(self.row("malformed-worker-error")["state"], "outcome_unknown")
        self.assertEqual(self.dispatch.call_count, 1)

    def test_non_object_body_worker_error_is_sticky_unknown(self):
        self.response({"ok": False, "worker_started": True,
                       "error": ["private unstructured failure"]})
        with self.assertRaises(ToolError) as caught:
            self.gateway.mutate_with_body("synthetic_body", {}, "malformed-body-error",
                                          lambda result: {})
        self.assertEqual(json.loads(str(caught.exception))["code"], "outcome_unknown")
        self.assertEqual(self.row("malformed-body-error")["state"], "outcome_unknown")

    def test_lost_guardian_response_is_sticky_unknown(self):
        self.dispatch.side_effect = ToolError(json.dumps({"code": "guardian_deadline", "outcome_unknown": True, "retry": False}))
        with self.assertRaises(ToolError):
            self.send()
        self.assertEqual(self.row()["state"], "outcome_unknown")
        with self.assertRaises(ToolError) as caught:
            self.send()
        self.assertEqual(json.loads(str(caught.exception))["code"], "outcome_unknown")
        self.assertEqual(self.dispatch.call_count, 1)

    def test_started_worker_failure_cannot_be_downgraded_to_safe_rejection(self):
        self.response(worker_failure("WORKER_ERROR", started=True, unknown=False))
        with self.assertRaises(ToolError):
            self.send()
        self.assertEqual(self.row()["state"], "outcome_unknown")

    def test_guardian_pre_worker_rejection_is_persisted(self):
        self.response(worker_failure("UNSUPPORTED_BUILD", started=False))
        with self.assertRaises(ToolError):
            self.send()
        row = self.row()
        self.assertEqual(row["state"], "rejected")
        self.assertEqual(row["reason_code"], "UNSUPPORTED_BUILD")
        self.assertEqual(row["result"], {"ok": False, "status": "rejected", "error_code": "UNSUPPORTED_BUILD"})

    def test_busy_draft_guardian_rejection_is_not_reported_as_complete(self):
        self.response(worker_failure("BUSY", started=False))
        with self.assertRaises(ToolError) as caught:
            self.gateway.wechat_set_draft("session-ref", "n", "", "draft-busy")
        self.assertEqual(json.loads(str(caught.exception))["error"]["code"], "BUSY")
        status = self.gateway.wechat_operation_status("draft-busy")
        self.assertTrue(status["found"])
        self.assertEqual(status["operation"]["state"], "rejected")
        self.assertEqual(status["operation"]["reason_code"], "BUSY")
        self.assertEqual(status["operation"]["result"],
                         {"ok": False, "status": "rejected", "error_code": "BUSY"})
        with self.assertRaises(ToolError):
            self.gateway.wechat_set_draft("session-ref", "n", "", "draft-busy")
        self.assertEqual(self.dispatch.call_count, 1)

    def test_replayed_rejection_stays_a_tool_error_without_dispatch(self):
        self.response(worker_failure("UNSUPPORTED_BUILD", started=False))
        with self.assertRaises(ToolError):
            self.send()
        with self.assertRaises(ToolError) as caught:
            self.send()
        self.assertIn("UNSUPPORTED_BUILD", str(caught.exception))
        self.assertEqual(self.dispatch.call_count, 1)

    def test_real_worker_send_metadata_is_allowed_and_body_is_not_persisted(self):
        self.response(worker_success(self.summary))
        result = self.send()
        self.assertEqual(result["result"], self.summary)
        self.assertEqual(self.row()["result"], self.summary)
        database = (self.directory / "operations.sqlite3").read_bytes()
        self.assertNotIn(b"private synthetic chat text", database)

    def test_real_worker_draft_metadata_replays_without_dispatch(self):
        summary = {"status": "draft_updated", "verification_level": "client_readback", "counts": {"characters": 5}, "background_mode": "minimized", "ok": True}
        self.response(worker_success(summary))
        original = self.gateway.wechat_set_draft("session-ref", "hello", "", "draft-1")
        replay = self.gateway.wechat_set_draft("session-ref", "hello", "", "draft-1")
        self.assertEqual(original["result"], summary)
        self.assertEqual(replay["result"], summary)
        self.assertEqual(self.dispatch.call_count, 1)

    def test_invalid_post_worker_summary_reports_structured_unknown(self):
        self.response(worker_success({"status": "submitted", "text": "must not persist"}))
        with self.assertRaises(ToolError) as caught:
            self.send()
        error = json.loads(str(caught.exception))
        self.assertTrue(error.get("outcome_unknown"))
        self.assertFalse(error.get("retry", True))
        self.assertIn(self.row()["state"], ("executing", "outcome_unknown"))
        with self.assertRaises(ToolError):
            self.send()
        self.assertEqual(self.dispatch.call_count, 1)

    def test_invalid_operation_id_cannot_dispatch(self):
        with self.assertRaises(ToolError) as caught:
            self.send(operation_id="")
        self.assertEqual(json.loads(str(caught.exception))["code"], "invalid_operation_id")
        self.dispatch.assert_not_called()

    def test_operation_status_is_read_only_and_has_no_payload_or_token(self):
        self.response(worker_success(self.summary))
        self.send()
        status = self.gateway.wechat_operation_status("send-1")
        self.assertTrue(status["found"])
        self.assertNotIn("args", status["operation"])
        self.assertNotIn("token", status["operation"])
        self.assertEqual(self.dispatch.call_count, 1)

    def test_operation_status_invalid_id_uses_fixed_public_error(self):
        with self.assertRaises(ToolError) as caught:
            self.gateway.wechat_operation_status("")
        self.assertEqual(json.loads(str(caught.exception)), {
            "code": "invalid_operation_id", "outcome_unknown": False,
            "retry": False})
        self.dispatch.assert_not_called()

    def test_operation_status_storage_failure_preserves_unknown_state(self):
        from wxbg.journal import StorageError
        with patch.object(self.gateway, "Journal", side_effect=StorageError("private path")):
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_operation_status("synthetic-op")
        self.assertEqual(json.loads(str(caught.exception)), {
            "code": "journal_storage_error", "outcome_unknown": True,
            "retry": False})
        self.assertNotIn("private", str(caught.exception))


class GatewayCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = load_isolated_gateway(Path(self.temp.name))

    def test_financial_features_are_excluded_and_unimplemented_are_separate(self):
        with patch.object(self.gateway, "execute", side_effect=AssertionError("capabilities must not dispatch")):
            capabilities = self.gateway.wechat_capabilities()
        self.assertTrue({"payments", "red_packets", "money_transfer", "collection"} <= set(capabilities["excluded"]))
        self.assertFalse(set(capabilities["implemented"]) & set(capabilities["not_implemented"]))
        self.assertFalse(set(capabilities["implemented"]) & set(capabilities["excluded"]))
        self.assertTrue({"full_contact_directory", "video_voice_send", "general_file_transfer", "group_management", "moments", "favorites", "calls", "account_changes"} <= set(capabilities["not_implemented"]))
        self.assertIn("main window minimized", capabilities["requires"])
        self.assertIn("no remote delivery receipt", capabilities["limits"])

    def test_registered_tools_offer_no_financial_or_arbitrary_execution_escape(self):
        registered = asyncio.run(self.gateway.SERVER.list_tools())
        names = {tool.name for tool in registered}
        approved = {
            "wechat_read_inbox",
            "wechat_list_conversations",
            "wechat_capabilities", "wechat_status", "wechat_list_sessions", "wechat_open_session",
            "wechat_probe_session_container", "wechat_scan_open_session",
            "wechat_scan_session_viewports",
            "wechat_search", "wechat_read_new_messages", "wechat_get_attachment",
            "wechat_read_messages", "wechat_get_draft", "wechat_set_draft", "wechat_send_text",
            "wechat_operation_status", "wechat_send_file", "wechat_list_contacts", "wechat_wait_for_ui_hint", "wechat_scroll_messages", "wechat_read_history_span", "wechat_read_contact_span", "wechat_read_file_card", "wechat_send_image",
            "wechat_batch_read_messages", "wechat_watch_new_messages",
            "wechat_view_attachment_handoff",
            "wechat_send_at_username",
        }
        self.assertEqual(names, approved)


if __name__ == "__main__":
    unittest.main()
