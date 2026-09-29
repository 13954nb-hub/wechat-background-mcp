"""Synthetic, offline regression tests for fixed read-store error categories."""

from contextlib import contextmanager, ExitStack
import hashlib
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg import readstore_provider
from wxbg.readstore_batch_provider import run_batch_read
from wxbg.readstore_capture import CaptureError
from wxbg.readstore_database import DatabaseError, _BudgetConnection
from wxbg.readstore_account import AccountLookupError
from wxbg.readstore_keys import KeyLookupError
from wxbg.readstore_locations import LocationError
from wxbg.readstore_message_identity import IdentityError, resolve_conversation
from wxbg.readstore_process import ProcessReaderError
from wxbg.readstore_provider import ProviderError
from wxbg.readstore_query import ReadStoreError, _QueryBudget as MessageQueryBudget


ROOM = "synthetic-test@chatroom"
EPOCH = "synthetic-epoch"


def _session_connection(*, persistent_steps):
    connection = sqlite3.connect(":memory:", factory=_BudgetConnection)
    connection.execute("CREATE TABLE SessionTable (username TEXT, is_hidden INTEGER)")
    connection.execute("INSERT INTO SessionTable VALUES (?, 0)", (ROOM,))
    connection.commit()
    connection.install_budget(persistent_steps, time.monotonic() + 10)
    return connection


def _request():
    return {
        "account_epoch": EPOCH,
        "conversations": [{"conversation_key": ROOM, "cursor": None,
                           "start_from": "beginning", "limit": 1}],
        "max_total": 1,
    }


@contextmanager
def _fake_open_stores(connection):
    yield {"connections": {"session": connection},
           "account_epoch": EPOCH, "message_shard_ids": [], "evidence": {}}


@contextmanager
def _synthetic_open_stages():
    @contextmanager
    def fake_reader(target, *, deadline):
        yield SimpleNamespace(read=lambda *_: None, module_base=1,
                              module_size=1, regions=lambda: [])

    account = SimpleNamespace(username="synthetic", cfg_pointer=1)
    storage = SimpleNamespace(path=Path("/synthetic"), account_id="a",
                              storage_id="s")
    capture = SimpleNamespace(database=b"0" * 4096, wal=b"", wal_index=b"",
                              evidence={})
    with ExitStack() as stack:
        stack.enter_context(patch.object(readstore_provider, "ProcessReader", fake_reader))
        stack.enter_context(patch.object(readstore_provider, "locate_account",
                                         return_value=account))
        stack.enter_context(patch.object(readstore_provider, "discover_roots",
                                         return_value=[]))
        stack.enter_context(patch.object(readstore_provider, "select_storage",
                                         return_value=storage))
        stack.enter_context(patch.object(readstore_provider, "_file_identity",
                                         side_effect=lambda path: (
                                             1, 1 if "session" in str(path) else 2)))
        stack.enter_context(patch.object(readstore_provider, "capture_quiet",
                                         return_value=capture))
        yield


def _open_stage_error(stage, error):
    with _synthetic_open_stages(), ExitStack() as stack:
        if stage == "open_snapshot":
            stack.enter_context(patch.object(readstore_provider, "find_database_keys",
                                             return_value=({"session": b"x",
                                                            "contact": b"y"}, {})))
        stack.enter_context(patch.object(readstore_provider, stage,
                                         side_effect=error))
        try:
            with readstore_provider.open_stores(
                    None, deadline=time.monotonic() + 5,
                    include_messages=False):
                raise AssertionError("synthetic stage must fail")
        except ProviderError as caught:
            return caught.code


