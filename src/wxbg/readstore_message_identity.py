"""Resolve an exact account-bound database conversation key, never a UI ref."""
import hashlib
import math
import sqlite3
import time
from .readstore_inbox import InboxReadError, _QueryBudget


class IdentityError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def resolve_conversation(connection, conversation_key, *, deadline):
    if (type(conversation_key) is not str or not 1 <= len(conversation_key) <= 1024
            or "\x00" in conversation_key):
        raise IdentityError('invalid_conversation_key')
    try:
        encoded=conversation_key.encode('utf-8',errors='strict')
    except UnicodeError:
        raise IdentityError('invalid_conversation_key') from None
    if len(encoded)>4096:
        raise IdentityError('invalid_conversation_key')
    if type(deadline) not in (int,float) or not math.isfinite(deadline):
        raise IdentityError('invalid_deadline')
    budget=_QueryBudget(min(deadline,time.monotonic()+1))
    try:
        rows=budget.fetchall(connection,
            'SELECT username FROM SessionTable WHERE username=? AND is_hidden=0 LIMIT 2',
            (conversation_key,))
    except InboxReadError as exc:
        code='query_budget_exceeded' if exc.code=='query_budget_exceeded' else 'unsupported_schema'
        raise IdentityError(code) from None
    except sqlite3.Error:
        raise IdentityError('unsupported_schema') from None
    if not rows:
        raise IdentityError('conversation_not_found')
    if len(rows)!=1 or rows[0]!=(conversation_key,):
        raise IdentityError('conversation_ambiguous')
    return hashlib.md5(encoded).hexdigest()
