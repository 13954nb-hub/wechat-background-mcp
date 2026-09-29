"""Worker, guardian, and gateway integration for bounded contact spans."""

import asyncio
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from wxbg import supervisor, worker
from wxbg.policy import AdapterError
from test_gate import FakeBackend
from test_gateway import load_isolated_gateway
from wxbg import contact_span_contract as contract
from wxbg.journal import Journal
from worker_dpi_stub import worker_monitor_module


def body(steps=1, text="private contact label"):
    views = []
    for offset in range(steps + 1):
        views.append({
            "offset": offset,
            "rows": [{"display_text": text}],
            "exposed_count": 1,
            "returned_count": 1,
            "viewport_changed": offset > 0,
        })
    return {
        "ok": True,
        "status": "contact_span_observed",
        "verification_level": "settled_visible_span_with_restoration",
        "background_mode": "minimized",
        "origin": "observed_top",
        "views": views,
        "counts": {
            "requested_steps": steps,
            "completed_steps": steps,
            "view_count": steps + 1,
            "changed_view_count": steps,
        },
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "original_contacts_view_restored": True,
        "original_conversation_restored": True,
    }


def evidence(steps=1, *, delivery_started=True, primary_error_code=None,
             cleanup_error_code=None):
    return {
        "mode": "bounded_contacts_span",
        "origin": "observed_top",
        "requested_steps": steps,
        "delivery_started": delivery_started,
        "entered": True if delivery_started else False,
        "attempted_down_steps": steps if delivery_started else 0,
        "completed_steps": steps if delivery_started else 0,
        "observed_view_count": steps + 1 if delivery_started else 1,
        "attempted_restore_steps": steps if delivery_started else 0,
        "viewport_settled": True if delivery_started else False,
        "contacts_view_restored": True if delivery_started else False,
        "conversation_restored": True,
        "draft_preserved": True,
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "primary_error_code": primary_error_code,
        "cleanup_error_code": cleanup_error_code,
    }


ARGS = {"steps": 1, "limit_per_view": 100}


class ContactSpanGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.gateway = load_isolated_gateway(self.state)

    def test_schema_is_strict_and_registers_exact_limits(self):
        tools = asyncio.run(self.gateway.SERVER.list_tools())
        tool = next(item for item in tools if item.name == "wechat_read_contact_span")
        self.assertFalse(tool.annotations.readOnlyHint)
        self.assertFalse(tool.annotations.idempotentHint)
        self.assertFalse(tool.annotations.destructiveHint)
        self.assertFalse(tool.annotations.openWorldHint)
        self.assertIs(tool.inputSchema["additionalProperties"], False)
        self.assertEqual(
            (tool.inputSchema["properties"]["steps"]["type"],
             tool.inputSchema["properties"]["steps"]["minimum"],
             tool.inputSchema["properties"]["steps"]["maximum"]),
            ("integer", 1, 4),
        )
        self.assertEqual(
            (tool.inputSchema["properties"]["limit_per_view"]["type"],
             tool.inputSchema["properties"]["limit_per_view"]["minimum"],
             tool.inputSchema["properties"]["limit_per_view"]["maximum"]),
            ("integer", 1, 100),
        )
        base = {"operation_id": "contact-schema"}
        invalid = [("steps", value) for value in (True, 1.0, "1", 0, 5)]
        invalid += [("limit_per_view", value) for value in (True, 1.0, "2", 0, 101)]
        invalid += [("operation_id", ""), ("query", "private")]
        for key, value in invalid:
            with self.subTest(key=key, value=value), patch.object(self.gateway, "execute") as dispatch:
                with self.assertRaises(ToolError):
                    asyncio.run(self.gateway.SERVER.call_tool(
                        "wechat_read_contact_span", {**base, key: value},
                    ))
                dispatch.assert_not_called()

    def test_success_replay_is_metadata_only_and_never_dispatches_again(self):
        private_body = body()
        reply = {
            "ok": True,
            "worker_started": True,
            "result": private_body,
            "contact_span_evidence": evidence(),
            "evidence": {"background_observation_passed": True},
        }
        with patch.object(self.gateway, "execute", return_value=deepcopy(reply)) as dispatch:
            first = self.gateway.wechat_read_contact_span("contact-once", 1, 100)
            replay = self.gateway.wechat_read_contact_span("contact-once", 1, 100)
        dispatch.assert_called_once_with("read_contact_span", ARGS)
        self.assertTrue(first["body_available"])
        self.assertEqual(first["result"], private_body)
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["body_available"])
        self.assertNotIn("views", replay["result"])
        self.assertNotIn("private contact label", json.dumps(replay, ensure_ascii=False))
        database = (self.state / "operations.sqlite3").read_bytes()
        self.assertNotIn(b"private contact label", database)

    def test_same_id_with_changed_args_conflicts_without_dispatch(self):
        reply = {
            "ok": True,
            "worker_started": True,
            "result": body(),
            "contact_span_evidence": evidence(),
            "evidence": {"background_observation_passed": True},
        }
        with patch.object(self.gateway, "execute", return_value=reply) as dispatch:
            self.gateway.wechat_read_contact_span("contact-conflict", 1, 100)
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_read_contact_span("contact-conflict", 2, 100)
        self.assertEqual(json.loads(str(caught.exception))["code"], "operation_conflict")
        dispatch.assert_called_once()

    def test_invalid_body_or_evidence_is_unknown_and_does_not_store_private_rows(self):
        for alteration in ("missing_evidence", "wrong_count", "extra_evidence"):
            with self.subTest(alteration=alteration):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                gateway = load_isolated_gateway(Path(temp.name))
                reply = {
                    "ok": True,
                    "worker_started": True,
                    "result": body(),
                    "contact_span_evidence": evidence(),
                    "evidence": {"background_observation_passed": True},
                }
                if alteration == "missing_evidence":
                    del reply["contact_span_evidence"]
                elif alteration == "wrong_count":
                    reply["result"]["counts"]["completed_steps"] = 0
                else:
                    reply["contact_span_evidence"]["private_label"] = "private contact label"
                with patch.object(gateway, "execute", return_value=reply):
                    with self.assertRaises(ToolError) as caught:
                        gateway.wechat_read_contact_span("contact-invalid-" + alteration, 1, 100)
                error = json.loads(str(caught.exception))
                self.assertTrue(error.get("outcome_unknown"))
                self.assertEqual(error.get("reason_code"), "contact_span_success_unverified")
                self.assertNotIn("private contact label", str(caught.exception))


class ContactSpanGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.state_dir = self.directory

    def run_guardian(self, reply):
        return supervisor.run_request(
            {"action": "read_contact_span", "args": ARGS},
            state_dir=self.directory,
            backend=self.backend,
            worker_runner=lambda *_: deepcopy(reply),
            mutex_factory=lambda *_: nullcontext(),
        )

    def run_guardian_with_cleanup(self, reply, cleanup):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            backend = FakeBackend()
            backend.state_dir = directory
            with patch.object(supervisor.GateLease, "restore", return_value=cleanup):
                return supervisor.run_request(
                    {"action": "read_contact_span", "args": ARGS},
                    state_dir=directory,
                    backend=backend,
                    worker_runner=lambda *_: deepcopy(reply),
                    mutex_factory=lambda *_: nullcontext(),
                )

    def good(self):
        return {
            "ok": True,
            "result": body(),
            "contact_span_evidence": evidence(),
            "evidence": {"background_observation_passed": True},
        }

    def test_success_requires_body_evidence_background_and_gate(self):
        result = self.run_guardian(self.good())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], body())
        self.assertEqual(result["contact_span_evidence"], evidence())
        self.assertTrue(result["cleanup"]["restored"])

    def test_missing_extra_false_evidence_or_count_mismatch_never_releases_body(self):
        for alteration in ("missing", "extra", "false_restore", "bad_count"):
            with self.subTest(alteration=alteration):
                reply = self.good()
                if alteration == "missing":
                    del reply["contact_span_evidence"]
                elif alteration == "extra":
                    reply["contact_span_evidence"]["private_label"] = "private contact label"
                elif alteration == "false_restore":
                    reply["contact_span_evidence"]["contacts_view_restored"] = False
                else:
                    reply["result"]["counts"]["completed_steps"] = 0
                result = self.run_guardian(reply)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("result", result)
                self.assertNotIn("private contact label", json.dumps(result, ensure_ascii=False))
                self.assertTrue(result["error"]["outcome_unknown"])

    def test_cleanup_failure_or_started_worker_failure_strips_body_and_fixed_error(self):
        self.backend.fail_restore = True
        result = self.run_guardian(self.good())
        self.assertFalse(result["ok"])
        self.assertNotIn("result", result)
        self.assertNotIn("private contact label", json.dumps(result, ensure_ascii=False))
        self.assertTrue(result["error"]["outcome_unknown"])

        def raises(*_):
            raise RuntimeError("private contact label")

        self.backend.fail_restore = False
        result = supervisor.run_request(
            {"action": "read_contact_span", "args": ARGS},
            state_dir=self.directory,
            backend=self.backend,
            worker_runner=raises,
            mutex_factory=lambda *_: nullcontext(),
        )
        self.assertFalse(result["ok"])
        self.assertNotIn("private contact label", json.dumps(result, ensure_ascii=False))

        reply = {
            "ok": False,
            "worker_started": True,
            "error": {
                "code": "private-worker-code",
                "message": "private contact label",
                "outcome_unknown": True,
            },
            "contact_span_evidence": evidence(delivery_started=True),
        }
        result = self.run_guardian(reply)
        self.assertFalse(result["ok"])
        self.assertNotIn("private contact label", json.dumps(result, ensure_ascii=False))

    def test_malformed_cleanup_errors_never_prove_gate_or_publish_private_text(self):
        malformed = (
            ["private cleanup text"],
            "private cleanup text",
            [{"code": "private-cleanup-code", "message": "private cleanup text"}],
        )
        for errors in malformed:
            with self.subTest(errors=errors):
                result = self.run_guardian_with_cleanup(self.good(), {
                    "status": "restored", "restored": True, "observed": 0,
                    "errors": errors,
                })
                self.assertFalse(result["ok"], result)
                self.assertNotIn("result", result)
                self.assertTrue(result["error"]["outcome_unknown"])
                self.assertNotIn("private cleanup text", json.dumps(result, ensure_ascii=False))
                self.assertEqual(result["cleanup"]["errors"], [{"code": "contact_span_failed"}])

    def test_worker_cleanup_errors_use_fixed_allowlisted_codes(self):
        def raises(*_):
            failure = supervisor.WorkerFailure("private-worker-code", "private contact label")
            failure.cleanup_errors = [{"code": "private-cleanup-code", "message": "private cleanup text"}]
            raise failure

        result = supervisor.run_request(
            {"action": "read_contact_span", "args": ARGS},
            state_dir=self.directory,
            backend=self.backend,
            worker_runner=raises,
            mutex_factory=lambda *_: nullcontext(),
        )
        self.assertFalse(result["ok"])
        self.assertNotIn("private cleanup text", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["worker_cleanup_errors"], [{"code": "contact_span_failed"}])


