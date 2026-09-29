"""Post-Send proof while the visible window drops earlier image rows."""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch
from wxbg import image_entry_probe as probe
from wxbg.policy import AdapterError
from test_image_entry_probe import FakeAdapter, FakeDriver, state, message, image_message, SESSION_REF, PNG_DESCRIPTOR


def row(runtime,kind='mmui::ChatBubbleReferItemView',name='圖片'):
    top={'old':0,'anchor':100,'time':200,'new':300}.get(runtime,400)
    return dict(runtime=runtime,ref=hashlib.sha256(runtime.encode()).hexdigest()[:32],
        kind=kind,name=name,rectangle=(100,top,300,top+100))


class ImageAppendProofTests(unittest.TestCase):
    def test_one_new_tail_image_after_retained_anchor_survives_viewport_eviction(self):
        old,anchor,new=row('old'),row('anchor'),row('new')
        baseline=[old,anchor];current=[anchor,new]
        self.assertTrue(probe._appended_image_after_tail(baseline,current,'owned.png',{'old','anchor'}))
        current[0]['rectangle']=(100,20,300,100)
        self.assertTrue(probe._appended_image_after_tail(baseline,current,'owned.png',{'old','anchor'}))

    def test_allows_only_optional_separator_before_the_single_last_image(self):
        anchor=row('anchor');new=row('new');separator=row('time','mmui::ChatItemView','12:34')
        self.assertTrue(probe._appended_image_after_tail([anchor],[anchor,separator,new],'owned.png',{'anchor'}))
        for tail in ([new,separator],[separator,separator,new],[new,row('extra')],
                     [row('text','mmui::ChatBubbleItemView','unrelated')]):
            self.assertFalse(probe._appended_image_after_tail([anchor],[anchor]+tail,'owned.png',{'anchor'}))

    def test_old_image_churn_and_lost_or_changed_anchor_cannot_pass(self):
        anchor=row('anchor');new=row('new')
        for current in ([new], [new,anchor], [anchor], [anchor,anchor,new]):
            self.assertFalse(probe._appended_image_after_tail([anchor],current,'owned.png',{'anchor'}))
        for key,value in (('runtime','changed'),('ref','e'*32),('kind','mmui::ChatBubbleItemView'),('name','Image')):
            changed=deepcopy(anchor);changed[key]=value
            self.assertFalse(probe._appended_image_after_tail([anchor],[changed,new],'owned.png',{'anchor'}))

    def test_baseline_or_accepted_alias_cannot_be_reused_as_new_image(self):
        old,anchor=row('old'),row('anchor')
        self.assertFalse(probe._appended_image_after_tail([old,anchor],[anchor,old],'owned.png',{'old','anchor'}))
        self.assertFalse(probe._appended_image_after_tail([anchor],[anchor,row('alias')],'owned.png',{'anchor','alias'}))
        reused=row('new');reused['ref']=old['ref']
        self.assertFalse(probe._appended_image_after_tail([old,anchor],[anchor,reused],'owned.png',{'old','anchor'}))
        new=row('new')
        self.assertFalse(probe._appended_image_after_tail([anchor],[anchor,new],'owned.png',{'anchor'},{new['ref']}))

    def test_non_time_separator_and_wrong_geometric_order_are_rejected(self):
        anchor,new=row('anchor'),row('new')
        for name in ('private text','25:99','12:345',''):
            separator=row('time','mmui::ChatItemView',name)
            self.assertFalse(probe._appended_image_after_tail([anchor],[anchor,separator,new],'owned.png',{'anchor'}))
        for rect in ((100,50,300,90),(100,100,300,400),(400,300,500,400),(100,300,100,400)):
            wrong=deepcopy(new);wrong['rectangle']=rect
            self.assertFalse(probe._appended_image_after_tail([anchor],[anchor,wrong],'owned.png',{'anchor'}))

    def test_postsend_separator_reuse_and_same_image_count_are_successful_once(self):
        old=image_message('old')
        anchor=message('mmui::ChatBubbleItemView','anchor','图片',(100,100,300,200))
        new=message('mmui::ChatBubbleItemView','new','图片',(100,300,300,400))
        time_before=message('mmui::ChatItemView','reused-time','11:11')
        time_after=message('mmui::ChatItemView','reused-time','12:34')
        before=state(messages=(time_before,old,anchor))
        pending=state(draft='\ufffc',messages=(time_before,old,anchor))
        after=state(messages=(time_after,anchor,new))
        adapter=FakeAdapter([before,before,before,pending,pending,pending,after,after]);driver=FakeDriver()
        with patch.object(probe.time,'sleep',return_value=None):
            result=probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(result['status'],'local_transition_observed')
        self.assertEqual(len(driver.select_calls),1)
        self.assertEqual(adapter.clicks,['傳送'])
        self.assertTrue(all(frame['image_novelty'] for frame in adapter.image_evidence['final_frames']))

    def test_changing_anchor_geometry_never_becomes_stable_from_unchanged_new_image(self):
        old=image_message('old')
        anchor=message('mmui::ChatBubbleItemView','anchor','图片',(100,100,300,200))
        new=message('mmui::ChatBubbleItemView','new','图片',(100,300,300,400))
        before=state(messages=(old,anchor));pending=state(draft='\ufffc',messages=(old,anchor))
        finals=[state(messages=(message('mmui::ChatBubbleItemView','anchor','图片',
            (100,100+n,300,200+n)),new)) for n in range(4)]
        adapter=FakeAdapter([before,before,before,pending,pending,pending]+finals);driver=FakeDriver()
        with patch.object(probe.time,'sleep',return_value=None),self.assertRaises(AdapterError):
            probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(adapter.clicks,['傳送'])
        self.assertEqual(len(driver.select_calls),1)
        self.assertEqual([frame['stable_count'] for frame in adapter.image_evidence['final_frames']],[1,1,1,1])


if __name__=='__main__':unittest.main()
