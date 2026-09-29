"""只讀 Inbox SQLite 查詢核心的自建 fixture 測試。"""

from __future__ import annotations

import sqlite3
import sys
import time
import unittest
import json
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import wxbg.readstore_inbox as inbox  # noqa: E402
from wxbg.readstore_inbox import InboxReadError, read_inbox  # noqa: E402


EPOCH = "account-epoch-1"


def make_session(*, include_columns: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    if include_columns:
        connection.execute(
            """
            CREATE TABLE SessionTable (
                username TEXT PRIMARY KEY,
                unread_count INTEGER,
                summary TEXT,
                last_timestamp INTEGER,
                sort_timestamp INTEGER,
                is_hidden INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO SessionTable
                (username, unread_count, summary, last_timestamp,
                 sort_timestamp, is_hidden)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("alice", 2, "Alice summary", 10, 100, 0),
                ("bob", 0, "Bob summary", 20, 100, 0),
                ("carol", None, "Carol summary", 30, 90, 0),
                ("dave", "unknown", "Unknown count", 40, 80, 0),
                ("hidden", 99, "Hidden", 50, 999, 1),
            ],
        )
    else:
        connection.execute("CREATE TABLE SessionTable (username TEXT)")
    connection.commit()
    return connection


def make_contacts(*, include_columns: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    if include_columns:
        connection.execute(
            "CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT)"
        )
        connection.executemany(
            "INSERT INTO contact (username, remark, nick_name) VALUES (?, ?, ?)",
            [
                ("alice", "Alice remark", "Alice nick"),
                ("bob", "", "Bob nick"),
                ("carol", None, "Carol nick"),
                ("same-a", "Same display", "A nick"),
                ("same-b", "Same display", "B nick"),
            ],
        )
    else:
        connection.execute("CREATE TABLE contact (username TEXT)")
    connection.commit()
    return connection


def call(session, contacts, **kwargs):
    kwargs.setdefault("account_epoch", EPOCH)
    kwargs.setdefault("deadline", time.monotonic() + 2)
    return read_inbox(session, contacts, **kwargs)


class ReadstoreInboxTests(unittest.TestCase):
    def setUp(self):
        self.session = make_session()
        self.contacts = make_contacts()
        self.addCleanup(self.session.close)
        self.addCleanup(self.contacts.close)

    def test_sorted_rows_have_database_key_and_contact_fallbacks(self):
        result = call(self.session, self.contacts, limit=4, unread_only=False)

        self.assertEqual(
            [row["conversation_key"] for row in result["conversations"]],
            ["alice", "bob", "carol", "dave"],
        )
        self.assertEqual(result["conversations"][0]["display_name"], "Alice remark")
        self.assertEqual(result["conversations"][0]["display_source"], "remark")
        self.assertEqual(result["conversations"][1]["display_name"], "Bob nick")
        self.assertEqual(result["conversations"][1]["display_source"], "nick_name")
        self.assertEqual(result["conversations"][2]["display_name"], "Carol nick")
        self.assertEqual(result["conversations"][3]["display_name"], "dave")
        self.assertEqual(result["conversations"][3]["display_source"], "username")
        self.assertEqual(result["coverage"], "sessiontable_contact_bounded_nonhidden")
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def test_unread_filter_uses_independent_integer_count_and_preserves_unknown(self):
        unread = call(self.session, self.contacts, unread_only=True, limit=10)
        self.assertEqual(
            [row["conversation_key"] for row in unread["conversations"]],
            ["alice"],
        )
        self.assertEqual(unread["conversations"][0]["unread_count"], 2)
        self.assertTrue(unread["conversations"][0]["unread_known"])

        all_rows = call(self.session, self.contacts, unread_only=False, limit=10)
        dave = next(row for row in all_rows["conversations"]
                    if row["conversation_key"] == "dave")
        self.assertIsNone(dave["unread_count"])
        self.assertFalse(dave["unread_known"])

    def test_include_hidden_returns_every_session_row_with_typed_state(self):
        self.session.execute(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
            ("unknown-state", 0, "Unknown visibility", 60, 1000, "unknown"),
        )
        self.session.commit()

        result = call(self.session, self.contacts, limit=10,
                      unread_only=False, include_hidden=True)

        rows = {row["conversation_key"]: row for row in result["conversations"]}
        self.assertEqual(set(rows), {
            "alice", "bob", "carol", "dave", "hidden", "unknown-state",
        })
        self.assertIs(rows["alice"]["is_hidden"], False)
        self.assertIs(rows["hidden"]["is_hidden"], True)
        self.assertIsNone(rows["unknown-state"]["is_hidden"])
        self.assertEqual(result["coverage"], "sessiontable_contact_bounded_all")

        legacy = call(self.session, self.contacts, limit=10, unread_only=False)
        self.assertEqual(len(legacy["conversations"]), 4)
        self.assertNotIn("is_hidden", legacy["conversations"][0])
        self.assertEqual(legacy["coverage"],
                         "sessiontable_contact_bounded_nonhidden")

    def test_include_hidden_cursor_is_v2_and_rejects_cross_mode_reuse(self):
        first = call(self.session, self.contacts, limit=1,
                     unread_only=False, include_hidden=True)
        self.assertEqual(first["conversations"][0]["conversation_key"], "hidden")
        cursor = first["next_cursor"]
        self.assertEqual(cursor["version"], 2)
        self.assertIs(cursor["include_hidden"], True)

        second = call(self.session, self.contacts, limit=1,
                      unread_only=False, include_hidden=True, cursor=cursor)
        self.assertEqual(second["conversations"][0]["conversation_key"], "alice")

        with self.assertRaisesRegex(InboxReadError, "invalid_cursor"):
            call(self.session, self.contacts, limit=1, unread_only=False,
                 cursor=cursor)
        legacy = call(self.session, self.contacts, limit=1, unread_only=False)
        with self.assertRaisesRegex(InboxReadError, "invalid_cursor"):
            call(self.session, self.contacts, limit=1,
                 unread_only=False, include_hidden=True,
                 cursor=legacy["next_cursor"])

    def test_include_hidden_requires_strict_bool(self):
        with self.assertRaisesRegex(InboxReadError, "invalid_include_hidden"):
            call(self.session, self.contacts, include_hidden=1)

    def test_include_hidden_bounds_unknown_visibility_in_sql(self):
        traces = []
        self.session.execute(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
            ("opaque-state", 0, "Opaque state", 60, 1000,
             sqlite3.Binary(b"x" * 100_000)),
        )
        self.session.commit()
        self.session.set_trace_callback(traces.append)

        result = call(self.session, self.contacts, limit=10,
                      unread_only=False, include_hidden=True)

        opaque = next(row for row in result["conversations"]
                      if row["conversation_key"] == "opaque-state")
        self.assertIsNone(opaque["is_hidden"])
        self.assertTrue(any(
            "CASE WHEN typeof(s.is_hidden)='integer'" in statement
            for statement in traces
        ))

    def test_non_text_username_in_limit_plus_one_row_fails_closed(self):
        self.session.execute("DELETE FROM SessionTable WHERE username=?", ("bob",))
        self.session.execute("DELETE FROM SessionTable WHERE username=?", ("hidden",))
        self.session.execute(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
            (sqlite3.Binary(b"blob-user"), 0, "Blob key", 20, 100, 0),
        )
        self.session.commit()

        for include_hidden in (False, True):
            with self.subTest(include_hidden=include_hidden):
                with self.assertRaisesRegex(InboxReadError, "^unsupported_schema$"):
                    call(self.session, self.contacts, limit=1, unread_only=False,
                         include_hidden=include_hidden)

    def test_limit_plus_one_and_same_timestamp_cursor_keep_rows(self):
        first = call(self.session, self.contacts, limit=1, unread_only=False)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["conversations"][0]["conversation_key"], "alice")
        self.assertEqual(
            set(first["next_cursor"]),
            {"version", "account_epoch", "unread_only", "order",
             "sort_timestamp", "username"},
        )

        second = call(
            self.session,
            self.contacts,
            limit=1,
            unread_only=False,
            cursor=first["next_cursor"],
        )
        self.assertEqual(second["conversations"][0]["conversation_key"], "bob")
        self.assertFalse(second["conversations"][0]["conversation_key"] == "alice")

    def test_same_display_name_is_not_merged(self):
        self.session.executemany(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
            [("same-a", 0, "a", 1, 70, 0), ("same-b", 0, "b", 2, 60, 0)],
        )
        self.session.commit()
        result = call(self.session, self.contacts, limit=10, unread_only=False)
        same = [row for row in result["conversations"]
                if row["display_name"] == "Same display"]
        self.assertEqual(
            [row["conversation_key"] for row in same], ["same-a", "same-b"]
        )

    def test_duplicate_contact_username_is_an_error(self):
        self.contacts.execute(
            "INSERT INTO contact (username, remark, nick_name) VALUES (?, ?, ?)",
            ("alice", "second", "second nick"),
        )
        self.contacts.commit()
        with self.assertRaises(InboxReadError) as caught:
            call(self.session, self.contacts, limit=1)
        self.assertEqual(caught.exception.code, "ambiguous_contact_username")

    def test_cursor_is_strictly_bound_and_rejects_bool_version(self):
        page = call(self.session, self.contacts, limit=1, unread_only=False)
        cursor = page["next_cursor"]

        bad_version = dict(cursor)
        bad_version["version"] = True
        with self.assertRaises(InboxReadError) as caught:
            call(self.session, self.contacts, limit=1, unread_only=False,
                 cursor=bad_version)
        self.assertEqual(caught.exception.code, "invalid_cursor")

        for field, value in (
            ("account_epoch", "other-account"),
            ("unread_only", True),
            ("order", "username_asc"),
            ("sort_timestamp", True),
        ):
            with self.subTest(field=field):
                bad = dict(cursor)
                bad[field] = value
                with self.assertRaisesRegex(InboxReadError, "invalid_cursor"):
                    call(self.session, self.contacts, limit=1,
                         unread_only=False, cursor=bad)

    def test_huge_summary_and_contact_text_are_bounded_before_decode(self):
        traces = []
        self.session.set_trace_callback(traces.append)
        self.contacts.set_trace_callback(traces.append)
        self.session.execute(
            "UPDATE SessionTable SET summary=? WHERE username=?",
            ("s" * 100_000, "alice"),
        )
        self.contacts.execute(
            "UPDATE contact SET remark=? WHERE username=?",
            ("r" * 100_000, "alice"),
        )
        self.session.commit()
        self.contacts.commit()

        result = call(self.session, self.contacts, limit=1, unread_only=False)
        row = result["conversations"][0]
        self.assertLessEqual(len(row["summary"]), 4096)
        self.assertTrue(row["summary_truncated"])
        self.assertLessEqual(len(row["display_name"]), 4096)
        self.assertTrue(row["display_truncated"])
        self.assertTrue(any("substr(CAST(s.summary AS BLOB)" in item for item in traces))
        self.assertTrue(any("substr(CAST(c.remark AS BLOB)" in item for item in traces))

    def test_multibyte_json_budget_shortens_page_and_cursor_uses_last_returned(self):
        rows = [
            (f"wide-{index:03d}", 0, "界" * 4096, index, 1_000 - index, 0)
            for index in range(25)
        ]
        self.session.executemany(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)", rows
        )
        self.contacts.executemany(
            "INSERT INTO contact (username, remark, nick_name) VALUES (?, ?, ?)",
            [(row[0], "😀" * 4096, "unused") for row in rows],
        )
        self.session.commit()
        self.contacts.commit()

        result = call(self.session, self.contacts, limit=100, unread_only=False)
        encoded = json.dumps(result, ensure_ascii=True).encode("utf-8")
        self.assertLessEqual(len(encoded), inbox.MAX_RESULT_BYTES)
        self.assertTrue(result["has_more"])
        self.assertLess(len(result["conversations"]), 25)
        self.assertEqual(
            result["next_cursor"]["username"],
            result["conversations"][-1]["conversation_key"],
        )
        seen = []
        page = result
        for _ in range(10):
            seen.extend(row["conversation_key"] for row in page["conversations"])
            if not page["has_more"]:
                break
            page = call(
                self.session,
                self.contacts,
                limit=100,
                unread_only=False,
                cursor=page["next_cursor"],
            )
        self.assertEqual(len(seen), 29)
        self.assertEqual(len(set(seen)), 29)

    def test_single_row_that_cannot_fit_has_fixed_error(self):
        with patch.object(inbox, "MAX_RESULT_BYTES", 1):
            with self.assertRaisesRegex(InboxReadError, "row_too_large"):
                call(self.session, self.contacts, limit=1, unread_only=False)

    def test_missing_columns_are_explicitly_unsupported(self):
        bad_session = make_session(include_columns=False)
        bad_contacts = make_contacts(include_columns=False)
        self.addCleanup(bad_session.close)
        self.addCleanup(bad_contacts.close)

        with self.assertRaisesRegex(InboxReadError, "unsupported_schema"):
            call(bad_session, self.contacts)
        with self.assertRaisesRegex(InboxReadError, "unsupported_schema"):
            call(self.session, bad_contacts)

    def test_expired_deadline_and_vm_budget_have_fixed_errors(self):
        with self.assertRaisesRegex(InboxReadError, "query_budget_exceeded"):
            read_inbox(
                self.session,
                self.contacts,
                account_epoch=EPOCH,
                deadline=time.monotonic() - 1,
            )

        with patch.object(inbox, "MAX_SQL_STEPS", 1):
            with self.assertRaisesRegex(InboxReadError, "query_budget_exceeded"):
                call(self.session, self.contacts, limit=1)

    def test_read_only_authorizer_and_trace_show_no_writes_or_attach(self):
        traces = []
        for connection in (self.session, self.contacts):
            connection.set_trace_callback(traces.append)
            connection.set_authorizer(self._read_only_authorizer)
        before = [list(connection.iterdump())
                  for connection in (self.session, self.contacts)]
        before_changes = [connection.total_changes
                          for connection in (self.session, self.contacts)]

        call(self.session, self.contacts, limit=2, unread_only=False)

        after = [list(connection.iterdump())
                 for connection in (self.session, self.contacts)]
        after_changes = [connection.total_changes
                         for connection in (self.session, self.contacts)]
        self.assertEqual(before, after)
        self.assertEqual(before_changes, after_changes)
        self.assertFalse(any("ATTACH" in statement.upper() for statement in traces))
        self.assertFalse(any("DRAFT" in statement.upper() for statement in traces))
        self.assertFalse(any(
            statement.lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REINDEX")
            )
            for statement in traces
        ))

    @staticmethod
    def _read_only_authorizer(action, arg1, arg2, _database, _source):
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        }
        if action in allowed:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "table_info":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def test_input_types_are_bounded(self):
        for limit in (True, 0, 101, "2"):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(InboxReadError, "invalid_limit"):
                    call(self.session, self.contacts, limit=limit)
        with self.assertRaisesRegex(InboxReadError, "invalid_unread_only"):
            call(self.session, self.contacts, unread_only=1)
        with self.assertRaisesRegex(InboxReadError, "invalid_account_epoch"):
            read_inbox(
                self.session,
                self.contacts,
                account_epoch=True,
                deadline=time.monotonic() + 1,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
