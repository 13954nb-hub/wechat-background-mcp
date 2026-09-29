"""Fail-closed sender identity mapping for one authenticated read-store snapshot."""

from collections.abc import Mapping, Sequence
import sqlite3
from typing import Any

from .sender_role import sender_role_fields, unavailable_sender_role


_MAX_SENDER_IDS_PER_PAGE = 1000
_SQL_ID_CHUNK = 400
_MAX_USERNAME_BYTES = 1024
_MAX_GROUP_MEMBERS = 1000


def _username(value: Any) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= _MAX_USERNAME_BYTES:
        return None
    if "\x00" in value:
        return None
    try:
        if len(value.encode("utf-8", errors="strict")) > _MAX_USERNAME_BYTES:
            return None
    except UnicodeError:
        return None
    return value


def _name2id_supported(connection: sqlite3.Connection) -> bool:
    try:
        columns = connection.execute('PRAGMA table_info("Name2Id")').fetchall()
    except sqlite3.Error:
        return False
    return ([(row[1], str(row[2]).upper()) for row in columns]
            == [("user_name", "TEXT"), ("is_session", "INTEGER")])


def _load_name2id(connection: Any, sender_ids: set[int]) -> dict[int, str]:
    if (not isinstance(connection, sqlite3.Connection)
            or not sender_ids or len(sender_ids) > _MAX_SENDER_IDS_PER_PAGE
            or not _name2id_supported(connection)):
        return {}
    output: dict[int, str] = {}
    ordered = sorted(sender_ids)
    try:
        for start in range(0, len(ordered), _SQL_ID_CHUNK):
            chunk = ordered[start:start + _SQL_ID_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f'SELECT rowid, user_name, is_session FROM Name2Id '
                f'WHERE rowid IN ({placeholders})', tuple(chunk)
            ).fetchall()
            for rowid, name, is_session in rows:
                name = _username(name)
                if (type(rowid) is int and rowid in sender_ids
                        and type(is_session) is int and is_session in (0, 1)
                        and name is not None):
                    output[rowid] = name
    except sqlite3.Error:
        return {}
    return output


def _has_exact_table_schema(
    connection: sqlite3.Connection,
    table: str,
    expected: list[tuple[str, str, int]],
) -> bool:
    try:
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return False
    actual = [(row[1], str(row[2]).upper(), row[5]) for row in columns]
    return actual == expected


def load_group_member_usernames(
    contact_connection: Any,
    conversation_key: str,
) -> frozenset[str] | None:
    """Load a bounded, exact current group roster from one contact-store snapshot.

    ``None`` means the schema, room, or roster could not be verified. This keeps
    group senders unknown instead of treating an incomplete roster as evidence.
    """
    if (not isinstance(contact_connection, sqlite3.Connection)
            or _username(conversation_key) is None
            or not conversation_key.endswith("@chatroom")):
        return None
    if not (
        _has_exact_table_schema(contact_connection, "chat_room", [
            ("id", "INTEGER", 1), ("username", "TEXT", 0),
        ])
        and _has_exact_table_schema(contact_connection, "chatroom_member", [
            ("room_id", "INTEGER", 0), ("member_id", "INTEGER", 0),
        ])
        and _has_exact_table_schema(contact_connection, "contact", [
            ("id", "INTEGER", 1), ("username", "TEXT", 0),
        ])
    ):
        return None
    try:
        rooms = contact_connection.execute(
            "SELECT id FROM chat_room WHERE username = ? LIMIT 2",
            (conversation_key,),
        ).fetchall()
        if len(rooms) != 1 or type(rooms[0][0]) is not int:
            return None
        members = contact_connection.execute(
            "SELECT member_id FROM chatroom_member WHERE room_id = ? LIMIT ?",
            (rooms[0][0], _MAX_GROUP_MEMBERS + 1),
        ).fetchall()
        if not members or len(members) > _MAX_GROUP_MEMBERS:
            return None
        member_ids = [row[0] for row in members]
        if any(type(member_id) is not int for member_id in member_ids):
            return None
        usernames: set[str] = set()
        for start in range(0, len(member_ids), _SQL_ID_CHUNK):
            chunk = member_ids[start:start + _SQL_ID_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            contacts = contact_connection.execute(
                f"SELECT id, username FROM contact WHERE id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            mapped = {row[0]: _username(row[1]) for row in contacts
                      if type(row[0]) is int}
            if (len(mapped) != len(set(chunk))
                    or any(mapped.get(member_id) is None for member_id in chunk)):
                return None
            usernames.update(mapped[member_id] for member_id in chunk)
        if not usernames or len(usernames) > _MAX_GROUP_MEMBERS:
            return None
        return frozenset(usernames)
    except sqlite3.Error:
        return None


def _classify_name(
    name: str | None,
    *,
    account_username: str | None,
    conversation_key: str | None,
    group_member_usernames: frozenset[str] | None,
) -> dict[str, object]:
    if (name is None or account_username is None or conversation_key is None
            or account_username == conversation_key):
        return unavailable_sender_role()
    if name == account_username:
        return sender_role_fields("self", "database_account_match")
    if conversation_key.endswith("@chatroom"):
        if group_member_usernames is None:
            return unavailable_sender_role()
        if name in group_member_usernames:
            return sender_role_fields("other", "database_group_member_match")
        return sender_role_fields("unknown", "ambiguous")
    if name == conversation_key:
        return sender_role_fields("other", "database_direct_contact_match")
    return sender_role_fields("unknown", "ambiguous")


def classify_message_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    message_connections: Mapping[str, sqlite3.Connection],
    account_username: str,
    conversation_key: str,
    contact_connection: sqlite3.Connection | None = None,
    group_member_usernames: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Annotate rows only when their sender maps to an exact participant identity.

    ``Name2Id.rowid`` is scoped to its message shard and authenticated account
    snapshot. Group identities require a separately verified current-member set;
    no database-local integer is promoted by itself.
    """
    account_username = _username(account_username)
    conversation_key = _username(conversation_key)
    if (group_member_usernames is None and contact_connection is not None
            and conversation_key.endswith("@chatroom")):
        group_member_usernames = load_group_member_usernames(
            contact_connection, conversation_key
        )
    if group_member_usernames is None:
        members = None
    elif (isinstance(group_member_usernames, (set, frozenset))
          and all(_username(value) is not None for value in group_member_usernames)):
        members = frozenset(group_member_usernames)
    else:
        members = None

    by_shard: dict[str, set[int]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        shard_id = row.get("shard_id")
        sender_id = row.get("sender_id")
        if (type(shard_id) is str and type(sender_id) is int
                and -(1 << 63) <= sender_id < (1 << 63)
                and shard_id in message_connections):
            by_shard.setdefault(shard_id, set()).add(sender_id)

    identities = {
        shard_id: _load_name2id(message_connections.get(shard_id), sender_ids)
        for shard_id, sender_ids in by_shard.items()
    }
    output = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        shard_id = item.get("shard_id")
        sender_id = item.get("sender_id")
        name = (identities.get(shard_id, {}).get(sender_id)
                if type(shard_id) is str and type(sender_id) is int else None)
        item.update(_classify_name(
            name,
            account_username=account_username,
            conversation_key=conversation_key,
            group_member_usernames=members,
        ))
        output.append(item)
    return output


__all__ = ["classify_message_rows", "load_group_member_usernames"]
