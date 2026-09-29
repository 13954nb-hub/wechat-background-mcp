"""Unchanged visible scene may rebuild controls while the owned draft is pending."""
import unittest
from unittest.mock import patch
from wxbg import image_entry_probe as probe
from wxbg.image_row_identity import Reconciler
from test_image_append_proof import row
from test_image_entry_probe import FakeAdapter, FakeDriver, state, message, SESSION_REF, PNG_DESCRIPTOR


class SceneRebindTests(unittest.TestCase):
    def test_explicit_scene_mode_rebinds_unchanged_time_and_image_slots(self):
        old=[row('time','mmui::ChatItemView','12:34'),row('anchor')]
        new=[dict(item,runtime='new-'+item['runtime'],ref=str(index+4)*32) for index,item in enumerate(old)]
        strict=Reconciler(old,'owned.png');strict.reconcile(new)
        self.assertNotIn('new-time',strict.ignored_runtime_ids)
        scene=Reconciler(old,'owned.png')
        summary=scene.reconcile(new,allow_scene_rebind=True)
        self.assertEqual(summary['rebound_count'],2)
        self.assertIn('new-time',scene.ignored_runtime_ids)
        self.assertIn('new-anchor',scene.ignored_runtime_ids)

    def test_scene_mode_never_suppresses_added_or_changed_content_or_geometry(self):
        old=[row('anchor')]
        for new in ([row('anchor'),row('new')], [dict(row('new'),name='different')],
                    [dict(row('new'),rectangle=(100,101,300,201))]):
            scene=Reconciler(old,'owned.png');scene.reconcile(new,allow_scene_rebind=True)
            self.assertNotIn('new',scene.ignored_runtime_ids)

    def test_equal_image_names_at_distinct_exact_positions_are_distinct_scene_slots(self):
        old=[row('old'),row('anchor')]
        new=[dict(item,runtime='new-'+item['runtime'],ref=str(index+4)*32) for index,item in enumerate(old)]
        scene=Reconciler(old,'owned.png');summary=scene.reconcile(new,allow_scene_rebind=True)
        self.assertEqual(summary['rebound_count'],2)

    def test_pending_unchanged_scene_then_single_send_and_stable_tail(self):
        time_before=message('mmui::ChatItemView','time-old','12:34',(100,0,300,100))
        time_after=message('mmui::ChatItemView','time-rebuilt','12:34',(100,0,300,100))
        old=message('mmui::ChatBubbleReferItemView','old','圖片',(100,100,300,200))
        anchor=message('mmui::ChatBubbleReferItemView','anchor','圖片',(100,300,300,400))
        new=message('mmui::ChatBubbleReferItemView','new','圖片',(100,500,300,600))
        before=state(messages=(time_before,old,anchor))
        pending=state(draft='\ufffc',messages=(time_after,old,anchor))
        after=state(messages=(anchor,new))
        adapter=FakeAdapter([before,before,before,pending,pending,pending,after,after]);driver=FakeDriver()
        with patch.object(probe.time,'sleep',return_value=None):
            result=probe.probe(adapter,driver,SESSION_REF,PNG_DESCRIPTOR)
        self.assertEqual(result['status'],'local_transition_observed')
        self.assertEqual(adapter.clicks,['傳送'])
        self.assertEqual(len(driver.select_calls),1)
        self.assertEqual([frame['new_rows_count'] for frame in adapter.image_evidence['selection_frames']],[0,0])


if __name__=='__main__':unittest.main()
