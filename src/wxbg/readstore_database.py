"""Authenticated in-memory, read-only SQLite connection owned by one worker."""
from contextlib import contextmanager
import math
import sqlite3
import time

from .readstore_snapshot import SnapshotError, decode_snapshot


class DatabaseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _BudgetConnection(sqlite3.Connection):
    """Compose per-query callbacks with the connection's persistent budget."""
    def install_budget(self, max_steps, deadline):
        self._remaining_steps = max_steps
        self._deadline = deadline
        self._persistent_budget_exhausted = False
        self.set_progress_handler(None, 0)

    @property
    def persistent_budget_exhausted(self):
        return getattr(self, '_persistent_budget_exhausted', False)

    def set_progress_handler(self, callback, n):
        if not hasattr(self, '_remaining_steps'):
            return super().set_progress_handler(callback, n)
        interval = math.gcd(100, n) if callback is not None and n > 0 else 100
        elapsed = 0
        def combined():
            nonlocal elapsed
            self._remaining_steps -= interval
            if self._remaining_steps <= 0 or time.monotonic() >= self._deadline:
                self._persistent_budget_exhausted = True
                return 1
            elapsed += interval
            if callback is not None and n > 0 and elapsed >= n:
                elapsed = 0
                return callback()
            return 0
        super().set_progress_handler(combined, interval)


def _authorize(action, first, second, database, trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (second or '').lower() not in (
            'load_extension', 'readfile', 'writefile'):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (first or '').lower() in ('table_info', 'quick_check'):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@contextmanager
def open_snapshot(capture, *, key, deadline, max_steps=1000000):
    """Authenticate, deserialize, integrity-check and yield an owned connection.

    The caller separately establishes capture/account consistency. No plaintext
    tempfile, ATTACH, extension loading or database mutation is supported.
    Connection close is guaranteed even if the caller's query raises.
    Python/SQLite internal copies are reclaimed by worker exit, not claimed
    to be securely zeroed by this context manager.
    """
    if (type(deadline) not in (int,float) or not math.isfinite(deadline)
            or type(max_steps) is not int or not 100 <= max_steps <= 1000000):
        raise DatabaseError('database_input_invalid')
    if time.monotonic() >= deadline:
        raise DatabaseError('database_deadline')
    try:
        plain, evidence = decode_snapshot(database=capture.database, wal=capture.wal,
            wal_index=capture.wal_index, key=key)
    except SnapshotError as exc:
        # Distinguish a missing or malformed WAL index from page authentication,
        # but never replay the WAL without its commit index.
        if exc.code in ('snapshot_index_invalid', 'snapshot_index_required'):
            raise DatabaseError('database_index_invalid') from None
        raise DatabaseError('database_authentication_failed') from None
    if time.monotonic() >= deadline:
        raise DatabaseError('database_deadline')
    connection = sqlite3.connect(':memory:', factory=_BudgetConnection)
    try:
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 64 * 1024)
            connection.deserialize(plain)
            del plain
            connection.enable_load_extension(False)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.execute('PRAGMA query_only=ON')
            connection.install_budget(max_steps, deadline)
            connection.set_authorizer(_authorize)
            check = connection.execute('PRAGMA quick_check(1)').fetchall()
            if check != [('ok',)]:
                raise DatabaseError('database_integrity_failed')
        except sqlite3.DatabaseError:
            raise DatabaseError('database_open_failed') from None
        if time.monotonic() >= deadline:
            raise DatabaseError('database_deadline')
        yield connection, {**evidence, 'integrity_verified': True,
                           'plaintext_storage': 'worker_memory_only'}
    finally:
        connection.close()
