"""Authenticate bounded SQLCipher-4 pages and an indexed committed WAL image.

Pure bytes only: this module never finds keys, opens source files, saves a
plaintext database or claims that the caller's multi-file capture was atomic.
Format references: zetetic.net/sqlcipher/design and sqlite.org/walformat.html.
"""
from __future__ import annotations

import hashlib
import hmac
import struct

PAGE_SIZE = 4096
FRAME_SIZE = 4120
MAX_BYTES = 64 * 1024 * 1024


class SnapshotError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise SnapshotError(code)


def _checksum(data, state=(0, 0), endian='<'):
    first, second = state
    for x, y in struct.iter_unpack(endian + 'II', data):
        first = (first + x + second) & 0xFFFFFFFF
        second = (second + y + first) & 0xFFFFFFFF
    return first, second


def decode_snapshot(*, database: bytes, wal: bytes, wal_index: bytes | None,
                    key: bytes, max_bytes: int = MAX_BYTES) -> tuple[bytes, dict]:
    """Return one authenticated image at the supplied WAL index's commit.

    The caller must capture/revalidate the source file identities, contents and
    both index copies while bound to the intended client and account. A bad
    indexed frame rejects the whole request; there is no older-data fallback.
    """
    if (type(database) is not bytes or type(wal) is not bytes or type(key) is not bytes
            or len(key) != 32 or not database or len(database) % PAGE_SIZE
            or (wal_index is not None and type(wal_index) is not bytes)
            or type(max_bytes) is not int or not PAGE_SIZE <= max_bytes <= MAX_BYTES):
        _fail('snapshot_input_invalid')
    if len(database) > max_bytes or len(wal) > max_bytes:
        _fail('snapshot_size_limit')
    salt = database[:16]
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(b ^ 0x3A for b in salt), 2, 32)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def decrypt(page, number):
        start = 16 if number == 1 else 0
        if number == 1 and page[:16] != salt:
            _fail('snapshot_page_authentication_failed')
        expected = hmac.new(mac_key, page[start:4032] + struct.pack('<I', number), hashlib.sha512).digest()
        if not hmac.compare_digest(expected, page[4032:]):
            _fail('snapshot_page_authentication_failed')
        decoder = Cipher(algorithms.AES(key), modes.CBC(page[4016:4032])).decryptor()
        plain = decoder.update(page[start:4016]) + decoder.finalize()
        return (b'SQLite format 3\0' if number == 1 else b'') + plain + bytes(80)

    plain = bytearray()
    for offset in range(0, len(database), PAGE_SIZE):
        plain.extend(decrypt(database[offset:offset + PAGE_SIZE], offset // PAGE_SIZE + 1))
    if (plain[:16] != b'SQLite format 3\0' or plain[16:18] != b'\x10\x00'
            or plain[20] != 80 or plain[18:20] not in (b'\x01\x01', b'\x02\x02')):
        _fail('snapshot_database_header_invalid')
    journal_mode = plain[18:20]
    frames, pages, ignored = 0, len(plain) // PAGE_SIZE, 0
    if wal_index is None:
        if wal or journal_mode != b'\x01\x01':
            _fail('snapshot_index_required')
    else:
        if journal_mode != b'\x02\x02':
            _fail('snapshot_index_invalid')
        if len(wal_index) != 136 or wal_index[:48] != wal_index[48:96]:
            _fail('snapshot_index_invalid')
        header = wal_index[:48]
        version, unused = struct.unpack_from('<II', header)
        frames, indexed_pages = struct.unpack_from('<II', header, 16)
        page_size = struct.unpack_from('<H', header, 14)[0]
        # Recovery may leave szPage unset when the WAL has no committed frames.
        if (version != 3007000 or unused != 0 or header[12] != 1 or header[13] not in (0, 1)
                or not (page_size == PAGE_SIZE
                        or (page_size == 0 and frames == 0 and indexed_pages == 0))
                or _checksum(header[:40]) != struct.unpack_from('<II', header, 40)):
            _fail('snapshot_index_invalid')
        if frames > max_bytes // FRAME_SIZE or indexed_pages > max_bytes // PAGE_SIZE:
            _fail('snapshot_size_limit')
        if (struct.unpack_from('<I', wal_index, 96)[0] > frames
                or struct.unpack_from('<I', wal_index, 128)[0] > frames):
            _fail('snapshot_index_invalid')
        if frames:
            if len(wal) < 32 + frames * FRAME_SIZE:
                _fail('snapshot_wal_truncated')
            magic, version, size = struct.unpack_from('>III', wal)
            if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or size != PAGE_SIZE:
                _fail('snapshot_wal_header_invalid')
            if header[13] != (magic & 1) or header[32:40] != wal[16:24]:
                _fail('snapshot_wal_generation_mismatch')
            endian = '>' if magic & 1 else '<'
            rolling = _checksum(wal[:24], endian=endian)
            if rolling != struct.unpack_from('>II', wal, 24):
                _fail('snapshot_wal_checksum_invalid')
            final_commit = 0
            for frame in range(frames):
                offset = 32 + frame * FRAME_SIZE
                number, commit_size = struct.unpack_from('>II', wal, offset)
                if not 1 <= number <= max_bytes // PAGE_SIZE or commit_size > max_bytes // PAGE_SIZE:
                    _fail('snapshot_size_limit')
                if wal[offset + 8:offset + 16] != wal[16:24]:
                    _fail('snapshot_wal_generation_mismatch')
                encrypted_page = wal[offset + 24:offset + FRAME_SIZE]
                rolling = _checksum(wal[offset:offset + 8] + encrypted_page, rolling, endian)
                if rolling != struct.unpack_from('>II', wal, offset + 16):
                    _fail('snapshot_wal_checksum_invalid')
                page = decrypt(encrypted_page, number)
                required = number * PAGE_SIZE
                if required > len(plain):
                    plain.extend(bytes(required - len(plain)))
                plain[required - PAGE_SIZE:required] = page
                final_commit = commit_size
            if (final_commit == 0 or final_commit != indexed_pages
                    or rolling != struct.unpack_from('<II', header, 24)):
                _fail('snapshot_commit_mismatch')
            pages = final_commit
            if pages * PAGE_SIZE > len(plain):
                _fail('snapshot_commit_mismatch')
            del plain[pages * PAGE_SIZE:]
            ignored = len(wal) - (32 + frames * FRAME_SIZE)
        else:
            # mxFrame=0 means no WAL content is committed, including stale tails.
            if indexed_pages not in (0, pages):
                _fail('snapshot_commit_mismatch')
            ignored = len(wal)
    if (plain[:16] != b'SQLite format 3\0' or plain[16:18] != b'\x10\x00'
            or plain[20] != 80 or plain[18:20] != journal_mode):
        _fail('snapshot_database_header_invalid')
    if struct.unpack_from('>I', plain, 28)[0] != pages:
        _fail('snapshot_database_size_mismatch')
    # This is a standalone in-memory image, with no external WAL connection.
    plain[18:20] = b'\x01\x01'
    return bytes(plain), {'committed_frames': frames, 'database_pages': pages,
                         'ignored_tail_bytes': ignored,
                         'ignored_tail_unverified': bool(ignored),
                         'all_applied_page_hmac_verified': True,
                         'source_consistency': 'caller_must_verify_capture'}
