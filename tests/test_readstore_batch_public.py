import json
import unittest

from mcp.server.fastmcp.exceptions import ToolError

from wxbg.readstore_batch_public import batch_result_or_error


REQUEST = {
    "account_epoch": "epoch-1",
    "conversations": [
        {
            "conversation_key": "alice",
            "cursor": None,
            "start_from": "beginning",
            "limit": 1,
        }
    ],
    "max_total": 1,
}


def _background():
    return {
        "background_observation_passed": True,
        "observations": 2,
        "foreground_changed": False,
        "clipboard_changed": False,
        "cursor_changed": False,
        "target_restored": False,
        "capture_observed": False,
        "new_visible_windows": [],
        "monitor_errors": [],
        "private_detail": "SECRET_BACKGROUND_DETAIL",
    }


def _cleanup():
    return {
        "status": "read_only",
        "restored": True,
        "modified": False,
        "gate_touched": False,
        "errors": [],
        "private_detail": "SECRET_CLEANUP_DETAIL",
    }


def _failure(*, code="readstore_unavailable", **changes):
    response = {
        "ok": False,
        "worker_started": True,
        "target_validation": {"status": "stable"},
        "error": {
            "code": code,
            "message": "SECRET_PROVIDER_DETAIL",
            "outcome_unknown": False,
            "submission_started": False,
        },
        "evidence": _background(),
        "cleanup": _cleanup(),
    }
    response.update(changes)
    return response


class ReadstoreBatchPublicTests(unittest.TestCase):
    def _error_payload(self, response):
        with self.assertRaises(ToolError) as caught:
            batch_result_or_error(response, request=REQUEST)
        return str(caught.exception)

    def test_propagates_verified_fixed_error_in_readstore_public_shape(self):
        payload = self._error_payload(_failure())

        self.assertEqual(json.loads(payload), {
            "code": "readstore_unavailable",
            "outcome_unknown": False,
            "retry": False,
        })
        self.assertNotIn("SECRET", payload)

    def test_background_preflight_fixed_code_requires_complete_safe_envelope(self):
        safe = _failure(code="readstore_background_failed")
        payload = self._error_payload(safe)
        self.assertEqual(json.loads(payload), {
            "code": "readstore_background_failed",
            "outcome_unknown": False,
            "retry": False,
        })
        self.assertNotIn("SECRET", payload)
        unsafe = _failure(code="readstore_background_failed", evidence=None)
        payload = self._error_payload(unsafe)
        self.assertEqual(json.loads(payload)["code"], "readstore_result_invalid")
        self.assertNotIn("SECRET", payload)

    def test_rejects_unverified_error_envelopes(self):
        cases = [
            _failure(worker_started=False),
            _failure(target_validation={"status": "changed"}),
            _failure(evidence=None),
            _failure(cleanup={**_cleanup(), "modified": True}),
        ]
        for response in cases:
            with self.subTest(response=response):
                payload = self._error_payload(response)
                self.assertEqual(json.loads(payload)["code"], "readstore_result_invalid")
                self.assertNotIn("SECRET", payload)

    def test_rejects_unknown_or_ambiguous_error_codes_without_echo(self):
        for response in (
            _failure(code="SECRET_PROVIDER_CODE"),
            _failure(error={
                "code": "readstore_unavailable",
                "message": "SECRET_PROVIDER_DETAIL",
                "outcome_unknown": True,
                "submission_started": False,
            }),
        ):
            with self.subTest(error=response["error"]):
                payload = self._error_payload(response)
                self.assertEqual(json.loads(payload)["code"], "readstore_result_invalid")
                self.assertNotIn("SECRET", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
