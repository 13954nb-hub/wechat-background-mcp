"""Bounded static image-format inspection for read-only image evidence.

The function in this module accepts bytes only.  Pillow is loaded lazily and
is used after a format-specific dimension preflight; no decoded pixels,
metadata, paths, or source bytes are returned.
"""

from __future__ import annotations

import io
import struct
import warnings
from typing import Final, Any


MAX_INPUT_BYTES: Final[int] = 16 * 1024 * 1024
MAX_PIXELS: Final[int] = 16_000_000
MAX_DIMENSION: Final[int] = 8192

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI = b"\xff\xd8"
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"
_APNG_CHUNKS = frozenset((b"acTL", b"fcTL", b"fdAT"))
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xDA)})
_JPEG_SOF = frozenset(
    set(range(0xC0, 0xC4))
    | set(range(0xC5, 0xC8))
    | set(range(0xC9, 0xCC))
    | set(range(0xCD, 0xD0))
)
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_EXTENSION = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


class ImageFormatError(ValueError):
    """Stable validation failure without retaining image bytes or metadata."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ImageFormatError(code={self.code!r})"


def _fail(code: str) -> None:
    raise ImageFormatError(code)


def _validate_limits(max_pixels: int, max_dimension: int) -> None:
    if (
        type(max_pixels) is not int
        or not 1 <= max_pixels <= MAX_PIXELS
        or type(max_dimension) is not int
        or not 1 <= max_dimension <= MAX_DIMENSION
    ):
        _fail("invalid_limits")


def _bounded_bytes(data: bytes) -> bytes:
    if type(data) is not bytes:
        _fail("invalid_data")
    if len(data) > MAX_INPUT_BYTES:
        _fail("input_too_large")
    return data


def _check_dimensions(
    width: int,
    height: int,
    *,
    max_pixels: int,
    max_dimension: int,
) -> tuple[int, int]:
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        _fail("invalid_image")
    if width > max_dimension or height > max_dimension:
        _fail("dimension_limit")
    if width * height > max_pixels:
        _fail("pixel_limit")
    return width, height


def _png_header(
    data: bytes,
    *,
    max_pixels: int,
    max_dimension: int,
) -> tuple[int, int]:
    if len(data) < len(_PNG_SIGNATURE) + 12:
        _fail("truncated")
    offset = len(_PNG_SIGNATURE)
    seen_ihdr = False
    seen_iend = False
    dimensions: tuple[int, int] | None = None
    while offset < len(data):
        if len(data) - offset < 12:
            _fail("truncated")
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            _fail("truncated")
        chunk = data[offset + 8:offset + 8 + length]
        if not seen_ihdr:
            if kind != b"IHDR" or length != 13:
                _fail("invalid_image")
            width, height = struct.unpack_from(">II", chunk, 0)
            dimensions = _check_dimensions(
                width,
                height,
                max_pixels=max_pixels,
                max_dimension=max_dimension,
            )
            seen_ihdr = True
        elif kind == b"IHDR":
            _fail("invalid_image")

        if kind in _APNG_CHUNKS:
            _fail("animated")
        if kind == b"IEND":
            if length != 0:
                _fail("invalid_image")
            seen_iend = True
            offset = end
            break
        offset = end

    if not seen_ihdr or dimensions is None:
        _fail("invalid_image")
    if not seen_iend:
        _fail("truncated")
    if offset != len(data):
        _fail("invalid_image")
    return dimensions


def _jpeg_header(
    data: bytes,
    *,
    max_pixels: int,
    max_dimension: int,
) -> tuple[int, int]:
    if len(data) < 2:
        _fail("truncated")
    position = 2
    dimensions: tuple[int, int] | None = None
    while position < len(data):
        if data[position] != 0xFF:
            _fail("invalid_image")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            _fail("truncated")
        marker = data[position]
        position += 1
        if marker == 0x00:
            _fail("invalid_image")
        if marker == 0xD9:
            break
        if marker in _JPEG_STANDALONE:
            continue
        if position + 2 > len(data):
            _fail("truncated")
        segment_length = struct.unpack_from(">H", data, position)[0]
        if segment_length < 2:
            _fail("invalid_image")
        segment_end = position + segment_length
        if segment_end > len(data):
            _fail("truncated")
        if marker in _JPEG_SOF and dimensions is None:
            if segment_length < 7:
                _fail("invalid_image")
            height, width = struct.unpack_from(">HH", data, position + 3)
            dimensions = _check_dimensions(
                width,
                height,
                max_pixels=max_pixels,
                max_dimension=max_dimension,
            )
        if marker == 0xDA:
            if dimensions is None:
                _fail("invalid_image")
            # The entropy-coded part is opaque here, but a complete JPEG must
            # still contain its EOI marker before Pillow performs full decode.
            if b"\xff\xd9" not in data[segment_end:]:
                _fail("truncated")
            return dimensions
        position = segment_end

    _fail("invalid_image")


def _webp_header(
    data: bytes,
    *,
    max_pixels: int,
    max_dimension: int,
) -> tuple[int, int]:
    if len(data) < 12:
        _fail("truncated")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size < 4:
        _fail("invalid_image")
    declared_end = riff_size + 8
    if declared_end > len(data):
        _fail("truncated")
    if declared_end != len(data):
        _fail("invalid_image")

    position = 12
    dimensions: tuple[int, int] | None = None
    frame_chunks = 0
    while position < declared_end:
        if declared_end - position < 8:
            _fail("truncated")
        kind = data[position:position + 4]
        length = struct.unpack_from("<I", data, position + 4)[0]
        chunk_start = position + 8
        chunk_end = chunk_start + length
        if chunk_end > declared_end:
            _fail("truncated")
        chunk = data[chunk_start:chunk_end]

        if kind in (b"ANIM", b"ANMF"):
            _fail("animated")
        if kind == b"VP8X":
            if length < 10:
                _fail("truncated")
            if chunk[0] & 0x02:
                _fail("animated")
            width = 1 + int.from_bytes(chunk[4:7], "little")
            height = 1 + int.from_bytes(chunk[7:10], "little")
            dimensions = _check_dimensions(
                width,
                height,
                max_pixels=max_pixels,
                max_dimension=max_dimension,
            )
        elif kind == b"VP8 ":
            frame_chunks += 1
            if frame_chunks > 1:
                _fail("animated")
            if length < 10 or chunk[3:6] != b"\x9d\x01\x2a":
                _fail("invalid_image")
            if dimensions is None:
                width = struct.unpack_from("<H", chunk, 6)[0] & 0x3FFF
                height = struct.unpack_from("<H", chunk, 8)[0] & 0x3FFF
                dimensions = _check_dimensions(
                    width,
                    height,
                    max_pixels=max_pixels,
                    max_dimension=max_dimension,
                )
        elif kind == b"VP8L":
            frame_chunks += 1
            if frame_chunks > 1:
                _fail("animated")
            if length < 5 or chunk[0] != 0x2F:
                _fail("invalid_image")
            bits = int.from_bytes(chunk[1:5], "little")
            width = 1 + (bits & 0x3FFF)
            height = 1 + ((bits >> 14) & 0x3FFF)
            if dimensions is None:
                dimensions = _check_dimensions(
                    width,
                    height,
                    max_pixels=max_pixels,
                    max_dimension=max_dimension,
                )

        position = chunk_end + (length & 1)
        if position > declared_end:
            _fail("truncated")

    if dimensions is None:
        _fail("invalid_image")
    return dimensions


def _load_pillow() -> Any:
    try:
        from PIL import Image
    except Exception as exc:
        raise ImageFormatError("unavailable") from exc
    return Image


def _pillow_validate(
    data: bytes,
    *,
    image_format: str,
    dimensions: tuple[int, int],
    image_module: Any,
) -> None:
    bomb_types = tuple(
        value
        for value in (
            getattr(image_module, "DecompressionBombError", None),
            getattr(image_module, "DecompressionBombWarning", None),
        )
        if isinstance(value, type)
    )
    bomb_warning = getattr(image_module, "DecompressionBombWarning", None)
    try:
        with warnings.catch_warnings():
            if isinstance(bomb_warning, type):
                warnings.simplefilter("error", bomb_warning)
            with image_module.open(io.BytesIO(data)) as opened:
                if opened.format != image_format:
                    _fail("invalid_image")
                try:
                    frames = int(getattr(opened, "n_frames", 1))
                except (TypeError, ValueError, OverflowError):
                    _fail("invalid_image")
                if bool(getattr(opened, "is_animated", False)) or frames != 1:
                    _fail("animated")
                if tuple(opened.size) != dimensions:
                    _fail("invalid_image")
                opened.load()
            with image_module.open(io.BytesIO(data)) as verified:
                verified.verify()
    except ImageFormatError:
        raise
    except Exception as exc:
        if bomb_types and isinstance(exc, bomb_types):
            _fail("pixel_limit")
        message = str(exc).lower()
        if "truncat" in message or "end of data" in message or "eof" in message:
            _fail("truncated")
        _fail("invalid_image")


def inspect_image(
    data: bytes,
    *,
    max_pixels: int = MAX_PIXELS,
    max_dimension: int = MAX_DIMENSION,
) -> dict[str, object]:
    """Inspect one bounded static PNG, JPEG, or WebP byte string."""

    _validate_limits(max_pixels, max_dimension)
    payload = _bounded_bytes(data)

    if payload.startswith(_PNG_SIGNATURE):
        image_format = "PNG"
        dimensions = _png_header(
            payload,
            max_pixels=max_pixels,
            max_dimension=max_dimension,
        )
    elif payload.startswith(_JPEG_SOI):
        image_format = "JPEG"
        dimensions = _jpeg_header(
            payload,
            max_pixels=max_pixels,
            max_dimension=max_dimension,
        )
    elif (
        len(payload) >= 12
        and payload[:4] == _WEBP_RIFF
        and payload[8:12] == _WEBP_TAG
    ):
        image_format = "WEBP"
        dimensions = _webp_header(
            payload,
            max_pixels=max_pixels,
            max_dimension=max_dimension,
        )
    else:
        _fail("unsupported_format")

    image_module = _load_pillow()
    _pillow_validate(
        payload,
        image_format=image_format,
        dimensions=dimensions,
        image_module=image_module,
    )
    return {
        "format": image_format,
        "mime_type": _MIME[image_format],
        "width": dimensions[0],
        "height": dimensions[1],
        "extension": _EXTENSION[image_format],
    }


__all__ = ["ImageFormatError", "inspect_image"]
