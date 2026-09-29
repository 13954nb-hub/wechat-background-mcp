import copy
import json
import unittest

from mcp.server.fastmcp.exceptions import ToolError

from wxbg.readstore_public import inbox_result_or_error


def _background(**changes):
    value = {
        "background_observation_passed": True,
        "observations": 2,
        "foreground_changed": False,
        "clipboard_changed": False,
        "cursor_changed": False,
        "target_restored": False,
        "capture_observed": False,
        "new_visible_windows": [],
        "monitor_errors": [],
        "private_detail": "SECRET_MONITOR_DETAIL",
    }
    value.update(changes)
    return value


def _cleanup(**changes):
    value = {
        "status": "read_only",
        "restored": True,
        "modified": False,
        "gate_touched": False,
        "errors": [],
        "private_detail": "SECRET_CLEANUP_DETAIL",
    }
    value.update(changes)
    return value


def _row(**changes):
    value = {
        "conversation_key": "alice",
        "display_name": "Alice",
        "display_source": "remark",
        "display_available": True,
        "display_truncated": False,
        "unread_count": 2,
        "unread_known": True,
        "summary": "hello",
        "summary_available": True,
        "summary_truncated": False,
        "last_timestamp": 101,
        "sort_timestamp": 100,
    }
    value.update(changes)
    return value


def _readstore_evidence(**changes):
    snapshot = {
        "all_applied_page_hmac_verified": True,
        "integrity_verified": True,
        "source_consistency": "caller_must_verify_capture",
        "plaintext_storage": "worker_memory_only",
        "private_path": "SECRET_DB_PATH",
    }
    capture = {
        "consistency": "observed_quiet_window",
        "matching_passes": 2,
        "same_handles": True,
        "path_identity_rechecked": True,
        "atomic_snapshot": False,
        "private_path": "SECRET_CAPTURE_PATH",
    }
    value = {
        "multi_database_atomic": False,
        "source_consistency": "observed_quiet_window",
        "captures": {"session": copy.deepcopy(capture), "contact": copy.deepcopy(capture)},
        "snapshots": {"session": copy.deepcopy(snapshot), "contact": copy.deepcopy(snapshot)},
        "key_lookup": {"private": "SECRET_KEY_MATERIAL"},
    }
    value.update(changes)
    return value


_MISSING = object()


def _response(*, rows=None, next_cursor=None, has_more=False, result_extra=None,
              evidence=_MISSING, cleanup=_MISSING, **extra):
    result = {
        "conversations": [_row()] if rows is None else list(rows),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "coverage": "sessiontable_contact_bounded_nonhidden",
        "readstore_evidence": _readstore_evidence(),
        "private_result_detail": "SECRET_PROVIDER_DETAIL",
    }
    if result_extra:
        result.update(result_extra)
    value = {
        "ok": True,
        "worker_started": True,
        "target_validation": {"status": "stable", "private": "SECRET_TARGET"},
        "result": result,
        "evidence": _background() if evidence is _MISSING else evidence,
        "cleanup": _cleanup() if cleanup is _MISSING else cleanup,
        "private_top_level": "SECRET_TOP_LEVEL",
    }
    value.update(extra)
    return value


