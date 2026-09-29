"""Owned SQLite + SQLCipher-4 fixtures; never open a customer database."""
import hashlib
import hmac
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wxbg.readstore_snapshot import SnapshotError, decode_snapshot


KEY = bytes(range(32))
SALT = bytes(range(16, 32))


def checksum(data, state=(0, 0), order='<'):
    a, b = state
    words = struct.unpack(order + str(len(data) // 4) + 'I', data)
    for first, second in zip(words[::2], words[1::2]):
        a = (a + first + b) % (2 ** 32)
        b = (b + second + a) % (2 ** 32)
    return a, b


def encrypt_page(plain, number):
    iv = hashlib.sha256(struct.pack('<I', number) + plain).digest()[:16]
    start = 16 if number == 1 else 0
    cipher = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    data = cipher.update(plain[start:4016]) + cipher.finalize()
    body = data + iv
    mac_key = hashlib.pbkdf2_hmac('sha512', KEY, bytes(x ^ 0x3A for x in SALT), 2, 32)
    mac = hmac.new(mac_key, body + struct.pack('<I', number), hashlib.sha512).digest()
    return (SALT if number == 1 else b'') + body + mac


def index_checksum(index):
    header = bytearray(index[:48])
    header[40:48] = struct.pack('<II', *checksum(header[:40]))
    return bytes(header + header) + index[96:]


def repair_wal_checksums(wal, index):
    wal = bytearray(wal)
    endian = '<' if struct.unpack_from('>I', wal)[0] == 0x377F0682 else '>'
    state = checksum(wal[:24], order=endian)
    wal[24:32] = struct.pack('>II', *state)
    for offset in range(32, len(wal) - 4119, 4120):
        state = checksum(wal[offset:offset + 8] + wal[offset + 24:offset + 4120], state, endian)
        wal[offset + 16:offset + 24] = struct.pack('>II', *state)
    header = bytearray(index)
    header[24:32] = struct.pack('<II', *state)
    return bytes(wal), index_checksum(header)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        connection = sqlite3.connect(':memory:')
        connection.execute('PRAGMA page_size=4096')
        connection.execute('VACUUM')
        empty = bytearray(connection.serialize())
        connection.close()
        empty[20] = 80
        empty[105:107] = (4016).to_bytes(2, 'big')
        connection = sqlite3.connect(':memory:')
        connection.deserialize(empty)
        connection.execute('CREATE TABLE SessionTable(username TEXT PRIMARY KEY, unread_count INTEGER)')
        connection.execute('INSERT INTO SessionTable VALUES (?, ?)', ('owned_fixture', 1))
        connection.commit()
        self.path = Path(self.temp.name) / 'owned.db'
        self.path.write_bytes(connection.serialize())
        connection.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA wal_autocheckpoint=0')
        self.connection.execute('UPDATE SessionTable SET unread_count=7')
        self.connection.commit()
        self.connection.execute('UPDATE SessionTable SET unread_count=9')
        self.connection.commit()
        self.base = self.path.read_bytes()
        self.original_wal = Path(str(self.path) + '-wal').read_bytes()
        self.original_index = Path(str(self.path) + '-shm').read_bytes()[:136]
        self.encrypted = b''.join(encrypt_page(self.base[i:i + 4096], i // 4096 + 1)
                                  for i in range(0, len(self.base), 4096))
        wal = bytearray(self.original_wal)
        for offset in range(32, len(wal), 4120):
            number = struct.unpack_from('>I', wal, offset)[0]
            wal[offset + 24:offset + 4120] = encrypt_page(wal[offset + 24:offset + 4120], number)
        self.wal, self.index = repair_wal_checksums(wal, self.original_index)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def decode(self, **changes):
        args = {'database': self.encrypted, 'wal': self.wal, 'wal_index': self.index, 'key': KEY}
        args.update(changes)
        return decode_snapshot(**args)

    def encrypted_for_plain(self, plain):
        return b''.join(
            encrypt_page(plain[i:i + 4096], i // 4096 + 1)
            for i in range(0, len(plain), 4096)
        )

    def index_variant(self, *, mx_frame=None, n_page=None,
                      n_backfill_attempted=None, big_end=None):
        index = bytearray(self.index)
        header = bytearray(index[:48])
        if mx_frame is not None:
            struct.pack_into('<I', header, 16, mx_frame)
        if n_page is not None:
            struct.pack_into('<I', header, 20, n_page)
        if big_end is not None:
            header[13] = big_end
        index[:48] = header
        index[48:96] = header
        if n_backfill_attempted is not None:
            struct.pack_into('<I', index, 128, n_backfill_attempted)
        return index_checksum(bytes(index))

    def big_endian_wal(self):
        wal = bytearray(self.wal)
        wal[3] = 0x83
        state = checksum(wal[:24], order='>')
        wal[24:32] = struct.pack('>II', *state)
        for offset in range(32, len(wal), 4120):
            state = checksum(
                wal[offset:offset + 8] + wal[offset + 24:offset + 4120],
                state,
                order='>',
            )
            wal[offset + 16:offset + 24] = struct.pack('>II', *state)
        index = bytearray(self.index)
        header = bytearray(index[:48])
        header[13] = 1
        header[24:32] = struct.pack('<II', *state)
        index[:48] = header
        index[48:96] = header
        return bytes(wal), index_checksum(bytes(index))

    def wal_with_page1(self, *, page_size=None, page_count=None):
        page = bytearray(self.base[:4096])
        if page_size is not None:
            page[16:18] = page_size
        if page_count is not None:
            page[28:32] = page_count.to_bytes(4, 'big')
        wal = bytearray(self.wal)
        struct.pack_into('>I', wal, 32, 1)
        wal[56:56 + 4096] = encrypt_page(bytes(page), 1)
        return repair_wal_checksums(wal, self.index)

    def assert_error(self, code, **changes):
        with self.assertRaises(SnapshotError) as caught:
            self.decode(**changes)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_fixture_checksum_encoder_matches_actual_sqlite(self):
        wal, index = repair_wal_checksums(self.original_wal, self.original_index)
        self.assertEqual(wal, self.original_wal)
        self.assertEqual(index, self.original_index)

    def test_last_committed_values_are_readable_and_base_is_old(self):
        plain, evidence = self.decode()
        copy = sqlite3.connect(':memory:')
        try:
            copy.deserialize(plain)
            self.assertEqual(copy.execute('PRAGMA quick_check').fetchall(), [('ok',)])
            self.assertEqual(copy.execute('SELECT unread_count FROM SessionTable').fetchone(), (9,))
        finally:
            copy.close()
        self.assertEqual(evidence['committed_frames'], 2)
        self.assertTrue(evidence['all_applied_page_hmac_verified'])
        self.assertFalse(evidence['ignored_tail_unverified'])
        self.assertNotIn('all_page_hmac_verified', evidence)
        self.assertEqual(evidence['source_consistency'], 'caller_must_verify_capture')

    def test_journal_header_mode_is_paired_and_wal_requires_wal_mode(self):
        for mode in (b'\x01\x02', b'\x02\x01', b'\x01\x01'):
            with self.subTest(mode=mode):
                plain = bytearray(self.base)
                plain[18:20] = mode
                self.assert_error(
                    'snapshot_database_header_invalid'
                    if mode not in (b'\x01\x01', b'\x02\x02')
                    else 'snapshot_index_invalid',
                    database=self.encrypted_for_plain(plain),
                )

    def test_shm_header_requires_complete_136_bytes(self):
        for candidate in (self.index[:-1], self.index + b'x'):
            with self.subTest(length=len(candidate)):
                self.assert_error('snapshot_index_invalid', wal_index=candidate)

    def test_shm_nbackfill_attempted_cannot_exceed_mxframe(self):
        bad = self.index_variant(n_backfill_attempted=3)
        self.assert_error('snapshot_index_invalid', wal_index=bad)

    def test_middle_database_page_hmac_tamper_is_rejected(self):
        bad = bytearray(self.encrypted)
        bad[4096 + 100] ^= 1
        self.assert_error('snapshot_page_authentication_failed', database=bytes(bad))

    def test_middle_wal_frame_hmac_tamper_is_rejected_after_checksum_repair(self):
        bad = bytearray(self.wal)
        bad[32 + 24 + 100] ^= 1
        bad, index = repair_wal_checksums(bad, self.index)
        self.assert_error(
            'snapshot_page_authentication_failed', wal=bad, wal_index=index
        )

    def test_wrong_database_salt_is_rejected(self):
        bad = bytearray(self.encrypted)
        bad[0] ^= 1
        self.assert_error('snapshot_page_authentication_failed', database=bytes(bad))

    def test_wal_page_zero_is_rejected_after_checksum_repair(self):
        bad = bytearray(self.wal)
        struct.pack_into('>I', bad, 32, 0)
        bad, index = repair_wal_checksums(bad, self.index)
        self.assert_error('snapshot_size_limit', wal=bad, wal_index=index)

    def test_invalid_wal_header_is_rejected_after_checksum_repair(self):
        bad = bytearray(self.wal)
        bad[:4] = b'\x00\x00\x00\x00'
        bad, index = repair_wal_checksums(bad, self.index)
        self.assert_error('snapshot_wal_header_invalid', wal=bad, wal_index=index)

    def test_invalid_database_header_is_rejected_after_authentication(self):
        plain = bytearray(self.base)
        plain[16:18] = b'\x00\x10'
        self.assert_error(
            'snapshot_database_header_invalid',
            database=self.encrypted_for_plain(plain),
        )

    def test_final_replayed_page1_header_is_validated(self):
        wal, index = self.wal_with_page1(page_size=b'\x00\x10')
        self.assert_error(
            'snapshot_database_header_invalid', wal=wal, wal_index=index
        )

    def test_final_replayed_page1_count_is_validated(self):
        wal, index = self.wal_with_page1(page_count=2)
        self.assert_error('snapshot_database_size_mismatch', wal=wal, wal_index=index)

    def test_big_endian_wal_checksum_is_verified(self):
        wal, index = self.big_endian_wal()
        plain, evidence = self.decode(wal=wal, wal_index=index)
        self.assertEqual(evidence['committed_frames'], 2)
        copy = sqlite3.connect(':memory:')
        try:
            copy.deserialize(plain)
            self.assertEqual(
                copy.execute('SELECT unread_count FROM SessionTable').fetchone(),
                (9,),
            )
        finally:
            copy.close()
        bad = bytearray(wal)
        bad[24] ^= 1
        self.assert_error('snapshot_wal_checksum_invalid', wal=bytes(bad), wal_index=index)

    def test_wrong_key_is_rejected(self):
        self.assert_error('snapshot_page_authentication_failed', key=b'z' * 32)

    def test_database_page_tamper_is_rejected(self):
        bad = bytearray(self.encrypted)
        bad[-100] ^= 1
        self.assert_error('snapshot_page_authentication_failed', database=bytes(bad))

    def test_wal_tamper_is_rejected_without_old_snapshot_fallback(self):
        bad = bytearray(self.wal)
        bad[-100] ^= 1
        self.assert_error('snapshot_wal_checksum_invalid', wal=bytes(bad))

    def test_authenticated_wal_pages_required_even_with_repaired_checksum(self):
        bad = bytearray(self.wal)
        bad[-100] ^= 1
        bad, index = repair_wal_checksums(bad, self.index)
        self.assert_error('snapshot_page_authentication_failed', wal=bad, wal_index=index)

    def test_index_copies_must_match(self):
        bad = bytearray(self.index)
        bad[50] ^= 1
        self.assert_error('snapshot_index_invalid', wal_index=bytes(bad))

    def test_index_checksum_must_match(self):
        bad = bytearray(self.index)
        bad[8] ^= 1
        bad[48:96] = bad[:48]
        self.assert_error('snapshot_index_invalid', wal_index=bytes(bad))

    def test_index_must_correspond_to_same_wal_generation(self):
        bad = bytearray(self.index)
        bad[32] ^= 1
        self.assert_error('snapshot_wal_generation_mismatch', wal_index=index_checksum(bad))

    def test_index_final_checksum_must_match_commit(self):
        bad = bytearray(self.index)
        bad[24] ^= 1
        self.assert_error('snapshot_commit_mismatch', wal_index=index_checksum(bad))

    def test_missing_index_refuses_live_wal(self):
        self.assert_error('snapshot_index_required', wal_index=None)

    def test_index_cannot_reference_unavailable_frame(self):
        self.assert_error('snapshot_wal_truncated', wal=self.wal[:-1])

    def test_physical_tail_beyond_committed_index_is_not_applied(self):
        extra = self.wal[32:32 + 4120]
        plain, evidence = self.decode(wal=self.wal + extra + b'uncommitted partial tail')
        expected, _ = self.decode()
        self.assertEqual(plain, expected)
        self.assertEqual(evidence['ignored_tail_bytes'], len(extra) + 24)
        self.assertTrue(evidence['ignored_tail_unverified'])

    def test_different_generation_ignored_tail_is_explicitly_unverified(self):
        extra = bytearray(self.wal[32:32 + 4120])
        extra[8] ^= 1
        plain, evidence = self.decode(wal=self.wal + bytes(extra))
        expected, _ = self.decode()
        self.assertEqual(plain, expected)
        self.assertEqual(evidence['ignored_tail_bytes'], len(extra))
        self.assertTrue(evidence['ignored_tail_unverified'])

    def test_mxframe_zero_ignores_stale_wal_tail_without_wal_header_validation(self):
        stale_tail = b'not-a-wal-header-and-not-authenticated'
        index = self.index_variant(mx_frame=0, n_page=len(self.base) // 4096)
        plain, evidence = self.decode(wal=stale_tail, wal_index=index)
        expected, _ = self.decode(wal=b'', wal_index=index)
        self.assertEqual(plain, expected)
        self.assertEqual(evidence['ignored_tail_bytes'], len(stale_tail))
        self.assertTrue(evidence['ignored_tail_unverified'])

    def test_commit_frame_must_have_matching_database_size(self):
        bad = bytearray(self.wal)
        bad[-4120 + 4:-4120 + 8] = b'\0' * 4
        bad, index = repair_wal_checksums(bad, self.index)
        self.assert_error('snapshot_commit_mismatch', wal=bad, wal_index=index)

    def test_bounded_input_and_strict_key(self):
        self.assert_error('snapshot_input_invalid', key='not bytes')
        self.assert_error('snapshot_input_invalid', key=KEY[:-1])
        self.assert_error('snapshot_input_invalid', database=self.encrypted[:-1])
        self.assert_error('snapshot_size_limit', max_bytes=4096)

    def test_valid_rollback_database_needs_no_index(self):
        base = bytearray(self.base)
        base[18:20] = b'\x01\x01'
        encrypted = b''.join(encrypt_page(base[i:i + 4096], i // 4096 + 1)
                             for i in range(0, len(base), 4096))
        plain, evidence = self.decode(database=encrypted, wal=b'', wal_index=None)
        self.assertEqual(evidence['committed_frames'], 0)
        copy = sqlite3.connect(':memory:')
        try:
            copy.deserialize(plain)
            self.assertEqual(copy.execute('SELECT unread_count FROM SessionTable').fetchone(), (1,))
        finally:
            copy.close()


if __name__ == '__main__':
    unittest.main()
