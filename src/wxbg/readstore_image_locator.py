"""Parse the bounded packed-info basename locator for a message image.

The observed packed-info wire shape is deliberately narrow: one outer field
3 (length-delimited) contains one field 4 (length-delimited) whose value is
exactly 32 ASCII hexadecimal bytes.  The value is returned only as a
``candidate_basename``.  It is an index hint, never a checksum, proof of
cache bytes, or proof of an original rendition.

This module accepts bytes supplied by the caller and performs no file,
database, process, client, key, or network operation.
"""

from __future__ import annotations

import re
from typing import Any


MAX_PACKED_INFO_BYTES = 65_536
MAX_PROTO_FIELDS = 256
_MAX_FIELD_NUMBER = (1 << 29) - 1
_HEX32 = re.compile(rb"[0-9A-Fa-f]{32}\Z")


class ImageLocatorError(ValueError):
    """Stable locator failure without payload data in its message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ImageLocatorError(code)


def _bounded_bytes(value: Any) -> bytes:
    if isinstance(value, memoryview):
        if value.nbytes > MAX_PACKED_INFO_BYTES:
            _fail("packed_info_too_large")
        try:
            value = bytes(value)
        except (TypeError, ValueError):
            _fail("packed_info_invalid")
    elif isinstance(value, bytearray):
        if len(value) > MAX_PACKED_INFO_BYTES:
            _fail("packed_info_too_large")
        value = bytes(value)
    elif isinstance(value, bytes):
        if len(value) > MAX_PACKED_INFO_BYTES:
            _fail("packed_info_too_large")
    else:
        _fail("packed_info_invalid")
    if not value:
        _fail("packed_info_empty")
    if len(value) > MAX_PACKED_INFO_BYTES:
        _fail("packed_info_too_large")
    return value


def _encode_varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    start = position
    value = 0
    for index in range(10):
        if position >= len(data):
            _fail("wire_truncated")
        byte = data[position]
        position += 1
        if index == 9 and byte > 1:
            _fail("wire_invalid")
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            if data[start:position] != _encode_varint(value):
                _fail("wire_invalid")
            return value, position
    _fail("wire_invalid")
    raise AssertionError("unreachable")


class _FieldBudget:
    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def consume(self) -> None:
        self.count += 1
        if self.count > MAX_PROTO_FIELDS:
            _fail("field_budget_exceeded")


def _header(data: bytes, position: int) -> tuple[int, int, int]:
    key, position = _read_varint(data, position)
    field_number = key >> 3
    wire_type = key & 0x07
    if not 1 <= field_number <= _MAX_FIELD_NUMBER:
        _fail("wire_invalid")
    if wire_type > 5:
        _fail("wire_invalid")
    return field_number, wire_type, position


def _skip_value(
    data: bytes,
    position: int,
    wire_type: int,
) -> int:
    if wire_type == 0:
        _, position = _read_varint(data, position)
        return position
    if wire_type == 1:
        end = position + 8
        if end > len(data):
            _fail("wire_truncated")
        return end
    if wire_type == 2:
        length, position = _read_varint(data, position)
        end = position + length
        if end < position or end > len(data):
            _fail("wire_truncated")
        return end
    if wire_type == 5:
        end = position + 4
        if end > len(data):
            _fail("wire_truncated")
        return end
    # Groups (wire 3/4) are not accepted as an opaque extension.  Supporting
    # them would require recursive matching of start/end field numbers and is
    # outside the observed packed-info contract.
    _fail("wire_unsupported")
    raise AssertionError("unreachable")


def _inner_candidate(data: bytes, budget: _FieldBudget) -> str:
    position = 0
    candidate: bytes | None = None
    while position < len(data):
        budget.consume()
        field_number, wire_type, position = _header(data, position)
        if field_number == 4:
            if wire_type != 2:
                _fail("field4_wire_invalid")
            if candidate is not None:
                _fail("field4_duplicate")
            length, position = _read_varint(data, position)
            end = position + length
            if end < position or end > len(data):
                _fail("wire_truncated")
            if length != 32:
                _fail("candidate_invalid")
            value = data[position:end]
            if _HEX32.fullmatch(value) is None:
                _fail("candidate_invalid")
            candidate = value.lower()
            position = end
        else:
            position = _skip_value(data, position, wire_type)
    if candidate is None:
        _fail("field4_missing")
    return candidate.decode("ascii")


def parse_image_locator(
    packed_info_data: bytes | bytearray | memoryview,
) -> dict[str, str]:
    """Return one lower-case candidate basename from packed-info protobuf."""

    data = _bounded_bytes(packed_info_data)
    budget = _FieldBudget()
    position = 0
    candidate: str | None = None
    while position < len(data):
        budget.consume()
        field_number, wire_type, position = _header(data, position)
        if field_number == 3:
            if wire_type != 2:
                _fail("field3_wire_invalid")
            if candidate is not None:
                _fail("field3_duplicate")
            length, position = _read_varint(data, position)
            end = position + length
            if end < position or end > len(data):
                _fail("wire_truncated")
            candidate = _inner_candidate(data[position:end], budget)
            position = end
        else:
            position = _skip_value(data, position, wire_type)
    if candidate is None:
        _fail("field3_missing")
    return {"candidate_basename": candidate}


__all__ = [
    "ImageLocatorError",
    "MAX_PACKED_INFO_BYTES",
    "MAX_PROTO_FIELDS",
    "parse_image_locator",
]
