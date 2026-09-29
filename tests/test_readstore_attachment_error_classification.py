"""Offline regressions for fixed attachment query error categories."""

from contextlib import nullcontext
import hashlib
import json
import time
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from wxbg.readstore_attachment_provider import SAFE_ATTACHMENT_ERRORS, run_attachment
from wxbg.readstore_attachment_public import attachment_result_or_error
from wxbg.readstore_provider import ProviderError
from wxbg.readstore_query import ReadStoreError
from wxbg.worker import _READSTORE_SAFE_ERRORS


ROOM = "synthetic-attachment@chatroom"
CHAT = hashlib.md5(ROOM.encode("utf-8")).hexdigest()
EPOCH = "a" * 64
ARGS = {
    "conversation_key": ROOM,
    "account_epoch": EPOCH,
    "message_identity": {
        "chat_md5": CHAT,
        "shard_id": "message_0",
        "local_id": 1,
        "server_id": 2,
        "rowid": 3,
    },
}


def _background():
    return {
        "background_observation_passed": True,
        "observations": 1,
        "foreground_changed": False,
        "clipboard_changed": False,
        "cursor_changed": False,
        "target_restored": False,
        "capture_observed": False,
        "new_visible_windows": [],
        "monitor_errors": [],
    }


def _cleanup():
    return {
        "status": "read_only",
        "restored": True,
        "modified": False,
        "gate_touched": False,
        "errors": [],
    }


def _failure(code):
    return {
        "ok": False,
        "worker_started": True,
        "target_validation": {"status": "stable"},
        "error": {"code": code, "outcome_unknown": False,
                  "submission_started": False},
        "evidence": _background(),
        "cleanup": _cleanup(),
    }


def _public_error(response):
    try:
        attachment_result_or_error(
            response, conversation_key=ROOM, account_epoch=EPOCH,
            message_identity=ARGS["message_identity"],
        )
    except ToolError as error:
        return json.loads(str(error))
    raise AssertionError("failure response must not yield attachment data")
OPENED = {
    "account_epoch": EPOCH,
    "connections": {"session": object(), "message_0": object()},
    "message_shard_ids": ["message_0"],
}


def _provider_code(inner_code):
    with patch("wxbg.readstore_provider.open_stores", return_value=nullcontext(OPENED)), \
         patch("wxbg.readstore_attachment_provider.resolve_conversation", return_value=CHAT), \
         patch("wxbg.readstore_attachment_provider.query_attachment",
               side_effect=ReadStoreError(inner_code)):
        try:
            run_attachment(ARGS, target=None, deadline=time.monotonic() + 5)
        except ProviderError as error:
            return error.code
    raise AssertionError("synthetic query must fail")


class AttachmentErrorClassificationTests(unittest.TestCase):
    def test_declaration_locator_and_body_codes_have_existing_safe_category(self):
        cases = (
            "declaration_invalid", "declaration_too_large",
            "image_locator_invalid", "image_packed_info_missing",
            "packed_info_missing", "packed_info_unsupported",
            "packed_info_too_large", "body_missing", "body_unsupported",
            "body_unavailable", "body_too_large",
        )
        for inner in cases:
            with self.subTest(inner=inner):
                self.assertEqual(_provider_code(inner), "attachment_declaration_invalid")

    def test_query_budget_has_existing_safe_deadline_category(self):
        self.assertEqual(_provider_code("query_budget_exceeded"), "attachment_deadline")

    def test_identity_and_unknown_codes_remain_generic_and_do_not_leak(self):
        for inner in ("message_not_found", "message_ambiguous", "identity_mismatch",
                      "invalid_message_identity", "account_epoch_mismatch", "unsupported",
                      "private-content-must-not-escape"):
            with self.subTest(inner=inner):
                self.assertEqual(_provider_code(inner), "attachment_message_unavailable")

    def test_selected_public_codes_are_already_allowlisted_across_boundary(self):
        for code in ("attachment_declaration_invalid", "attachment_deadline",
                     "attachment_message_unavailable"):
            self.assertIn(code, SAFE_ATTACHMENT_ERRORS)
            self.assertIn(code, _READSTORE_SAFE_ERRORS)

    def test_public_boundary_emits_only_selected_fixed_code(self):
        for inner in ("declaration_invalid", "query_budget_exceeded",
                      "message_not_found", "private-content-must-not-escape"):
            with self.subTest(inner=inner):
                code = _provider_code(inner)
                payload = _public_error(_failure(code))
                self.assertEqual(payload, {"code": code, "outcome_unknown": False,
                                           "retry": False})
                self.assertNotIn("private-content-must-not-escape", json.dumps(payload))

    def test_public_boundary_requires_complete_safe_failure_envelope(self):
        base = _failure("attachment_not_downloaded")
        cases = (
            {"ok": False, "error": {"code": "attachment_not_downloaded"}},
            {**base, "worker_started": False},
            {**base, "target_validation": {"status": "changed"}},
            {**base, "evidence": None},
            {**base, "cleanup": {**_cleanup(), "modified": True}},
            {**base, "result": {"private": "SECRET_RESULT"}},
            {**base, "error": {**base["error"], "outcome_unknown": True}},
            {**base, "error": {**base["error"], "submission_started": True}},
        )
        for response in cases:
            with self.subTest(response=response):
                payload = _public_error(response)
                self.assertEqual(payload, {"code": "attachment_result_invalid",
                                           "outcome_unknown": False, "retry": False})
                self.assertNotIn("SECRET", json.dumps(payload))

    def test_public_boundary_preserves_verified_readstore_and_background_codes(self):
        for code in ("readstore_deadline", "readstore_result_invalid"):
            with self.subTest(code=code):
                self.assertEqual(_public_error(_failure(code))["code"], code)
        side_effect = _failure("background_side_effect")
        side_effect["evidence"] = {**_background(),
                                   "background_observation_passed": False}
        self.assertEqual(_public_error(side_effect)["code"],
                         "background_side_effect")

    def test_public_boundary_does_not_echo_unknown_code(self):
        payload = _public_error(_failure("SECRET_PROVIDER_CODE"))
        self.assertEqual(payload["code"], "attachment_result_invalid")
        self.assertNotIn("SECRET", json.dumps(payload))

    def test_public_boundary_strips_private_details_from_verified_error(self):
        response = _failure("attachment_not_downloaded")
        response["error"]["detail"] = "SECRET_ERROR"
        response["evidence"]["private_detail"] = "SECRET_EVIDENCE"
        response["cleanup"]["private_detail"] = "SECRET_CLEANUP"
        payload = _public_error(response)
        self.assertEqual(payload, {"code": "attachment_not_downloaded",
                                   "outcome_unknown": False, "retry": False})
        self.assertNotIn("SECRET", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
