"""Behavioral regressions for existing file-card UIA identity recreation."""
import json
import unittest
from unittest.mock import patch

from wxbg import image_entry_probe as probe
from wxbg.policy import AdapterError
from test_image_entry_probe import FakeAdapter, FakeDriver, state, message, image_message, SESSION_REF, PNG_DESCRIPTOR


def card(runtime, filename='old-owned.txt'):
    return message('mmui::ChatBubbleItemView',runtime,'檔案\n'+filename+'\n92B\n微信电脑版')


class ImageIdentityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sleep=patch.object(probe.time,'sleep',return_value=None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def run_probe(self,states):
        adapter,driver=FakeAdapter(states),FakeDriver()
        result=probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        return adapter,driver,result

    def test_existing_nonimage_card_rebound_allows_exactly_one_image_send(self):
        old,new=card('original'),card('recreated')
        before=state(messages=(old,))
        pending=state(draft='\ufffc',messages=(new,))
        final=state(messages=(new,image_message('new-image')))
        adapter,driver,result=self.run_probe([before,before,before,pending,pending,pending,final,final])
        self.assertEqual(result['status'],'local_transition_observed')
        self.assertEqual(len(driver.select_calls),1)
        self.assertEqual(adapter.clicks,['傳送'])
        self.assertEqual(result['counts']['new_rows'],1)
        evidence=adapter.image_evidence
        self.assertEqual(evidence['baseline']['row_count'],1)
        self.assertEqual([v['rebound_count'] for v in evidence['selection_frames']],[1,0])
        self.assertEqual([v['new_rows_count'] for v in evidence['selection_frames']],[0,0])
        self.assertEqual(evidence['final_frames'][-1]['ignored_rebound_total'],1)
        text=json.dumps(evidence)
        for raw in ('old-owned.txt','original','recreated','檔案'):
            self.assertNotIn(raw,text)
        with self.assertRaises(AdapterError):
            probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(adapter.clicks,['傳送'])

    def test_pending_unchanged_old_png_card_allows_one_send_but_no_false_success(self):
        old,new=card('original','old.png'),card('recreated','old.png')
        before=state(messages=(old,));pending=state(draft='\ufffc',messages=(new,))
        adapter,driver=FakeAdapter([before,before,before,pending]),FakeDriver()
        with self.assertRaises(AdapterError):probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(adapter.clicks,['傳送'])
        self.assertEqual(len(driver.select_calls),1)

    def test_current_fixture_card_rebound_still_never_grants_send(self):
        old,new=card('original',PNG_DESCRIPTOR['name']),card('recreated',PNG_DESCRIPTOR['name'])
        before=state(messages=(old,));pending=state(draft='\ufffc',messages=(new,))
        adapter,driver=FakeAdapter([before,before,before,pending]),FakeDriver()
        with self.assertRaises(AdapterError):probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(adapter.clicks,[])
        self.assertEqual(len(driver.select_calls),1)

    def test_appended_identical_file_is_not_mistaken_for_image_success(self):
        old,new=card('original'),card('appended')
        before=state(messages=(old,));after=state(messages=(old,new))
        adapter,driver=FakeAdapter([before,before,before,after]),FakeDriver()
        with self.assertRaises(AdapterError) as caught:probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(json.loads(str(caught.exception).split(': ',1)[1])['reason_code'],'image_transition_row_unproved')
        self.assertEqual(adapter.clicks,[])
        self.assertEqual(len(driver.select_calls),1)

    def test_unrelated_new_text_alone_never_proves_image_transition(self):
        before=state();after=state(messages=(message('mmui::ChatTextItemView','new-text','private caption'),))
        adapter,driver=FakeAdapter([before,before,before,after]),FakeDriver()
        with self.assertRaises(AdapterError) as caught:probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(json.loads(str(caught.exception).split(': ',1)[1])['reason_code'],'image_transition_row_unproved')
        self.assertEqual(adapter.clicks,[])

    def test_old_image_identity_churn_cannot_prove_auto_or_postsend_success(self):
        old,new=image_message('old-image'),image_message('recreated-image')
        before=state(messages=(old,));after=state(messages=(new,))
        for sent in (False,True):
            with self.subTest(sent=sent):
                states=[before,before,before]
                if sent:states += [state(draft='\ufffc',messages=(old,))]*3
                states += [after,after]
                adapter,driver=FakeAdapter(states),FakeDriver()
                with self.assertRaises(AdapterError) as caught:probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
                self.assertEqual(json.loads(str(caught.exception).split(': ',1)[1])['reason_code'],'image_transition_row_unproved')
                self.assertEqual(len(adapter.clicks),int(sent))
                self.assertEqual(len(driver.select_calls),1)

    def test_appended_identical_image_is_count_growth_and_automatic_no_send(self):
        old,new=image_message('old-image'),image_message('new-image')
        before=state(messages=(old,));after=state(messages=(old,new))
        adapter,driver,result=self.run_probe([before,before,before,after,after])
        self.assertEqual(result['status'],'automatic_transition_observed')
        self.assertEqual(adapter.clicks,[])
        self.assertEqual(result['counts']['new_rows'],1)
        self.assertTrue(all(v['image_novelty'] for v in adapter.image_evidence['selection_frames']))


if __name__=='__main__':unittest.main(verbosity=2)