class ReadstoreErrorClassificationTests(unittest.TestCase):
    def test_known_open_stage_deadlines_are_not_masked_as_unavailable(self):
        cases = (
            ("ProcessReader", ProcessReaderError("deadline")),
            ("locate_account", AccountLookupError("account_deadline")),
            ("discover_roots", LocationError("storage_deadline")),
            ("find_database_keys", KeyLookupError("key_deadline")),
            ("open_snapshot", DatabaseError("database_deadline")),
        )
        for stage, error in cases:
            with self.subTest(stage=stage):
                self.assertEqual(_open_stage_error(stage, error), "readstore_deadline")

    def test_other_open_stage_errors_remain_generic(self):
        cases = (
            ("ProcessReader", ProcessReaderError("access_denied")),
            ("locate_account", AccountLookupError("account_unavailable")),
            ("discover_roots", LocationError("storage_unavailable")),
            ("find_database_keys", KeyLookupError("key_unavailable")),
            ("open_snapshot", DatabaseError("database_open_failed")),
        )
        for stage, error in cases:
            with self.subTest(stage=stage):
                self.assertEqual(_open_stage_error(stage, error), "readstore_unavailable")

    def test_ample_persistent_budget_keeps_identity_successful(self):
        connection = _session_connection(persistent_steps=100000)
        try:
            self.assertEqual(
                resolve_conversation(connection, ROOM, deadline=time.monotonic() + 5),
                hashlib.md5(ROOM.encode()).hexdigest(),
            )
        finally:
            connection.close()

    def test_persistent_sqlite_step_budget_reports_query_budget(self):
        connection = _session_connection(persistent_steps=1)
        try:
            with self.assertRaises(IdentityError) as caught:
                resolve_conversation(connection, ROOM, deadline=time.monotonic() + 5)
            self.assertEqual(caught.exception.code, "query_budget_exceeded")
        finally:
            connection.close()

    def test_message_query_sees_persistent_sqlite_budget_exhaustion(self):
        connection = _session_connection(persistent_steps=1)
        try:
            budget = MessageQueryBudget(100000, time.monotonic() + 5)
            with self.assertRaises(ReadStoreError) as caught:
                budget.fetchall(connection, "SELECT username FROM SessionTable")
            self.assertEqual(caught.exception.code, "query_budget_exceeded")
        finally:
            connection.close()

    def test_message_cursor_open_sees_persistent_sqlite_budget_exhaustion(self):
        connection = _session_connection(persistent_steps=1)
        try:
            budget = MessageQueryBudget(100000, time.monotonic() + 5)
            with self.assertRaises(ReadStoreError) as caught:
                budget.cursor(connection, "SELECT username FROM SessionTable")
            self.assertEqual(caught.exception.code, "query_budget_exceeded")
        finally:
            connection.close()

    def test_message_cursor_fetch_sees_persistent_sqlite_budget_exhaustion(self):
        connection = _session_connection(persistent_steps=100000)
        cursor = None
        try:
            budget = MessageQueryBudget(100000, time.monotonic() + 5)
            cursor = budget.cursor(
                connection,
                "SELECT username FROM SessionTable UNION ALL "
                "SELECT username FROM SessionTable",
            )
            connection._remaining_steps = 1
            with self.assertRaises(ReadStoreError) as caught:
                cursor.fetchone()
            self.assertEqual(caught.exception.code, "query_budget_exceeded")
        finally:
            if cursor is not None:
                cursor.close()
            connection.close()

    def test_batch_preserves_known_identity_budget_code(self):
        connection = _session_connection(persistent_steps=1)
        try:
            with self.assertRaises(ProviderError) as caught:
                run_batch_read(_request(), target=None, deadline=time.monotonic() + 5,
                               open_stores=lambda *args, **kwargs: _fake_open_stores(connection))
            self.assertEqual(caught.exception.code, "query_budget_exceeded")
        finally:
            connection.close()

    def test_batch_preserves_known_query_budget_code(self):
        connection = _session_connection(persistent_steps=100000)
        try:
            with patch("wxbg.readstore_batch_provider.ReadStoreQuery.get_new_messages",
                       side_effect=ReadStoreError("query_budget_exceeded")):
                with self.assertRaises(ProviderError) as caught:
                    run_batch_read(
                        _request(), target=None, deadline=time.monotonic() + 5,
                        open_stores=lambda *args, **kwargs: _fake_open_stores(connection))
            self.assertEqual(caught.exception.code, "query_budget_exceeded")
        finally:
            connection.close()

    def test_batch_reports_query_output_size_codes_as_size_limit(self):
        connection = _session_connection(persistent_steps=100000)
        try:
            for inner_code in ("row_too_large", "output_budget_exceeded"):
                with self.subTest(inner_code=inner_code), patch(
                    "wxbg.readstore_batch_provider.ReadStoreQuery.get_new_messages",
                    side_effect=ReadStoreError(inner_code),
                ):
                    with self.assertRaises(ProviderError) as caught:
                        run_batch_read(
                            _request(), target=None, deadline=time.monotonic() + 5,
                            open_stores=lambda *args, **kwargs: _fake_open_stores(connection),
                        )
                    self.assertEqual(caught.exception.code, "readstore_size_limit")
        finally:
            connection.close()

    def test_batch_keeps_other_query_errors_generic(self):
        connection = _session_connection(persistent_steps=100000)
        try:
            with patch("wxbg.readstore_batch_provider.ReadStoreQuery.get_new_messages",
                       side_effect=ReadStoreError("unsupported_schema")):
                with self.assertRaises(ProviderError) as caught:
                    run_batch_read(
                        _request(), target=None, deadline=time.monotonic() + 5,
                        open_stores=lambda *args, **kwargs: _fake_open_stores(connection))
            self.assertEqual(caught.exception.code, "readstore_unavailable")
        finally:
            connection.close()

    def test_batch_rejects_malformed_query_row_as_result_invalid(self):
        connection = _session_connection(persistent_steps=100000)
        malformed_page = {
            "items": [{"shard_id": "invalid shard"}],
            "count": 1,
            "has_more": False,
            "next_cursor": None,
            "bounded": True,
            "full_history": False,
            "exact_once": False,
            "gap_detected": False,
            "ordering": "rowid_asc_per_shard_merge",
            "coverage": "bounded_message_table_query",
        }
        try:
            with patch("wxbg.readstore_batch_provider.ReadStoreQuery.get_new_messages",
                       return_value=malformed_page):
                with self.assertRaises(ProviderError) as caught:
                    run_batch_read(
                        _request(), target=None, deadline=time.monotonic() + 5,
                        open_stores=lambda *args, **kwargs: _fake_open_stores(connection))
            self.assertEqual(caught.exception.code, "readstore_result_invalid")
        finally:
            connection.close()

    def test_batch_keeps_other_row_projection_failures_generic(self):
        connection = _session_connection(persistent_steps=100000)
        try:
            with patch("wxbg.readstore_batch_provider.ReadStoreQuery.get_new_messages",
                       return_value={
                           "items": [{}], "count": 1, "has_more": False,
                           "next_cursor": None, "bounded": True,
                           "full_history": False, "exact_once": False,
                           "gap_detected": False,
                           "ordering": "rowid_asc_per_shard_merge",
                           "coverage": "bounded_message_table_query",
                       }), patch("wxbg.readstore_batch_provider.message_row",
                                 side_effect=RuntimeError("synthetic projection fault")):
                with self.assertRaises(ProviderError) as caught:
                    run_batch_read(
                        _request(), target=None, deadline=time.monotonic() + 5,
                        open_stores=lambda *args, **kwargs: _fake_open_stores(connection))
            self.assertEqual(caught.exception.code, "readstore_unavailable")
        finally:
            connection.close()

    def test_batch_keeps_other_identity_errors_generic(self):
        connection = _session_connection(persistent_steps=100000)
        try:
            with patch("wxbg.readstore_batch_provider.resolve_conversation",
                       side_effect=IdentityError("conversation_not_found")):
                with self.assertRaises(ProviderError) as caught:
                    run_batch_read(
                        _request(), target=None, deadline=time.monotonic() + 5,
                        open_stores=lambda *args, **kwargs: _fake_open_stores(connection))
            self.assertEqual(caught.exception.code, "readstore_unavailable")
        finally:
            connection.close()

    def test_changed_quiet_capture_reports_source_changed(self):
        @contextmanager
        def fake_reader(target, *, deadline):
            yield SimpleNamespace(read=lambda *_: None, module_base=1,
                                  module_size=1, regions=lambda: [])

        account = SimpleNamespace(username="synthetic", cfg_pointer=1)
        storage = SimpleNamespace(path=Path("/synthetic"), account_id="a",
                                  storage_id="s")
        with patch.object(readstore_provider, "ProcessReader", fake_reader), \
             patch.object(readstore_provider, "locate_account", return_value=account), \
             patch.object(readstore_provider, "discover_roots", return_value=[]), \
             patch.object(readstore_provider, "select_storage", return_value=storage), \
             patch.object(readstore_provider, "_file_identity",
                          side_effect=lambda path: (1, 1 if "session" in str(path) else 2)), \
             patch.object(readstore_provider, "capture_quiet",
                          side_effect=CaptureError("capture_changed")):
            with self.assertRaises(ProviderError) as caught:
                with readstore_provider.open_stores(
                    None, deadline=time.monotonic() + 5, include_messages=False):
                    pass
        self.assertEqual(caught.exception.code, "readstore_source_changed")

    def test_capture_deadline_reports_readstore_deadline(self):
        @contextmanager
        def fake_reader(target, *, deadline):
            yield SimpleNamespace(read=lambda *_: None, module_base=1,
                                  module_size=1, regions=lambda: [])

        account = SimpleNamespace(username="synthetic", cfg_pointer=1)
        storage = SimpleNamespace(path=Path("/synthetic"), account_id="a",
                                  storage_id="s")
        with patch.object(readstore_provider, "ProcessReader", fake_reader), \
             patch.object(readstore_provider, "locate_account", return_value=account), \
             patch.object(readstore_provider, "discover_roots", return_value=[]), \
             patch.object(readstore_provider, "select_storage", return_value=storage), \
             patch.object(readstore_provider, "_file_identity",
                          side_effect=lambda path: (1, 1 if "session" in str(path) else 2)), \
             patch.object(readstore_provider, "capture_quiet",
                          side_effect=CaptureError("capture_deadline")):
            with self.assertRaises(ProviderError) as caught:
                with readstore_provider.open_stores(
                    None, deadline=time.monotonic() + 5, include_messages=False):
                    pass
        self.assertEqual(caught.exception.code, "readstore_deadline")

    def test_capture_limit_reports_readstore_size_limit(self):
        self.assertEqual(
            _open_stage_error("capture_quiet", CaptureError("capture_limit")),
            "readstore_size_limit",
        )

    def test_other_capture_errors_remain_generic(self):
        @contextmanager
        def fake_reader(target, *, deadline):
            yield SimpleNamespace(read=lambda *_: None, module_base=1,
                                  module_size=1, regions=lambda: [])

        account = SimpleNamespace(username="synthetic", cfg_pointer=1)
        storage = SimpleNamespace(path=Path("/synthetic"), account_id="a",
                                  storage_id="s")
        with patch.object(readstore_provider, "ProcessReader", fake_reader), \
             patch.object(readstore_provider, "locate_account", return_value=account), \
             patch.object(readstore_provider, "discover_roots", return_value=[]), \
             patch.object(readstore_provider, "select_storage", return_value=storage), \
             patch.object(readstore_provider, "_file_identity",
                          side_effect=lambda path: (1, 1 if "session" in str(path) else 2)), \
             patch.object(readstore_provider, "capture_quiet",
                          side_effect=CaptureError("capture_unavailable")):
            with self.assertRaises(ProviderError) as caught:
                with readstore_provider.open_stores(
                    None, deadline=time.monotonic() + 5, include_messages=False):
                    pass
        self.assertEqual(caught.exception.code, "readstore_unavailable")


if __name__ == "__main__":
    unittest.main()
