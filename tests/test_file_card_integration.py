"""File-card MCP/worker integration with pure adapter/monitor fakes."""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from wxbg import worker
from wxbg.policy import AdapterError
from worker_dpi_stub import worker_monitor_module


SESSION_REF = "a" * 32
MESSAGE_REF = "b" * 32


def _file_card_result(session_ref=SESSION_REF, message_ref=MESSAGE_REF):
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
        "refs": {"conversation": session_ref, "message": message_ref},
    }


def _background_evidence(**changes):
    snapshot = {'foreground': 11, 'clipboard_sequence': 22, 'cursor': [1, 2],
                'minimized': True, 'visible_windows': [33], 'capture': 0,
                'cursor_api': 'GetCursorPos', 'cursor_dpi_context': 'per_monitor_v2',
                'cursor_coordinate_space': 'screen_coordinates_under_pm_v2'}
    evidence = {
        'before': deepcopy(snapshot), 'after': deepcopy(snapshot),
        "observations": 1,
        "background_observation_passed": True,
        "foreground_changed": False,
        "clipboard_changed": False,
        "cursor_changed": False,
        "target_restored": False,
        "capture_observed": False,
        "new_visible_windows": [],
        "monitor_errors": [],
    }
    evidence.update(changes)
    return evidence


def _cleanup(*, restored=True, observed=1, errors=None):
    return {
        "status": "restored" if restored else "failed",
        "restored": restored,
        "observed": observed,
        "errors": [] if errors is None else errors,
    }


