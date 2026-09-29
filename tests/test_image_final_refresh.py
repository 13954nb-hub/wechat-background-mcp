"""Only read observations may be retried after the sole Send dispatch."""
import unittest
from unittest.mock import patch
from wxbg import image_entry_probe as probe
from wxbg.policy import AdapterError
from test_image_entry_probe import FakeAdapter,FakeDriver,state,image_message,SESSION_REF,PNG_DESCRIPTOR


class FinalRefreshTests(unittest.TestCase):
    def run_case(self,failures):
        before=state();pending=state(draft='\ufffc');after=state(messages=(image_message('new'),))
        adapter=FakeAdapter([before,before,before,pending,pending,pending,after]);driver=FakeDriver()
        original=probe._identity_rows
        seen=[]
        def rows(observation,deadline):
            if adapter.clicks:
                seen.append(1)
                if len(seen)<=failures:raise AdapterError('image_identity_invalid')
            return original(observation,deadline)
        with patch.object(probe,'_identity_rows',side_effect=rows),patch.object(probe.time,'sleep',return_value=None):
            try:result=probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
            except AdapterError as error:result=error
        return adapter,driver,result,len(seen)

    def test_transient_refresh_then_two_stable_frames_succeeds_without_second_send(self):
        adapter,driver,result,reads=self.run_case(1)
        self.assertIsInstance(result,dict)
        self.assertEqual(result['status'],'local_transition_observed')
        self.assertEqual(reads,3)
        self.assertEqual(adapter.clicks,['傳送'])
        self.assertEqual(len(driver.select_calls),1)
        self.assertEqual([frame['stable_count'] for frame in adapter.image_evidence['final_frames']],[1,2])

    def test_persistent_invalid_rows_remain_unknown_after_four_reads(self):
        adapter,driver,result,reads=self.run_case(100)
        self.assertIsInstance(result,AdapterError)
        self.assertEqual(result.code,'outcome_unknown')
        self.assertEqual(reads,4)
        self.assertEqual(adapter.clicks,['傳送'])
        self.assertEqual(len(driver.select_calls),1)


if __name__=='__main__':unittest.main()
