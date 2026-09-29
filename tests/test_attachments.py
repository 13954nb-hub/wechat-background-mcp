"""Worker wrapper tests with owned files; never load a bridge or contact Weixin."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pywintypes
import win32con
import win32file

from wxbg import attachments
from wxbg.native_driver import VerifiedFile
from wxbg.policy import AdapterError


class AttachmentWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='owned-wrapper-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'owned.txt'
        self.path.write_bytes(b'old bytes\r\n')
        # Model the gateway's earlier descriptor; its handle is deliberately
        # closed so the worker's independent verification can be challenged.
        with VerifiedFile(self.path) as descriptor:
            self.descriptor = descriptor
        self.adapter = SimpleNamespace(pid=771, hwnd=882, created=1234567.25)
        self.session_ref = 'owned-session-reference'
        self.driver_object = object()
        self.returned = {'status': 'submitted', 'upload_status': 'interrupted',
                         'remote_receipt_verified': False}
        self.thread_pid = self.enterContext(patch(
            'win32process.GetWindowThreadProcessId', return_value=(993, self.adapter.pid)))
        self.driver = self.enterContext(patch(
            'wxbg.native_driver.NativeAttachmentDriver', return_value=self.driver_object))
        self.submit = self.enterContext(patch(
            'wxbg.file_actions.send_file', return_value=self.returned))

    def invoke(self, descriptor=None):
        return attachments.send_file(self.adapter, self.session_ref,
                                     self.descriptor if descriptor is None else descriptor)

    def assert_read_lock_held(self):
        # A second read handle remains compatible with native fixture hashing.
        reader = win32file.CreateFile(str(self.path), win32con.GENERIC_READ,
                                     win32con.FILE_SHARE_READ, None,
                                     win32con.OPEN_EXISTING, 0, None)
        reader.Close()
        try:
            writer = win32file.CreateFile(str(self.path), win32con.GENERIC_WRITE,
                                         win32con.FILE_SHARE_READ, None,
                                         win32con.OPEN_EXISTING, 0, None)
        except pywintypes.error as exc:
            self.assertEqual(exc.winerror, 32, 'expected a sharing violation from the worker read lock')
        else:
            writer.Close()
            self.fail('worker released its read lock before submission completed')

    def test_worker_rehash_rejects_changed_bytes_before_native_construction(self):
        self.path.write_bytes(b'new bytes\r\n')  # Same size: rejection must depend on the hash.
        self.assertEqual(self.path.stat().st_size, self.descriptor['size'])
        with self.assertRaises(AdapterError) as raised:
            self.invoke()
        self.assertEqual(raised.exception.code, 'file_hash_mismatch')
        self.driver.assert_not_called()
        self.submit.assert_not_called()
        self.path.write_bytes(b'worker failure released the handle')

    def test_descriptor_name_mismatch_is_rejected_before_native_construction(self):
        wrong = dict(self.descriptor, name='other.txt')
        with self.assertRaises(AdapterError) as raised:
            self.invoke(wrong)
        self.assertEqual(raised.exception.code, 'file_descriptor_changed')
        self.driver.assert_not_called()
        self.submit.assert_not_called()
        self.path.write_bytes(b'name mismatch released the handle')

    def test_worker_holds_read_lock_through_submit_return(self):
        phases = []
        def submit(adapter, driver, session_ref, descriptor):
            self.assertEqual(descriptor, self.descriptor)
            self.assert_read_lock_held()
            phases.append('selection')
            try:
                phases.append('send-and-card-observation')
                return self.returned
            finally:
                self.assert_read_lock_held()
                phases.append('submit-exit')
        self.submit.side_effect = submit
        self.assertIs(self.invoke(), self.returned)
        self.assertEqual(phases, ['selection', 'send-and-card-observation', 'submit-exit'])
        self.path.write_bytes(b'write possible only after wrapper exit')

    def test_submit_exception_preserves_error_and_releases_read_lock(self):
        failure = AdapterError('outcome_unknown', 'owned interrupted submission')
        def submit(*args):
            self.assert_read_lock_held()
            raise failure
        self.submit.side_effect = submit
        with self.assertRaises(AdapterError) as raised:
            self.invoke()
        self.assertIs(raised.exception, failure)
        self.path.write_bytes(b'exception path released the handle')
        self.driver.assert_called_once()
        self.submit.assert_called_once()

    def test_exact_target_package_path_and_sha_are_passed_to_driver(self):
        result = self.invoke()
        expected_dll = (Path(attachments.__file__).parent / 'native' /
                        attachments.NATIVE_SHA256 / 'wxbg_attachment.dll').resolve(strict=True)
        self.assertEqual(hashlib.sha256(expected_dll.read_bytes()).hexdigest(), attachments.NATIVE_SHA256)
        self.thread_pid.assert_called_once_with(self.adapter.hwnd)
        self.driver.assert_called_once_with(
            {'pid': 771, 'hwnd': 882, 'created': 1234567.25, 'tid': 993},
            expected_dll, attachments.NATIVE_SHA256)
        self.submit.assert_called_once_with(self.adapter, self.driver_object,
                                            self.session_ref, self.descriptor)
        # Preserve interrupted upload evidence; the wrapper cannot turn it into delivery.
        self.assertIs(result, self.returned)
        self.assertEqual(result['upload_status'], 'interrupted')
        self.assertIs(result['remote_receipt_verified'], False)

    def test_current_native_manifest_matches_packaged_binary(self):
        native = Path(attachments.__file__).parent / 'native'
        manifest = json.loads((native / 'manifest.json').read_text(encoding='utf-8'))
        binary = manifest['binary']
        self.assertEqual(binary['sha256'], attachments.NATIVE_SHA256)
        self.assertEqual(binary['path'], f'{attachments.NATIVE_SHA256}/wxbg_attachment.dll')
        packaged = native / binary['path']
        self.assertEqual(packaged.stat().st_size, binary['size_bytes'])
        self.assertEqual(hashlib.sha256(packaged.read_bytes()).hexdigest(), binary['sha256'])
        self.assertEqual(manifest['verified_scope']['current_pinned_v4']['binary_sha256'],
                         binary['sha256'])
        self.assertIs(manifest['verified_scope']['current_pinned_v4']['client_live_submission_verified'],
                      False)

    def test_mismatched_window_pid_refuses_before_file_open_or_native_construction(self):
        self.thread_pid.return_value = (993, 9999)
        with patch('wxbg.native_driver.VerifiedFile', wraps=VerifiedFile) as holder:
            with self.assertRaises(AdapterError) as raised:
                self.invoke()
            holder.assert_not_called()
        self.assertEqual(raised.exception.code, 'stale_window')
        self.driver.assert_not_called()
        self.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
