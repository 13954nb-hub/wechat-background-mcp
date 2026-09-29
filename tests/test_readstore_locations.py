from pathlib import Path
import tempfile
import time
import unittest

from wxbg.readstore_locations import LocationError, select_storage, parse_config_root, discover_roots


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def add(self, name, prefix=''):
        path = self.root / prefix / name / 'db_storage'
        path.mkdir(parents=True)
        return path.resolve()

    def call(self, **kw):
        return select_storage('wxid_owned', roots=[self.root],
                              deadline=time.monotonic()+2, **kw)

    def test_exact_account(self):
        expected = self.add('wxid_owned')
        self.add('wxid_other')
        self.assertEqual(self.call().path, expected)

    def test_suffix_layout_and_duplicate_roots(self):
        expected = self.add('wxid_owned_a1B2', 'xwechat_files')
        value = select_storage('wxid_owned', roots=[self.root, self.root/'xwechat_files'],
                               deadline=time.monotonic()+2)
        self.assertEqual(value.path, expected)
        self.assertNotIn('wxid_owned', repr(value))

    def test_ambiguous_accounts_fail(self):
        self.add('wxid_owned')
        self.add('wxid_owned_a1B2')
        with self.assertRaisesRegex(LocationError, '^storage_ambiguous$'):
            self.call()

    def test_no_prefix_guess(self):
        self.add('wxid_owned_extra')
        with self.assertRaisesRegex(LocationError, '^storage_unavailable$'):
            self.call()

    def test_identity_recheck_after_replace(self):
        self.add('wxid_owned')
        result = self.call()
        result.path.rename(result.path.with_name('old'))
        result.path.mkdir()
        with self.assertRaisesRegex(LocationError, '^storage_changed$'):
            result.revalidate()

    def test_total_entry_limit(self):
        self.add('wxid_owned')
        self.add('other')
        with self.assertRaisesRegex(LocationError, '^storage_limit$'):
            self.call(max_entries=1)

    def test_config_explicit_paths_only(self):
        self.assertEqual(parse_config_root(b'{"dataDir":"C:\\\\owned"}'), Path('C:\\owned'))
        self.assertIsNone(parse_config_root(b'junk C:\\owned text'))
        self.assertIsNone(parse_config_root(b'{"path":"relative"}'))
        self.assertIsNone(parse_config_root(b'x'*8193))

    def test_equivalent_config_paths_not_ambiguous(self):
        import json
        raw=json.dumps({'dataDir':'C:\\owned\\', 'path':'c:/owned'}).encode()
        self.assertEqual(parse_config_root(raw), Path('C:\\owned'))
        raw=json.dumps({'dataDir':'C:\\owned', 'path':'C:\\other'}).encode()
        self.assertIsNone(parse_config_root(raw))

    def test_discover_config_and_defaults_bounded(self):
        config = self.root/'app'/'Tencent'/'xwechat'/'config'
        config.mkdir(parents=True)
        target = self.root/'data'
        (config/'settings.ini').write_text(str(target), encoding='utf-8')
        roots = discover_roots(deadline=time.monotonic()+2,
            environment={'APPDATA':str(self.root/'app'), 'USERPROFILE':str(self.root/'user')},
            registry_roots=[])
        self.assertIn(target, roots)
        self.assertIn(self.root/'user'/'Documents', roots)

    def test_discover_deadline(self):
        with self.assertRaisesRegex(LocationError, '^storage_deadline$'):
            discover_roots(deadline=time.monotonic()-1, environment={}, registry_roots=[])


if __name__ == '__main__':
    unittest.main()
