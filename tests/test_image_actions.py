"""Pure tests for the public PNG image transition wrapper."""

from copy import deepcopy
import builtins
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from wxbg.policy import AdapterError


SESSION_REF = "b" * 32
PNG_DESCRIPTOR = {
    "path": r"C:\owned-image.png",
    "name": "owned-image.png",
    "size": 4,
    "sha256": "2" * 64,
}
COUNTS = {"native_selections": 1, "send_clicks": 1, "new_rows": 1}


class ReadHandle:
    def __init__(self, payload, locked):
        self.payload = payload
        self.locked = locked
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        if not self.locked.active:
            raise AssertionError("PNG bytes must be read while VerifiedFile is held")
        self.read_sizes.append(size)
        return self.payload


class LockedVerifiedFile:
    def __init__(self, descriptor, events):
        self.descriptor = descriptor
        self.events = events
        self.active = False

    def __enter__(self):
        self.active = True
        self.events.append("enter")
        return deepcopy(self.descriptor)

    def __exit__(self, *_args):
        self.events.append("exit")
        self.active = False
        return False


class ImageActionsTests(unittest.TestCase):
    def setUp(self):
        from wxbg import image_actions

        self.image_actions = image_actions
        self.adapter = SimpleNamespace()

    def _validation_module(self, validator):
        module = ModuleType("wxbg.png_validation")
        module.validate_png_bytes = validator
        return module

    def _private_result(self):
        return {
            "status": "local_transition_observed",
            "diagnostic_only": True,
            "remote_receipt_verified": False,
            "counts": deepcopy(COUNTS),
            "rows": [{"runtime_sha256": "private"}],
        }

    def test_non_png_is_rejected_before_verified_file_or_probe(self):
        descriptor = dict(PNG_DESCRIPTOR, path=r"C:\owned-image.jpg", name="owned-image.jpg")
        verified = Mock()
        probe = Mock()

        with patch.object(self.image_actions, "VerifiedFile", verified), \
             patch.object(self.image_actions.image_entry_probe, "run", probe):
            with self.assertRaises(AdapterError) as caught:
                self.image_actions.run(self.adapter, SESSION_REF, descriptor, deadline=99.0)

        self.assertEqual(caught.exception.code, "invalid_image_file_type")
        verified.assert_not_called()
        probe.assert_not_called()

    def test_unknown_descriptor_is_rejected_before_verified_file_or_probe(self):
        verified = Mock()
        probe = Mock()

        with patch.object(self.image_actions, "VerifiedFile", verified), \
             patch.object(self.image_actions.image_entry_probe, "run", probe):
            with self.assertRaises(AdapterError) as caught:
                self.image_actions.run(self.adapter, SESSION_REF, {"path": PNG_DESCRIPTOR["path"]})

        self.assertEqual(caught.exception.code, "invalid_fixture")
        verified.assert_not_called()
        probe.assert_not_called()

    def test_invalid_png_bytes_stop_before_probe(self):
        events = []
        verified_instance = LockedVerifiedFile(PNG_DESCRIPTOR, events)
        verified_factory = Mock(return_value=verified_instance)
        validator = Mock(side_effect=AdapterError("invalid_png"))
        probe = Mock()

        with patch.dict(sys.modules, {
            "wxbg.png_validation": self._validation_module(validator),
        }), patch.object(self.image_actions, "VerifiedFile", verified_factory), \
             patch.object(self.image_actions.image_entry_probe, "run", probe), \
             patch.object(builtins, "open", return_value=ReadHandle(b"\x89PNG", verified_instance)):
            with self.assertRaises(AdapterError) as caught:
                self.image_actions.run(self.adapter, SESSION_REF, PNG_DESCRIPTOR, deadline=99.0)

        self.assertEqual(caught.exception.code, "invalid_png")
        self.assertEqual(events, ["enter", "exit"])
        probe.assert_not_called()

    def test_valid_result_is_public_summary_and_lock_covers_probe(self):
        events = []
        verified_instance = LockedVerifiedFile(PNG_DESCRIPTOR, events)
        verified_factory = Mock(return_value=verified_instance)
        validator = Mock(return_value={"format": "png"})
        private_result = self._private_result()
        probe = Mock()

        def run_probe(adapter, session_ref, descriptor, deadline=None, *, own_only):
            self.assertTrue(verified_instance.active)
            self.assertIs(adapter, self.adapter)
            self.assertEqual(session_ref, SESSION_REF)
            self.assertEqual(descriptor, PNG_DESCRIPTOR)
            self.assertEqual(deadline, 99.0)
            self.assertFalse(own_only)
            return private_result

        probe.side_effect = run_probe
        read_handle = ReadHandle(b"\x89PNG", verified_instance)
        with patch.dict(sys.modules, {
            "wxbg.png_validation": self._validation_module(validator),
        }), patch.object(self.image_actions, "VerifiedFile", verified_factory), \
             patch.object(self.image_actions.image_entry_probe, "run", probe), \
             patch.object(builtins, "open", return_value=read_handle):
            result = self.image_actions.run(
                self.adapter, SESSION_REF, PNG_DESCRIPTOR, deadline=99.0,
            )

        self.assertEqual(result, {
            "ok": True,
            "status": "local_image_transition_observed",
            "verification_level": "stable_local_image_ui_transition",
            "counts": COUNTS,
            "refs": {"conversation": SESSION_REF},
            "background_mode": "minimized",
            "remote_receipt_verified": False,
            "upload_status": "unknown",
        })
        self.assertEqual(events, ["enter", "exit"])
        self.assertEqual(read_handle.read_sizes, [PNG_DESCRIPTOR["size"]])
        self.assertLessEqual(sum(read_handle.read_sizes), 1024 * 1024)
        verified_factory.assert_called_once_with(
            PNG_DESCRIPTOR["path"],
            expected_sha256=PNG_DESCRIPTOR["sha256"],
            expected_size=PNG_DESCRIPTOR["size"],
        )
        validator.assert_called_once_with(b"\x89PNG")
        probe.assert_called_once()
        self.assertNotIn("rows", result)
        self.assertNotIn("diagnostic_only", result)
        self.assertNotIn("private", repr(result))


if __name__ == "__main__":
    unittest.main()
