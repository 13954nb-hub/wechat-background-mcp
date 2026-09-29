import sqlite3
import unittest
from contextlib import contextmanager
import hashlib
from unittest.mock import patch

from wxbg.readstore_sender_roles import (
    classify_message_rows,
    load_group_member_usernames,
)
from wxbg import readstore_provider


class ReadstoreSenderRoleTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)"
        )
        self.connection.executemany(
            "INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (?, ?, ?)",
            ((17, "self_wxid", 1), (93, "peer_wxid", 0),
             (104, "unrelated_wxid", 0)),
        )

    def tearDown(self):
        self.connection.close()

    def test_exact_same_snapshot_mapping_distinguishes_self_and_direct_peer(self):
        rows = [
            {"shard_id": "message_0", "sender_id": 17,
             "local_type": 1, "content": "self body"},
            {"shard_id": "message_0", "sender_id": 93,
             "local_type": 1, "content": "peer body"},
            {"shard_id": "message_0", "sender_id": 104,
             "local_type": 1, "content": "unexpected sender"},
            {"shard_id": "message_0", "sender_id": 999,
             "local_type": 1, "content": "unmapped sender"},
        ]

        result = classify_message_rows(
            rows,
            message_connections={"message_0": self.connection},
            account_username="self_wxid",
            conversation_key="peer_wxid",
        )

        self.assertEqual(
            [(row["sender_role"], row["sender_role_verified"],
              row["sender_role_evidence"]) for row in result],
            [
                ("self", True, "database_account_match"),
                ("other", True, "database_direct_contact_match"),
                ("unknown", False, "ambiguous"),
                ("unknown", False, "unavailable"),
            ],
        )
        self.assertEqual([row["content"] for row in result],
                         [row["content"] for row in rows])
        self.assertEqual([row["local_type"] for row in result],
                         [row["local_type"] for row in rows])

    def test_group_sender_requires_exact_current_member_identity(self):
        rows = [
            {"shard_id": "message_0", "sender_id": 93, "content": "member"},
            {"shard_id": "message_0", "sender_id": 104, "content": "not member"},
        ]

        result = classify_message_rows(
            rows,
            message_connections={"message_0": self.connection},
            account_username="self_wxid",
            conversation_key="room@chatroom",
            group_member_usernames={"peer_wxid"},
        )

        self.assertEqual(
            [(row["sender_role"], row["sender_role_verified"],
              row["sender_role_evidence"]) for row in result],
            [
                ("other", True, "database_group_member_match"),
                ("unknown", False, "ambiguous"),
            ],
        )

    def test_group_member_roster_uses_exact_same_database_join_path(self):
        contact = sqlite3.connect(":memory:")
        contact.execute(
            "CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT)"
        )
        contact.execute(
            "CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT)"
        )
        contact.execute(
            "CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER)"
        )
        contact.execute(
            "INSERT INTO chat_room (id, username) VALUES (900, 'room@chatroom')"
        )
        contact.executemany(
            "INSERT INTO contact (id, username) VALUES (?, ?)",
            [(17, "self_wxid"), (93, "peer_wxid"), (104, "outsider_wxid")],
        )
        contact.execute(
            "INSERT INTO chatroom_member (room_id, member_id) VALUES (900, 93)"
        )

        try:
            roster = load_group_member_usernames(contact, "room@chatroom")
            rows = [
                {"shard_id": "message_0", "sender_id": 17},
                {"shard_id": "message_0", "sender_id": 93},
                {"shard_id": "message_0", "sender_id": 104},
            ]
            result = classify_message_rows(
                rows,
                message_connections={"message_0": self.connection},
                account_username="self_wxid",
                conversation_key="room@chatroom",
                contact_connection=contact,
            )
        finally:
            contact.close()

        self.assertEqual(roster, frozenset({"peer_wxid"}))
        self.assertEqual(
            [(row["sender_role"], row["sender_role_verified"],
              row["sender_role_evidence"]) for row in result],
            [("self", True, "database_account_match"),
             ("other", True, "database_group_member_match"),
             ("unknown", False, "ambiguous")],
        )

    def test_unsupported_name2id_schema_leaves_messages_readable_and_unknown(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE Name2Id (name TEXT)")
        rows = [{"shard_id": "message_0", "sender_id": 17,
                 "content": "still readable"}]
        try:
            result = classify_message_rows(
                rows,
                message_connections={"message_0": connection},
                account_username="self_wxid",
                conversation_key="peer_wxid",
            )
        finally:
            connection.close()

        self.assertEqual(result[0]["content"], "still readable")
        self.assertEqual(
            (result[0]["sender_role"], result[0]["sender_role_verified"],
             result[0]["sender_role_evidence"]),
            ("unknown", False, "unavailable"),
        )

    def test_incremental_provider_uses_authenticated_snapshot_identity_map(self):
        conversation = "peer_wxid"
        session = sqlite3.connect(":memory:")
        session.execute(
            "CREATE TABLE SessionTable (username TEXT, is_hidden INTEGER)"
        )
        session.execute(
            "INSERT INTO SessionTable (username, is_hidden) VALUES (?, 0)",
            (conversation,),
        )
        message = sqlite3.connect(":memory:")
        table = "Msg_" + hashlib.md5(conversation.encode()).hexdigest()
        message.execute(
            f'CREATE TABLE "{table}" ('
            "local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, message_content TEXT, server_id INTEGER, "
            "sort_seq INTEGER)"
        )
        message.executemany(
            f'INSERT INTO "{table}" '
            "(local_id, local_type, real_sender_id, create_time, "
            "message_content, server_id, sort_seq) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ((1, 1, 17, 10, "self body", 1, 1),
             (2, 1, 93, 11, "peer body", 2, 2)),
        )
        message.execute(
            "CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)"
        )
        message.executemany(
            "INSERT INTO Name2Id (rowid, user_name, is_session) VALUES (?, ?, ?)",
            ((17, "self_wxid", 1), (93, conversation, 0)),
        )
        opened = {
            "connections": {"session": session, "contact": sqlite3.connect(":memory:"),
                            "message_0": message},
            "account_epoch": "a" * 64,
            "account_username": "self_wxid",
            "message_shard_ids": ["message_0"],
            "evidence": {"source_consistency": "observed_quiet_window"},
        }

        @contextmanager
        def open_snapshot(*_args, **_kwargs):
            yield opened

        try:
            with patch.object(readstore_provider, "open_stores", open_snapshot):
                result = readstore_provider.run(
                    "readstore_read_new",
                    {"conversation_key": conversation, "limit": 10,
                     "start_from": "beginning"},
                    target={"pid": 1, "created": 2, "hwnd": 3},
                    deadline=10**12,
                )
        finally:
            session.close()
            opened["connections"]["contact"].close()
            message.close()

        self.assertEqual(
            [(row["sender_role"], row["sender_role_verified"],
              row["sender_role_evidence"]) for row in result["items"]],
            [
                ("self", True, "database_account_match"),
                ("other", True, "database_direct_contact_match"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
