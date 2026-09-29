"""Bounded, read-only lookup of the currently loaded account identity.

The caller owns the process-memory reader.  This module never opens a process,
reads a file, derives a key, or follows the legacy Config.Cipher path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import struct
import time


MIN_ADDRESS = 0x10000
MAX_ADDRESS = 0x800000000000
MAX_MODULE_SIZE = 512 * 1024 * 1024
MAX_SECTION_COUNT = 32
MAX_SECTION_BYTES = 64 * 1024 * 1024
# Pinned 4.1.13.12 .rdata + .data require 70,011,160 bytes.
MAX_TOTAL_SECTION_BYTES = 80 * 1024 * 1024
MAX_ACCOUNT_BYTES = 256
MAX_SSO_CAPACITY = 4096
MAX_LANDMARKS = 16
SCAN_CHUNK_BYTES = 1024 * 1024
_PE_HEADER_BYTES = 4096
_SECTION_HEADER_BYTES = 40
_LANDMARK = b"global_config"
_LANDMARK_RECORD_BYTES = 32
_LANDMARK_BACK_OFFSET = 0x138


class AccountLookupError(ValueError):
    """Stable, non-sensitive lookup failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AccountIdentity:
    """Internal identity used by a caller to detect an account switch."""

    cfg_pointer: int
    username: str = field(repr=False)

    @property
    def cfg_address(self) -> int:
        return self.cfg_pointer

    def __repr__(self) -> str:
        return f"AccountIdentity(cfg_pointer={self.cfg_pointer}, username=<redacted>)"


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise AccountLookupError("account_deadline")


def _valid_address(address: int, size: int = 1) -> bool:
    return (type(address) is int and type(size) is int
            and size > 0
            and MIN_ADDRESS <= address < MAX_ADDRESS
            and address + size <= MAX_ADDRESS)


def _read_exact(read, address: int, size: int, deadline: float) -> bytes:
    _check_deadline(deadline)
    if not _valid_address(address, size):
        raise AccountLookupError("account_pointer_invalid")
    try:
        value = read(address, size)
    except AccountLookupError:
        raise
    except Exception:
        raise AccountLookupError("account_read_failed") from None
    _check_deadline(deadline)
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise AccountLookupError("account_read_failed")
    value = bytes(value)
    if len(value) != size:
        raise AccountLookupError("account_partial_read")
    return value


def _module_read(read, base: int, module_size: int, offset: int,
                 size: int, deadline: float) -> bytes:
    if (type(offset) is not int or offset < 0
            or offset + size > module_size):
        raise AccountLookupError("account_section_invalid")
    return _read_exact(read, base + offset, size, deadline)


def _read_pointer(read, address: int, deadline: float) -> int | None:
    raw = _read_exact(read, address, 8, deadline)
    value = struct.unpack("<Q", raw)[0]
    return value if _valid_address(value, 1) else None


def _read_username(read, cfg_pointer: int, deadline: float) -> str | None:
    if not _valid_address(cfg_pointer + 0x48, 32):
        return None
    header = _read_exact(read, cfg_pointer + 0x48, 32, deadline)
    size, capacity = struct.unpack_from("<QQ", header, 16)
    if (not 0 < size <= MAX_ACCOUNT_BYTES
            or capacity < size
            or capacity > MAX_SSO_CAPACITY):
        return None
    if capacity == 15 and size <= 15:
        payload = header[:size]
    else:
        pointer = struct.unpack_from("<Q", header, 0)[0]
        if not _valid_address(pointer, size):
            return None
        payload = _read_exact(read, pointer, size, deadline)
    try:
        username = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if not username or any(char in username for char in ("/", "\\", ":", "\0")):
        return None
    return username


