"""Local, bounded JPEG/WebP/PNG to PNG normalization.

This module only reads and writes caller-selected local files.  Pillow is imported
inside the preparation path so importing the wider wxbg package does not require
the optional image dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import stat
import sys
import warnings
from typing import Any


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_PIXELS = 16_000_000
MAX_DIMENSION = 4096
MAX_PIXELS = 4_000_000
MAX_OUTPUT_BYTES = 1 * 1024 * 1024
_ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_APNG_CHUNKS = frozenset((b"acTL", b"fcTL", b"fdAT"))


_MESSAGES = {
    "invalid_limits": "image limits must be positive integers",
    "invalid_path": "image paths must be local path-like values",
    "input_not_found": "input image does not exist",
    "input_not_file": "input image is not a regular file",
    "input_read_failed": "input image could not be read",
    "input_too_large": "input image exceeds the bounded byte limit",
    "pixel_limit": "input image exceeds the bounded decode pixel limit",
    "unsupported_format": "input content is not JPEG, PNG, or WebP",
    "invalid_image": "input image content is invalid or truncated",
    "animated_image": "animated images are not accepted",
    "destination_exists": "destination already exists and will not be overwritten",
    "destination_parent_missing": "destination parent directory does not exist",
    "destination_write_failed": "new PNG could not be written",
    "output_too_large": "PNG could not meet the bounded output byte limit",
    "output_invalid": "generated PNG failed the staged PNG validator",
    "pillow_missing": "Pillow is required for image preparation",
    "validator_missing": "the staged PNG validator is unavailable",
}


class ImagePrepareError(RuntimeError):
    """Stable, local-only image preparation failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = _MESSAGES.get(code, "image preparation failed")
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def _positive_limit(value: int, hard_limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ImagePrepareError("invalid_limits")
    return min(value, hard_limit)


def _path_value(value: str | os.PathLike[str]) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ImagePrepareError("invalid_path")
    if isinstance(value, str) and value == "":
        raise ImagePrepareError("invalid_path")
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError) as exc:
        raise ImagePrepareError("invalid_path") from exc
    if not str(path):
        raise ImagePrepareError("invalid_path")
    return path


def _load_pillow() -> tuple[Any, Any]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ImagePrepareError("pillow_missing") from exc
    return Image, ImageOps


def _read_bounded_source(source: Path, max_input_bytes: int) -> bytes:
    try:
        initial = source.stat()
    except FileNotFoundError as exc:
        raise ImagePrepareError("input_not_found") from exc
    except OSError as exc:
        raise ImagePrepareError("input_read_failed") from exc
    if not stat.S_ISREG(initial.st_mode):
        raise ImagePrepareError("input_not_file")
    if initial.st_size > max_input_bytes:
        raise ImagePrepareError("input_too_large")
    try:
        with source.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ImagePrepareError("input_not_file")
            if (
                before.st_dev != initial.st_dev
                or before.st_ino != initial.st_ino
            ):
                raise ImagePrepareError("input_read_failed")
            if before.st_size > max_input_bytes:
                raise ImagePrepareError("input_too_large")
            payload = handle.read(max_input_bytes + 1)
            after = os.fstat(handle.fileno())
            if not stat.S_ISREG(after.st_mode):
                raise ImagePrepareError("input_not_file")
            if after.st_size > max_input_bytes or len(payload) > max_input_bytes:
                raise ImagePrepareError("input_too_large")
            if after.st_size != before.st_size or len(payload) != after.st_size:
                raise ImagePrepareError("input_read_failed")
    except ImagePrepareError:
        raise
    except (OSError, TypeError) as exc:
        raise ImagePrepareError("input_read_failed") from exc
    if not isinstance(payload, bytes):
        raise ImagePrepareError("input_read_failed")
    return payload


def _has_apng_control_chunks(payload: bytes) -> bool:
    if len(payload) < len(_PNG_SIGNATURE) or payload[:8] != _PNG_SIGNATURE:
        return False
    offset = len(_PNG_SIGNATURE)
    while offset + 12 <= len(payload):
        length = int.from_bytes(payload[offset:offset + 4], "big")
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            return False
        kind = payload[offset + 4:offset + 8]
        if kind in _APNG_CHUNKS:
            return True
        offset = chunk_end
        if kind == b"IEND":
            break
    return False


def _check_input_header(
    payload: bytes,
    max_input_pixels: int,
    image_module: Any,
) -> tuple[str, tuple[int, int]]:
    bomb_error = getattr(image_module, "DecompressionBombError", RuntimeError)
    bomb_warning = getattr(image_module, "DecompressionBombWarning", Warning)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", bomb_warning)
            with image_module.open(io.BytesIO(payload)) as opened:
                image_format = opened.format
                if image_format not in _ACCEPTED_FORMATS:
                    raise ImagePrepareError("unsupported_format")
                if bool(getattr(opened, "is_animated", False)) or (
                    int(getattr(opened, "n_frames", 1)) > 1
                ):
                    raise ImagePrepareError("animated_image")
                width, height = opened.size
                if (
                    not isinstance(width, int)
                    or not isinstance(height, int)
                    or width < 1
                    or height < 1
                    or width * height > max_input_pixels
                ):
                    raise ImagePrepareError("pixel_limit")
                opened.verify()
                if image_format == "PNG" and _has_apng_control_chunks(payload):
                    raise ImagePrepareError("animated_image")
    except ImagePrepareError:
        raise
    except bomb_error as exc:
        raise ImagePrepareError("pixel_limit") from exc
    except bomb_warning as exc:
        raise ImagePrepareError("pixel_limit") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImagePrepareError("invalid_image") from exc
    except Exception as exc:
        unidentified = getattr(image_module, "UnidentifiedImageError", ())
        if unidentified and isinstance(exc, unidentified):
            raise ImagePrepareError("invalid_image") from exc
        raise ImagePrepareError("invalid_image") from exc
    return image_format, (width, height)


