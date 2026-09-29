"""Pure-mock coverage for the staged attachment type gate and DLL handoff."""
import hashlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from wxbg import attachments, file_actions, native_driver
from wxbg.policy import AdapterError


class _Adapter:
    hwnd = 321
    pid = 654
    created = 987654


class _VerifiedFile:
    def __init__(self, path, expected_sha256, expected_size):
        self.descriptor = {
            'path': path,
            'name': Path(path).name,
            'size': expected_size,
            'sha256': expected_sha256,
        }

    def __enter__(self):
        return self.descriptor

    def __exit__(self, exc_type, exc, tb):
        return False


class AttachmentTypeTests(unittest.TestCase):
    def test_declared_extensions_are_case_insensitive(self):
        self.assertEqual(getattr(attachments, 'SUPPORTED_FILE_EXTENSIONS', ()),
                         ('.txt', '.pdf', '.zip'))
        for suffix in ('.txt', '.TXT', '.pdf', '.PDF', '.zip', '.ZIP'):
            with self.subTest(suffix=suffix):
                attachments.validate_file_type(r'C:\owned\fixture' + suffix)

    def test_unverified_extensions_still_fail_at_the_type_gate(self):
        for path in (r'C:\owned\fixture.png', r'C:\owned\fixture.exe', r'C:\owned\fixture'):
            with self.subTest(path=path):
                with self.assertRaises(AdapterError) as raised:
                    attachments.validate_file_type(path)
                self.assertEqual(raised.exception.code, 'unverified_file_type')

    def test_pdf_send_passes_the_formal_resolved_dll_without_opening_a_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'owned.PdF'
            payload = b'%PDF-owned-fixture%\n'
            path.write_bytes(payload)
            file = {
                'path': str(path),
                'name': path.name,
                'size': len(payload),
                'sha256': hashlib.sha256(payload).hexdigest(),
            }
            fake_win32process = types.SimpleNamespace(
                GetWindowThreadProcessId=Mock(return_value=(777, _Adapter.pid)))
            submit = Mock(return_value={'submitted': True})
            driver = object()
            driver_ctor = Mock(return_value=driver)
            candidate_dll = (Path(attachments.__file__).parent / 'native' /
                             attachments.NATIVE_SHA256 / 'wxbg_attachment.dll')
            canonical_dll = candidate_dll.resolve(strict=True)

            with patch.dict(sys.modules, {'win32process': fake_win32process}), \
                 patch.object(native_driver, 'VerifiedFile', _VerifiedFile), \
                 patch.object(native_driver, 'NativeAttachmentDriver', driver_ctor), \
                 patch.object(file_actions, 'send_file', submit):
                try:
                    result = attachments.send_file(_Adapter(), 'self-ref', file)
                except AdapterError as exc:
                    self.fail(f'PDF dispatch was rejected before the staged type extension: {exc}')

            self.assertEqual(result, {'submitted': True})
            self.assertEqual(driver_ctor.call_args.args[1], canonical_dll)
            self.assertEqual(fake_win32process.GetWindowThreadProcessId.call_count, 1)
            submit.assert_called_once_with(
                unittest.mock.ANY, driver, 'self-ref', file)

    def test_unverified_extension_never_reaches_driver_or_submit(self):
        file = {'path': r'C:\owned\fixture.png', 'name': 'fixture.png',
                'size': 4, 'sha256': 'a' * 64}
        fake_win32process = types.SimpleNamespace(
            GetWindowThreadProcessId=Mock(return_value=(777, _Adapter.pid)))
        driver_ctor = Mock()
        submit = Mock()
        with patch.dict(sys.modules, {'win32process': fake_win32process}), \
             patch.object(native_driver, 'VerifiedFile', _VerifiedFile), \
             patch.object(native_driver, 'NativeAttachmentDriver', driver_ctor), \
             patch.object(file_actions, 'send_file', submit):
            with self.assertRaises(AdapterError) as raised:
                attachments.send_file(_Adapter(), 'self-ref', file)

        self.assertEqual(raised.exception.code, 'unverified_file_type')
        fake_win32process.GetWindowThreadProcessId.assert_not_called()
        driver_ctor.assert_not_called()
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
