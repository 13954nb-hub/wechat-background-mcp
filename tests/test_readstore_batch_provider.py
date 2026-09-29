from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from wxbg import readstore_provider as provider
from wxbg import readstore_batch_provider as batch_provider
from wxbg.readstore_batch_public import batch_result_or_error
from wxbg.readstore_batch_public import MAX_BATCH_PUBLIC_BYTES
from wxbg.worker import _bounded_readstore_result


ACCOUNT_EPOCH = "readstore-batch-epoch"
ROOM_A = "alice@chatroom"
ROOM_B = "project-room"
TARGET = {"pid": 123, "created": 456.5, "hwnd": 789}
EVIDENCE = {"multi_database_atomic": False, "source_consistency": "observed_quiet_window"}


def _verified_batch_response(result):
    capture = {"consistency": "observed_quiet_window", "matching_passes": 2,
               "same_handles": True, "path_identity_rechecked": True,
               "atomic_snapshot": False}
    snapshot = {"all_applied_page_hmac_verified": True,
                "integrity_verified": True,
                "source_consistency": "caller_must_verify_capture"}
    names = ("session", "contact", "message_0")
    return {
        "ok": True,
        "worker_started": True,
        "target_validation": {"status": "stable"},
        "evidence": {"background_observation_passed": True, "observations": 2,
                     "foreground_changed": False, "clipboard_changed": False,
                     "cursor_changed": False, "target_restored": False,
                     "capture_observed": False, "new_visible_windows": [],
                     "monitor_errors": []},
        "cleanup": {"status": "read_only", "restored": True,
                    "modified": False, "gate_touched": False, "errors": []},
        "result": {**result, "readstore_evidence": {
            "multi_database_atomic": False,
            "source_consistency": "observed_quiet_window",
            "captures": {name: dict(capture) for name in names},
            "snapshots": {name: dict(snapshot) for name in names},
        }},
    }


def _message_table(room):
    return "Msg_" + hashlib.md5(room.encode("utf-8")).hexdigest()


def _stores():
    session = sqlite3.connect(":memory:")
    session.execute(
        "CREATE TABLE SessionTable (username TEXT, unread_count INTEGER, summary TEXT, "
        "last_timestamp INTEGER, is_hidden INTEGER, sort_timestamp INTEGER)"
    )
    session.executemany(
        "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
        [(ROOM_A, 0, "a", 1, 0, 1), (ROOM_B, 0, "b", 2, 0, 2)],
    )
    messages = sqlite3.connect(":memory:")
    for room, text in ((ROOM_A, "hello alice"), (ROOM_B, "hello project")):
        table = _message_table(room)
        messages.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER, local_type INTEGER, '
            "real_sender_id INTEGER, create_time INTEGER, message_content, "
            "server_id INTEGER, sort_seq INTEGER)"
        )
        messages.execute(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
            (1, 1, 7, 1700000000, text, 9, 10),
        )
    messages.commit()
    return session, messages


def _opened(session, messages, *, contact=None, epoch=ACCOUNT_EPOCH):
    return {
        "connections": {"session": session, "contact": contact,
                        "message_0": messages},
        "account_epoch": epoch,
        "account_username": "self_wxid",
        "message_shard_ids": ["message_0"],
        "evidence": EVIDENCE,
    }


def _request(*, conversations=None, max_total=4, epoch=ACCOUNT_EPOCH):
    if conversations is None:
        conversations = [
            {"conversation_key": ROOM_A, "cursor": None, "start_from": "beginning", "limit": 2},
            {"conversation_key": ROOM_B, "cursor": None, "start_from": "beginning", "limit": 2},
        ]
    return {"account_epoch": epoch, "conversations": conversations, "max_total": max_total}


