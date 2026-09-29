"""Pure, fail-closed decoding for WeChat 4.x message bodies.

The caller supplies the already-read ``message_content`` value and its
``WCDB_CT_message_content`` flag.  This module never opens a database, file,
process, cache, or network connection.  The primary WCDB compression
constants encode normal Zstandard text as merged flag ``4``; only
``NULL/0`` (plain) and that value are accepted.  Every other flag is
unavailable rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import math
import time
from typing import Any


MAX_INPUT_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 200_000
MAX_OUTPUT_RATIO = 128.0
MAX_DECODE_SECONDS = 0.25
ZSTD_COMPRESSION_FLAG = 4
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# python-zstandard expresses this limit in KiB and rejects values below 1024.
# Keep the native window bounded even when the public output limit is smaller.
_MIN_ZSTD_WINDOW_KIB = 1024


@dataclass(frozen=True, slots=True)
class BodyDecodeResult:
    """Bounded decode outcome; unavailable outcomes never carry body text."""

    available: bool
    text: str | None = field(repr=False)
    code: str
    compression: str
    compression_flag: int | None
    input_bytes: int
    output_bytes: int

    @property
    def ok(self) -> bool:
        return self.available

    def __repr__(self) -> str:
        return (
            "BodyDecodeResult(available=%r, code=%r, compression=%r, "
            "compression_flag=%r, input_bytes=%d, output_bytes=%d)"
            % (
                self.available,
                self.code,
                self.compression,
                self.compression_flag,
                self.input_bytes,
                self.output_bytes,
            )
        )


def _available(
    text: str,
    code: str,
    compression: str,
    flag: int | None,
    input_bytes: int,
) -> BodyDecodeResult:
    return BodyDecodeResult(
        True,
        text,
        code,
        compression,
        flag,
        input_bytes,
        len(text.encode("utf-8")),
    )


def _unavailable(
    code: str,
    compression: str,
    flag: int | None,
    input_bytes: int,
    output_bytes: int = 0,
) -> BodyDecodeResult:
    return BodyDecodeResult(
        False,
        None,
        code,
        compression,
        flag,
        input_bytes,
        output_bytes,
    )


def _valid_limit(value: Any, maximum: float, *, integer: bool = False):
    if integer:
        if type(value) is not int or value <= 0 or value > maximum:
            return None
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > maximum:
        return None
    return value


def _normalise_flag(value: Any) -> tuple[int | None, str | None]:
    if value is None:
        return 0, None
    if type(value) is not int:
        return None, "compression_flag_invalid"
    if value == 0:
        return 0, None
    if value == ZSTD_COMPRESSION_FLAG:
        return value, None
    return None, "compression_flag_unknown"


def _normalise_input(value: Any) -> tuple[str | bytes | None, int, str | None]:
    if isinstance(value, str):
        # Check a conservative UTF-8 upper bound before invoking encode().
        # Calling str.__len__/str.isascii also avoids overridable subclass
        # methods being able to force an unbounded temporary allocation.
        character_count = str.__len__(value)
        if character_count > MAX_INPUT_BYTES:
            return None, character_count, "input_too_large"
        if not str.isascii(value) and character_count > MAX_INPUT_BYTES // 4:
            return None, character_count * 4, "input_too_large"
        try:
            raw = str.encode(value, "utf-8")
        except UnicodeEncodeError:
            return None, 0, "invalid_utf8"
        if len(raw) > MAX_INPUT_BYTES:
            return None, len(raw), "input_too_large"
        return value, len(raw), None
    if isinstance(value, memoryview):
        # nbytes is available without materialising a non-contiguous view.
        input_bytes = value.nbytes
        if input_bytes > MAX_INPUT_BYTES:
            return None, input_bytes, "input_too_large"
        try:
            raw = bytes(value)
        except (TypeError, ValueError):
            return None, 0, "input_invalid"
        return raw, len(raw), None
    if isinstance(value, (bytes, bytearray)):
        input_bytes = len(value)
        if input_bytes > MAX_INPUT_BYTES:
            return None, input_bytes, "input_too_large"
        try:
            raw = bytes(value)
        except (TypeError, ValueError):
            return None, 0, "input_invalid"
        return raw, len(raw), None
    return None, 0, "input_type_invalid"


def _deadline_value(deadline: Any, now: float) -> tuple[float | None, str | None]:
    if deadline is None:
        return now + MAX_DECODE_SECONDS, None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return None, "decode_deadline_invalid"
    deadline = float(deadline)
    if not math.isfinite(deadline):
        return None, "decode_deadline_invalid"
    return deadline, None


def _inspect_zstd_frame(frame: bytes) -> str | None:
    """Return a bounded, explicit header issue before using zstandard."""
    if len(frame) < 5:
        return "zstd_truncated"
    descriptor = frame[4]
    if descriptor & 0x18:
        return "zstd_invalid_header"
    fcs_flag = descriptor >> 6
    single_segment = bool(descriptor & 0x20)
    dictionary_flag = descriptor & 0x03
    dictionary_bytes = (0, 1, 2, 4)[dictionary_flag]
    if not single_segment:
        header_bytes = 1  # Window_Descriptor
    else:
        header_bytes = 0
    header_bytes += dictionary_bytes
    if fcs_flag == 0:
        header_bytes += 1 if single_segment else 0
    elif fcs_flag == 1:
        header_bytes += 2
    elif fcs_flag == 2:
        header_bytes += 4
    else:
        header_bytes += 8
    header_end = 5 + header_bytes
    if len(frame) < header_end:
        return "zstd_truncated"
    if dictionary_flag:
        return "zstd_dictionary_required"
    if len(frame) < header_end + 3:
        return "zstd_truncated"
    block_header = int.from_bytes(frame[header_end:header_end + 3], "little")
    block_type = (block_header >> 1) & 0x03
    block_size = block_header >> 3
    if block_type == 3:
        return "zstd_invalid_header"
    payload_bytes = 1 if block_type == 1 else block_size
    if len(frame) < header_end + 3 + payload_bytes:
        return "zstd_truncated"
    return None


def _classify_zstd_error(exc: Exception) -> str:
    message = str(exc).lower()
    if any(
        token in message
        for token in (
            "dictionary",
            "dict mismatch",
            "requires dict",
        )
    ):
        return "zstd_dictionary_required"
    if any(
        token in message
        for token in (
            "unused data",
            "extra data",
            "trailing",
            "concatenated",
        )
    ):
        return "zstd_trailing_data"
    if any(
        token in message
        for token in (
            "incomplete",
            "truncated",
            "unexpected end",
            "src size",
            "frame size",
        )
    ):
        return "zstd_truncated"
    if any(
        token in message
        for token in (
            "destination buffer",
            "max output",
            "too large",
            "requires too much memory",
            "dstsize",
        )
    ):
        return "output_bound_exceeded"
    return "zstd_decode_failed"


def _load_zstandard():
    try:
        return importlib.import_module("zstandard")
    except (ImportError, ModuleNotFoundError):
        return None
    except Exception:
        return None


def decode_body(
    content: str | bytes | bytearray | memoryview,
    compression_flag: int | None,
    *,
    deadline: float | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    max_output_ratio: float = MAX_OUTPUT_RATIO,
) -> BodyDecodeResult:
    """Decode one already-read body with strict flags and bounded work.

    ``deadline`` is an absolute ``time.monotonic()`` deadline.  When omitted,
    a short local deadline is used.  The native decoder receives bounded
    output and window ceilings derived from ``max_output_bytes`` and
    ``max_output_ratio``; the deadline is checked before and after the single
    bounded native call.
    """
    now = time.monotonic()
    effective_deadline, deadline_error = _deadline_value(deadline, now)
    if deadline_error is not None:
        return _unavailable(deadline_error, "unknown", None, 0)
    assert effective_deadline is not None
    if now >= effective_deadline:
        return _unavailable("decode_deadline_exceeded", "unknown", None, 0)

    output_limit = _valid_limit(max_output_bytes, MAX_OUTPUT_BYTES, integer=True)
    ratio_limit = _valid_limit(max_output_ratio, MAX_OUTPUT_RATIO)
    if output_limit is None or ratio_limit is None:
        return _unavailable("decode_limits_invalid", "unknown", None, 0)

    flag, flag_error = _normalise_flag(compression_flag)
    if flag_error is not None:
        return _unavailable(flag_error, "unknown", None, 0)
    assert flag is not None

    normalised, input_bytes, input_error = _normalise_input(content)
    if input_error is not None:
        return _unavailable(input_error, "unknown", flag, input_bytes)
    assert normalised is not None
    if input_bytes > MAX_INPUT_BYTES:
        return _unavailable("input_too_large", "plain" if flag == 0 else "zstd", flag, input_bytes)
    if input_bytes == 0:
        return _unavailable("empty_input", "plain" if flag == 0 else "zstd", flag, 0)

    if flag == 0:
        if not isinstance(normalised, str):
            try:
                text = normalised.decode("utf-8")
            except UnicodeDecodeError:
                return _unavailable("invalid_utf8", "plain", flag, input_bytes)
        else:
            text = normalised
        output_bytes = len(text.encode("utf-8"))
        if output_bytes > output_limit:
            return _unavailable("output_too_large", "plain", flag, input_bytes, output_bytes)
        if time.monotonic() >= effective_deadline:
            return _unavailable("decode_deadline_exceeded", "plain", flag, input_bytes)
        return _available(text, "decoded_plain", "plain", flag, input_bytes)

    if isinstance(normalised, str):
        return _unavailable("compressed_requires_bytes", "zstd", flag, input_bytes)
    if not normalised.startswith(_ZSTD_MAGIC):
        return _unavailable("zstd_magic_missing", "zstd", flag, input_bytes)
    frame_error = _inspect_zstd_frame(normalised)
    if frame_error is not None:
        return _unavailable(frame_error, "zstd", flag, input_bytes)

    if time.monotonic() >= effective_deadline:
        return _unavailable("decode_deadline_exceeded", "zstd", flag, input_bytes)
    zstandard = _load_zstandard()
    if zstandard is None:
        return _unavailable("zstd_dependency_missing", "zstd", flag, input_bytes)
    if time.monotonic() >= effective_deadline:
        return _unavailable("decode_deadline_exceeded", "zstd", flag, input_bytes)

    allowed_output = min(output_limit, max(1, int(math.ceil(input_bytes * ratio_limit))))
    frame_size_reader = getattr(zstandard, "frame_content_size", None)
    if not callable(frame_size_reader):
        return _unavailable("zstd_dependency_incompatible", "zstd", flag, input_bytes)
    try:
        frame_content_size = frame_size_reader(normalised)
    except Exception:
        return _unavailable("zstd_invalid_header", "zstd", flag, input_bytes)
    # python-zstandard's one-shot API can resize past max_output_size when a
    # frame advertises its content size.  Reject an advertised size before
    # entering native code.  -1 (unknown) and other negative sentinel values
    # retain the max_output_size buffer bound below.
    if type(frame_content_size) is int and frame_content_size >= 0:
        if frame_content_size > allowed_output:
            return _unavailable(
                "output_bound_exceeded",
                "zstd",
                flag,
                input_bytes,
                frame_content_size,
            )
    window_limit_kib = max(
        _MIN_ZSTD_WINDOW_KIB,
        int(math.ceil(allowed_output / 1024)),
    )
    try:
        decompressed = zstandard.ZstdDecompressor(
            max_window_size=window_limit_kib,
        ).decompress(
            normalised,
            max_output_size=allowed_output,
            allow_extra_data=False,
        )
    except Exception as exc:
        if time.monotonic() >= effective_deadline:
            return _unavailable("decode_deadline_exceeded", "zstd", flag, input_bytes)
        return _unavailable(_classify_zstd_error(exc), "zstd", flag, input_bytes)
    if time.monotonic() >= effective_deadline:
        return _unavailable("decode_deadline_exceeded", "zstd", flag, input_bytes)
    if not isinstance(decompressed, (bytes, bytearray, memoryview)):
        return _unavailable("zstd_output_invalid", "zstd", flag, input_bytes)
    decompressed = bytes(decompressed)
    output_bytes = len(decompressed)
    if output_bytes > output_limit:
        return _unavailable("output_too_large", "zstd", flag, input_bytes, output_bytes)
    if output_bytes > input_bytes * ratio_limit:
        return _unavailable("output_ratio_exceeded", "zstd", flag, input_bytes, output_bytes)
    try:
        text = decompressed.decode("utf-8")
    except UnicodeDecodeError:
        return _unavailable("invalid_utf8", "zstd", flag, input_bytes, output_bytes)
    return _available(text, "decoded_zstd", "zstd", flag, input_bytes)


decode_message_body = decode_body


__all__ = [
    "BodyDecodeResult",
    "MAX_DECODE_SECONDS",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_OUTPUT_RATIO",
    "ZSTD_COMPRESSION_FLAG",
    "decode_body",
    "decode_message_body",
]