def _parse_sections(header: bytes, module_size: int) -> list[tuple[int, int]]:
    if len(header) < _PE_HEADER_BYTES or header[:2] != b"MZ":
        raise AccountLookupError("account_pe_invalid")
    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    if pe_offset < 64 or pe_offset + 24 > _PE_HEADER_BYTES:
        raise AccountLookupError("account_pe_invalid")
    if header[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise AccountLookupError("account_pe_invalid")
    section_count = struct.unpack_from("<H", header, pe_offset + 6)[0]
    if not 0 < section_count <= MAX_SECTION_COUNT:
        raise AccountLookupError("account_section_limit")
    optional_size = struct.unpack_from("<H", header, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    end = table + section_count * _SECTION_HEADER_BYTES
    if table > _PE_HEADER_BYTES or end > _PE_HEADER_BYTES:
        raise AccountLookupError("account_pe_invalid")

    sections = []
    total = 0
    for index in range(section_count):
        entry = header[table + index * _SECTION_HEADER_BYTES:
                       table + (index + 1) * _SECTION_HEADER_BYTES]
        name = entry[:8].split(b"\0", 1)[0]
        if name not in (b".data", b".rdata"):
            continue
        virtual_size, rva, raw_size = struct.unpack_from("<III", entry, 8)
        span = max(virtual_size, raw_size)
        if span == 0:
            continue
        if (span > MAX_SECTION_BYTES or rva >= module_size
                or rva + span > module_size):
            raise AccountLookupError("account_section_limit")
        total += span
        if total > MAX_TOTAL_SECTION_BYTES:
            raise AccountLookupError("account_section_limit")
        sections.append((rva, span))
    if not sections:
        raise AccountLookupError("account_unavailable")
    return sections


def _scan_section(read, base: int, rva: int, span: int, module_size: int,
                  deadline: float, landmark_addresses: set[int]):
    candidates = []
    tail = b""
    offset = 0
    overlap = _LANDMARK_RECORD_BYTES - 1
    while offset < span:
        _check_deadline(deadline)
        take = min(SCAN_CHUNK_BYTES, span - offset)
        data = _module_read(read, base, module_size, rva + offset, take, deadline)
        block = tail + data
        origin = offset - len(tail)
        search = 0
        while True:
            found = block.find(_LANDMARK, search)
            if found < 0:
                break
            absolute = origin + found
            search = found + 1
            landmark_address = base + rva + absolute
            if absolute < 0 or landmark_address in landmark_addresses:
                continue
            if found + _LANDMARK_RECORD_BYTES > len(block):
                continue
            landmark_addresses.add(landmark_address)
            if len(landmark_addresses) > MAX_LANDMARKS:
                raise AccountLookupError("account_landmark_limit")
            size, capacity = struct.unpack_from("<QQ", block, found + 16)
            if (size, capacity) != (13, 15):
                continue
            field_address = landmark_address + 16 - _LANDMARK_BACK_OFFSET
            if not (base <= field_address and field_address + 8 <= base + module_size):
                continue
            object_pointer = _read_pointer(read, field_address, deadline)
            if object_pointer is None or not _valid_address(object_pointer + 0x68, 8):
                continue
            cfg_pointer = _read_pointer(read, object_pointer + 0x68, deadline)
            if cfg_pointer is None:
                continue
            username = _read_username(read, cfg_pointer, deadline)
            if username is not None:
                candidates.append(AccountIdentity(cfg_pointer, username))
        tail = block[-overlap:]
        offset += take
    return candidates


def locate_account(read, module_base, module_size, deadline) -> AccountIdentity:
    """Locate one unique account identity using only the caller's memory reader."""
    if (not callable(read)
            or type(module_base) is not int
            or type(module_size) is not int
            or type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or not MIN_ADDRESS <= module_base < MAX_ADDRESS
            or not _PE_HEADER_BYTES <= module_size < MAX_MODULE_SIZE
            or module_base + module_size > MAX_ADDRESS):
        raise AccountLookupError("account_input_invalid")
    _check_deadline(deadline)
    header = _read_exact(read, module_base, _PE_HEADER_BYTES, deadline)
    sections = _parse_sections(header, module_size)
    landmark_addresses: set[int] = set()
    found: dict[tuple[int, str], AccountIdentity] = {}
    for rva, span in sections:
        for identity in _scan_section(read, module_base, rva, span,
                                      module_size, deadline, landmark_addresses):
            found[(identity.cfg_pointer, identity.username)] = identity
            if len(found) > 1:
                raise AccountLookupError("account_ambiguous")
    if len(found) != 1:
        raise AccountLookupError("account_unavailable")
    return next(iter(found.values()))


__all__ = [
    "AccountIdentity",
    "AccountLookupError",
    "MAX_ACCOUNT_BYTES",
    "MAX_MODULE_SIZE",
    "MAX_SECTION_COUNT",
    "MAX_TOTAL_SECTION_BYTES",
    "SCAN_CHUNK_BYTES",
    "locate_account",
]
