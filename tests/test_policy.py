import unittest
from wxbg.policy import AdapterError, exact_one, compare_draft, validate_text, validate_ui_action, stable_ref

class PolicyTests(unittest.TestCase):
    def test_ambiguous_recipient_cannot_select_first(self):
        with self.assertRaisesRegex(AdapterError,'ambiguous'):exact_one([1,2],'recipient')
    def test_missing_recipient_has_no_fallback(self):
        with self.assertRaisesRegex(AdapterError,'not_found'):exact_one([],'recipient')
    def test_existing_draft_cannot_be_overwritten_without_compare(self):
        with self.assertRaisesRegex(AdapterError,'draft_conflict'):compare_draft('unsent user text','')
    def test_text_preserves_unicode_but_rejects_null_empty_and_oversize(self):
        self.assertEqual(validate_text('測試\n🙂'),'測試\n🙂')
        for text in ('','bad\0text','a'*10001):
            with self.assertRaises(AdapterError):validate_text(text)
    def test_financial_controls_are_blocked_even_when_present_as_parent(self):
        for path in (['紅包'],['服務','轉帳'],['WeChat Pay','Confirm'],['微信支付','設定']):
            with self.assertRaisesRegex(AdapterError,'payment_excluded'):validate_ui_action(path,'Button')
    def test_unlabelled_generic_actions_are_not_accepted(self):
        with self.assertRaises(AdapterError):validate_ui_action([''],'Button')
    def test_refs_expire_when_context_or_process_changes(self):
        r=stable_ref('836:123','self','runtime42','button','name')
        self.assertNotEqual(r,stable_ref('837:123','self','runtime42','button','name'))
        self.assertNotEqual(r,stable_ref('836:123','group','runtime42','button','name'))
        self.assertEqual(r,stable_ref('836:123','self','runtime42','button','name'))

if __name__=='__main__':unittest.main()