def _load_gateway(state_directory):
    source = Path(__file__).parents[1] / "src" / "wxbg" / "gateway.py"
    spec = importlib.util.spec_from_file_location("wxbg._file_card_gateway_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("os.environ", {"WXBG_STATE_DIR": str(state_directory)}):
        spec.loader.exec_module(module)
    return module


class FileCardGatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.gateway = _load_gateway(self.state)

    def _success_response(self, result=None, **changes):
        response = {
            "ok": True,
            "worker_started": True,
            "result": deepcopy(result or _file_card_result()),
            "evidence": _background_evidence(),
            "cleanup": _cleanup(),
        }
        response.update(changes)
        return response

    def test_schema_is_read_only_strict_and_rejects_type_or_extra(self):
        tools = asyncio.run(self.gateway.SERVER.list_tools())
        tool = next(item for item in tools if item.name == "wechat_read_file_card")
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertTrue(tool.annotations.idempotentHint)
        self.assertFalse(tool.annotations.destructiveHint)
        self.assertFalse(tool.annotations.openWorldHint)
        self.assertIs(tool.inputSchema["additionalProperties"], False)
        self.assertEqual(set(tool.inputSchema["required"]), {"session_ref", "message_ref"})
        self.assertEqual(
            tool.inputSchema["properties"]["session_ref"]["pattern"],
            "^[0-9a-f]{32}$",
        )
        self.assertEqual(
            tool.inputSchema["properties"]["message_ref"]["pattern"],
            "^[0-9a-f]{32}$",
        )

        invalid_calls = [
            {"session_ref": SESSION_REF.upper(), "message_ref": MESSAGE_REF},
            {"session_ref": SESSION_REF, "message_ref": "short"},
            {"session_ref": SESSION_REF, "message_ref": MESSAGE_REF, "private": 1},
            {"session_ref": SESSION_REF, "message_ref": True},
        ]
        with patch.object(self.gateway, "execute") as dispatch:
            for args in invalid_calls:
                with self.subTest(args=args), self.assertRaises(ToolError):
                    asyncio.run(self.gateway.SERVER.call_tool("wechat_read_file_card", args))
            dispatch.assert_not_called()

    def test_direct_success_dispatches_once_without_journal_and_binds_refs(self):
        response = self._success_response()
        with patch.object(self.gateway, "execute", return_value=response) as dispatch, \
                patch.object(self.gateway, "Journal", side_effect=AssertionError("read must not journal")) as journal:
            result = self.gateway.wechat_read_file_card(SESSION_REF, MESSAGE_REF)

        self.assertEqual(result, response)
        dispatch.assert_called_once_with(
            "read_file_card", {"session_ref": SESSION_REF, "message_ref": MESSAGE_REF}
        )
        journal.assert_not_called()
        self.assertFalse((self.state / "operations.sqlite3").exists())

    def test_private_or_invalid_success_body_is_rejected_without_leaking_body(self):
        private_result = _file_card_result()
        private_result["filename"] = "private-name.pdf"
        with patch.object(self.gateway, "execute", return_value=self._success_response(private_result)):
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_read_file_card(SESSION_REF, MESSAGE_REF)
        payload = json.loads(str(caught.exception))
        self.assertEqual(payload["code"], "file_card_result_invalid")
        self.assertNotIn("filename", json.dumps(payload))
        self.assertNotIn("private-name.pdf", json.dumps(payload))
        self.assertNotIn("result", payload)
        self.assertNotIn("worker_result", payload)

    def test_success_rejects_private_top_level_or_worker_result_without_leaking_it(self):
        for extra in (
            {"private_top_level": "private-top-level"},
            {"worker_result": {"private_body": "private-worker-body"}},
        ):
            with self.subTest(extra=extra):
                response = self._success_response(**extra)
                with patch.object(self.gateway, "execute", return_value=response):
                    with self.assertRaises(ToolError) as caught:
                        self.gateway.wechat_read_file_card(SESSION_REF, MESSAGE_REF)
                payload = json.loads(str(caught.exception))
                self.assertEqual(payload["code"], "file_card_result_invalid")
                self.assertNotIn("private-top-level", json.dumps(payload))
                self.assertNotIn("private-worker-body", json.dumps(payload))
                self.assertNotIn("worker_result", payload)

    def test_background_or_gate_failure_drops_result_and_uses_bounded_metadata(self):
        cases = [
            (self._success_response(evidence=_background_evidence(
                background_observation_passed=False,
                monitor_errors=["private-monitor-detail"],
            )), "background_observation_unverified"),
            (self._success_response(evidence=_background_evidence(
                background_observation_passed=False,
                cursor_changed=True,
            )), "background_observation_unverified"),
            (self._success_response(cleanup=_cleanup(errors=["private-cleanup-detail"])),
             "file_card_cleanup_unverified"),
            (self._success_response(cleanup=_cleanup(restored=False)),
             "file_card_cleanup_unverified"),
            (self._success_response(cleanup=_cleanup(observed=True)),
             "file_card_cleanup_unverified"),
            (self._success_response(cleanup=_cleanup(observed=2)),
             "file_card_cleanup_unverified"),
        ]
        for response, expected_code in cases:
            with self.subTest(expected_code=expected_code), patch.object(
                    self.gateway, "execute", return_value=response):
                with self.assertRaises(ToolError) as caught:
                    self.gateway.wechat_read_file_card(SESSION_REF, MESSAGE_REF)
            payload = json.loads(str(caught.exception))
            self.assertEqual(payload["code"], expected_code)
            self.assertNotIn("result", payload)
            self.assertNotIn("worker_result", payload)
            self.assertNotIn("private-monitor-detail", json.dumps(payload))
            self.assertNotIn("private-cleanup-detail", json.dumps(payload))
            self.assertLessEqual(len(json.dumps(payload)), 512)

    def test_worker_failure_is_projected_to_fixed_code_without_raw_detail(self):
        response = {
            "ok": False,
            "worker_started": True,
            "error": {
                "code": "raw-private-code",
                "detail": "raw private body",
                "outcome_unknown": True,
            },
            "result": {"filename": "secret.pdf"},
            "worker_result": {"detail": "secret worker result"},
            "evidence": _background_evidence(),
            "cleanup": _cleanup(),
        }
        with patch.object(self.gateway, "execute", return_value=response):
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_read_file_card(SESSION_REF, MESSAGE_REF)
        payload = json.loads(str(caught.exception))
        self.assertEqual(payload["code"], "file_card_read_failed")
        self.assertFalse(payload["retry"])
        self.assertNotIn("detail", payload)
        self.assertNotIn("worker_result", payload)
        self.assertNotIn("secret.pdf", json.dumps(payload))
        self.assertNotIn("raw private body", json.dumps(payload))


class FileCardWorkerIntegrationTests(unittest.TestCase):
    def _invoke(self, *, reader_result=None, reader_error=None, monitor_error=None):
        calls = []

        class FakeAdapter:
            submission_started = False
            native_evidence = None
            navigation_evidence = None

            def __init__(self, target):
                self.target = target

            def dispatch(self, *_args, **_kwargs):
                raise AssertionError("file-card reads must use the dedicated reader")

        class FakeMonitor:
            def __init__(self, *_args):
                pass

            def start(self):
                return self

            def stop(self):
                if monitor_error is not None:
                    raise monitor_error
                return _background_evidence()

        def read_file_card(adapter, session_ref, message_ref, deadline=None):
            calls.append((adapter.target, session_ref, message_ref, deadline))
            if reader_error is not None:
                raise reader_error
            return deepcopy(reader_result or _file_card_result(session_ref, message_ref))

        modules = {}
        for name, values in (
            ("wxbg.adapter", {"Adapter": FakeAdapter}),
            ("wxbg.monitor", {"Monitor": FakeMonitor}),
            ("wxbg.file_card", {"read_file_card": read_file_card}),
        ):
            module = types.ModuleType(name)
            for key, value in values.items():
                setattr(module, key, value)
            modules[name] = module
        modules["wxbg.monitor"] = worker_monitor_module(FakeMonitor)

        request = {
            "action": "read_file_card",
            "args": {"session_ref": SESSION_REF, "message_ref": MESSAGE_REF},
            "target": {"pid": 1, "hwnd": 2, "created": 3},
            "deadline": 12345.0,
        }
        output = io.StringIO()
        with patch.dict(sys.modules, modules), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(request))), \
                redirect_stdout(output):
            worker.main()
        return calls, json.loads(output.getvalue())

    def test_success_uses_dedicated_reader_with_deadline_and_no_native_dispatch(self):
        calls, response = self._invoke()
        self.assertEqual(calls, [(
            {"pid": 1, "hwnd": 2, "created": 3},
            SESSION_REF,
            MESSAGE_REF,
            12345.0,
        )])
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], _file_card_result())
        self.assertNotIn("submission_started", response)

    def test_reader_failure_uses_safe_error_and_never_returns_raw_body(self):
        error = AdapterError("file_card_layout_unverified", "secret card body")
        error.outcome_unknown = False
        error.submission_started = False
        _calls, response = self._invoke(reader_error=error)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "file_card_layout_unverified")
        self.assertEqual(response["error"]["detail"], "file_card_layout_unverified")
        self.assertFalse(response["error"]["submission_started"])
        self.assertNotIn("result", response)
        self.assertNotIn("secret card body", json.dumps(response))

    def test_monitor_failure_strips_file_card_result_and_raw_exception(self):
        error = RuntimeError("private monitor detail")
        _calls, response = self._invoke(monitor_error=error)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "monitor_failed")
        self.assertFalse(response["error"]["submission_started"])
        self.assertNotIn("result", response)
        self.assertNotIn("private monitor detail", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
