"""Thread-safe, bounded phase metadata for background diagnostics.

The recorder describes observations only. It cannot make a phase change and a
real UI observation atomic; consumers must treat phase-at-sample as metadata,
not causal attribution.
"""

from __future__ import annotations

import threading


PHASES = (
    "operation",
    "enter_contacts",
    "span_read",
    "restore_contacts",
    "restore_chat",
    "complete",
    "image_preflight",
    "image_native_select",
    "image_selection_observe",
    "image_presend",
    "image_send_dispatch",
    "image_final_observe",
)
FLAG_NAMES = (
    "foreground_changed",
    "clipboard_changed",
    "cursor_changed",
    "target_restored",
    "capture_observed",
    "new_visible_window",
    "monitor_error",
)
MAX_SAMPLES = 2**31 - 1
_PHASE_SET = frozenset(PHASES)
_FLAG_SET = frozenset(FLAG_NAMES)
_SCHEMA = "phase_recorder_v1"


def _validate_flags(flags):
    if type(flags) is not dict or set(flags) != _FLAG_SET:
        raise ValueError("invalid_phase_recorder_flags")
    if any(type(flags[name]) is not bool for name in FLAG_NAMES):
        raise ValueError("invalid_phase_recorder_flags")


class PhaseRecorder:
    """Keep fixed-size counters for observations sampled in named phases."""

    __slots__ = (
        "_lock",
        "_max_samples",
        "_phase",
        "_sample_count",
        "_overflow",
        "_phase_sample_counts",
        "_flags",
    )

    def __init__(self, max_samples=MAX_SAMPLES):
        if type(max_samples) is not int or not 1 <= max_samples <= MAX_SAMPLES:
            raise ValueError("invalid_phase_recorder_capacity")
        self._lock = threading.Lock()
        self._max_samples = max_samples
        self._phase = PHASES[0]
        self._sample_count = 0
        self._overflow = False
        self._phase_sample_counts = {phase: 0 for phase in PHASES}
        self._flags = {
            name: {
                "first_sample_index": None,
                "hit_count": 0,
                "phase_at_first_hit": None,
            }
            for name in FLAG_NAMES
        }

    def set_phase(self, phase):
        if type(phase) is not str or phase not in _PHASE_SET:
            raise ValueError("invalid_phase")
        with self._lock:
            self._phase = phase

    def observe(self, flags):
        """Record one validated flag sample, or mark overflow and drop it."""
        _validate_flags(flags)
        with self._lock:
            if self._sample_count >= self._max_samples:
                self._overflow = True
                return
            sample_index = self._sample_count
            phase = self._phase
            self._sample_count += 1
            self._phase_sample_counts[phase] += 1
            for name in FLAG_NAMES:
                if flags[name]:
                    metadata = self._flags[name]
                    metadata["hit_count"] += 1
                    if metadata["first_sample_index"] is None:
                        metadata["first_sample_index"] = sample_index
                        metadata["phase_at_first_hit"] = phase

    def snapshot(self):
        """Return a JSON-serializable metadata snapshot with one lock hold."""
        with self._lock:
            return {
                "schema": _SCHEMA,
                "phase": self._phase,
                "sample_count": self._sample_count,
                "overflow": self._overflow,
                "phase_sample_counts": dict(self._phase_sample_counts),
                "flags": {
                    name: dict(metadata) for name, metadata in self._flags.items()
                },
            }

    def summary(self):
        """Alias for the fixed metadata snapshot used by later Monitor code."""
        return self.snapshot()


__all__ = ["FLAG_NAMES", "MAX_SAMPLES", "PHASES", "PhaseRecorder"]
