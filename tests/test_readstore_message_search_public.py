"""Contract checks for the message-search implementation used by wechat_search."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unittest
from unittest import mock

import zstandard

from mcp.server.fastmcp.exceptions import ToolError

from wxbg import readstore_message_search as message_search
from wxbg.readstore_message_search import search_messages
from wxbg.readstore_new_public import new_result_or_error
from wxbg.readstore_query import ReadStoreError, ReadStoreQuery
from wxbg.readstore_search_public import search_result_or_error


CONVERSATION_KEY = "room@chatroom"
CHAT_MD5 = hashlib.md5(CONVERSATION_KEY.encode("utf-8")).hexdigest()
TABLE = "Msg_" + CHAT_MD5
ACCOUNT_EPOCH = "a" * 64


def _capture():
    return {
        "consistency": "observed_quiet_window",
        "matching_passes": 2,
        "same_handles": True,
        "path_identity_rechecked": True,
        "atomic_snapshot": False,
    }


def _snapshot():
    return {
        "all_applied_page_hmac_verified": True,
        "integrity_verified": True,
        "source_consistency": "caller_must_verify_capture",
    }


def _verified_response(raw, *, message_shards=("message_0",)):
    return {
        "ok": True,
        "worker_started": True,
        "target_validation": {"status": "stable"},
        "evidence": {
            "background_observation_passed": True,
            "observations": 1,
            "foreground_changed": False,
            "clipboard_changed": False,
            "cursor_changed": False,
            "target_restored": False,
            "capture_observed": False,
            "new_visible_windows": [],
            "monitor_errors": [],
        },
        "cleanup": {
            "status": "read_only",
            "restored": True,
            "modified": False,
            "gate_touched": False,
            "errors": [],
        },
        "result": {
            **raw,
            "readstore_account_epoch": ACCOUNT_EPOCH,
            "readstore_evidence": {
                "multi_database_atomic": False,
                "source_consistency": "observed_quiet_window",
                "captures": {name: _capture() for name in ("session", "contact", *message_shards)},
                "snapshots": {name: _snapshot() for name in ("session", "contact", *message_shards)},
            },
        },
    }


def _background_rejection():
    response = _verified_response({})
    response["ok"] = False
    response.pop("result")
    response["evidence"]["background_observation_passed"] = False
    response["evidence"]["foreground_changed"] = True
    response["error"] = {
        "code": "background_side_effect",
        "outcome_unknown": False,
        "submission_started": False,
        "message": "private details must not be echoed",
    }
    return response


def _verified_provider_rejection(code="readstore_unavailable"):
    response = _verified_response({})
    response["ok"] = False
    response.pop("result")
    response["error"] = {
        "code": code,
        "outcome_unknown": False,
        "submission_started": False,
        "message": "private provider or database details must not be echoed",
    }
    return response


class MessageSearchPublicTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            f'CREATE TABLE "{TABLE}" ('
            "local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, message_content, server_id INTEGER, sort_seq INTEGER, "
            "WCDB_CT_message_content INTEGER)"
        )

    def tearDown(self):
        self.connection.close()

    def _insert(self, content, *, local_id=1, sort_seq=100, flag=0):
        self.connection.execute(
            f'INSERT INTO "{TABLE}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (local_id, 1, 2, 123, content, 42, sort_seq, flag),
        )
        self.connection.commit()

    def _search(self, keyword="needle", *, limit=10, cursor=None):
        return search_messages(
            {"message_0": self.connection}, account_epoch=ACCOUNT_EPOCH,
            chat_md5=CHAT_MD5, keyword=keyword, limit=limit, cursor=cursor,
            deadline=time.monotonic() + 5,
        )

    def _new(self, *, limit=10, cursor=None, start_from="beginning"):
        query = ReadStoreQuery(
            self.connection, {"message_0": self.connection},
            account_epoch=ACCOUNT_EPOCH,
        )
        return query.get_new_messages(
            CHAT_MD5, limit=limit, cursor=cursor, start_from=start_from,
        )

    def test_public_search_rejects_complete_row_without_keyword(self):
        self._insert("needle is here")
        raw = self._search()
        self.assertEqual(raw["count"], 1)
        self.assertTrue(raw["items"][0]["content_available"])
        raw["items"][0]["content"] = "unrelated complete body"
        with self.assertRaises(ToolError) as caught:
            search_result_or_error(
                _verified_response(raw), scope="messages", keyword="needle",
                conversation_key=CONVERSATION_KEY, limit=10,
            )
        self.assertEqual(json.loads(str(caught.exception))["code"],
                         "readstore_result_invalid")

    def test_public_search_keeps_a_match_beyond_truncated_preview(self):
        self._insert("x" * 4096 + "needle")
        raw = self._search()
        self.assertEqual(raw["count"], 1)
        self.assertFalse(raw["items"][0]["content_available"])
        self.assertTrue(raw["items"][0]["content_truncated"])
        self.assertNotIn("needle", raw["items"][0]["content"])
        result = search_result_or_error(
            _verified_response(raw), scope="messages", keyword="needle",
            conversation_key=CONVERSATION_KEY, limit=10,
        )
        self.assertEqual(result["result"]["count"], 1)

    def test_public_search_rejects_unavailable_untruncated_nonmatch(self):
        self._insert("needle is here")
        raw = self._search()
        raw["items"][0].update(
            content=None, content_available=False, content_truncated=False,
        )
        with self.assertRaises(ToolError) as caught:
            search_result_or_error(
                _verified_response(raw), scope="messages", keyword="needle",
                conversation_key=CONVERSATION_KEY, limit=10,
            )
        self.assertEqual(json.loads(str(caught.exception))["code"],
                         "readstore_result_invalid")

    def test_new_public_rejects_cursor_highwater_past_returned_row(self):
        self._insert("first", local_id=1, sort_seq=100)
        self._insert("second", local_id=2, sort_seq=200)
        raw = self._new(limit=1)
        self.assertEqual(raw["items"][0]["message_identity"]["rowid"], 1)
        self.assertTrue(raw["has_more"])
        for key in ("shard_highwater", "shard_boundary"):
            raw["next_cursor"][key][0].update(sort_seq=9999, local_id=9999,
                                               rowid=9999)
        with self.assertRaises(ToolError) as caught:
            new_result_or_error(
                _verified_response(raw), conversation_key=CONVERSATION_KEY,
                limit=1,
            )
        self.assertEqual(json.loads(str(caught.exception))["code"],
                         "readstore_result_invalid")

    def test_new_public_rejects_wrong_ordering(self):
        self._insert("first", local_id=1, sort_seq=100)
        wrong_ordering = self._new(limit=10)
        wrong_ordering["ordering"] = "arbitrary"
        with self.assertRaises(ToolError) as caught:
            new_result_or_error(
                _verified_response(wrong_ordering), conversation_key=CONVERSATION_KEY,
                limit=10,
            )
        self.assertEqual(json.loads(str(caught.exception))["code"],
                         "readstore_result_invalid")

    def test_new_public_rejects_reversed_multishard_rows(self):
        self._insert("first", local_id=1, sort_seq=100)
        second = sqlite3.connect(":memory:")
        try:
            second.execute(
                f'CREATE TABLE "{TABLE}" ('
                "local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
                "create_time INTEGER, message_content, server_id INTEGER, sort_seq INTEGER)"
            )
            second.execute(
                f'INSERT INTO "{TABLE}" VALUES (1, 1, 2, 123, ?, 42, 100)',
                ("second shard",),
            )
            second.commit()
            query = ReadStoreQuery(
                self.connection,
                {"message_0": self.connection, "message_1": second},
                account_epoch=ACCOUNT_EPOCH,
            )
            reversed_page = query.get_new_messages(CHAT_MD5, limit=10)
            self.assertEqual(
                [(row["shard_id"], row["message_identity"]["rowid"])
                 for row in reversed_page["items"]],
                [("message_0", 1), ("message_1", 1)],
            )
            reversed_page["items"].reverse()
            with self.assertRaises(ToolError) as caught:
                new_result_or_error(
                    _verified_response(reversed_page,
                                       message_shards=("message_0", "message_1")),
                    conversation_key=CONVERSATION_KEY, limit=10,
                )
            self.assertEqual(json.loads(str(caught.exception))["code"],
                             "readstore_result_invalid")
        finally:
            second.close()

    def test_new_public_keeps_real_multishard_empty_and_untouched_highwater(self):
        self._insert("old in first shard", local_id=1, sort_seq=100)
        second = sqlite3.connect(":memory:")
        try:
            second.execute(
                f'CREATE TABLE "{TABLE}" ('
                "local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
                "create_time INTEGER, message_content, server_id INTEGER, sort_seq INTEGER)"
            )
            second.executemany(
                f'INSERT INTO "{TABLE}" VALUES (?, 1, 2, 123, ?, 42, ?)',
                [(index, "old in second shard", index)
                 for index in range(1, 101)],
            )
            second.commit()
            query = ReadStoreQuery(
                self.connection,
                {"message_0": self.connection, "message_1": second},
                account_epoch=ACCOUNT_EPOCH,
            )
            bootstrap = query.get_new_messages(CHAT_MD5, start_from="now")
            self._insert("new in first shard", local_id=2, sort_seq=200)
            following = query.get_new_messages(
                CHAT_MD5, cursor=bootstrap["next_cursor"],
            )
            caught_up = query.get_new_messages(
                CHAT_MD5, cursor=following["next_cursor"],
            )
            historical_first = query.get_new_messages(
                CHAT_MD5, start_from="beginning", limit=2,
            )
            historical_second = query.get_new_messages(
                CHAT_MD5, cursor=historical_first["next_cursor"], limit=2,
            )
            for raw, page_limit in (
                (bootstrap, 100), (following, 100), (caught_up, 100),
                (historical_first, 2), (historical_second, 2),
            ):
                with self.subTest(count=raw["count"]):
                    projected = new_result_or_error(
                        _verified_response(raw, message_shards=("message_0", "message_1")),
                        conversation_key=CONVERSATION_KEY, limit=page_limit,
                    )["result"]
                    self.assertEqual(projected["count"], raw["count"])
            self.assertEqual((bootstrap["count"], following["count"], caught_up["count"]),
                             (0, 1, 0))
            self.assertEqual(
                following["next_cursor"]["shard_highwater"][1]["rowid"], 100,
            )
            self.assertEqual(
                [(row["shard_id"], row["message_identity"]["rowid"])
                 for row in historical_first["items"]],
                [("message_0", 1), ("message_1", 1)],
            )
        finally:
            second.close()

    def test_real_search_continuation_skips_nonmatches_without_repeating_rows(self):
        self._insert("not a match", local_id=1, sort_seq=300)
        self._insert("Needle one", local_id=2, sort_seq=200)
        self._insert("needle two", local_id=3, sort_seq=100)
        first = self._search(limit=1)
        second = self._search(limit=1, cursor=first["next_cursor"])
        self.assertEqual(first["scanned"], 2)
        self.assertTrue(first["has_more"])
        self.assertEqual(second["scanned"], 1)
        self.assertFalse(second["has_more"])
        self.assertEqual([page["items"][0]["local_id"] for page in (first, second)],
                         [2, 3])
        for page in (first, second):
            public = search_result_or_error(
                _verified_response(page), scope="messages", keyword="needle",
                conversation_key=CONVERSATION_KEY, limit=1,
            )
            self.assertEqual(public["result"]["count"], 1)

    def test_expired_search_continuation_before_first_row_raises_fixed_budget_code(self):
        self._insert("needle one", local_id=1, sort_seq=200)
        self._insert("needle two", local_id=2, sort_seq=100)
        first = self._search(limit=1)
        self.assertTrue(first["has_more"])
        original_open_streams = message_search._open_streams

        def expire_after_inventory(*args, **kwargs):
            streams, heap = original_open_streams(*args, **kwargs)
            kwargs["budget"].deadline = time.monotonic() - 1
            return streams, heap

        with mock.patch.object(message_search, "_open_streams", side_effect=expire_after_inventory):
            with self.assertRaises(ReadStoreError) as caught:
                self._search(limit=1, cursor=first["next_cursor"])
        self.assertEqual(caught.exception.code, "query_budget_exceeded")

    def test_unsupported_body_is_counted_and_not_returned_as_a_match(self):
        self._insert(b"needle", local_id=1, sort_seq=200, flag=9)
        self._insert("needle valid", local_id=2, sort_seq=100)
        raw = self._search()
        self.assertEqual((raw["count"], raw["scanned"], raw["unsupported_count"]),
                         (1, 2, 1))
        public = search_result_or_error(
            _verified_response(raw), scope="messages", keyword="needle",
            conversation_key=CONVERSATION_KEY, limit=10,
        )
        self.assertEqual(public["result"]["unsupported_count"], 1)

    def test_real_search_decodes_supported_wcdb_zstd_body(self):
        compressed = zstandard.ZstdCompressor().compress(b"Needle compressed")
        self._insert(compressed, flag=4)
        raw = self._search()
        self.assertEqual((raw["count"], raw["unsupported_count"]), (1, 0))
        public = search_result_or_error(
            _verified_response(raw), scope="messages", keyword="needle",
            conversation_key=CONVERSATION_KEY, limit=10,
        )
        self.assertEqual(public["result"]["items"][0]["content"],
                         "Needle compressed")

    def test_verified_background_rejection_keeps_fixed_code_for_search_and_new(self):
        for public in (
            lambda response: search_result_or_error(
                response, scope="messages", keyword="needle",
                conversation_key=CONVERSATION_KEY, limit=5),
            lambda response: new_result_or_error(
                response, conversation_key=CONVERSATION_KEY, limit=5),
        ):
            with self.subTest(public=public), self.assertRaises(ToolError) as caught:
                public(_background_rejection())
            self.assertEqual(json.loads(str(caught.exception)), {
                "code": "background_side_effect",
                "outcome_unknown": False,
                "retry": False,
            })

    def test_unverified_background_rejection_remains_generic(self):
        response = _background_rejection()
        response["target_validation"]["status"] = "changed"
        with self.assertRaises(ToolError) as caught:
            new_result_or_error(response, conversation_key=CONVERSATION_KEY, limit=5)
        self.assertEqual(json.loads(str(caught.exception))["code"],
                         "readstore_result_invalid")

    def test_verified_fixed_provider_error_keeps_code_for_search_and_new(self):
        for public in (
            lambda response: search_result_or_error(
                response, scope="messages", keyword="needle",
                conversation_key=CONVERSATION_KEY, limit=5),
            lambda response: new_result_or_error(
                response, conversation_key=CONVERSATION_KEY, limit=5),
        ):
            for code in ("readstore_unavailable", "query_budget_exceeded",
                         "readstore_background_failed"):
                with self.subTest(public=public, code=code), self.assertRaises(ToolError) as caught:
                    public(_verified_provider_rejection(code))
                self.assertEqual(json.loads(str(caught.exception)), {
                    "code": code,
                    "outcome_unknown": False,
                    "retry": False,
                })

    def test_unverified_or_unknown_provider_error_remains_generic(self):
        cases = []
        unknown = _verified_provider_rejection("private_provider_error")
        cases.append(unknown)
        changed = _verified_provider_rejection()
        changed["target_validation"]["status"] = "changed"
        cases.append(changed)
        unmonitored = _verified_provider_rejection()
        unmonitored["evidence"]["background_observation_passed"] = False
        cases.append(unmonitored)
        with_result = _verified_provider_rejection()
        with_result["result"] = {"private": "do not return"}
        cases.append(with_result)
        for response in cases:
            with self.subTest(response=response), self.assertRaises(ToolError) as caught:
                search_result_or_error(
                    response, scope="messages", keyword="needle",
                    conversation_key=CONVERSATION_KEY, limit=5)
            self.assertEqual(json.loads(str(caught.exception))["code"],
                             "readstore_result_invalid")

    def test_background_preflight_code_requires_complete_safe_evidence(self):
        unsafe_responses = []
        missing_monitor = _verified_provider_rejection("readstore_background_failed")
        missing_monitor["evidence"].pop("observations")
        unsafe_responses.append(missing_monitor)
        uncertain = _verified_provider_rejection("readstore_background_failed")
        uncertain["error"]["outcome_unknown"] = True
        unsafe_responses.append(uncertain)
        failed_cleanup = _verified_provider_rejection("readstore_background_failed")
        failed_cleanup["cleanup"]["status"] = "recovery_unknown"
        unsafe_responses.append(failed_cleanup)
        for response in unsafe_responses:
            for public in (
                lambda value: search_result_or_error(
                    value, scope="messages", keyword="needle",
                    conversation_key=CONVERSATION_KEY, limit=5),
                lambda value: new_result_or_error(
                    value, conversation_key=CONVERSATION_KEY, limit=5),
            ):
                with self.subTest(response=response, public=public), self.assertRaises(ToolError) as caught:
                    public(response)
                self.assertEqual(json.loads(str(caught.exception))["code"],
                                 "readstore_result_invalid")


if __name__ == "__main__":
    unittest.main()
