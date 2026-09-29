import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from wxbg.readstore_capture import CaptureError, capture_quiet


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'owned.db'
        self.db.write_bytes(b'D' * 4096)
        Path(str(self.db) + '-wal').write_bytes(b'W' * 128)
        Path(str(self.db) + '-shm').write_bytes(b'S' * 136)

    def call(self, **kwargs):
        return capture_quiet(self.db, deadline=time.monotonic() + 2, **kwargs)

    def test_same_handles_quiet_bytes_and_honest_evidence(self):
        result = self.call()
        self.assertEqual(result.database, b'D' * 4096)
        self.assertEqual(result.wal, b'W' * 128)
        self.assertEqual(result.wal_index, b'S' * 136)
        self.assertFalse(result.evidence['atomic_snapshot'])
        self.assertEqual(result.evidence['bytes_read'], 2 * (4096 + 128 + 136))

    def test_missing_sidecar_does_not_fallback_to_base(self):
        Path(str(self.db) + '-wal').unlink()
        with self.assertRaisesRegex(CaptureError, '^capture_unavailable$'):
            self.call()

    def test_preflight_total_bound(self):
        with self.assertRaisesRegex(CaptureError, '^capture_limit$'):
            self.call(max_bytes=4200)

    def test_short_shm_rejected(self):
        Path(str(self.db) + '-shm').write_bytes(b'x' * 135)
        with self.assertRaisesRegex(CaptureError, '^capture_shape$'):
            self.call()

    def test_expired_deadline(self):
        with self.assertRaisesRegex(CaptureError, '^capture_deadline$'):
            capture_quiet(self.db, deadline=time.monotonic() - 1)

    def test_change_after_first_pass_rejected(self):
        from wxbg import readstore_capture as module
        original = module._read_pass
        count = 0
        def changed(*args, **kwargs):
            nonlocal count
            result = original(*args, **kwargs)
            count += 1
            if count == 1:
                self.db.write_bytes(b'X' * 4096)
            return result
        with patch.object(module, '_read_pass', side_effect=changed):
            with self.assertRaisesRegex(CaptureError, '^capture_changed$'):
                self.call()

    def test_checkpoint_only_change_rejected(self):
        from wxbg import readstore_capture as module
        original = module._read_pass
        count = 0
        def changed(*args, **kwargs):
            nonlocal count
            result = original(*args, **kwargs)
            count += 1
            if count == 1:
                Path(str(self.db) + '-shm').write_bytes(b'S' * 128 + b'C' * 8)
            return result
        with patch.object(module, '_read_pass', side_effect=changed):
            with self.assertRaisesRegex(CaptureError, '^capture_changed$'):
                self.call()

    def test_invalid_bound_rejected(self):
        for value in (True, 0, -1, 128 * 1024 * 1024):
            with self.subTest(value=value), self.assertRaises(CaptureError):
                self.call(max_bytes=value)

    def test_windows_stat_ctime_rounding_is_not_replacement(self):
        original = Path.stat
        def rounded(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            return SimpleNamespace(st_dev=result.st_dev, st_ino=result.st_ino,
                st_size=result.st_size, st_mtime_ns=result.st_mtime_ns,
                st_ctime_ns=result.st_ctime_ns+570000, st_mode=result.st_mode)
        with patch.object(Path, 'stat', rounded):
            self.assertFalse(self.call().evidence['atomic_snapshot'])


if __name__ == '__main__':
    unittest.main()
