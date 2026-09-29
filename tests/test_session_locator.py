"""Owned UIA fakes for the read-only offscreen session locator probe."""
import unittest
from types import SimpleNamespace

from wxbg.policy import AdapterError


class Pattern:
    def __init__(self, item):
        self.item = item

    def QueryInterface(self, _interface):
        return self

    def FindItemByProperty(self, start_after, property_id, value):
        assert start_after is None
        assert property_id == 30011
        return self.item if value == self.item.CurrentAutomationId else None


class RawItem:
    CurrentAutomationId = 'session_item_Example Group'
    CurrentClassName = 'mmui::ChatSessionCell'
    CurrentControlType = 50007

    def GetCurrentPattern(self, pattern_id):
        if pattern_id in (10017, 10020):
            return object()
        raise OSError('pattern unavailable')


class RawList:
    def __init__(self, item):
        self.item = item

    def GetCurrentPattern(self, pattern_id):
        if pattern_id == 10019:
            return Pattern(self.item)
        raise OSError('pattern unavailable')


class BrokenPattern:
    def QueryInterface(self, _interface):
        raise OSError('interface unavailable')


class BrokenRawList:
    def GetCurrentPattern(self, pattern_id):
        if pattern_id == 10019:
            return BrokenPattern()
        raise OSError('pattern unavailable')


class NullPattern:
    def __bool__(self):
        return False


class NullRawList:
    def GetCurrentPattern(self, pattern_id):
        if pattern_id == 10019:
            return NullPattern()
        raise OSError('pattern unavailable')


def list_node(raw):
    return SimpleNamespace(element_info=SimpleNamespace(
        class_name='mmui::XTableView', control_type='List',
        automation_id='session_list', element=raw))


class FakeAdapter:
    uia_item_container_interface = object()

    def __init__(self, item):
        self.item = item
        self.preconditions = 0

    def precondition(self):
        self.preconditions += 1

    def nodes(self):
        return [list_node(RawList(self.item))]


class SessionLocatorTests(unittest.TestCase):
    def test_item_container_finds_exact_offscreen_session_without_action(self):
        from wxbg.session_locator import probe_item_container

        adapter = FakeAdapter(RawItem())
        result = probe_item_container(adapter, 'Example Group')
        self.assertTrue(result['target_found'])
        self.assertTrue(result['scroll_item_available'])
        self.assertTrue(result['virtualized_item_available'])
        self.assertEqual(result['item_container_count'], 1)
        self.assertIn('layout', result)
        self.assertEqual(result['layout']['visible_session_count'], 0)
        self.assertGreaterEqual(adapter.preconditions, 2)

    def test_failed_provider_lookup_is_uncertain_and_does_not_claim_absence(self):
        from wxbg.session_locator import probe_item_container

        adapter = FakeAdapter(RawItem())
        adapter.nodes = lambda: [list_node(BrokenRawList())]
        result = probe_item_container(adapter, 'Example Group')
        self.assertFalse(result['target_found'])
        self.assertEqual(result['lookup_error_count'], 1)
        self.assertEqual(result['conclusion'], 'unknown')

    def test_null_com_pattern_means_item_container_unavailable(self):
        from wxbg.session_locator import probe_item_container

        adapter = FakeAdapter(RawItem())
        adapter.nodes = lambda: [list_node(NullRawList())]
        result = probe_item_container(adapter, 'Example Group')
        self.assertFalse(result['target_found'])
        self.assertEqual(result['item_container_count'], 0)
        self.assertEqual(result['lookup_error_count'], 0)
        self.assertEqual(result['conclusion'], 'not_found')

    def test_invalid_title_rejected_before_uia(self):
        from wxbg.session_locator import probe_item_container

        adapter = FakeAdapter(RawItem())
        with self.assertRaisesRegex(AdapterError, 'invalid_title'):
            probe_item_container(adapter, '')
        self.assertEqual(adapter.preconditions, 0)

    def test_visible_exact_target_reports_only_its_rectangle(self):
        from wxbg.session_locator import probe_item_container

        class Rect:
            left, top, right, bottom = 152, 1900, 669, 2062

        row = SimpleNamespace(
            element_info=SimpleNamespace(
                class_name='mmui::ChatSessionCell', control_type='ListItem',
                automation_id='session_item_Example Group'),
            rectangle=lambda: Rect())
        adapter = FakeAdapter(RawItem())
        adapter.nodes = lambda: [list_node(NullRawList()), row]
        result = probe_item_container(adapter, 'Example Group')
        self.assertEqual(result['visible_exact_target_rects'], [[152, 1900, 669, 2062]])


if __name__ == '__main__':
    unittest.main()
