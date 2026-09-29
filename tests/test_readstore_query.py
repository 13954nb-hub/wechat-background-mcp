"""只讀微信 SQLite 查詢核心的 fixture 驅動測試。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import unittest


from wxbg.readstore_query import (
    InvalidQuery,
    ReadStoreError,
    ReadStoreQuery,
    UnsupportedSchema,
)


CHAT = "alice@example"
CHAT_MD5 = hashlib.md5(CHAT.encode("utf-8")).hexdigest()
TABLE = f"Msg_{CHAT_MD5}"
ACCOUNT_EPOCH = "account-epoch-1"


def make_session_connection(*, complete: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    columns = """
        username TEXT NOT NULL,
        unread_count INTEGER,
        summary TEXT,
        last_timestamp INTEGER,
        is_hidden INTEGER NOT NULL DEFAULT 0,
        sort_timestamp INTEGER
    """
    if complete:
        conn.execute(f"CREATE TABLE SessionTable ({columns})")
        conn.executemany(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("hidden", 99, "hidden row", 99, 1, 99),
                ("older", 0, "old", 100, 0, 100),
                ("newer", 3, "preview", 200, 0, 200),
            ],
        )
    else:
        conn.execute(
            "CREATE TABLE SessionTable (username TEXT, unread_count INTEGER)"
        )
    conn.commit()
    return conn


def make_message_connection(*, include_table: bool = True, complete: bool = True):
    conn = sqlite3.connect(":memory:")
    if include_table:
        columns = """
            local_id INTEGER,
            local_type INTEGER,
            real_sender_id INTEGER,
            create_time INTEGER,
            message_content,
            server_id INTEGER,
            sort_seq INTEGER
        """
        if not complete:
            columns = "local_id INTEGER, sort_seq INTEGER"
        conn.execute(f'CREATE TABLE "{TABLE}" ({columns})')
    conn.commit()
    return conn


def add_message(conn, local_id, sort_seq, content, *, server_id=1, sender_id=2):
    conn.execute(
        f'''INSERT INTO "{TABLE}"
            (local_id, local_type, real_sender_id, create_time,
             message_content, server_id, sort_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (local_id, 1, sender_id, sort_seq, content, server_id, sort_seq),
    )
    conn.commit()


class ReadStoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.session = make_session_connection()
        self.shard_a = make_message_connection()
        self.shard_b = make_message_connection()
        add_message(self.shard_a, 1, 10, "a same sequence", server_id=0)
        add_message(self.shard_a, 2, 8, "100%_ literal")
        add_message(self.shard_a, 3, 7, "100Xfoo")
        add_message(self.shard_b, 1, 10, "b same sequence", server_id=0)
        add_message(self.shard_b, 4, 9, "newer shard")
        self.store = ReadStoreQuery(
            self.session,
            {"shard-a": self.shard_a, "shard-b": self.shard_b},
            account_epoch=ACCOUNT_EPOCH,
        )

    def tearDown(self):
        self.session.close()
        self.shard_a.close()
        self.shard_b.close()

    def test_sessions_keep_unread_summary_and_last_timestamp_independent(self):
        rows = self.store.get_sessions(limit=10)
        self.assertEqual([row["username"] for row in rows], ["newer", "older"])
        self.assertEqual(rows[0]["unread_count"], 3)
        self.assertEqual(rows[0]["summary"], "preview")
        self.assertEqual(rows[0]["last_timestamp"], 200)
        self.assertNotIn("hidden", {row["username"] for row in rows})

    def test_same_sort_sequence_pages_without_dropping_rows(self):
        first = self.store.get_messages(CHAT_MD5, limit=1)
        self.assertEqual(first["count"], 1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["items"][0]["sort_seq"], 10)
        second = self.store.get_messages(
            CHAT_MD5, cursor=first["next_cursor"], limit=1
        )
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["items"][0]["sort_seq"], 10)
        self.assertNotEqual(
            first["items"][0]["message_identity"],
            second["items"][0]["message_identity"],
        )
        self.assertEqual(
            {item["shard_id"] for item in first["items"] + second["items"]},
            {"shard-a", "shard-b"},
        )

    def test_server_zero_does_not_collapse_shard_namespaced_messages(self):
        page = self.store.get_messages(CHAT_MD5, limit=10)
        zero_server = [item for item in page["items"] if item["server_id"] == 0]
        self.assertEqual(len(zero_server), 2)
        self.assertEqual(
            {tuple(item["message_identity"].values()) for item in zero_server},
            {
                (CHAT_MD5, "shard-a", 1, 0, 1),
                (CHAT_MD5, "shard-b", 1, 0, 1),
            },
        )

    def test_search_treats_percent_and_underscore_as_literal(self):
        page = self.store.search_messages(CHAT_MD5, "%_", limit=10)
        self.assertEqual(page["count"], 1)
        self.assertEqual(page["items"][0]["content"], "100%_ literal")

    def test_start_from_now_bootstraps_and_continues_without_a_message_table(self):
        missing = make_message_connection(include_table=False)
        store = ReadStoreQuery(
            self.session, {"only": missing}, account_epoch=ACCOUNT_EPOCH
        )
        try:
            page = store.get_new_messages(
                CHAT_MD5, limit=2, start_from="now"
            )
            self.assertEqual(page["items"], [])
            self.assertFalse(page["has_more"])
            self.assertEqual(page["next_cursor"]["filter"], "start_from:now")
            self.assertEqual(
                page["next_cursor"]["shard_highwater"],
                [{"shard_id": "only", "sort_seq": None,
                  "local_id": None, "rowid": None}],
            )
            again = store.get_new_messages(
                CHAT_MD5, limit=2, cursor=page["next_cursor"]
            )
            self.assertEqual(again["items"], [])
            self.assertEqual(again["next_cursor"], page["next_cursor"])
            with self.assertRaises(UnsupportedSchema):
                store.get_new_messages(CHAT_MD5, limit=2, start_from="beginning")
        finally:
            missing.close()

    def test_incremental_read_uses_rowid_cursor_for_late_lower_sort_seq_rows(self):
        page = self.store.get_new_messages(CHAT_MD5, limit=2)
        self.assertEqual(
            [(item["sort_seq"], item["shard_id"]) for item in page["items"]],
            [(10, "shard-a"), (10, "shard-b")],
        )
        self.assertEqual(page["ordering"], "rowid_asc_per_shard_merge")
        next_page = self.store.get_new_messages(
            CHAT_MD5, cursor=page["next_cursor"], limit=2
        )
        self.assertEqual(
            [(item["sort_seq"], item["shard_id"]) for item in next_page["items"]],
            [(8, "shard-a"), (9, "shard-b")],
        )
        self.assertEqual(next_page["ordering"], "rowid_asc_per_shard_merge")

    def test_incremental_caught_up_keeps_cursor_for_next_round(self):
        page = self.store.get_new_messages(CHAT_MD5, limit=10)
        self.assertFalse(page["has_more"])
        self.assertIsNotNone(page["next_cursor"])

        add_message(self.shard_b, 5, 11, "arrived after caught up")
        next_page = self.store.get_new_messages(
            CHAT_MD5, cursor=page["next_cursor"], limit=10
        )
        self.assertEqual(
            [(item["sort_seq"], item["content"]) for item in next_page["items"]],
            [(11, "arrived after caught up")],
        )

    def test_incremental_empty_caught_up_preserves_existing_cursor(self):
        page = self.store.get_new_messages(CHAT_MD5, limit=10)
        again = self.store.get_new_messages(
            CHAT_MD5, cursor=page["next_cursor"], limit=10
        )
        self.assertFalse(again["has_more"])
        self.assertEqual(again["items"], [])
        self.assertEqual(again["next_cursor"], page["next_cursor"])

    def test_cursor_strict_version_and_context_binding(self):
        page = self.store.get_messages(CHAT_MD5, limit=1)

        bad_version = dict(page["next_cursor"])
        bad_version["version"] = True
        with self.assertRaises(InvalidQuery):
            self.store.get_messages(CHAT_MD5, cursor=bad_version, limit=1)

        search_page = self.store.search_messages(CHAT_MD5, "", limit=1)
        with self.assertRaises(InvalidQuery):
            self.store.search_messages(
                CHAT_MD5, "different", cursor=search_page["next_cursor"], limit=1
            )

        other_epoch = ReadStoreQuery(
            self.session,
            {"shard-a": self.shard_a, "shard-b": self.shard_b},
            account_epoch="account-epoch-2",
        )
        with self.assertRaises(InvalidQuery):
            other_epoch.get_messages(
                CHAT_MD5, cursor=page["next_cursor"], limit=1
            )

        other_shards = ReadStoreQuery(
            self.session, {"shard-a": self.shard_a}, account_epoch=ACCOUNT_EPOCH
        )
        with self.assertRaises(InvalidQuery):
            other_shards.get_messages(
                CHAT_MD5, cursor=page["next_cursor"], limit=1
            )

        without_epoch = ReadStoreQuery(
            self.session, {"shard-a": self.shard_a, "shard-b": self.shard_b}
        )
        with self.assertRaises(InvalidQuery):
            without_epoch.get_messages(CHAT_MD5, limit=1)

    def test_cursor_filter_binding_covers_since_seq(self):
        page = self.store.get_new_messages(CHAT_MD5, since_seq=7, limit=2)
        next_page = self.store.get_new_messages(
            CHAT_MD5, cursor=page["next_cursor"], limit=2
        )
        self.assertEqual(
            [(item["sort_seq"], item["shard_id"]) for item in next_page["items"]],
            [(8, "shard-a"), (9, "shard-b")],
        )
        self.assertTrue(
            all(item["sort_seq"] > 7 for item in page["items"] + next_page["items"])
        )
        with self.assertRaises(InvalidQuery):
            self.store.search_messages(
                CHAT_MD5, "since_seq:7", cursor=page["next_cursor"], limit=2
            )

    def test_full_text_search_reports_bounded_text_only_coverage(self):
        long_text = "x" * 5000 + "NEEDLE"
        add_message(self.shard_a, 50, 50, long_text)
        add_message(self.shard_a, 51, 51, b"NEEDLE in an opaque blob")
        page = self.store.search_messages(CHAT_MD5, "NEEDLE", limit=10)
        self.assertEqual(page["coverage"], "text_rows_full_content_only_bounded")
        self.assertEqual(
            page["unsupported_content"],
            "blob_or_oversized_text_or_compressed",
        )
        self.assertEqual(
            [(item["local_id"], item["content_truncated"]) for item in page["items"]],
            [(50, True)],
        )

    def test_message_content_is_raw_byte_bounded_in_sql(self):
        traces = []
        self.shard_a.set_trace_callback(traces.append)
        add_message(self.shard_a, 60, 60, "z" * 100_000)
        page = self.store.get_messages(CHAT_MD5, limit=1)
        self.assertTrue(any("substr(CAST(message_content AS BLOB)" in t for t in traces))
        self.assertLessEqual(len(page["items"][0]["content"]), 4096)
        self.assertTrue(page["items"][0]["content_truncated"])

    def test_session_summary_is_raw_byte_bounded_in_sql(self):
        traces = []
        self.session.set_trace_callback(traces.append)
        self.session.execute(
            "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
            ("huge", 1, "s" * 100_000, 300, 0, 300),
        )
        self.session.commit()
        rows = self.store.get_sessions(limit=1)
        self.assertTrue(any("substr(CAST(summary AS BLOB)" in t for t in traces))
        self.assertLessEqual(len(rows[0]["summary"]), 4096)
        self.assertTrue(rows[0]["summary_truncated"])

    def test_sqlite_progress_budget_has_fixed_error(self):
        limited = ReadStoreQuery(
            self.session,
            {"shard-a": self.shard_a},
            account_epoch=ACCOUNT_EPOCH,
            max_sql_steps=1,
        )
        with self.assertRaises(ReadStoreError) as caught:
            limited.get_messages(CHAT_MD5, limit=1)
        self.assertEqual(caught.exception.code, "query_budget_exceeded")

    def test_expired_deadline_has_fixed_error(self):
        with self.assertRaises(ReadStoreError) as caught:
            self.store.get_sessions(deadline=time.monotonic() - 1)
        self.assertEqual(caught.exception.code, "query_budget_exceeded")

    def test_invalid_md5_and_limits_are_rejected(self):
        with self.assertRaises(InvalidQuery):
            self.store.get_messages("not-a-md5", limit=1)
        with self.assertRaises(InvalidQuery):
            self.store.get_messages(CHAT_MD5, limit=0)
        with self.assertRaises(InvalidQuery):
            self.store.search_messages(CHAT_MD5, "x", limit=101)

    def test_missing_schema_is_explicitly_unsupported(self):
        with self.assertRaises(UnsupportedSchema) as session_error:
            ReadStoreQuery(make_session_connection(complete=False)).get_sessions()
        self.assertEqual(session_error.exception.code, "unsupported")

        missing_table = make_message_connection(include_table=False)
        with self.assertRaises(UnsupportedSchema) as message_error:
            ReadStoreQuery(
                self.session,
                {"only": missing_table},
                account_epoch=ACCOUNT_EPOCH,
            ).get_messages(CHAT_MD5, limit=1)
        self.assertEqual(message_error.exception.code, "unsupported")
        missing_table.close()

    def test_query_never_writes_to_any_connection(self):
        traces = []
        for conn in (self.session, self.shard_a, self.shard_b):
            conn.set_trace_callback(traces.append)
        before = [list(conn.iterdump()) for conn in (self.session, self.shard_a, self.shard_b)]
        before_changes = [conn.total_changes for conn in (self.session, self.shard_a, self.shard_b)]

        self.store.get_sessions()
        self.store.search_messages(CHAT_MD5, "literal")
        self.store.get_messages(CHAT_MD5, limit=2)
        self.store.get_new_messages(CHAT_MD5, limit=2)

        after = [list(conn.iterdump()) for conn in (self.session, self.shard_a, self.shard_b)]
        after_changes = [conn.total_changes for conn in (self.session, self.shard_a, self.shard_b)]
        self.assertEqual(before, after)
        self.assertEqual(before_changes, after_changes)
        self.assertFalse(
            any(
                statement.lstrip().upper().startswith(
                    ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REINDEX")
                )
                for statement in traces
            )
        )

    def test_unavailable_compressed_body_is_explicit(self):
        compressed = make_message_connection()
        compressed.execute(
            f'''INSERT INTO "{TABLE}"
                (local_id, local_type, real_sender_id, create_time,
                 message_content, server_id, sort_seq)
                VALUES (99, 1, 2, 99, ?, 0, 99)''',
            (b"\x28\xb5\x2f\xfdcompressed",),
        )
        compressed.commit()
        page = ReadStoreQuery(
            self.session,
            {"compressed": compressed},
            account_epoch=ACCOUNT_EPOCH,
        ).get_messages(CHAT_MD5, limit=1)
        item = page["items"][0]
        self.assertFalse(item["content_available"])
        self.assertIsNone(item["content"])
        compressed.close()


if __name__ == "__main__":
    unittest.main()
