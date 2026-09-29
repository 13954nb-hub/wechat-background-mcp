"""Bounded Config.Cipher lookup through a caller-owned read-only reader.

Config layout and XOR constant derived from wechatauto/db.py (Apache-2.0);
see THIRD_PARTY_READSTORE.md. This implementation removes disk key caches,
whole-process dumps, master-key fallbacks and runtime reference-file imports.
Caller pins the process/version/account and owns the short-lived worker.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import struct
import time

NAME = b'com.Tencent.WCDB.Config.Cipher'
MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
MAX_ADDRESS = 0x800000000000


class KeyLookupError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _verified(key, page):
    mac_key = hashlib.pbkdf2_hmac('sha512', key,
                  bytes(x ^ 0x3a for x in page[:16]), 2, 32)
    expected = hmac.new(mac_key, page[16:-64] + struct.pack('<I', 1), 'sha512').digest()
    return hmac.compare_digest(expected, page[-64:])


def find_database_keys(*, read, regions, pages, deadline,
                       max_read_bytes=3 * 1024**3, chunk_bytes=1024**2):
    """Return only HMAC-verified keys, or fail the whole lookup.

    read(address,size) returns exact bytes or None; regions() yields readable
    (address,length) ranges anew for each pass. Every read attempt, including
    failed reads and object lookups, spends the single total byte budget.
    Key material never appears in evidence/errors. No I/O is performed here
    except through the caller's reader. Keys must stay in the owning child.
    """
    if (type(pages) is not dict or not 1 <= len(pages) <= 32
            or any(type(k) is not str or not 1 <= len(k) <= 128
                   or type(v) is not bytes or len(v) != 4096 for k, v in pages.items())
            or type(max_read_bytes) is not int or not 0 < max_read_bytes <= 3 * 1024**3
            or type(chunk_bytes) is not int or not 64 <= chunk_bytes <= 1024**2
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise KeyLookupError('key_input_invalid')
    stats = {'read_bytes': 0, 'regions': 0, 'passes': 0, 'candidates': 0,
             'verified_databases': 0}

    def check_time():
        if time.monotonic() >= deadline:
            raise KeyLookupError('key_deadline')

    def bounded_read(address, size):
        check_time()
        if not 0x10000 <= address < MAX_ADDRESS or not 0 < size <= 1024**2 or address + size > MAX_ADDRESS:
            return None
        if stats['read_bytes'] + size > max_read_bytes:
            raise KeyLookupError('key_read_limit')
        stats['read_bytes'] += size
        try:
            value = read(address, size)
        except Exception:
            raise KeyLookupError('key_reader_failed') from None
        check_time()
        return value if type(value) is bytes and len(value) == size else None

    def bounded_regions():
        try:
            iterator = iter(regions())
        except Exception:
            raise KeyLookupError('key_reader_failed') from None
        while True:
            check_time()
            try:
                item = next(iterator)
            except StopIteration:
                return
            except Exception:
                raise KeyLookupError('key_reader_failed') from None
            if type(item) not in (tuple, list) or len(item) != 2:
                raise KeyLookupError('key_region_invalid')
            yield item

    def scan(needles):
        hits = {n: [] for n in needles}
        overlap = max(map(len, needles)) - 1
        stats['passes'] += 1
        last_end = 0
        for address, length in bounded_regions():
            check_time()
            stats['regions'] += 1
            if stats['regions'] > 200000:
                raise KeyLookupError('key_region_limit')
            if (type(address) is not int or type(length) is not int
                    or address < 0x10000 or length <= 0
                    or address < last_end or address + length > MAX_ADDRESS):
                raise KeyLookupError('key_region_invalid')
            last_end = address + length
            tail = b''
            for offset in range(0, length, chunk_bytes):
                data = bounded_read(address + offset, min(chunk_bytes, length-offset))
                if data is None:
                    tail = b''
                    continue
                block = tail + data
                origin = address + offset - len(tail)
                for needle in needles:
                    start = 0
                    while True:
                        found = block.find(needle, start)
                        if found < 0:
                            break
                        hit = origin + found
                        if not hits[needle] or hits[needle][-1] != hit:
                            hits[needle].append(hit)
                        if len(hits[needle]) > 256:
                            raise KeyLookupError('key_reference_limit')
                        start = found + 1
                tail = block[-overlap:]
        return hits

    check_time()
    locations = scan([NAME])[NAME]
    if not 1 <= len(locations) <= 16:
        raise KeyLookupError('key_unavailable')
    pairs = [struct.pack('<QQ', address, len(NAME)) for address in locations]
    references = scan(pairs)
    tested, keys = set(), {}
    for pair, addresses in references.items():
        for address in addresses:
            node = bounded_read(address-16, 80)
            if node is None or node[16:32] != pair:
                continue
            config = struct.unpack_from('<Q', node, 40)[0]
            metadata = bounded_read(config+0x88, 40)
            if metadata is None:
                continue
            pointer, length = struct.unpack_from('<QQ', metadata, 8)
            if not 0 < length <= 1024:
                continue
            blob = bounded_read(pointer, length)
            if blob is None:
                continue
            decoded = bytes(x ^ MASK[i % len(MASK)] for i, x in enumerate(blob))
            for match in LITERAL.finditer(decoded):
                run = match.group(1)
                offsets = [0] if len(run) <= 96 else sorted({0, *range(0, len(run)-63, 32), len(run)-64})
                for start in offsets:
                    check_time()
                    candidate = bytes.fromhex(run[start:start+64].decode('ascii'))
                    if candidate in tested:
                        continue
                    tested.add(candidate)
                    stats['candidates'] += 1
                    if stats['candidates'] > 256:
                        raise KeyLookupError('key_candidate_limit')
                    for label, page in pages.items():
                        if label not in keys and _verified(candidate, page):
                            keys[label] = candidate
                    if len(keys) == len(pages):
                        stats['verified_databases'] = len(keys)
                        return keys, stats
    raise KeyLookupError('key_unavailable')
