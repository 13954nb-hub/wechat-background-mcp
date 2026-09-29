"""Desktop observation regressions; snapshot is faked, no UI is touched."""
import types
import unittest
from unittest.mock import patch
from wxbg.monitor import Monitor


def observation(capture=0):
    return {'foreground':10,'cursor':[1,2],'clipboard_sequence':3,
            'minimized':True,'visible_windows':[20],'capture':capture}


class MonitorContractTests(unittest.TestCase):
    def result(self,before=None,after=None,samples=None):
        with patch('wxbg.monitor.snapshot',side_effect=[before or observation(),after or observation()]):
            monitor=Monitor(123,20)
            monitor.thread=types.SimpleNamespace(join=lambda _:None,
                                                  is_alive=lambda:False)
            monitor.samples=samples or []
            return monitor.stop()

    def test_unchanged_desktop_without_capture_passes(self):
        result=self.result()
        self.assertTrue(result['background_observation_passed'])
        self.assertFalse(result['capture_observed'])

    def test_capture_still_held_at_completion_fails_background_check(self):
        result=self.result(after=observation(capture=20))
        self.assertTrue(result['capture_observed'])
        self.assertFalse(result['background_observation_passed'])

    def test_capture_seen_then_released_cannot_be_reported_as_no_effect(self):
        result=self.result(samples=[observation(capture=20),observation()])
        self.assertTrue(result['capture_observed'])
        self.assertFalse(result['background_observation_passed'])

    def test_capture_already_present_before_operation_cannot_pass(self):
        result=self.result(before=observation(capture=20))
        self.assertTrue(result['capture_observed'])
        self.assertFalse(result['background_observation_passed'])


if __name__=='__main__':unittest.main()