def _has_alpha(image: Any) -> bool:
    try:
        if "A" in image.getbands():
            return True
    except (AttributeError, TypeError):
        pass
    return "transparency" in getattr(image, "info", {})


def _normalized_image(
    payload: bytes,
    image_module: Any,
    image_ops: Any,
) -> Any:
    bomb_error = getattr(image_module, "DecompressionBombError", RuntimeError)
    bomb_warning = getattr(image_module, "DecompressionBombWarning", Warning)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", bomb_warning)
            opened = image_module.open(io.BytesIO(payload))
            try:
                oriented = image_ops.exif_transpose(opened)
                alpha = _has_alpha(oriented)
                normalized = oriented.convert("RGBA" if alpha else "RGB")
                normalized.info = {}
                normalized.load()
            finally:
                opened.close()
                if "oriented" in locals() and oriented is not opened:
                    oriented.close()
    except (ImagePrepareError,):
        raise
    except bomb_error as exc:
        raise ImagePrepareError("pixel_limit") from exc
    except bomb_warning as exc:
        raise ImagePrepareError("pixel_limit") from exc
    except Exception as exc:
        raise ImagePrepareError("invalid_image") from exc
    return normalized


def _fit_dimensions(
    width: int,
    height: int,
    max_dimension: int,
    max_pixels: int,
) -> tuple[int, int]:
    if width <= max_dimension and height <= max_dimension and width * height <= max_pixels:
        return width, height
    scale = min(
        1.0,
        max_dimension / width,
        max_dimension / height,
        math.sqrt(max_pixels / (width * height)),
    )
    fitted_width = max(1, int(math.floor(width * scale)))
    fitted_height = max(1, int(math.floor(height * scale)))
    while (
        fitted_width > max_dimension
        or fitted_height > max_dimension
        or fitted_width * fitted_height > max_pixels
    ):
        if fitted_width > max_dimension or (
            fitted_height <= max_dimension and fitted_width >= fitted_height
        ):
            fitted_width -= 1
        else:
            fitted_height -= 1
    return fitted_width, fitted_height


def _resize_image(image: Any, size: tuple[int, int], image_module: Any) -> Any:
    if tuple(image.size) == size:
        resized = image.copy()
    else:
        resampling = getattr(getattr(image_module, "Resampling", image_module), "LANCZOS")
        resized = image.resize(size, resampling)
    resized.info = {}
    return resized


def _encode_png(image: Any) -> bytes:
    buffer = io.BytesIO()
    try:
        image.save(
            buffer,
            format="PNG",
            optimize=True,
            compress_level=9,
            interlace=False,
        )
    except Exception as exc:
        raise ImagePrepareError("output_invalid") from exc
    return buffer.getvalue()


def validate_png(data: bytes | bytearray | memoryview) -> dict[str, int]:
    """Validate output with the staged non-interlaced PNG validator."""
    try:
        from .png_validation import validate_png_bytes
    except ImportError as exc:
        raise ImagePrepareError("validator_missing") from exc
    try:
        return validate_png_bytes(data)
    except Exception as exc:
        raise ImagePrepareError("output_invalid") from exc


def _validate_output(payload: bytes, max_output_bytes: int) -> dict[str, int]:
    if len(payload) > max_output_bytes or len(payload) > MAX_OUTPUT_BYTES:
        raise ImagePrepareError("output_too_large")
    try:
        return validate_png(payload)
    except ImagePrepareError:
        raise
    except Exception as exc:
        raise ImagePrepareError("output_invalid") from exc


def _render_until_fit(
    image: Any,
    initial_size: tuple[int, int],
    max_output_bytes: int,
    image_module: Any,
) -> tuple[Any, bytes, dict[str, int]]:
    current_size = initial_size
    current = _resize_image(image, current_size, image_module)
    try:
        for _ in range(24):
            payload = _encode_png(current)
            if len(payload) <= max_output_bytes and len(payload) <= MAX_OUTPUT_BYTES:
                validation = _validate_output(payload, max_output_bytes)
                return current, payload, validation
            if current_size == (1, 1):
                break
            ratio = math.sqrt(max_output_bytes / max(1, len(payload))) * 0.9
            ratio = min(0.82, max(0.35, ratio))
            next_size = (
                max(1, int(math.floor(current_size[0] * ratio))),
                max(1, int(math.floor(current_size[1] * ratio))),
            )
            if next_size == current_size:
                if current_size[0] >= current_size[1] and current_size[0] > 1:
                    next_size = (current_size[0] - 1, current_size[1])
                elif current_size[1] > 1:
                    next_size = (current_size[0], current_size[1] - 1)
                else:
                    break
            next_image = _resize_image(image, next_size, image_module)
            if next_image is not current:
                current.close()
            current, current_size = next_image, next_size
        raise ImagePrepareError("output_too_large")
    except Exception:
        current.close()
        raise


