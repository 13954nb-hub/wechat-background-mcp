from __future__ import annotations

import struct
import sys
import time
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from wxbg.readstore_account import (
        AccountIdentity,
        AccountLookupError,
        MAX_ADDRESS,
        MAX_MODULE_SIZE,
        SCAN_CHUNK_BYTES,
        locate_account,
    )
except ImportError:
    class AccountLookupError(AssertionError):
        pass

    AccountIdentity = object
    MAX_ADDRESS = 0x800000000000
    MAX_MODULE_SIZE = 512 * 1024 * 1024
    SCAN_CHUNK_BYTES = 1024 * 1024

    def locate_account(**_kwargs):
        raise AccountLookupError("account_unimplemented")


BASE = 0x100000
PE_OFFSET = 0x80
SECTION_TABLE = PE_OFFSET + 24 + 0xF0
SECTIONS = {
    ".rdata": (0x1000, SCAN_CHUNK_BYTES + 128),
    ".data": (SCAN_CHUNK_BYTES + 0x2000, 0x1000),
}


class PinnedLayoutTests(unittest.TestCase):
    def test_current_pinned_binary_data_sections_fit_bounded_budget(self):
        from wxbg.readstore_account import _parse_sections
        fixture=MemoryFixture()
        for index,values in enumerate(((60541600,119959552,60541952),(9469208,180502528,8995328))):
            struct.pack_into('<III',fixture.memory,SECTION_TABLE+40*index+8,*values)
        sections=_parse_sections(bytes(fixture.memory[:4096]),0xBC2E000)
        self.assertEqual(sum(size for _,size in sections),70011160)


class MemoryFixture:
    def __init__(self):
        self.module_size = SECTIONS[".data"][0] + SECTIONS[".data"][1] + 0x1000
        self.memory = bytearray(self.module_size)
        self.calls = []
        self._write_pe()

    def _write_pe(self, section_count=2):
        self.memory[0:2] = b"MZ"
        struct.pack_into("<I", self.memory, 0x3C, PE_OFFSET)
        self.memory[PE_OFFSET:PE_OFFSET + 4] = b"PE\0\0"
        struct.pack_into("<H", self.memory, PE_OFFSET + 6, section_count)
        struct.pack_into("<H", self.memory, PE_OFFSET + 20, 0xF0)
        for index, (name, (rva, span)) in enumerate(SECTIONS.items()):
            offset = SECTION_TABLE + index * 40
            self.memory[offset:offset + 8] = name.encode().ljust(8, b"\0")
            struct.pack_into("<IIII", self.memory, offset + 8,
                             span, rva, span, 0)

    def set_section(self, name, *, rva=None, span=None):
        index = list(SECTIONS).index(name)
        offset = SECTION_TABLE + index * 40
        current_rva, current_span = SECTIONS[name]
        struct.pack_into("<II", self.memory, offset + 8,
                         current_span if span is None else span,
                         current_rva if rva is None else rva)
        if rva is not None or span is not None:
            self.module_size = max(self.module_size,
                                   (rva or current_rva) + (span or current_span))

    def add_candidate(self, username, *, section=".rdata",
                      relative=SCAN_CHUNK_BYTES - 16,
                      object_offset=0x3000, cfg_offset=0x5000,
                      object_pointer=None, cfg_pointer=None,
                      sso_size=None, sso_capacity=None, sso_pointer=None):
        rva, span = SECTIONS[section]
        found = rva + relative
        self.memory[found:found + 13] = b"global_config"
        struct.pack_into("<QQ", self.memory, found + 16, 13, 15)
        field_offset = found + 16 - 0x138
        object_address = (BASE + object_offset if object_pointer is None
                          else object_pointer)
        struct.pack_into("<Q", self.memory, field_offset, object_address)
        cfg_address = (BASE + cfg_offset if cfg_pointer is None
                       else cfg_pointer)
        struct.pack_into("<Q", self.memory, object_offset + 0x68, cfg_address)
        sso_offset = cfg_offset + 0x48
        raw = username if isinstance(username, bytes) else username.encode("utf-8")
        size = len(raw) if sso_size is None else sso_size
        capacity = (15 if len(raw) <= 15 else len(raw) + 8
                    ) if sso_capacity is None else sso_capacity
        if capacity == 15 and size <= 15:
            self.memory[sso_offset:sso_offset + 16] = raw[:16].ljust(16, b"\0")
        else:
            pointer_offset = 0x5800 if sso_pointer is None else sso_pointer
            struct.pack_into("<Q", self.memory, sso_offset, BASE + pointer_offset)
            self.memory[pointer_offset:pointer_offset + len(raw)] = raw
        struct.pack_into("<QQ", self.memory, sso_offset + 16, size, capacity)
        return BASE + cfg_address - BASE

    def read(self, address, size):
        self.calls.append((address, size))
        offset = address - BASE
        if offset < 0 or offset + size > len(self.memory):
            return None
        return bytes(self.memory[offset:offset + size])

    def call(self, **overrides):
        kwargs = dict(read=self.read, module_base=BASE,
                      module_size=self.module_size,
                      deadline=time.monotonic() + 3)
        kwargs.update(overrides)
        return locate_account(**kwargs)


