"""Bounded pure decoder for the confirmed WeChat V2 image container.

This module only decodes a caller-provided byte string.  It does not discover
keys, read files, inspect a process, infer image identity, or trim image
footers.  The caller must already have an authenticated AES key and XOR byte.
"""

from __future__ import annotations

import struct
from typing import Final


V2_MAGIC: Final[bytes] = b"\x07\x08\x56\x32\x08\x07"
V2_HEADER_SIZE: Final[int] = 15
MAX_CONTAINER_BYTES: Final[int] = 16 * 1024 * 1024


class ContainerError(ValueError):
    """Stable failure code without retaining payload or key material."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ContainerError(code={self.code!r})"


def _fail(code: str) -> None:
    raise ContainerError(code)


def _validate_arguments(
    data: bytes,
    aes_key: bytes,
    xor_key: int,
    max_output_bytes: int,
) -> None:
    if type(data) is not bytes:
        _fail("data_invalid")
    if len(data) > MAX_CONTAINER_BYTES:
        _fail("input_too_large")
    if type(aes_key) is not bytes or len(aes_key) != 16:
        _fail("aes_key_invalid")
    if type(xor_key) is not int or not 0 <= xor_key <= 0xFF:
        _fail("xor_key_invalid")
    if (
        type(max_output_bytes) is not int
        or not 1 <= max_output_bytes <= MAX_CONTAINER_BYTES
    ):
        _fail("output_limit_invalid")


def _aligned_aes_length(aes_size: int) -> int:
    """Return the ciphertext length for PKCS#7 padded ``aes_size`` bytes."""

    return ((aes_size // 16) + 1) * 16


def decode_v2(
    data: bytes,
    *,
    aes_key: bytes,
    xor_key: int,
    max_output_bytes: int = MAX_CONTAINER_BYTES,
) -> bytes:
    """Decode one bounded V2 payload and return the exact plaintext bytes.

    ``data`` must be the complete V2 container.  ``aes_key`` is the already
    authenticated 16-byte AES key and ``xor_key`` is the already authenticated
    integer in the inclusive range 0..255.  No image-format or identity claim
    is made about the returned bytes.
    """

    _validate_arguments(data, aes_key, xor_key, max_output_bytes)
    if len(data) < V2_HEADER_SIZE:
        _fail("header_truncated")
    if data[:6] != V2_MAGIC:
        _fail("bad_magic")

    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    aes_cipher_size = _aligned_aes_length(aes_size)
    aes_start = V2_HEADER_SIZE
    aes_end = aes_start + aes_cipher_size
    if aes_end > len(data):
        _fail("truncated")

    if xor_size:
        xor_start = len(data) - xor_size
        if xor_start < aes_end:
            _fail("truncated")
    else:
        # ``data[-0:]`` means the entire byte string; keep the empty-tail case
        # explicit so an empty XOR segment cannot duplicate the container.
        xor_start = len(data)

    raw = data[aes_end:xor_start]
    encrypted_xor = data[xor_start:] if xor_size else b""
    output_size = aes_size + len(raw) + len(encrypted_xor)
    if output_size > max_output_bytes:
        _fail("output_too_large")

    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )

        decryptor = Cipher(
            algorithms.AES(aes_key),
            modes.ECB(),
        ).decryptor()
        padded = decryptor.update(data[aes_start:aes_end])
        padded += decryptor.finalize()
    except Exception:
        _fail("aes_decrypt_failed")

    if not padded:
        _fail("bad_padding")
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        _fail("bad_padding")
    aes_plain = padded[:-pad]
    if len(aes_plain) != aes_size:
        _fail("bad_padding")

    xor_plain = bytes(value ^ xor_key for value in encrypted_xor)
    return aes_plain + raw + xor_plain


__all__ = ["ContainerError", "MAX_CONTAINER_BYTES", "V2_MAGIC", "decode_v2"]