def _ensure_destination(destination: Path) -> None:
    try:
        if destination.exists() or destination.is_symlink():
            raise ImagePrepareError("destination_exists")
        if not destination.parent.exists() or not destination.parent.is_dir():
            raise ImagePrepareError("destination_parent_missing")
    except ImagePrepareError:
        raise
    except OSError as exc:
        raise ImagePrepareError("destination_write_failed") from exc


def _write_new_file(destination: Path, payload: bytes) -> None:
    try:
        with destination.open("xb") as handle:
            written = handle.write(payload)
            if written != len(payload):
                raise OSError("short destination write")
    except FileExistsError as exc:
        raise ImagePrepareError("destination_exists") from exc
    except OSError as exc:
        raise ImagePrepareError("destination_write_failed") from exc


def prepare_image(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    max_input_bytes: int = MAX_INPUT_BYTES,
    max_input_pixels: int = MAX_INPUT_PIXELS,
    max_dimension: int = MAX_DIMENSION,
    max_pixels: int = MAX_PIXELS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, object]:
    """Normalize one local JPEG/WebP/PNG into a new bounded PNG.

    original_size is the logical size after applying EXIF orientation.
    resized and lossy_resize are true whenever resampling was needed
    for output dimensions, pixels, or bytes.  The source and destination are
    never overwritten.
    """
    input_byte_limit = _positive_limit(max_input_bytes, MAX_INPUT_BYTES)
    input_pixel_limit = _positive_limit(max_input_pixels, MAX_INPUT_PIXELS)
    dimension_limit = _positive_limit(max_dimension, MAX_DIMENSION)
    pixel_limit = _positive_limit(max_pixels, MAX_PIXELS)
    output_byte_limit = _positive_limit(max_output_bytes, MAX_OUTPUT_BYTES)

    source_path = _path_value(source)
    destination_path = _path_value(destination)
    _ensure_destination(destination_path)
    payload = _read_bounded_source(source_path, input_byte_limit)

    image_module, image_ops = _load_pillow()
    _check_input_header(payload, input_pixel_limit, image_module)
    normalized = _normalized_image(payload, image_module, image_ops)
    try:
        original_size = tuple(normalized.size)
        target_size = _fit_dimensions(
            original_size[0],
            original_size[1],
            dimension_limit,
            pixel_limit,
        )
        rendered, output_payload, _validation = _render_until_fit(
            normalized,
            target_size,
            output_byte_limit,
            image_module,
        )
        try:
            output_size = tuple(rendered.size)
            _write_new_file(destination_path, output_payload)
        finally:
            if rendered is not normalized:
                rendered.close()
    finally:
        normalized.close()

    return {
        "source": str(source_path),
        "destination": str(destination_path),
        "format": "PNG",
        "original_size": original_size,
        "output_size": output_size,
        "resized": output_size != original_size,
        "lossy_resize": output_size != original_size,
        "sha256": hashlib.sha256(output_payload).hexdigest(),
        "size_bytes": len(output_payload),
    }


def _json_result(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize one local JPEG, WebP, or PNG into a bounded PNG."
    )
    parser.add_argument("input", help="local input image path")
    parser.add_argument("output", help="new PNG output path")
    parser.add_argument("--max-input-bytes", type=int, default=MAX_INPUT_BYTES)
    parser.add_argument("--max-input-pixels", type=int, default=MAX_INPUT_PIXELS)
    parser.add_argument("--max-dimension", type=int, default=MAX_DIMENSION)
    parser.add_argument("--max-pixels", type=int, default=MAX_PIXELS)
    parser.add_argument("--max-output-bytes", type=int, default=MAX_OUTPUT_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = prepare_image(
            args.input,
            args.output,
            max_input_bytes=args.max_input_bytes,
            max_input_pixels=args.max_input_pixels,
            max_dimension=args.max_dimension,
            max_pixels=args.max_pixels,
            max_output_bytes=args.max_output_bytes,
        )
    except ImagePrepareError as exc:
        sys.stderr.write(
            _json_result(
                {
                    "ok": False,
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
            + "\n"
        )
        return 2
    except Exception as exc:
        sys.stderr.write(
            _json_result(
                {
                    "ok": False,
                    "error": {"code": "image_prepare_failed", "message": str(exc)},
                }
            )
            + "\n"
        )
        return 1
    sys.stdout.write(_json_result(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ImagePrepareError",
    "MAX_DIMENSION",
    "MAX_INPUT_BYTES",
    "MAX_INPUT_PIXELS",
    "MAX_OUTPUT_BYTES",
    "MAX_PIXELS",
    "prepare_image",
    "validate_png",
]
