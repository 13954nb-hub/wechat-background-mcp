import sqlite3
import time
from types import SimpleNamespace
import unittest

import test_readstore_snapshot as fixtures
from wxbg.readstore_database import DatabaseError, open_snapshot


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SnapshotTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.capture = SimpleNamespace(database=self.fixture.encrypted,
            wal=self.fixture.wal, wal_index=self.fixture.index)

    def open(self, **kwargs):
        return open_snapshot(self.capture, key=fixtures.KEY,
                             deadline=time.monotonic()+2, **kwargs)

    def test_authenticated_committed_data_and_close(self):
        with self.open() as (connection, evidence):
            self.assertEqual(connection.execute('SELECT unread_count FROM SessionTable').fetchone(), (9,))
            self.assertTrue(evidence['integrity_verified'])
            self.assertEqual(connection.execute('PRAGMA table_info(SessionTable)').fetchone()[1], 'username')
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')

    def test_mutation_attach_and_disable_readonly_denied(self):
        with self.open() as (connection, _):
            for sql in ('UPDATE SessionTable SET unread_count=0',
                        "ATTACH ':memory:' AS other", 'PRAGMA query_only=OFF',
                        'CREATE TEMP TABLE leaked(value)', "SELECT load_extension('anything')"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(sql)

    def test_query_step_budget(self):
        with self.open(max_steps=1000) as (connection, _):
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<1000000) SELECT sum(n) FROM x').fetchone()

    def test_wrong_key_fixed_error(self):
        with self.assertRaisesRegex(DatabaseError, '^database_authentication_failed$'):
            with open_snapshot(self.capture, key=b'0'*32, deadline=time.monotonic()+2):
                self.fail('must not yield')

    def test_expired_deadline(self):
        with self.assertRaisesRegex(DatabaseError, '^database_deadline$'):
            with open_snapshot(self.capture, key=fixtures.KEY, deadline=time.monotonic()-1):
                self.fail('must not yield')

    def test_query_core_cannot_remove_connection_budget(self):
        sql='WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<1000) SELECT sum(n) FROM x'
        for callback,interval in ((lambda:0,1),(None,0)):
            with self.subTest(interval=interval),self.open(max_steps=1000) as (connection,_):
                connection.set_progress_handler(callback,interval)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute(sql).fetchone()


if __name__ == '__main__':
    unittest.main()
