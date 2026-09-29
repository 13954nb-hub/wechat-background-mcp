"""Real SQLite tests; never imports or operates the Weixin adapter."""

import hashlib
from contextlib import closing
import importlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest


def _reserve_once(path, start, ready, results):
    from wxbg.journal import Journal, JournalError

    journal = Journal(path)
    ready.put(True)
    if not start.wait(10):
        results.put("start_timeout")
        return
    try:
        reservation = journal.reserve("send_text", {"text": "test"}, "shared-id")
        results.put("reserved" if not reservation.replay else "replay")
    except JournalError as exc:
        results.put(exc.code)


def _crash_after_begin(path):
    from wxbg.journal import Journal

    journal = Journal(path)
    reservation = journal.reserve("send_text", {"text": "test"}, "crashed-id")
    journal.begin("crashed-id", reservation.token)
    os._exit(17)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("wxbg.journal"),
            "Journal module has not been implemented",
        )
        self.api = importlib.import_module("wxbg.journal")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "operations.sqlite3"
        self.journal = self.api.Journal(self.path)

    def reserve(self, operation_id="test-id", args=None):
        return self.journal.reserve(
            "send_text", {"text": "test"} if args is None else args, operation_id
        )

    def test_completed_operation_replays_without_second_execution(self):
        original = self.reserve()
        self.assertFalse(original.replay)
        self.assertTrue(original.token)
        self.journal.begin("test-id", original.token)
        summary = {"status": "submitted", "counts": {"sent": 1}, "refs": {"message": "m-1"}}
        self.assertEqual(self.journal.complete("test-id", original.token, summary), summary)
        restarted = self.api.Journal(self.path)
        replay = restarted.reserve("send_text", {"text": "test"}, "test-id")
        self.assertTrue(replay.replay)
        self.assertIsNone(replay.token)
        self.assertEqual(replay.result, summary)

    def test_pre_worker_rejection_is_publicly_rejected_even_for_legacy_complete_row(self):
        reservation = self.reserve()
        self.journal.begin("test-id", reservation.token)
        rejected = {"ok": False, "status": "rejected", "error_code": "BUSY"}
        self.journal.complete("test-id", reservation.token, rejected)
        restarted = self.api.Journal(self.path)
        status = restarted.get("test-id")
        self.assertEqual(status["state"], "rejected")
        self.assertEqual(status["reason_code"], "BUSY")
        self.assertEqual(status["result"], rejected)
        replay = restarted.reserve("send_text", {"text": "test"}, "test-id")
        self.assertTrue(replay.replay)
        self.assertEqual(replay.result, rejected)

    def test_same_id_with_changed_arguments_conflicts(self):
        self.reserve()
        with self.assertRaises(self.api.Conflict):
            self.reserve(args={"text": "different"})

    def test_same_id_with_changed_action_conflicts(self):
        self.reserve()
        with self.assertRaises(self.api.Conflict):
            self.journal.reserve("clear_draft", {"text": "test"}, "test-id")

    def test_reserved_operation_is_busy(self):
        self.reserve()
        with self.assertRaises(self.api.Busy):
            self.reserve()

    def test_execution_cannot_begin_twice(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        with self.assertRaises(self.api.OutcomeUnknown):
            self.journal.begin("test-id", reserved.token)
        with self.assertRaises(self.api.OutcomeUnknown):
            self.reserve()

    def test_process_crash_never_releases_executing_operation(self):
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=_crash_after_begin, args=(str(self.path),))
        process.start()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join()
            self.fail("crash worker did not finish")
        self.assertEqual(process.exitcode, 17)
        restarted = self.api.Journal(self.path)
        with self.assertRaises(self.api.OutcomeUnknown):
            restarted.reserve("send_text", {"text": "test"}, "crashed-id")
        self.assertEqual(restarted.get("crashed-id")["state"], "executing")

    def test_cross_process_reservation_has_exactly_one_owner(self):
        context = multiprocessing.get_context("spawn")
        start, ready, results = context.Event(), context.Queue(), context.Queue()
        processes = [context.Process(target=_reserve_once, args=(str(self.path), start, ready, results)) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=15))
            start.set()
            outcomes = [results.get(timeout=15) for _ in processes]
            self.assertEqual(outcomes.count("reserved"), 1, outcomes)
            self.assertEqual(outcomes.count("operation_busy"), 3, outcomes)
        finally:
            start.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join()
            ready.close()
            results.close()

    def test_sorted_utf8_json_hash_is_stable(self):
        args = {"z": ["繁體中文", 2], "a": {"second": True, "first": None}}
        other = {"a": {"first": None, "second": True}, "z": ["繁體中文", 2]}
        canonical = json.dumps({"action": "send_text", "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(self.api.canonical_payload_hash("send_text", args), expected)
        self.assertEqual(self.api.canonical_payload_hash("send_text", other), expected)

    def test_invalid_json_arguments_are_rejected(self):
        for args in ({1: "value"}, {"x": float("nan")}, {"x": float("inf")}, {"x": object()}, {"x": (1, 2)}, {"x": "\ud800"}, []):
            with self.subTest(args=repr(args)):
                with self.assertRaises(self.api.InvalidPayload):
                    self.reserve(args=args)

    def test_invalid_operation_ids_are_rejected(self):
        for value in ("", "  ", "x" * 129, None, 123, "a\x00b"):
            with self.subTest(operation_id=value):
                with self.assertRaises(self.api.InvalidOperationId):
                    self.reserve(operation_id=value)
        self.reserve(operation_id="測" * 128)

    def test_begin_requires_matching_reservation_token(self):
        self.reserve()
        for token in ("not-owner", "非持有者", None):
            with self.subTest(token=token):
                with self.assertRaises(self.api.InvalidTransition):
                    self.journal.begin("test-id", token)
        self.assertEqual(self.journal.get("test-id")["state"], "reserved")

    def test_complete_requires_begin_and_owner(self):
        reserved = self.reserve()
        with self.assertRaises(self.api.InvalidTransition):
            self.journal.complete("test-id", reserved.token, {"ok": True})
        self.journal.begin("test-id", reserved.token)
        with self.assertRaises(self.api.InvalidTransition):
            self.journal.complete("test-id", "not-owner", {"ok": True})

    def test_completed_summary_is_immutable(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        summary = {"ok": True}
        self.journal.complete("test-id", reserved.token, summary)
        self.assertEqual(self.journal.complete("test-id", reserved.token, summary), summary)
        with self.assertRaises(self.api.InvalidTransition):
            self.journal.complete("test-id", reserved.token, {"ok": False})
        with self.assertRaises(self.api.InvalidTransition):
            self.journal.mark_unknown("test-id", reserved.token, "late_timeout")

    def test_unknown_outcome_is_sticky(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        self.journal.mark_unknown("test-id", reserved.token, "worker_timeout")
        with self.assertRaises(self.api.OutcomeUnknown):
            self.reserve()
        with self.assertRaises(self.api.OutcomeUnknown):
            self.journal.complete("test-id", reserved.token, {"ok": True})
        row = self.journal.get("test-id")
        self.assertEqual(row["state"], "outcome_unknown")
        self.assertEqual(row["reason_code"], "worker_timeout")

    def test_raw_payload_never_appears_in_database(self):
        private_text = "private-chat-do-not-store-繁體正文-7395281"
        reserved = self.reserve(args={"text": private_text})
        self.journal.begin("test-id", reserved.token)
        self.journal.complete("test-id", reserved.token, {"counts": {"sent": 1}})
        self.assertNotIn(private_text.encode("utf-8"), self.path.read_bytes())
        row = self.journal.get("test-id")
        self.assertNotIn("args", row)
        self.assertNotIn("token", row)
        self.assertEqual(len(row["payload_hash"]), 64)

    def test_result_summary_rejects_chat_content_and_invalid_metadata(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        for summary in ({"text": "private"}, {"result": {"body": "private"}}, {"refs": {"draft": "private"}}, {"counts": {"sent": -1}}, {"counts": {"sent": True}}, {"refs": {"message": ["m-1"]}}, {"ok": "yes"}, {"duration_ms": float("nan")}, {"duration_ms": 10 ** 400}):
            with self.subTest(summary=summary):
                with self.assertRaises(self.api.InvalidResult):
                    self.journal.complete("test-id", reserved.token, summary)
        self.assertEqual(self.journal.get("test-id")["state"], "executing")

    def test_file_submission_metadata_survives_completion_and_persistent_replay(self):
        for index, upload_status in enumerate(("uploading", "completed", "interrupted", "failed", "unknown")):
            with self.subTest(upload_status=upload_status):
                operation_id = f"file-{index}"
                args = {"conversation_ref": "conversation-1", "fixture_ref": "fixture-1"}
                reserved = self.journal.reserve("send_file", args, operation_id)
                self.journal.begin(operation_id, reserved.token)
                summary = {
                    "ok": True, "status": "submitted", "submitted": True,
                    "remote_receipt_verified": False, "upload_status": upload_status,
                    "verification_level": "new_local_attachment_card_and_empty_draft",
                    "counts": {"submitted": 1},
                    "refs": {"conversation": "conversation-1", "message": f"message-{index}"},
                }
                try:
                    completed = self.journal.complete(operation_id, reserved.token, summary)
                except self.api.InvalidResult as exc:
                    self.fail(f"Documented file metadata was rejected: {exc}")
                self.assertEqual(completed, summary)
                restarted = self.api.Journal(self.path)
                replay = restarted.reserve("send_file", args, operation_id)
                self.assertTrue(replay.replay)
                self.assertIsNone(replay.token)
                self.assertEqual(replay.result, summary)
                self.assertIs(replay.result["submitted"], True)
                self.assertIs(replay.result["remote_receipt_verified"], False)
                self.assertEqual(restarted.get(operation_id)["result"], summary)

    def test_file_metadata_boolean_flags_preserve_both_boolean_values(self):
        for submitted in (False, True):
            for receipt in (False, True):
                with self.subTest(submitted=submitted, receipt=receipt):
                    operation_id = f"flags-{submitted}-{receipt}"
                    reserved = self.reserve(operation_id)
                    self.journal.begin(operation_id, reserved.token)
                    summary = {"submitted": submitted, "remote_receipt_verified": receipt}
                    self.journal.complete(operation_id, reserved.token, summary)
                    replay = self.api.Journal(self.path).reserve("send_text", {"text": "test"}, operation_id)
                    self.assertIs(replay.result["submitted"], submitted)
                    self.assertIs(replay.result["remote_receipt_verified"], receipt)

    def test_file_metadata_boolean_flags_reject_numbers_strings_and_containers(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        invalid = (0, 1, -1, 0.0, 1.0, None, "true", "false", "private-chat-content", [], {}, object())
        for key in ("submitted", "remote_receipt_verified"):
            for value in invalid:
                with self.subTest(key=key, value=repr(value)):
                    with self.assertRaises(self.api.InvalidResult):
                        self.journal.complete("test-id", reserved.token, {key: value})
        row = self.journal.get("test-id")
        self.assertEqual(row["state"], "executing")
        self.assertIsNone(row["result"])
        self.assertNotIn(b"private-chat-content", self.path.read_bytes())
        with self.assertRaises(self.api.OutcomeUnknown):
            self.api.Journal(self.path).reserve("send_text", {"text": "test"}, "test-id")

    def test_upload_status_rejects_unlisted_text_and_non_string_types(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        invalid = ("", "sent", "Uploading", "正在上傳", "private_chat_excerpt", "private chat text",
                   "completed\n", "failed\0", None, True, 0, 1.0, b"completed", [], {})
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(self.api.InvalidResult):
                    self.journal.complete("test-id", reserved.token, {"upload_status": value})
        row = self.journal.get("test-id")
        self.assertEqual(row["state"], "executing")
        self.assertIsNone(row["result"])
        self.assertNotIn(b"private_chat_excerpt", self.path.read_bytes())

    def test_file_metadata_does_not_add_session_reference_key(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        with self.assertRaises(self.api.InvalidResult):
            self.journal.complete("test-id", reserved.token, {
                "submitted": True, "remote_receipt_verified": False,
                "upload_status": "uploading", "refs": {"session": "conversation-1"},
            })
        self.assertEqual(self.journal.get("test-id")["state"], "executing")

    def test_failed_upload_summary_cannot_be_overwritten_as_completed(self):
        reserved = self.reserve()
        self.journal.begin("test-id", reserved.token)
        summary = {"submitted": True, "remote_receipt_verified": False, "upload_status": "interrupted"}
        self.journal.complete("test-id", reserved.token, summary)
        with self.assertRaises(self.api.InvalidTransition):
            self.journal.complete("test-id", reserved.token, {**summary, "upload_status": "completed"})
        replay = self.api.Journal(self.path).reserve("send_text", {"text": "test"}, "test-id")
        self.assertEqual(replay.result, summary)

    def test_database_lock_is_reported_as_busy(self):
        other = self.api.Journal(self.path, timeout=0.01)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as blocking:
            blocking.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaises(self.api.Busy):
                    other.reserve("send_text", {}, "blocked-id")
            finally:
                blocking.execute("ROLLBACK")
        self.assertIsNone(other.get("blocked-id"))

    def test_get_absent_operation_returns_none(self):
        self.assertIsNone(self.journal.get("does-not-exist"))


if __name__ == "__main__":
    unittest.main()