class ContactSpanPreWorkerTests(unittest.TestCase):
    def setUp(self):
        self.gateway_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.gateway_temp.cleanup)
        self.gateway_state = Path(self.gateway_temp.name)
        self.gateway = load_isolated_gateway(self.gateway_state)

    def run_contact(self, backend, *, worker_runner=None, mutex_factory=None, timeout=30):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        backend.state_dir = directory
        return supervisor.run_request(
            {"action": "read_contact_span", "args": ARGS},
            timeout=timeout,
            state_dir=directory,
            backend=backend,
            worker_runner=worker_runner or (lambda *_: self.fail("worker must not start")),
            mutex_factory=mutex_factory or (lambda *_: nullcontext()),
        )

    def classify(self, response, operation_id, expected_state):
        with patch.object(self.gateway, "execute", return_value=response):
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_read_contact_span(operation_id, 1, 100)
        payload = json.loads(str(caught.exception))
        self.assertNotIn("PRIVATE-", str(caught.exception))
        self.assertNotIn("private cleanup", str(caught.exception))
        row = Journal(self.gateway_state / "operations.sqlite3").get(operation_id)
        self.assertEqual(row["state"], expected_state)
        return payload, row

    def test_activate_failure_is_known_when_lease_cleanup_is_proven(self):
        class ActivateFailureBackend(FakeBackend):
            def write_byte(self, identity, value):
                if value == 1:
                    raise RuntimeError("PRIVATE-ACTIVATE-TEXT")
                return super().write_byte(identity, value)

        response = self.run_contact(ActivateFailureBackend())
        self.assertFalse(response["ok"])
        self.assertFalse(response["error"]["outcome_unknown"])
        self.assertNotIn("result", response)
        self.assertNotIn("navigation_started", response["error"])
        self.assertEqual(response["cleanup"]["errors"], [])
        _, row = self.classify(response, "contact-activate-failure", "rejected")
        self.assertEqual(row["reason_code"], response["error"]["code"])

    def test_rollback_failure_is_unknown_and_sanitized_before_gateway(self):
        class RollbackFailureBackend(FakeBackend):
            def write_byte(self, identity, value):
                if value == 0:
                    raise RuntimeError("PRIVATE-ROLLBACK-TEXT")
                return super().write_byte(identity, value)

        backend = RollbackFailureBackend()
        with patch.object(supervisor.time, "monotonic", side_effect=(0.0, 0.0, 30.0, 30.0)):
            response = self.run_contact(backend, timeout=1)
        self.assertFalse(response["ok"])
        self.assertTrue(response["error"]["outcome_unknown"])
        self.assertNotIn("result", response)
        self.assertNotIn("navigation_started", response["error"])
        self.assertEqual(response["cleanup"]["errors"], [{"code": "contact_span_failed"}])
        self.classify(response, "contact-rollback-failure", "outcome_unknown")

    def test_preworker_backend_close_failure_is_unknown_and_private_free(self):
        class CloseFailureBackend(FakeBackend):
            def close(self):
                raise RuntimeError("PRIVATE-CLOSE-TEXT")

        def broken_mutex(*_):
            raise RuntimeError("PRIVATE-MUTEX-TEXT")

        response = self.run_contact(CloseFailureBackend(), mutex_factory=broken_mutex)
        self.assertFalse(response["ok"])
        self.assertTrue(response["error"]["outcome_unknown"])
        self.assertNotIn("result", response)
        self.assertNotIn("navigation_started", response["error"])
        self.assertEqual(response["cleanup"]["errors"], [{"code": "contact_span_failed"}])
        self.classify(response, "contact-close-failure", "outcome_unknown")

    def test_preworker_known_rejection_has_no_navigation_or_unknown(self):
        def broken_mutex(*_):
            raise RuntimeError("PRIVATE-PREWORKER-TEXT")

        response = self.run_contact(FakeBackend(), mutex_factory=broken_mutex)
        self.assertFalse(response["ok"])
        self.assertFalse(response["error"]["outcome_unknown"])
        self.assertNotIn("result", response)
        self.assertNotIn("navigation_started", response["error"])
        self.assertEqual(response["cleanup"]["errors"], [])
        _, row = self.classify(response, "contact-known-rejection", "rejected")
        self.assertEqual(row["reason_code"], response["error"]["code"])

    def test_non_dict_request_keeps_invalid_request_response(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        backend = FakeBackend()
        response = supervisor.run_request(None, state_dir=Path(temp.name), backend=backend)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")


class ContactSpanWorkerTests(unittest.TestCase):
    def invoke(self, *, failure=False, background_passed=True):
        calls = []
        private = "private contact label"

        class FakeAdapter:
            submission_started = False
            navigation_evidence = {"attempted": True, "restored": True}
            contact_span_evidence = evidence()

            def __init__(self, target):
                del target

            def dispatch(self, *_):
                raise AssertionError("read_contact_span must use the collector")

        class FakeMonitor:
            def __init__(self, pid, hwnd, *, phase_recorder):
                del pid, hwnd
                self.recorder = phase_recorder

            def start(self):
                return self

            def stop(self):
                return {"background_observation_passed": background_passed}

        def collect(adapter, *, steps, limit_per_view, deadline, phase):
            self.assertTrue(callable(phase))
            calls.append((steps, limit_per_view, deadline))
            adapter.contact_span_evidence = evidence(steps, delivery_started=True)
            if failure:
                error = AdapterError("context_conflict")
                error.outcome_unknown = True
                raise error
            result = body(steps, private)
            return result

        adapter_module = types.ModuleType("wxbg.adapter")
        adapter_module.Adapter = FakeAdapter
        monitor_module = worker_monitor_module(FakeMonitor)
        collector_module = types.ModuleType("wxbg.contact_span")
        collector_module.collect_contact_span = collect
        request = {
            "action": "read_contact_span",
            "args": ARGS,
            "target": {"pid": 1, "hwnd": 2},
            "deadline": 12345.0,
        }
        output = io.StringIO()
        with patch.dict(sys.modules, {
            "wxbg.adapter": adapter_module,
            "wxbg.monitor": monitor_module,
            "wxbg.contact_span": collector_module,
        }), patch.object(sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(output):
            worker.main()
        response = json.loads(output.getvalue())
        self.assertEqual(calls, [(1, 100, 12345.0)])
        self.assertEqual(response["contact_span_evidence"], evidence())
        self.assertNotIn("private contact label", output.getvalue()) if failure else None
        if failure:
            self.assertFalse(response["ok"])
            self.assertNotIn("result", response)
            self.assertTrue(response["error"]["outcome_unknown"])
        else:
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"], body())

    def test_success_routes_to_contact_span_collector(self):
        self.invoke()

    def test_failure_keeps_evidence_and_strips_body(self):
        self.invoke(failure=True)


if __name__ == "__main__":
    unittest.main()
