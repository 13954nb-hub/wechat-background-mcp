import unittest

from wxbg.file_card_contract import (
    parse_file_card,
    safe_file_card_error,
    valid_file_card_result,
)
from wxbg.policy import AdapterError


SESSION_REF = "a" * 32
MESSAGE_REF = "b" * 32


class FileCardParserTests(unittest.TestCase):
    def _owned_pdf_card(self):
        # Literal from the owned PDF read-only diagnosis; no workspace-file dependency.
        return "檔案\nwxbg-pdf-e599b79b142942f1.pdf\n1.5K\n微信电脑版"

    def test_real_owned_pdf_card_preserves_observed_k_display_size(self):
        result = parse_file_card(self._owned_pdf_card())
        self.assertEqual(result["display_size"], "1.5K")

    def test_unknown_size_units_are_rejected_without_m_or_g_inference(self):
        for size in ("1.5M", "1.5G", "1.5KiB", "1.5k"):
            with self.subTest(size=size), self.assertRaises(AdapterError) as caught:
                parse_file_card(f"檔案\nreport.pdf\n{size}\n微信电脑版")
            self.assertEqual(caught.exception.code, "file_card_layout_unverified")

    def test_plain_card_without_status_is_not_completed(self):
        self.assertEqual(
            parse_file_card("檔案\nreport.pdf\n92B\n微信电脑版"),
            {
                "filename": "report.pdf",
                "display_size": "92B",
                "transfer_indicator": "no_transfer_indicator",
                "progress_percent": None,
                "upload_status": "unknown",
            },
        )

    def test_interrupted_zero_percent_maps_to_interrupted(self):
        self.assertEqual(
            parse_file_card("檔案\n進度: 0%\nreport.pdf\n92B\n傳送中斷\n微信电脑版"),
            {
                "filename": "report.pdf",
                "display_size": "92B",
                "transfer_indicator": "interrupted",
                "progress_percent": 0,
                "upload_status": "interrupted",
            },
        )

    def test_progress_alone_maps_to_uploading_and_accepts_simplified_label(self):
        self.assertEqual(
            parse_file_card("檔案\n进度 : 23 %\nreport.pdf\n1.5 MB\n微信电脑版"),
            {
                "filename": "report.pdf",
                "display_size": "1.5MB",
                "transfer_indicator": "uploading",
                "progress_percent": 23,
                "upload_status": "uploading",
            },
        )

    def test_explicit_completion_is_only_a_local_completed_label(self):
        self.assertEqual(
            parse_file_card("檔案\n進度: 100%\nreport.pdf\n92B\n上传完成\n微信电脑版"),
            {
                "filename": "report.pdf",
                "display_size": "92B",
                "transfer_indicator": "completed_label",
                "progress_percent": 100,
                "upload_status": "completed",
            },
        )

    def test_filename_containing_status_words_does_not_create_status(self):
        self.assertEqual(
            parse_file_card("檔案\n正在上传_指南.txt\n92B\n微信电脑版"),
            {
                "filename": "正在上传_指南.txt",
                "display_size": "92B",
                "transfer_indicator": "no_transfer_indicator",
                "progress_percent": None,
                "upload_status": "unknown",
            },
        )

    def test_filename_starting_with_progress_word_is_still_a_filename(self):
        self.assertEqual(
            parse_file_card("檔案\n进度说明.txt\n92B\n微信电脑版")["filename"],
            "进度说明.txt",
        )

    def test_progress_like_filename_suffix_is_not_an_indicator(self):
        for filename in ("进度 20%.txt", "进度：说明.txt", "进度 20% 汇总.txt"):
            with self.subTest(filename=filename):
                result = parse_file_card(f"檔案\n{filename}\n92B\n微信电脑版")
                self.assertEqual(result["filename"], filename)
                self.assertEqual(result["transfer_indicator"], "no_transfer_indicator")

    def test_size_and_progress_reject_unverified_numeric_forms(self):
        cards = [f"檔案\nreport.txt\n{size}\n微信电脑版"
                 for size in ("9 2 B", "1 . 5 M B", "9２B")]
        cards += [f"檔案\n进度: {value}\nreport.txt\n92B\n微信电脑版"
                  for value in ("１２%", "20", "-1%", "12.5%", "101%")]
        for card in cards:
            with self.subTest(card=card), self.assertRaises(AdapterError) as caught:
                parse_file_card(card)
            self.assertEqual(caught.exception.code, "file_card_layout_unverified")

    def test_unsupported_status_is_unknown_and_not_echoed(self):
        result = parse_file_card(
            "檔案\n進度: 48%\nreport.pdf\n92B\n服务器处理中\n微信电脑版"
        )
        self.assertEqual(result["transfer_indicator"], "unknown")
        self.assertEqual(result["upload_status"], "unknown")
        self.assertEqual(result["progress_percent"], 48)
        self.assertNotIn("服务器处理中", result.values())

    def test_completion_with_less_than_full_progress_can_never_be_completed(self):
        result = parse_file_card(
            "檔案\n進度: 12%\nreport.pdf\n92B\n上传完成\n微信电脑版"
        )
        self.assertEqual(result["transfer_indicator"], "unknown")
        self.assertEqual(result["upload_status"], "unknown")
        self.assertEqual(result["progress_percent"], 12)

    def test_malformed_cards_raise_one_fixed_layout_error(self):
        malformed = (
            None,
            123,
            "",
            "檔案\n\0report.pdf\n92B\n微信电脑版",
            "檔案\n\n92B\n微信电脑版",
            "檔案\nreport.pdf\n微信电脑版",
            "檔案\nreport.pdf\n92B",
            "不是檔案\nreport.pdf\n92B\n微信电脑版",
            "檔案\n進度: 20%\n進度: 30%\nreport.pdf\n92B\n微信电脑版",
            "檔案\n進度: 20\nreport.pdf\n92B\n微信电脑版",
            "檔案\nreport.pdf\n92B\n   \n微信电脑版",
            "檔案\nreport.pdf\n9TB\n微信电脑版",
            "檔案\nreport.pdf\n92B\n多余行\n另一行\n微信电脑版",
            "檔案\nreport.pdf\n92B\n微信电脑版\n" + ("x" * 4096),
        )
        for text in malformed:
            with self.subTest(text=text):
                with self.assertRaises(AdapterError) as raised:
                    parse_file_card(text)
                self.assertEqual(raised.exception.code, "file_card_layout_unverified")

    def test_safe_error_never_returns_arbitrary_input(self):
        self.assertEqual(
            safe_file_card_error("file_card_layout_unverified"),
            "file_card_layout_unverified",
        )
        self.assertEqual(safe_file_card_error("raw private detail"), "file_card_read_failed")
        self.assertEqual(safe_file_card_error(None), "file_card_read_failed")

    def test_safe_error_preserves_known_fixed_reader_codes(self):
        known_codes = (
            "invalid_session_ref",
            "invalid_message_ref",
            "invalid_deadline",
            "context_conflict",
            "draft_conflict",
            "file_card_budget_exhausted",
            "file_card_not_found",
            "ambiguous_file_card",
            "file_card_changed",
            "file_card_ref_stale",
        )
        for code in known_codes:
            with self.subTest(code=code):
                self.assertEqual(safe_file_card_error(code), code)


