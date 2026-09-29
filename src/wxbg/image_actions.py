"""Public, local-only PNG image transition action."""

from __future__ import annotations

import importlib

from . import image_entry_probe
from .native_driver import VerifiedFile
from .policy import AdapterError


MAX_FILE_BYTES = image_entry_probe.MAX_FILE_BYTES


def _read_locked_bytes(descriptor):
    """Read the already verified file without exceeding the one MiB budget."""
    size = descriptor["size"]
    try:
        with open(descriptor["path"], "rb") as handle:
            payload = handle.read(size)
    except OSError as exc:
        raise AdapterError("file_read_failed") from exc
    if not isinstance(payload, bytes) or len(payload) != size:
        raise AdapterError("file_identity_changed")
    return payload


def _validate_png_bytes(payload):
    """Delegate content validation to the stage's PNG validator."""
    png_validation = importlib.import_module(".png_validation", __package__)

    try:
        result = png_validation.validate_png_bytes(payload)
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("invalid_png") from exc
    if result is False:
        raise AdapterError("invalid_png")
    return result


def _same_identity(requested, verified):
    return all(
        requested[key] == verified[key]
        for key in ("name", "size", "sha256")
    )


def run(adapter, session_ref, file, deadline=None):
    """Validate a PNG and publish only a bounded local transition summary."""
    session_ref = image_entry_probe._validate_session_ref(session_ref)
    requested = image_entry_probe._validate_png_descriptor(file)

    with VerifiedFile(
            requested["path"],
            expected_sha256=requested["sha256"],
            expected_size=requested["size"]) as verified:
        descriptor = image_entry_probe._validate_png_descriptor(verified)
        if not _same_identity(requested, descriptor):
            raise AdapterError("file_identity_changed")
        payload = _read_locked_bytes(descriptor)
        _validate_png_bytes(payload)
        private = image_entry_probe.run(
            adapter,
            session_ref,
            descriptor,
            deadline=deadline,
            own_only=False,
        )

    return {
        "ok": True,
        "status": "local_image_transition_observed",
        "verification_level": "stable_local_image_ui_transition",
        "counts": private["counts"],
        "refs": {"conversation": session_ref},
        "background_mode": "minimized",
        "remote_receipt_verified": False,
        "upload_status": "unknown",
    }
