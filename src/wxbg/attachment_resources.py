"""Bounded in-memory resources for verified local attachment bytes.

The store owns immutable byte strings only.  It never accepts a path, opens a
file, contacts a network, or registers an MCP resource.  A returned URI is a
capability for this process and expires without read-based TTL extension.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import math
import re
import secrets
import threading
import time
from typing import Any, Callable


RESOURCE_URI_PREFIX = "wechat-attachment://"
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 4
MAX_RESOURCE_BYTES = 16 * 1024 * 1024
MIN_RESOURCE_BYTES = 1
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 3600
ALLOWED_MIME_TYPES = frozenset(
    {
        "application/octet-stream",
        "image/png",
        "image/jpeg",
        "image/webp",
    }
)

_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_INVALID_FILENAME_CHARS = frozenset('/\\:*?<>|"')
_RESERVED_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL"})


class ResourceError(ValueError):
    """Stable resource-store failure without source bytes or paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Entry:
    data: bytes = field(repr=False)
    expires_at: float
    filename: str = field(repr=False)
    sha256: str = field(repr=False)
    mime_type: str = field(repr=False)


def _fail(code: str) -> None:
    raise ResourceError(code)


def _strict_filename(value: Any) -> str:
    if type(value) is not str or not value or value in (".", ".."):
        _fail("filename_invalid")
    if any(
        char in _INVALID_FILENAME_CHARS
        or ord(char) < 32
        or ord(char) == 127
        or 0x80 <= ord(char) <= 0x9F
        for char in value
    ):
        _fail("filename_invalid")
    if value.endswith((" ", ".")):
        _fail("filename_invalid")
    try:
        units = len(value.encode("utf-16-le", "strict")) // 2
    except UnicodeEncodeError:
        _fail("filename_invalid")
    if not 1 <= units <= 255:
        _fail("filename_invalid")
    stem = value.rstrip(" .").split(".", 1)[0].upper()
    if stem in _RESERVED_DEVICES or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
        _fail("filename_invalid")
    return value


def _strict_limits(
    max_bytes: Any,
    max_entries: Any,
    ttl_seconds: Any,
    clock: Any,
) -> tuple[int, int, int, Callable[[], Any]]:
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_TOTAL_BYTES:
        _fail("invalid_limits")
    if type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES:
        _fail("invalid_limits")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        _fail("invalid_limits")
    if not callable(clock):
        _fail("invalid_limits")
    return max_bytes, max_entries, ttl_seconds, clock


def _strict_now(clock: Callable[[], Any]) -> float:
    try:
        value = clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail("clock_invalid")
        value = float(value)
    except ResourceError:
        raise
    except Exception:
        _fail("clock_invalid")
    if not math.isfinite(value):
        _fail("clock_invalid")
    return value