class ReadstorePublicTests(unittest.TestCase):
    def test_all_conversations_projects_hidden_flag_and_v2_cursor(self):
        cursor = {
            "version": 2, "account_epoch": "epoch-1", "unread_only": False,
            "include_hidden": True, "order": "sort_timestamp_desc_username_asc",
            "sort_timestamp": 100, "username": "alice",
        }
        response = _response(rows=[_row(is_hidden=True)], next_cursor=cursor,
                             has_more=True, result_extra={
                                 "coverage": "sessiontable_contact_bounded_all"})
        public = inbox_result_or_error(response, limit=10, unread_only=False,
                                       include_hidden=True)
        self.assertEqual(public["result"]["coverage"],
                         "sessiontable_contact_bounded_all")
        self.assertIs(public["result"]["conversations"][0]["is_hidden"], True)
        self.assertEqual(public["result"]["next_cursor"], cursor)

    def test_all_conversations_rejects_unbound_cursor_and_missing_visibility(self):
        cursor = {
            "version": 1, "account_epoch": "epoch-1", "unread_only": False,
            "order": "sort_timestamp_desc_username_asc", "sort_timestamp": 100,
            "username": "alice",
        }
        cases = [
            _response(rows=[_row(is_hidden=True)], next_cursor=cursor,
                      has_more=True, result_extra={
                          "coverage": "sessiontable_contact_bounded_all"}),
            _response(rows=[_row()], result_extra={
                "coverage": "sessiontable_contact_bounded_all"}),
        ]
        for response in cases:
            with self.subTest(response=response["result"]),self.assertRaises(ToolError):
                inbox_result_or_error(response, limit=10, unread_only=False,
                                      include_hidden=True)

    def test_success_projects_only_validated_inbox_and_compact_evidence(self):
        cursor = {
            "version": 1,
            "account_epoch": "epoch-1",
            "unread_only": True,
            "order": "sort_timestamp_desc_username_asc",
            "sort_timestamp": 100,
            "username": "alice",
            "private_cursor": "SECRET_CURSOR",
        }
        response = _response(
            next_cursor=cursor,
            has_more=True,
            result_extra={"unexpected": {"private": "SECRET_EXTRA"}},
        )
        public = inbox_result_or_error(response, limit=10, unread_only=True)

        self.assertEqual(set(public), {"ok", "result", "evidence"})
        self.assertTrue(public["ok"])
        self.assertEqual(set(public["result"]), {
            "conversations", "next_cursor", "has_more", "coverage",
        })
        self.assertEqual(set(public["result"]["conversations"][0]), {
            "conversation_key", "display_name", "display_source",
            "display_available", "display_truncated", "unread_count",
            "unread_known", "summary", "summary_available",
            "summary_truncated", "last_timestamp", "sort_timestamp",
        })
        self.assertEqual(set(public["result"]["next_cursor"]), {
            "version", "account_epoch", "unread_only", "order",
            "sort_timestamp", "username",
        })
        self.assertEqual(set(public["evidence"]), {"background", "cleanup"})
        encoded = json.dumps(public, ensure_ascii=True)
        self.assertNotIn("SECRET", encoded)

    def test_no_result_mutation_while_projecting_extras(self):
        response = _response(result_extra={"unexpected": "SECRET_EXTRA"})
        before = copy.deepcopy(response)
        inbox_result_or_error(response, limit=10, unread_only=True)
        self.assertEqual(response, before)

    def test_provider_failure_uses_fixed_tool_error_without_private_echo(self):
        response = {
            "ok": False,
            "worker_started": True,
            "error": {"code": "SECRET_PROVIDER_CODE", "detail": "SECRET_PROVIDER_DETAIL"},
            "private": "SECRET_PRIVATE_FIELD",
        }
        with self.assertRaises(ToolError) as caught:
            inbox_result_or_error(response, limit=10, unread_only=True)
        payload = json.loads(str(caught.exception))
        self.assertEqual(payload, {
            "code": "readstore_result_invalid",
            "outcome_unknown": False,
            "retry": False,
        })
        self.assertNotIn("SECRET", str(caught.exception))

    def test_inbox_preserves_verified_fixed_readstore_codes_only(self):
        response = _response()
        response["ok"] = False
        response.pop("result")
        response["error"] = {"code": "readstore_unavailable",
                             "message": "SECRET_PROVIDER_DETAIL",
                             "outcome_unknown": False,
                             "submission_started": False}
        for code in ("readstore_unavailable", "readstore_deadline",
                     "readstore_size_limit", "readstore_result_invalid"):
            response["error"]["code"] = code
            for mode in ((True, False), (False, True)):
                with self.subTest(code=code, mode=mode), self.assertRaises(ToolError) as caught:
                    inbox_result_or_error(response, limit=10,
                                          unread_only=mode[0], include_hidden=mode[1])
                self.assertEqual(json.loads(str(caught.exception)), {
                    "code": code, "outcome_unknown": False, "retry": False})
                self.assertNotIn("SECRET", str(caught.exception))

        response["error"]["code"] = "readstore_unavailable"
        for unsafe in (
            {"evidence": None},
            {"target_validation": {"status": "changed"}},
            {"cleanup": _cleanup(status="recovery_unknown")},
            {"error": {**response["error"], "outcome_unknown": True}},
            {"error": {**response["error"], "code": "SECRET_UNKNOWN_CODE"}},
        ):
            candidate = {**response, **unsafe}
            with self.subTest(unsafe=unsafe), self.assertRaises(ToolError) as caught:
                inbox_result_or_error(candidate, limit=10, unread_only=True)
            self.assertEqual(json.loads(str(caught.exception))["code"],
                             "readstore_result_invalid")
            self.assertNotIn("SECRET", str(caught.exception))

    def test_background_side_effect_returns_only_fixed_diagnostic(self):
        response = _response(evidence=_background(
            background_observation_passed=False, foreground_changed=True))
        response["ok"] = False
        response.pop("result")
        response["error"] = {"code": "background_side_effect",
                             "message": "SECRET_PROVIDER_DETAIL",
                             "outcome_unknown": False,
                             "submission_started": False}
        with self.assertRaises(ToolError) as caught:
            inbox_result_or_error(response, limit=100, unread_only=False,
                                  include_hidden=True)
        self.assertEqual(json.loads(str(caught.exception)), {
            "code": "background_side_effect", "outcome_unknown": False,
            "retry": False})
        self.assertNotIn("SECRET", str(caught.exception))

    def test_background_preflight_failure_requires_complete_safe_envelope(self):
        response = _response()
        response["ok"] = False
        response.pop("result")
        response["error"] = {"code": "readstore_background_failed",
                             "message": "SECRET_PROVIDER_DETAIL",
                             "outcome_unknown": False,
                             "submission_started": False}
        with self.assertRaises(ToolError) as caught:
            inbox_result_or_error(response, limit=10, unread_only=True)
        self.assertEqual(json.loads(str(caught.exception)), {
            "code": "readstore_background_failed", "outcome_unknown": False,
            "retry": False})
        self.assertNotIn("SECRET", str(caught.exception))

        response["evidence"] = None
        with self.assertRaises(ToolError) as caught:
            inbox_result_or_error(response, limit=10, unread_only=True)
        self.assertEqual(json.loads(str(caught.exception))["code"],
                         "readstore_result_invalid")

    def test_missing_worker_target_or_cleanup_evidence_is_rejected(self):
        cases = [
            {"worker_started": False},
            {"target_validation": {"status": "unstable"}},
            {"cleanup": _cleanup(status="write_possible")},
            {"evidence": None},
            {"cleanup": None},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                response = _response(**changes)
                with self.assertRaises(ToolError):
                    inbox_result_or_error(response, limit=10, unread_only=True)

    def test_background_flags_must_be_complete_and_successful(self):
        cases = [
            {"background_observation_passed": False},
            {"observations": True},
            {"foreground_changed": True},
            {"clipboard_changed": True},
            {"cursor_changed": "false"},
            {"target_restored": True},
            {"capture_observed": True},
            {"new_visible_windows": [123]},
            {"monitor_errors": ["SECRET_MONITOR_DETAIL"]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                response = _response(evidence=_background(**changes))
                with self.assertRaises(ToolError):
                    inbox_result_or_error(response, limit=10, unread_only=True)

    def test_readstore_evidence_requires_both_authenticated_integrity_checked_sources(self):
        cases = [
            {"multi_database_atomic": True},
            {"source_consistency": "guessed_atomic"},
            {"captures": {"session": _readstore_evidence()["captures"]["session"]}},
            {"snapshots": {"session": _readstore_evidence()["snapshots"]["session"],
                           "contact": _readstore_evidence()["snapshots"]["contact"] | {
                               "integrity_verified": False}}},
            {"snapshots": {"session": _readstore_evidence()["snapshots"]["session"] | {
                               "all_applied_page_hmac_verified": False},
                           "contact": _readstore_evidence()["snapshots"]["contact"]}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                response = _response(result_extra={
                    "readstore_evidence": _readstore_evidence(**changes),
                })
                with self.assertRaises(ToolError):
                    inbox_result_or_error(response, limit=10, unread_only=True)

    def test_public_evidence_does_not_expose_source_capture_or_key_fields(self):
        public = inbox_result_or_error(_response(), limit=10, unread_only=True)
        encoded = json.dumps(public, ensure_ascii=True)
        self.assertNotIn("captures", encoded)
        self.assertNotIn("snapshots", encoded)
        self.assertNotIn("key_lookup", encoded)
        self.assertNotIn("SECRET", encoded)

    def test_limit_and_unread_only_require_strict_caller_types(self):
        for limit in (True, False, 0, 101, "10"):
            with self.subTest(limit=limit), self.assertRaises(ToolError):
                inbox_result_or_error(_response(), limit=limit, unread_only=True)
        for unread_only in (1, 0, "true", None):
            with self.subTest(unread_only=unread_only), self.assertRaises(ToolError):
                inbox_result_or_error(_response(), limit=10, unread_only=unread_only)

    def test_page_and_cursor_semantics_are_bound_to_request(self):
        row = _row()
        bad_pages = [
            _response(rows=[row, _row(conversation_key="bob")], has_more=False),
            _response(result_extra={"has_more": 1}),
            _response(rows=[], has_more=True),
            _response(has_more=False, next_cursor={
                "version": 1, "account_epoch": "epoch-1", "unread_only": True,
                "order": "sort_timestamp_desc_username_asc", "sort_timestamp": 100,
                "username": "alice",
            }),
            _response(has_more=True, next_cursor={
                "version": 1, "account_epoch": "epoch-1", "unread_only": False,
                "order": "sort_timestamp_desc_username_asc", "sort_timestamp": 100,
                "username": "alice",
            }),
            _response(has_more=True, next_cursor={
                "version": 1, "account_epoch": "epoch-1", "unread_only": True,
                "order": "sort_timestamp_desc_username_asc", "sort_timestamp": 99,
                "username": "alice",
            }),
        ]
        for response in bad_pages:
            with self.assertRaises(ToolError):
                inbox_result_or_error(response, limit=1, unread_only=True)

    def test_unread_only_page_cannot_claim_unknown_or_zero_unread_rows(self):
        for row in (
            _row(unread_count=None, unread_known=False),
            _row(unread_count=0, unread_known=True),
        ):
            with self.subTest(row=row):
                with self.assertRaises(ToolError):
                    inbox_result_or_error(_response(rows=[row]), limit=10, unread_only=True)

    def test_page_cannot_repeat_a_conversation_key_across_timestamps(self):
        response = _response(rows=[
            _row(sort_timestamp=100),
            _row(sort_timestamp=99),
        ])
        with self.assertRaises(ToolError):
            inbox_result_or_error(response, limit=10, unread_only=True)

    def test_page_cannot_repeat_a_conversation_key_non_adjacent(self):
        response = _response(rows=[
            _row(conversation_key="alice", sort_timestamp=100),
            _row(conversation_key="bob", sort_timestamp=99),
            _row(conversation_key="alice", sort_timestamp=98),
        ])
        with self.assertRaises(ToolError):
            inbox_result_or_error(response, limit=10, unread_only=True)

    def test_rows_require_bounded_typed_fields_and_drop_only_unexpected_extras(self):
        bad_rows = [
            _row(conversation_key=""),
            _row(display_source="private_source"),
            _row(display_source=[]),
            _row(display_available=1),
            _row(display_truncated=True, display_available=True),
            _row(unread_count=True),
            _row(unread_known=False),
            _row(summary_available=True, summary=None),
            _row(summary_truncated=True, summary_available=True),
            _row(sort_timestamp=True),
            {"conversation_key": "alice"},
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                with self.assertRaises(ToolError):
                    inbox_result_or_error(_response(rows=[row]), limit=10, unread_only=True)
        valid = _row(unexpected="SECRET_ROW_EXTRA")
        public = inbox_result_or_error(_response(rows=[valid]), limit=10, unread_only=True)
        self.assertNotIn("unexpected", public["result"]["conversations"][0])
        self.assertNotIn("SECRET", json.dumps(public, ensure_ascii=True))

    def test_cursor_shape_and_types_are_strictly_validated(self):
        base = {
            "version": 1,
            "account_epoch": "epoch-1",
            "unread_only": True,
            "order": "sort_timestamp_desc_username_asc",
            "sort_timestamp": 100,
            "username": "alice",
        }
        for key, value in (
            ("version", True),
            ("account_epoch", ""),
            ("unread_only", 1),
            ("order", "other"),
            ("sort_timestamp", True),
            ("username", ""),
        ):
            cursor = dict(base)
            cursor[key] = value
            with self.subTest(key=key):
                with self.assertRaises(ToolError):
                    inbox_result_or_error(_response(has_more=True, next_cursor=cursor),
                                          limit=10, unread_only=True)


if __name__ == "__main__":
    unittest.main()
