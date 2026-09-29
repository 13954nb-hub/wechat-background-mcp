"""Bounded, content-free validation for one non-interlaced PNG image."""

import binascii
import struct
import zlib


try:
    from wxbg.policy import AdapterError
except ImportError:
    class AdapterError(RuntimeError):
        """Small standalone fallback when the staged policy is not installed."""

        def __init__(self, code):
            self.code = code
            super().__init__(code)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_INPUT_BYTES = 1 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 4_000_000
MAX_DECODED_BYTES = 16 * 1024 * 1024

_SUPPORTED_COLOR_TYPES = frozenset((0, 2, 3, 4, 6))
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_KNOWN_CRITICAL = frozenset((b"IHDR", b"PLTE", b"IDAT", b"IEND"))
_APNG_CHUNKS = frozenset((b"acTL", b"fcTL", b"fdAT"))


def _fail(code):
    raise AdapterError(code)


def _bounded_bytes(data):
    if isinstance(data, memoryview):
        if data.nbytes > MAX_INPUT_BYTES:
            _fail("invalid_png")
        try:
            data = data.tobytes()
        except Exception:
            _fail("invalid_png")
    elif isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_INPUT_BYTES:
            _fail("invalid_png")
        data = bytes(data)
    else:
        _fail("invalid_png")
    if len(data) > MAX_INPUT_BYTES:
        _fail("invalid_png")
    return data


def _valid_chunk_type(value):
    return (len(value) == 4 and all(
        65 <= item <= 90 or 97 <= item <= 122 for item in value
    ))


def _is_critical(value):
    return (value[0] & 0x20) == 0


def _read_chunks(data):
    if len(data) < len(PNG_SIGNATURE) or data[:8] != PNG_SIGNATURE:
        _fail("invalid_png")

    offset = len(PNG_SIGNATURE)
    seen_ihdr = False
    seen_iend = False
    seen_plte = False
    plte_entries = None
    idat_started = False
    idat_closed = False
    idat_data = []
    width = height = bit_depth = color_type = None

    while offset < len(data):
        if len(data) - offset < 12:
            _fail("invalid_png")
        length = struct.unpack_from(">I", data, offset)[0]
        chunk_type = data[offset + 4:offset + 8]
        if not _valid_chunk_type(chunk_type):
            _fail("invalid_png")
        payload_start = offset + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if payload_end < payload_start or crc_end > len(data):
            _fail("invalid_png")
        chunk_data = data[payload_start:payload_end]
        expected_crc = struct.unpack_from(">I", data, payload_end)[0]
        actual_crc = binascii.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            _fail("invalid_png")
        offset = crc_end

        if not seen_ihdr and chunk_type != b"IHDR":
            _fail("invalid_png")

        if chunk_type == b"IHDR":
            if seen_ihdr or length != 13:
                _fail("invalid_png")
            width, height, bit_depth, color_type, compression, filter_method, interlace = (
                struct.unpack(">IIBBBBB", chunk_data)
            )
            if width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION:
                _fail("invalid_png")
            if width * height > MAX_PIXELS:
                _fail("invalid_png")
            if color_type not in _SUPPORTED_COLOR_TYPES or bit_depth != 8:
                _fail("unsupported_png")
            if compression != 0 or filter_method != 0 or interlace != 0:
                _fail("unsupported_png")
            seen_ihdr = True
            continue

        if chunk_type in _APNG_CHUNKS:
            _fail("unsupported_png")
        if chunk_type not in _KNOWN_CRITICAL and _is_critical(chunk_type):
            _fail("unsupported_png")

        if chunk_type == b"PLTE":
            if seen_plte or idat_started or color_type in (0, 4):
                _fail("invalid_png")
            if length < 3 or length > 768 or length % 3:
                _fail("invalid_png")
            seen_plte = True
            plte_entries = length // 3
        elif chunk_type == b"IDAT":
            if idat_closed or length == 0:
                _fail("invalid_png")
            idat_started = True
            idat_data.append(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0 or not idat_started or offset != len(data):
                _fail("invalid_png")
            seen_iend = True
            break
        elif idat_started:
            idat_closed = True

    if not seen_ihdr or not seen_iend or not idat_data:
        _fail("invalid_png")
    if color_type == 3 and plte_entries is None:
        _fail("invalid_png")

    return width, height, bit_depth, color_type, b"".join(idat_data)


def _validate_scanlines(width, height, color_type, compressed):
    row_payload_bytes = width * _CHANNELS[color_type]
    row_stride = row_payload_bytes + 1
    expected_bytes = row_stride * height
    if expected_bytes > MAX_DECODED_BYTES:
        _fail("invalid_png")

    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, expected_bytes + 1)
        if len(decoded) > expected_bytes or decoder.unconsumed_tail or decoder.unused_data:
            _fail("invalid_png")
        if not decoder.eof:
            _fail("invalid_png")
        decoded += decoder.flush()
    except (zlib.error, ValueError, OverflowError):
        _fail("invalid_png")

    if len(decoded) != expected_bytes:
        _fail("invalid_png")
    for offset in range(0, expected_bytes, row_stride):
        if decoded[offset] > 4:
            _fail("invalid_png")


def validate_png_bytes(data):
    """Validate one bounded PNG and return only its public image dimensions."""
    try:
        payload = _bounded_bytes(data)
        width, height, bit_depth, color_type, compressed = _read_chunks(payload)
        _validate_scanlines(width, height, color_type, compressed)
        return {
            "width": width,
            "height": height,
            "bit_depth": bit_depth,
            "color_type": color_type,
        }
    except AdapterError:
        raise
    except Exception:
        _fail("invalid_png")


__all__ = ["AdapterError", "validate_png_bytes"]