class ReadstoreAccountTests(unittest.TestCase):
    def test_normal_chunk_boundary_returns_redacted_internal_identity(self):
        fixture = MemoryFixture()
        fixture.add_candidate("alice")

        identity = fixture.call()

        self.assertIsInstance(identity, AccountIdentity)
        self.assertEqual(identity.cfg_pointer, BASE + 0x5000)
        self.assertEqual(identity.username, "alice")
        self.assertNotIn("alice", repr(identity))
        self.assertGreater(len(fixture.calls), 2)
        self.assertLessEqual(max(size for _, size in fixture.calls[1:]),
                             SCAN_CHUNK_BYTES)
        self.assertFalse(any(address == BASE + 0x5000 + 0x2B8
                              for address, _ in fixture.calls))

    def test_missing_landmark_is_unavailable(self):
        with self.assertRaisesRegex(AccountLookupError, "^account_unavailable$"):
            MemoryFixture().call()

    def test_two_valid_accounts_are_ambiguous(self):
        fixture = MemoryFixture()
        fixture.add_candidate("alice", relative=SCAN_CHUNK_BYTES - 16,
                              object_offset=0x3000, cfg_offset=0x5000)
        fixture.add_candidate("bob", relative=0x400,
                              object_offset=0x3400, cfg_offset=0x5200)

        with self.assertRaisesRegex(AccountLookupError, "^account_ambiguous$"):
            fixture.call()

    def test_same_relative_landmark_in_two_sections_is_not_deduplicated(self):
        fixture = MemoryFixture()
        fixture.add_candidate("alice", section=".rdata", relative=0x100,
                              object_offset=0x3000, cfg_offset=0x5000)
        fixture.add_candidate("bob", section=".data", relative=0x100,
                              object_offset=0x3400, cfg_offset=0x5200)

        with self.assertRaisesRegex(AccountLookupError, "^account_ambiguous$"):
            fixture.call()

    def test_invalid_sso_utf8_and_path_characters_are_not_candidates(self):
        for value in (b"\xff", "alice/b", "alice\\b", "alice:b", "alice\0b"):
            with self.subTest(value=repr(value)):
                fixture = MemoryFixture()
                fixture.add_candidate(value)
                with self.assertRaisesRegex(AccountLookupError,
                                             "^account_unavailable$"):
                    fixture.call()

    def test_invalid_sso_length_or_capacity_is_not_a_candidate(self):
        for size, capacity in ((0, 15), (257, 257), (12, 8)):
            with self.subTest(size=size, capacity=capacity):
                fixture = MemoryFixture()
                fixture.add_candidate(b"alice", sso_size=size,
                                      sso_capacity=capacity)
                with self.assertRaisesRegex(AccountLookupError,
                                             "^account_unavailable$"):
                    fixture.call()

    def test_invalid_object_or_cfg_pointer_is_not_a_candidate(self):
        for object_pointer, cfg_pointer in ((0, None), (MAX_ADDRESS, None),
                                            (None, 0), (None, MAX_ADDRESS)):
            with self.subTest(object_pointer=object_pointer,
                              cfg_pointer=cfg_pointer):
                fixture = MemoryFixture()
                fixture.add_candidate("alice", object_pointer=object_pointer,
                                      cfg_pointer=cfg_pointer)
                with self.assertRaisesRegex(AccountLookupError,
                                             "^account_unavailable$"):
                    fixture.call()

    def test_partial_read_is_rejected_without_using_short_bytes(self):
        fixture = MemoryFixture()

        def partial(address, size):
            value = fixture.read(address, size)
            return None if value is None else value[:-1]

        with self.assertRaisesRegex(AccountLookupError, "^account_partial_read$"):
            fixture.call(read=partial)

    def test_deadline_and_module_bounds_are_rejected_before_scan(self):
        fixture = MemoryFixture()
        with self.assertRaisesRegex(AccountLookupError, "^account_deadline$"):
            fixture.call(deadline=time.monotonic() - 1)
        with self.assertRaisesRegex(AccountLookupError, "^account_input_invalid$"):
            fixture.call(module_size=MAX_MODULE_SIZE)
        with self.assertRaisesRegex(AccountLookupError, "^account_input_invalid$"):
            fixture.call(module_size=4095)

    def test_section_total_scan_budget_is_bounded(self):
        fixture = MemoryFixture()
        fixture.set_section(".rdata", rva=0x1000, span=0x02800001)
        fixture.set_section(".data", rva=0x04000000, span=0x02800001)
        fixture.module_size = 0x06000000

        with self.assertRaisesRegex(AccountLookupError, "^account_section_limit$"):
            fixture.call()

    def test_relocating_account_is_detected_by_identity_comparison(self):
        fixture = MemoryFixture()
        fixture.add_candidate("alice", object_offset=0x3000, cfg_offset=0x5000)
        before = fixture.call()
        fixture.add_candidate("bob", object_offset=0x3400, cfg_offset=0x5200)
        after = fixture.call()

        self.assertNotEqual(before, after)
        self.assertNotEqual(before.cfg_pointer, after.cfg_pointer)
        self.assertEqual(after.username, "bob")


if __name__ == "__main__":
    unittest.main(verbosity=2)
