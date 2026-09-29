"""Timestamp refs follow the real adapter's name-sensitive identity formula."""
from types import SimpleNamespace
import unittest

from wxbg.adapter import Adapter
from wxbg.image_row_identity import Reconciler
from wxbg.policy import AdapterError


def separator(name, runtime='same-time-runtime', kind='mmui::ChatItemView'):
    adapter = object.__new__(Adapter)
    adapter.identity = 'owned-process-identity'
    node = SimpleNamespace(element_info=SimpleNamespace(
        class_name=kind, automation_id='', name=name, runtime_id=runtime))
    return {'kind': kind, 'runtime': runtime, 'ref': adapter.ref(node, 'owned-chat'),
            'name': name, 'rectangle': (100, 100, 300, 140)}


class SeparatorRefsTests(unittest.TestCase):
    def test_real_adapter_changes_ref_when_same_runtime_timestamp_changes(self):
        before, after = separator('19:20'), separator('19:21')
        self.assertNotEqual(before['ref'], after['ref'])
        reconciler = Reconciler([before], 'owned.png')
        result = reconciler.reconcile([after], allow_separator_updates=True)
        self.assertEqual(result['rebound_count'], 0)
        self.assertIn(after['runtime'], reconciler.ignored_runtime_ids)

    def test_time_change_is_rejected_before_send(self):
        with self.assertRaises(AdapterError):
            Reconciler([separator('19:20')], 'owned.png').reconcile([separator('19:21')])

    def test_new_ref_cannot_hide_other_identity_changes(self):
        before = separator('19:20')
        for after in (separator('not-a-time'), separator('19:21', kind='mmui::ChatBubbleItemView'),
                      {**separator('19:21'), 'runtime': 'unknown-runtime', 'ref': before['ref']}):
            with self.subTest(after=after), self.assertRaises(AdapterError):
                Reconciler([before], 'owned.png').reconcile([after], allow_separator_updates=True)

    def test_ref_owned_by_another_row_is_still_rejected(self):
        before, other = separator('19:20'), separator('19:22', runtime='other-time-runtime')
        after = {**separator('19:21'), 'ref': other['ref']}
        with self.assertRaises(AdapterError):
            Reconciler([before, other], 'owned.png').reconcile([after], allow_separator_updates=True)


if __name__ == '__main__':
    unittest.main()
