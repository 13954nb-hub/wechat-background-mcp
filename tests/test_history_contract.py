import copy
import unittest
try:
    from wxbg import history_contract as contract
except ImportError:
    contract=None

def evidence():
    return dict(mode='intentional_navigation',direction='older',requested_steps=1,
        delivery_started=True,completed_steps=1,viewport_settled=True,conversation_preserved=True,
        draft_preserved=True,viewport_changed=True,primary_error_code=None,
        original_viewport_restoration_requested=False,not_full_history=True,boundary_verified=False)

def summary():
    return dict(ok=True,status='viewport_observed',verification_level='settled_view_after_bounded_scroll',
        background_mode='minimized',counts=dict(requested_steps=1,completed_steps=1,before_rows=10,after_rows=11,
        viewport_changed=1),refs={'conversation':'f'*32})

ARGS={'session_ref':'f'*32,'direction':'older','steps':1}

class HistoryContractTests(unittest.TestCase):
    def setUp(self):self.assertIsNotNone(contract,'history contract has not been implemented')
    def good(self,result=None,value=None,desktop=None,args=None):
        return contract.valid_history_success(result or summary(),value or evidence(),
            desktop or {'background_observation_passed':True},args or ARGS)
    def test_complete_consistent_success(self):self.assertTrue(self.good())
    def test_unknown_keeps_fixed_keys_without_success_flags(self):
        value,valid=contract.normalize_history_evidence(None)
        self.assertFalse(valid);self.assertEqual(set(value),set(evidence()))
        self.assertIsNone(value['delivery_started']);self.assertIsNone(value['viewport_settled'])
    def test_private_extra_fields_are_removed_and_invalidate_success(self):
        value=evidence();value['private_text']='must not leak'
        fixed,valid=contract.normalize_history_evidence(value)
        self.assertFalse(valid);self.assertNotIn('private_text',fixed);self.assertFalse(self.good(value=value))
    def test_boolean_flags_never_accept_one(self):
        for key in ('delivery_started','viewport_settled','conversation_preserved','draft_preserved',
                    'viewport_changed','original_viewport_restoration_requested','not_full_history','boundary_verified'):
            value=evidence();value[key]=1
            self.assertFalse(self.good(value=value),key)
    def test_no_false_restoration_or_boundary_claim(self):
        for key in ('original_viewport_restoration_requested','boundary_verified'):
            value=evidence();value[key]=True;self.assertFalse(self.good(value=value))
    def test_mismatched_direction_step_or_ref_reject(self):
        for key,value in (('direction','newer'),('steps',2),('session_ref','0'*32)):
            args={**ARGS,key:value};self.assertFalse(self.good(args=args))
    def test_result_extras_and_count_disagreement_reject(self):
        value=summary();value['text']='synthetic';self.assertFalse(self.good(result=value))
        for key,bad in (('completed_steps',0),('viewport_changed',0),('before_rows',0),('after_rows',True)):
            value=summary();value['counts'][key]=bad;self.assertFalse(self.good(result=value))
    def test_missing_or_failed_monitor_rejects(self):
        self.assertFalse(self.good(desktop={'background_observation_passed':False}))
        self.assertFalse(self.good(desktop={'unknown':True}))
    def test_unchanged_view_can_be_success_without_boundary_proof(self):
        value=evidence();value['viewport_changed']=False
        result=summary();result['counts']['viewport_changed']=0
        self.assertTrue(self.good(result=result,value=value))
    def test_invalid_error_code_not_leaked(self):
        value=evidence();value['primary_error_code']='synthetic private text'
        fixed,valid=contract.normalize_history_evidence(value)
        self.assertFalse(valid);self.assertNotEqual(fixed['primary_error_code'],value['primary_error_code'])
    def test_legal_shaped_but_unknown_error_code_not_leaked(self):
        value=evidence();value['primary_error_code']='synthetic_private_token'
        fixed,valid=contract.normalize_history_evidence(value)
        self.assertFalse(valid);self.assertNotEqual(fixed['primary_error_code'],value['primary_error_code'])

if __name__=='__main__':unittest.main()