class ReadStoreBatchProviderTests(unittest.TestCase):
    def setUp(self):
        # The module suite may run after many other tests. Keep the synthetic
        # deadline fresh per test instead of letting an import-time value age.
        self.deadline = time.monotonic() + 20
        self.session, self.messages = _stores()
        self.addCleanup(self.session.close)
        self.addCleanup(self.messages.close)
        self.contact = sqlite3.connect(":memory:")
        self.addCleanup(self.contact.close)
        self.open_calls = []

        @contextmanager
        def open_once(target, *, deadline, include_messages=False):
            self.open_calls.append((target, deadline, include_messages))
            yield _opened(self.session, self.messages, contact=self.contact)

        self.open_once = open_once

    def run_batch(self, args):
        with patch.object(provider, "open_stores", side_effect=self.open_once):
            return provider.run(
                "readstore_batch_read_new", args, target=TARGET, deadline=self.deadline
            )

    def test_uses_one_authenticated_open_and_projects_real_query_rows(self):
        result = self.run_batch(_request())

        self.assertEqual(len(self.open_calls), 1)
        self.assertEqual(self.open_calls[0], (TARGET, self.deadline, True))
        self.assertEqual(result["account_epoch"], ACCOUNT_EPOCH)
        self.assertEqual(result["readstore_account_epoch"], ACCOUNT_EPOCH)
        self.assertIs(result["readstore_evidence"], EVIDENCE)
        self.assertEqual(result["counts"], {"conversations": 2, "items": 2})
        states = result["conversations"]
        self.assertEqual([state["conversation_key"] for state in states], [ROOM_A, ROOM_B])
        self.assertEqual([state["items"][0]["content"] for state in states], ["hello alice", "hello project"])
        for state in states:
            row = state["items"][0]
            self.assertEqual(row["account_epoch"], ACCOUNT_EPOCH)
            self.assertEqual(row["conversation_key"], state["conversation_key"])
            self.assertEqual(row["message_identity"]["chat_md5"], _message_table(state["conversation_key"])[4:])
            self.assertIsNotNone(state["next_cursor"])
            self.assertFalse(state["has_more"])
        self.assertEqual(result["ordering"], "rowid_continuation_not_chronological")
        self.assertFalse(result["full_history"])
        self.assertFalse(result["exact_once"])

    def test_batch_maps_direct_self_and_peer_from_shard_name2id(self):
        peer = "peer_wxid"
        self.session.execute(
            "INSERT INTO SessionTable VALUES (?, 0, ?, ?, 0, ?)",
            (peer, "direct", 3, 3),
        )
        table = _message_table(peer)
        self.messages.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER, local_type INTEGER, '
            "real_sender_id INTEGER, create_time INTEGER, message_content TEXT, "
            "server_id INTEGER, sort_seq INTEGER)"
        )
        self.messages.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
            [(1, 1, 17, 10, "self body", 1, 1),
             (2, 1, 93, 11, "peer body", 2, 2)],
        )
        self.messages.execute(
            "CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)"
        )
        self.messages.executemany(
            "INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (?, ?, ?)",
            [(17, "self_wxid", 1), (93, peer, 0)],
        )
        self.session.commit()
        self.messages.commit()

        result = self.run_batch(_request(conversations=[
            {"conversation_key": peer, "cursor": None,
             "start_from": "beginning", "limit": 10}
        ], max_total=10))

        rows = result["conversations"][0]["items"]
        self.assertEqual(
            [(row["sender_role"], row["sender_role_verified"],
              row["sender_role_evidence"]) for row in rows],
            [("self", True, "database_account_match"),
             ("other", True, "database_direct_contact_match")],
        )

    def test_batch_maps_group_member_only_from_same_contact_snapshot(self):
        room = ROOM_A
        table = _message_table(room)
        self.messages.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
            [(2, 1, 17, 20, "self", 2, 2),
             (3, 1, 93, 30, "member", 3, 3),
             (4, 1, 104, 40, "outsider", 4, 4)],
        )
        self.messages.execute(
            "CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)"
        )
        self.messages.executemany(
            "INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (?, ?, ?)",
            [(17, "self_wxid", 1), (93, "member_wxid", 0),
             (104, "outsider_wxid", 0)],
        )
        self.contact.executescript(
            "CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT);"
            "CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER);"
            "CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT);"
            "INSERT INTO chat_room VALUES (900, 'alice@chatroom');"
            "INSERT INTO contact VALUES (93, 'member_wxid');"
            "INSERT INTO contact VALUES (104, 'outsider_wxid');"
            "INSERT INTO chatroom_member VALUES (900, 93);"
        )
        self.session.commit()
        self.messages.commit()

        result = self.run_batch(_request(conversations=[
            {"conversation_key": room, "cursor": None,
             "start_from": "beginning", "limit": 10}
        ], max_total=10))

        rows = result["conversations"][0]["items"]
        self.assertEqual(
            [(row["sender_role"], row["sender_role_verified"],
              row["sender_role_evidence"]) for row in rows[-3:]],
            [("self", True, "database_account_match"),
             ("other", True, "database_group_member_match"),
             ("unknown", False, "ambiguous")],
        )

    def test_continuation_reuses_each_room_cursor_when_already_caught_up(self):
        first = self.run_batch(_request())
        follow_up = _request(
            conversations=[
                {
                    "conversation_key": state["conversation_key"],
                    "cursor": state["next_cursor"],
                    "start_from": "cursor",
                    "limit": 2,
                }
                for state in first["conversations"]
            ]
        )

        second = self.run_batch(follow_up)

        self.assertEqual(second["counts"], {"conversations": 2, "items": 0})
        for prior, current in zip(first["conversations"], second["conversations"]):
            self.assertEqual(current["items"], [])
            self.assertFalse(current["has_more"])
            self.assertEqual(current["next_cursor"], prior["next_cursor"])

    def test_continuation_advances_nonempty_pages_in_both_rooms(self):
        for room in (ROOM_A, ROOM_B):
            table = _message_table(room)
            for local_id in (2, 3, 4):
                self.messages.execute(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (local_id, 1, 7, 1700000000 + local_id,
                     f"message {local_id}", 9 + local_id, 10 + local_id),
                )
        self.messages.commit()

        first_request = _request()
        first = self.run_batch(first_request)
        self.assertEqual(first["counts"], {"conversations": 2, "items": 4})
        self.assertTrue(all(state["has_more"] for state in first["conversations"]))
        first_public = batch_result_or_error(
            _verified_batch_response(first), request=first_request)
        self.assertEqual(first_public["result"]["counts"]["items"], 4)
        follow_up = _request(conversations=[
            {"conversation_key": state["conversation_key"],
             "cursor": state["next_cursor"], "start_from": "cursor", "limit": 2}
            for state in first["conversations"]
        ])
        second = self.run_batch(follow_up)
        self.assertEqual(second["counts"], {"conversations": 2, "items": 4})
        second_public = batch_result_or_error(
            _verified_batch_response(second), request=follow_up)
        self.assertEqual(second_public["result"]["counts"]["items"], 4)
        for prior, current in zip(first["conversations"], second["conversations"]):
            before = {row["message_identity"]["rowid"] for row in prior["items"]}
            after = {row["message_identity"]["rowid"] for row in current["items"]}
            self.assertTrue(before.isdisjoint(after))
            self.assertFalse(current["has_more"])

    def _install_large_synthetic_batch(self, room_count):
        """Populate only in-memory SQLite with valid, near-max-size rows."""
        rooms = [f"synthetic-room-{index}@chatroom" for index in range(room_count)]
        for room in rooms:
            self.session.execute(
                "INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?)",
                (room, 0, "synthetic", 1, 0, 1),
            )
            table = _message_table(room)
            self.messages.execute(
                f'CREATE TABLE "{table}" (local_id INTEGER, local_type INTEGER, '
                "real_sender_id INTEGER, create_time INTEGER, message_content, "
                "server_id INTEGER, sort_seq INTEGER)"
            )
            self.messages.executemany(
                f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
                [(index, 1, 7, 1700000000 + index, "A" * 4096, index, index)
                 for index in range(1, 9)],
            )
        self.session.commit()
        self.messages.commit()
        return _request(
            conversations=[
                {"conversation_key": room, "cursor": None,
                 "start_from": "beginning", "limit": 8}
                for room in rooms
            ],
            max_total=room_count * 8,
        )

    def _assert_large_batch_continues_without_loss(self, room_count):
        request = self._install_large_synthetic_batch(room_count)
        seen = set()
        ordered_by_room = {
            spec["conversation_key"]: [] for spec in request["conversations"]
        }
        expected = {(spec["conversation_key"], index)
                    for spec in request["conversations"] for index in range(1, 9)}
        for page in range(1, 6):
            self.deadline = time.monotonic() + 20
            raw = self.run_batch(request)
            # A valid provider page must survive both actual size boundaries.
            self.assertIs(_bounded_readstore_result(raw), raw)
            public = batch_result_or_error(
                _verified_batch_response(raw), request=request
            )["result"]
            self.assertLessEqual(
                len(json.dumps(public, ensure_ascii=True, allow_nan=False).encode()),
                MAX_BATCH_PUBLIC_BYTES,
            )
            self.assertEqual(len(public["conversations"]), room_count)
            new_rows = 0
            for state in public["conversations"]:
                for row in state["items"]:
                    identity = (state["conversation_key"],
                                row["message_identity"]["rowid"])
                    self.assertNotIn(identity, seen)
                    seen.add(identity)
                    ordered_by_room[state["conversation_key"]].append(
                        row["message_identity"]["rowid"]
                    )
                    new_rows += 1
            self.assertEqual(public["counts"]["items"], new_rows)
            if seen == expected:
                self.assertEqual(public["status"], "complete")
                break
            self.assertGreater(new_rows, 0, f"page {page} made no progress")
            self.assertEqual(public["status"], "partial")
            request = _request(
                conversations=[
                    {"conversation_key": state["conversation_key"],
                     "cursor": state["next_cursor"],
                     "start_from": "cursor", "limit": 8}
                    for state in public["conversations"]
                ],
                max_total=room_count * 8,
            )
        self.assertEqual(seen, expected)
        for rowids in ordered_by_room.values():
            self.assertEqual(rowids, list(range(1, 9)))

    def test_valid_batch_over_public_byte_limit_paginates_without_loss(self):
        self._assert_large_batch_continues_without_loss(12)

    def test_valid_batch_over_worker_byte_limit_paginates_without_loss(self):
        self._assert_large_batch_continues_without_loss(16)

    def test_one_row_too_large_for_batch_cap_has_fixed_size_error(self):
        request = self._install_large_synthetic_batch(1)
        with patch.object(batch_provider, "MAX_BATCH_PUBLIC_BYTES", 3500, create=True):
            with self.assertRaisesRegex(provider.ProviderError, "^readstore_size_limit$"):
                self.run_batch(request)

    def test_unfit_later_room_withholds_earlier_small_room(self):
        large_spec = self._install_large_synthetic_batch(1)["conversations"][0]
        request = _request(conversations=[
            {"conversation_key": ROOM_A, "cursor": None,
             "start_from": "beginning", "limit": 1},
            large_spec,
        ], max_total=9)
        with patch.object(batch_provider, "MAX_BATCH_PUBLIC_BYTES", 3500):
            with self.assertRaisesRegex(provider.ProviderError, "^readstore_size_limit$"):
                self.run_batch(request)

    def test_packed_batch_rejects_room_left_without_any_pending_row(self):
        request = self._install_large_synthetic_batch(2)
        # One large row fits the public envelope, but one from each room does
        # not. Returning only the first room would stall the second cursor.
        with patch.object(batch_provider, "MAX_BATCH_PUBLIC_BYTES", 7000):
            for spec in request["conversations"]:
                singleton = _request(conversations=[spec], max_total=8)
                self.assertGreater(self.run_batch(singleton)["counts"]["items"], 0)
            with self.assertRaisesRegex(provider.ProviderError, "^readstore_size_limit$"):
                self.run_batch(request)

    def test_packed_multishard_cursor_advances_only_returned_rows(self):
        room = ROOM_A
        table = _message_table(room)
        self.messages.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
            [(index, 1, 7, 1700000000 + index, "A" * 4096, index, index)
             for index in range(2, 9)],
        )
        self.messages.commit()
        second_shard = sqlite3.connect(":memory:")
        self.addCleanup(second_shard.close)
        second_shard.execute(
            f'CREATE TABLE "{table}" (local_id INTEGER, local_type INTEGER, '
            "real_sender_id INTEGER, create_time INTEGER, message_content, "
            "server_id INTEGER, sort_seq INTEGER)"
        )
        second_shard.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?)',
            [(100 + index, 1, 7, 1700000000 + index, "B" * 4096,
              100 + index, index) for index in range(1, 9)],
        )
        second_shard.commit()

        @contextmanager
        def open_two_shards(target, *, deadline, include_messages=False):
            opened = _opened(self.session, self.messages)
            opened["connections"]["message_1"] = second_shard
            opened["message_shard_ids"].append("message_1")
            yield opened

        request = _request(conversations=[
            {"conversation_key": room, "cursor": None,
             "start_from": "beginning", "limit": 16}
        ], max_total=16)
        observed = []
        with patch.object(provider, "open_stores", side_effect=open_two_shards), \
             patch.object(batch_provider, "MAX_BATCH_PUBLIC_BYTES", 25000):
            for page in range(1, 8):
                self.deadline = time.monotonic() + 20
                raw = provider.run(
                    "readstore_batch_read_new", request,
                    target=TARGET, deadline=self.deadline,
                )
                self.assertIs(_bounded_readstore_result(raw), raw)
                envelope = _verified_batch_response(raw)
                evidence = envelope["result"]["readstore_evidence"]
                for field in ("captures", "snapshots"):
                    evidence[field]["message_1"] = dict(evidence[field]["message_0"])
                public = batch_result_or_error(envelope, request=request)["result"]
                self.assertLessEqual(
                    len(json.dumps(public, ensure_ascii=True).encode()), 25000
                )
                state = public["conversations"][0]
                for row in state["items"]:
                    identity = (row["message_identity"]["rowid"], row["shard_id"])
                    self.assertNotIn(identity, observed)
                    observed.append(identity)
                if len(observed) == 16:
                    self.assertEqual(public["status"], "complete")
                    break
                self.assertTrue(state["has_more"], f"page {page} lost continuation")
                self.assertTrue(state["items"], f"page {page} made no progress")
                request = _request(conversations=[
                    {"conversation_key": room, "cursor": state["next_cursor"],
                     "start_from": "cursor", "limit": 16}
                ], max_total=16)
        self.assertEqual(
            observed,
            [(index, shard) for index in range(1, 9)
             for shard in ("message_0", "message_1")],
        )

    def test_invalid_request_is_rejected_before_opening_stores(self):
        request = _request()
        request["conversations"].append(dict(request["conversations"][0]))

        with patch.object(provider, "open_stores", side_effect=self.open_once):
            with self.assertRaisesRegex(provider.ProviderError, "^readstore_input_invalid$"):
                provider.run("readstore_batch_read_new", request, target=TARGET, deadline=self.deadline)

        self.assertEqual(self.open_calls, [])

    def test_total_budget_is_rejected_before_opening_stores(self):
        request = _request(max_total=3)

        with patch.object(provider, "open_stores", side_effect=self.open_once):
            with self.assertRaisesRegex(provider.ProviderError, "^readstore_input_invalid$"):
                provider.run("readstore_batch_read_new", request, target=TARGET, deadline=self.deadline)

        self.assertEqual(self.open_calls, [])

    def test_requested_epoch_mismatch_withholds_batch(self):
        request = _request(epoch="prior-account-epoch")

        @contextmanager
        def opened_other_account(target, *, deadline, include_messages=False):
            self.open_calls.append((target, deadline, include_messages))
            yield _opened(self.session, self.messages)

        with patch.object(provider, "open_stores", side_effect=opened_other_account):
            with self.assertRaisesRegex(provider.ProviderError, "^readstore_account_changed$"):
                provider.run("readstore_batch_read_new", request, target=TARGET, deadline=self.deadline)

        self.assertEqual(len(self.open_calls), 1)

    def test_epoch_drift_on_context_exit_withholds_batch_result(self):
        @contextmanager
        def drift_after_read(target, *, deadline, include_messages=False):
            self.open_calls.append((target, deadline, include_messages))
            yield _opened(self.session, self.messages)
            raise provider.ProviderError("readstore_account_changed")

        with patch.object(provider, "open_stores", side_effect=drift_after_read):
            with self.assertRaisesRegex(provider.ProviderError, "^readstore_account_changed$"):
                provider.run("readstore_batch_read_new", _request(), target=TARGET, deadline=self.deadline)

        self.assertEqual(len(self.open_calls), 1)

    def test_deadline_expiry_before_later_room_discards_earlier_rows(self):
        checks = [None, None, None, provider.ProviderError("readstore_deadline")]
        with patch.object(batch_provider, "_check_deadline", side_effect=checks):
            with patch.object(
                batch_provider,
                "resolve_conversation",
                wraps=batch_provider.resolve_conversation,
            ) as resolve:
                with patch.object(provider, "open_stores", side_effect=self.open_once):
                    with self.assertRaisesRegex(provider.ProviderError, "^readstore_deadline$"):
                        provider.run(
                            "readstore_batch_read_new",
                            _request(),
                            target=TARGET,
                            deadline=self.deadline,
                        )

        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(len(self.open_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