class AttachmentResourceStore:
    """Thread-safe FIFO/TTL store for bounded verified attachment bytes."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_TOTAL_BYTES,
        max_entries: int = MAX_ENTRIES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], Any] = time.monotonic,
    ):
        (
            self._max_bytes,
            self._max_entries,
            self._ttl_seconds,
            self._clock,
        ) = _strict_limits(max_bytes, max_entries, ttl_seconds, clock)
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._total_bytes = 0

    def __repr__(self) -> str:
        with self._lock:
            return (
                "AttachmentResourceStore(entries=%d, bytes=%d, max_entries=%d, "
                "max_bytes=%d, ttl_seconds=%d)"
                % (
                    len(self._entries),
                    self._total_bytes,
                    self._max_entries,
                    self._max_bytes,
                    self._ttl_seconds,
                )
            )

    def _purge_expired(self, now: float) -> None:
        expired = [
            token
            for token, entry in self._entries.items()
            if now >= entry.expires_at
        ]
        for token in expired:
            entry = self._entries.pop(token)
            self._total_bytes -= len(entry.data)

    def _new_token(self) -> str:
        for _ in range(64):
            try:
                token = secrets.token_hex(32)
            except Exception:
                _fail("token_unavailable")
            if type(token) is str and _TOKEN.fullmatch(token) and token not in self._entries:
                return token
        _fail("token_collision")

    def put(
        self,
        data: bytes,
        *,
        filename: str,
        sha256: str,
        mime_type: str = "application/octet-stream",
    ) -> dict[str, object]:
        """Store verified bytes and return an opaque process-local descriptor."""

        if type(data) is not bytes:
            _fail("invalid_data")
        size_bytes = len(data)
        if not MIN_RESOURCE_BYTES <= size_bytes <= MAX_RESOURCE_BYTES:
            _fail("resource_size_limit")
        if type(sha256) is not str or _SHA256.fullmatch(sha256) is None:
            _fail("sha256_invalid")
        if type(mime_type) is not str or mime_type not in ALLOWED_MIME_TYPES:
            _fail("mime_type_invalid")
        _strict_filename(filename)
        declared_sha256 = sha256.lower()
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != declared_sha256:
            _fail("sha256_mismatch")

        with self._lock:
            if size_bytes > self._max_bytes:
                _fail("resource_capacity_limit")
            now = _strict_now(self._clock)
            self._purge_expired(now)
            token = self._new_token()
            while self._entries and (
                len(self._entries) >= self._max_entries
                or self._total_bytes + size_bytes > self._max_bytes
            ):
                _, old = self._entries.popitem(last=False)
                self._total_bytes -= len(old.data)
            self._entries[token] = _Entry(
                data,
                now + self._ttl_seconds,
                filename,
                declared_sha256,
                mime_type,
            )
            self._total_bytes += size_bytes
        return {
            "uri": RESOURCE_URI_PREFIX + token,
            "filename": filename,
            "size_bytes": size_bytes,
            "sha256": declared_sha256,
            "mime_type": mime_type,
            "expires_in_seconds": self._ttl_seconds,
        }

    def list_resources(self) -> list[dict[str, object]]:
        """List live metadata descriptors without exposing resource bytes."""

        with self._lock:
            now = _strict_now(self._clock)
            self._purge_expired(now)
            listed: list[dict[str, object]] = []
            for token, entry in self._entries.items():
                remaining = max(1, int(math.ceil(entry.expires_at - now)))
                listed.append(
                    {
                        "uri": RESOURCE_URI_PREFIX + token,
                        "filename": entry.filename,
                        "size_bytes": len(entry.data),
                        "sha256": entry.sha256,
                        "mime_type": entry.mime_type,
                        "expires_in_seconds": remaining,
                    }
                )
            return listed

    @staticmethod
    def _token_id(token: Any) -> str:
        if type(token) is not str or not token.startswith(RESOURCE_URI_PREFIX):
            _fail("invalid_resource_token")
        token_id = token[len(RESOURCE_URI_PREFIX):]
        if _TOKEN.fullmatch(token_id) is None:
            _fail("invalid_resource_token")
        return token_id

    def read(self, token: str) -> bytes:
        """Read a live resource by its returned URI without extending TTL."""

        return self.read_with_metadata(token)[0]

    def read_with_metadata(self, token: str) -> tuple[bytes, str]:
        """Atomically read a live resource and its verified MIME type."""

        token_id = self._token_id(token)
        with self._lock:
            now = _strict_now(self._clock)
            entry = self._entries.get(token_id)
            if entry is None:
                _fail("resource_unavailable")
            if now >= entry.expires_at:
                removed = self._entries.pop(token_id)
                self._total_bytes -= len(removed.data)
                _fail("resource_unavailable")
            return entry.data, entry.mime_type


__all__ = [
    "AttachmentResourceStore",
    "DEFAULT_TTL_SECONDS",
    "MAX_ENTRIES",
    "MAX_RESOURCE_BYTES",
    "MAX_TOTAL_BYTES",
    "ALLOWED_MIME_TYPES",
    "ResourceError",
    "RESOURCE_URI_PREFIX",
]
