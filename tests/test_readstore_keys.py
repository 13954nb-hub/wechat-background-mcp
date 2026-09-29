import hashlib
import hmac
import struct
import time
import unittest

from wxbg.readstore_keys import KeyLookupError, find_database_keys


# Independently constructed owned Config.Cipher object and authenticated page.
MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
NAME = b'com.Tencent.WCDB.Config.Cipher'


class KeyLookupTests(unittest.TestCase):
    def setUp(self):
        self.base = 0x10000
        self.memory = bytearray(4096)
        self.key = bytes(range(32))
        salt = b'S' * 16
        body = b'B' * 4016
        mac_key = hashlib.pbkdf2_hmac('sha512', self.key,
                    bytes(x ^ 0x3a for x in salt), 2, 32)
        self.page = salt + body + hmac.new(mac_key, body + struct.pack('<I', 1), 'sha512').digest()
        self.memory[61:61+len(NAME)] = NAME  # Straddles a 64-byte chunk.
        struct.pack_into('<QQ', self.memory, 256+16, self.base+61, len(NAME))
        struct.pack_into('<Q', self.memory, 256+40, self.base+512)
        blob = b"x'" + self.key.hex().encode() + b"'"
        encoded = bytes(x ^ MASK[i % len(MASK)] for i, x in enumerate(blob))
        struct.pack_into('<QQ', self.memory, 512+0x88+8, self.base+1024, len(encoded))
        self.memory[1024:1024+len(encoded)] = encoded

    def read(self, address, size):
        offset = address - self.base
        if offset < 0 or offset + size > len(self.memory):
            return None
        return bytes(self.memory[offset:offset+size])

    def call(self, **overrides):
        kwargs = dict(read=self.read, regions=lambda: iter([(self.base, len(self.memory))]),
                      pages={'session': self.page}, deadline=time.monotonic()+2,
                      max_read_bytes=20000, chunk_bytes=64)
        kwargs.update(overrides)
        return find_database_keys(**kwargs)

    def test_verified_key_chunk_overlap_and_metadata_only_evidence(self):
        keys, evidence = self.call()
        self.assertEqual(keys, {'session': self.key})
        self.assertEqual(evidence['verified_databases'], 1)
        self.assertNotIn(self.key.hex(), repr(evidence))
        self.assertGreater(evidence['read_bytes'], 8192)

    def test_wrong_page_mac_never_returns_candidate(self):
        with self.assertRaisesRegex(KeyLookupError, '^key_unavailable$'):
            self.call(pages={'session': self.page[:-1]+bytes([self.page[-1]^1])})

    def test_two_pass_total_budget(self):
        with self.assertRaisesRegex(KeyLookupError, '^key_read_limit$'):
            self.call(max_read_bytes=5000)

    def test_deadline(self):
        with self.assertRaisesRegex(KeyLookupError, '^key_deadline$'):
            self.call(deadline=time.monotonic()-1)

    def test_partial_read_not_used(self):
        with self.assertRaisesRegex(KeyLookupError, '^key_unavailable$'):
            self.call(read=lambda address, size: b'x' * (size-1))

    def test_missing_one_db_fails_entire_lookup(self):
        with self.assertRaisesRegex(KeyLookupError, '^key_unavailable$'):
            self.call(pages={'session': self.page, 'message': b'Z'*4096})

    def test_duplicate_labels_share_verified_key(self):
        keys, evidence = self.call(pages={'session': self.page, 'message': self.page})
        self.assertEqual(set(keys), {'session', 'message'})
        self.assertEqual(evidence['verified_databases'], 2)

    def test_invalid_region_fails(self):
        with self.assertRaisesRegex(KeyLookupError, '^key_region_invalid$'):
            self.call(regions=lambda: iter([(self.base, -1)]))

    def test_reader_callback_failure_is_sanitized(self):
        def fail(*args):
            raise RuntimeError('secret-sentinel')
        for changes in ({'read': fail}, {'regions': fail}):
            with self.subTest(changes=changes), self.assertRaisesRegex(KeyLookupError, '^key_reader_failed$'):
                self.call(**changes)


if __name__ == '__main__':
    unittest.main()
