"""Bounded quiet-window observation of caller-selected database files.

This is not an atomic SQLite snapshot. It rejects observed append/checkpoint/
replacement races, but cannot exclude ABA changes between observations. The
provider must establish its transaction/epoch contract before publishing data.
No fallback to a base database when required sidecars are missing is allowed.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import stat
import time

MAX_CAPTURE_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class CaptureError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class QuietCapture:
    # Avoid accidental repr logging of encrypted database content or paths.
    database: bytes = field(repr=False)
    wal: bytes = field(repr=False)
    wal_index: bytes = field(repr=False)
    evidence: dict


def _deadline(deadline):
    if time.monotonic() >= deadline:
        raise CaptureError('capture_deadline')


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns)


def _read_pass(handles, lengths, deadline):
    values = []
    for handle, size in zip(handles, lengths):
        handle.seek(0)
        chunks = []
        remaining = size
        while remaining:
            _deadline(deadline)
            chunk = handle.read(min(remaining, CHUNK_BYTES))
            if not chunk:
                raise CaptureError('capture_changed')
            chunks.append(chunk)
            remaining -= len(chunk)
        values.append(b''.join(chunks))
    return values


def capture_quiet(source: Path, *, deadline: float,
                  max_bytes: int = MAX_CAPTURE_BYTES) -> QuietCapture:
    """Two identical bounded reads; required DB/WAL/full 136-byte SHM header.

    max_bytes limits retained bytes for one pass; at most twice that amount is
    read. Source is an internal provider path, never a public caller path.
    Empty WAL is permitted, and only the first 136 SHM bytes are captured.
    """
    if (type(max_bytes) is not int or not 0 < max_bytes <= MAX_CAPTURE_BYTES
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise CaptureError('capture_limit')
    _deadline(deadline)
    source = Path(source)
    paths = (source, Path(str(source) + '-wal'), Path(str(source) + '-shm'))
    try:
        with ExitStack() as stack:
            handles = []
            for path in paths:
                _deadline(deadline)
                # Preflight excludes normal directory/device inputs; fstat
                # below also validates the actual opened object.
                if not stat.S_ISREG(path.stat().st_mode):
                    raise CaptureError('capture_shape')
                handles.append(stack.enter_context(path.open('rb', buffering=0)))
            initial = [os.fstat(h.fileno()) for h in handles]
            if any(not stat.S_ISREG(s.st_mode) for s in initial):
                raise CaptureError('capture_shape')
            if initial[0].st_size <= 0 or initial[2].st_size < 136:
                raise CaptureError('capture_shape')
            lengths = (initial[0].st_size, initial[1].st_size, 136)
            if sum(lengths) > max_bytes:
                raise CaptureError('capture_limit')
            first = _read_pass(handles, lengths, deadline)
            second = _read_pass(handles, lengths, deadline)
            if first != second:
                raise CaptureError('capture_changed')
            for path, handle, before in zip(paths, handles, initial):
                _deadline(deadline)
                after = os.fstat(handle.fileno())
                current = path.stat()
                if _stamp(before) != _stamp(after):
                    raise CaptureError('capture_changed')
                # Windows handle/path APIs can round creation time differently.
                # File identity, length and mtime are the cross-API comparison;
                # creation time remains part of the same-handle comparison.
                if _stamp(after)[:4] != _stamp(current)[:4]:
                    raise CaptureError('capture_replaced')
            return QuietCapture(*second, evidence={
                'atomic_snapshot': False, 'matching_passes': 2,
                'same_handles': True, 'path_identity_rechecked': True,
                'bytes_read': 2 * sum(lengths),
                'consistency': 'observed_quiet_window',
            })
    except CaptureError:
        raise
    except (OSError, ValueError):
        raise CaptureError('capture_unavailable') from None