class FileCardResultValidationTests(unittest.TestCase):
    def _valid_result(self):
        return {
            "ok": True,
            "status": "observed",
            "verification_level": "two_matching_local_file_card_observations",
            "background_mode": "minimized",
            "transfer_indicator": "completed_label",
            "progress_percent": 100,
            "upload_status": "completed",
            "display_size": "92B",
            "remote_receipt_verified": False,
            "stable_message_id": False,
            "not_full_history": True,
            "counts": {"observations": 2},
            "refs": {"conversation": SESSION_REF, "message": MESSAGE_REF},
        }

    def test_public_result_accepts_observed_k_display_size_without_conversion(self):
        result = self._valid_result()
        result["display_size"] = "1.5K"
        self.assertTrue(valid_file_card_result(result, SESSION_REF, MESSAGE_REF))

    def test_valid_result_accepts_exact_contract_and_requested_refs(self):
        self.assertTrue(
            valid_file_card_result(self._valid_result(), SESSION_REF, MESSAGE_REF)
        )

    def test_result_keys_are_exact_and_private_fields_are_rejected(self):
        result = self._valid_result()
        result["filename"] = "report.pdf"
        self.assertFalse(valid_file_card_result(result))

        result = self._valid_result()
        result["error"] = "file_card_layout_unverified"
        self.assertFalse(valid_file_card_result(result))

    def test_display_size_must_be_a_string_with_a_verified_size_label(self):
        result = self._valid_result()
        result["display_size"] = None
        self.assertFalse(valid_file_card_result(result))

        result = self._valid_result()
        result["display_size"] = 92
        self.assertFalse(valid_file_card_result(result))

    def test_refs_must_be_lowercase_32_hex_and_match_requested_refs(self):
        result = self._valid_result()
        result["refs"]["message"] = "B" * 32
        self.assertFalse(valid_file_card_result(result))

        result = self._valid_result()
        self.assertFalse(valid_file_card_result(result, SESSION_REF, "c" * 32))
        self.assertFalse(valid_file_card_result(result, "not-a-ref", MESSAGE_REF))

    def test_json_types_are_strict_bool_is_not_int(self):
        result = self._valid_result()
        result["ok"] = 1
        self.assertFalse(valid_file_card_result(result))

        result = self._valid_result()
        result["progress_percent"] = True
        self.assertFalse(valid_file_card_result(result))

        result = self._valid_result()
        result["counts"] = {"observations": True}
        self.assertFalse(valid_file_card_result(result))

    def test_enums_and_field_coherence_are_strict(self):
        result = self._valid_result()
        result["transfer_indicator"] = "no_transfer_indicator"
        result["progress_percent"] = 5
        result["upload_status"] = "unknown"
        self.assertFalse(valid_file_card_result(result))

        result = self._valid_result()
        result["transfer_indicator"] = "completed_label"
        result["progress_percent"] = 99
        self.assertFalse(valid_file_card_result(result))

        result = self._valid_result()
        result["transfer_indicator"] = "unknown"
        result["progress_percent"] = 5
        result["upload_status"] = "unknown"
        self.assertTrue(valid_file_card_result(result))


if __name__ == "__main__":
    unittest.main()
