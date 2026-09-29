"""0.7 bounded-history-span contracts using owned fakes; never Weixin/UI."""

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

from wxbg import history_span as history_span_module
from wxbg import supervisor, worker
from wxbg.policy import AdapterError
from test_gate import FakeBackend
from test_gateway import load_isolated_gateway
from test_history_span_contract import body, evidence
from worker_dpi_stub import worker_monitor_module
from test_history_span import FakeAdapter as CollectorFakeAdapter, REF as COLLECTOR_REF
from wxbg.history_span_contract import valid_history_span_success


ARGS = {
    "session_ref": "session-opaque-ref",
    "direction": "older",
    "steps": 1,
    "limit_per_view": 200,
}


class HistorySpanGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.gateway = load_isolated_gateway(self.state)

    def test_schema_is_strict_and_rejects_extra_and_bool_coercion(self):
        tools = asyncio.run(self.gateway.SERVER.list_tools())
        tool = next(item for item in tools if item.name == "wechat_read_history_span")
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
            ("integer", 1, 200),
        )
        base = {**ARGS, "operation_id": "span-schema"}
        invalid = [("steps", value) for value in (True, 1.0, "1", 0, 5)]
        invalid += [("limit_per_view", value) for value in (True, 1.0, "2", 0, 201)]
        invalid += [("direction", "up"), ("filter", {}), ("body", True)]
        for key, value in invalid:
            with self.subTest(key=key, value=value), patch.object(self.gateway, "execute") as dispatch:
                with self.assertRaises(ToolError):
                    asyncio.run(self.gateway.SERVER.call_tool(
                        "wechat_read_history_span", {**base, key: value},
                    ))
                dispatch.assert_not_called()

    def test_success_persists_metadata_only_and_replay_does_not_resend_or_return_body(self):
        private_body = body()
        reply = {"ok": True, "worker_started": True, "result": private_body,
                 "history_span_evidence": evidence(),
                 "evidence": {"background_observation_passed": True}}
        with patch.object(self.gateway, "execute", return_value=reply) as dispatch:
            first = self.gateway.wechat_read_history_span(**ARGS, operation_id="span-once")
            replay = self.gateway.wechat_read_history_span(**ARGS, operation_id="span-once")
        dispatch.assert_called_once_with("read_history_span", ARGS)
        self.assertTrue(first["body_available"])
        self.assertEqual(first["result"], private_body)
        self.assertEqual(first["result"]["views"][0]["rows"][0]["text"], "private span text")
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["body_available"])
        self.assertNotIn("views", replay["result"])
        database = (self.state / "operations.sqlite3").read_bytes()
        self.assertNotIn(b"private span text", database)
        self.assertNotIn("private span text", json.dumps(replay, ensure_ascii=False))

    def test_unknown_outcome_is_sticky_and_never_resends(self):
        reply = {"ok": False, "worker_started": True,
                 "error": {"code": "TIMEOUT", "outcome_unknown": True}}
        with patch.object(self.gateway, "execute", return_value=reply) as dispatch:
            for _ in range(2):
                with self.assertRaises(ToolError) as caught:
                    self.gateway.wechat_read_history_span(**ARGS, operation_id="span-timeout")
                self.assertIn("outcome_unknown", str(caught.exception))
        dispatch.assert_called_once()

    def test_fake_success_body_is_not_persisted_or_replayed(self):
        fake = body()
        with patch.object(self.gateway, "execute", return_value={"ok": True, "result": fake}) as dispatch:
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_read_history_span(**ARGS, operation_id="span-fake")
            self.assertIn("outcome_unknown", str(caught.exception))
            with self.assertRaises(ToolError):
                self.gateway.wechat_read_history_span(**ARGS, operation_id="span-fake")
        dispatch.assert_called_once()

    def test_summary_exception_reason_is_fixed_and_private_text_is_not_leaked(self):
        reply = {"ok": True, "worker_started": True, "result": body(),
                 "history_span_evidence": evidence(),
                 "evidence": {"background_observation_passed": True}}
        with patch.object(self.gateway, "execute", return_value=reply), \
                patch.object(self.gateway, "history_span_journal_summary",
                             side_effect=RuntimeError("private body")):
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_read_history_span(**ARGS, operation_id="span-private-error")
        self.assertNotIn("private body", str(caught.exception))
        self.assertIn("outcome_unknown", str(caught.exception))


class HistorySpanGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.state_dir = self.directory

    def run_guardian(self, reply):
        return supervisor.run_request(
            {"action": "read_history_span", "args": ARGS},
            state_dir=self.directory, backend=self.backend,
            worker_runner=lambda *_: deepcopy(reply),
            mutex_factory=lambda *_: nullcontext(),
        )

    def good(self):
        return {"ok": True, "result": body(), "history_span_evidence": evidence(),
                "evidence": {"background_observation_passed": True}}

    def test_success_requires_body_evidence_background_and_gate(self):
        result = self.run_guardian(self.good())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], body())
        self.assertEqual(result["history_span_evidence"], evidence())
        self.assertTrue(result["cleanup"]["restored"])

    def test_current_verified_row_geometry_passes_the_supervisor_boundary(self):
        reply = self.good()
        for view in reply["result"]["views"]:
            for item in view["rows"]:
                viewport = view["viewport_bounds"]
                item["bounds"] = [viewport[0], viewport[1] + 1,
                                   viewport[2], viewport[3] - 1]
        result = self.run_guardian(reply)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], reply["result"])

    def test_missing_extra_false_evidence_or_count_mismatch_never_releases_body(self):
        for alteration in ("missing", "extra", "false_final", "bad_count", "evidence_direction"):
            with self.subTest(alteration=alteration):
                reply = self.good()
                if alteration == "missing":
                    del reply["history_span_evidence"]
                elif alteration == "extra":
                    reply["history_span_evidence"]["private_text"] = "must not leak"
                elif alteration == "false_final":
                    reply["history_span_evidence"]["final_view_retained"] = False
                elif alteration == "evidence_direction":
                    reply["history_span_evidence"]["direction"] = "newer"
                else:
                    reply["result"]["counts"]["completed_steps"] = 0
                result = self.run_guardian(reply)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("result", result)
                self.assertNotIn("private span text", json.dumps(result, ensure_ascii=False))
                self.assertTrue(result["error"]["outcome_unknown"])

    def test_background_failure_and_gate_restore_failure_never_release_body(self):
        reply = self.good()
        reply["evidence"]["background_observation_passed"] = False
        result = self.run_guardian(reply)
        self.assertFalse(result["ok"])
        self.assertNotIn("result", result)
        self.assertNotIn("private span text", json.dumps(result, ensure_ascii=False))

        self.backend.fail_restore = True
        result = self.run_guardian(self.good())
        self.assertFalse(result["ok"])
        self.assertNotIn("result", result)
        self.assertNotIn("worker_result", result)
        self.assertNotIn("private span text", json.dumps(result, ensure_ascii=False))
        self.assertTrue(result["error"]["outcome_unknown"])

    def test_real_collector_output_is_accepted_with_its_raw_row_kinds(self):
        fake = CollectorFakeAdapter()
        result = history_span_module.collect_history_span(
            fake, COLLECTOR_REF, "older", 1, 200, 30.0,
            observe=fake.observe, wheel=fake.wheel,
            clock=lambda: fake.time, sleep=fake.sleep,
        )
        self.assertEqual(
            {row["kind"] for view in result["views"] for row in view["rows"]},
            {"mmui::ChatTextItemView", "mmui::ChatBubbleItemView"},
        )
        self.assertTrue(valid_history_span_success(
            result, fake.history_span_evidence,
            {"background_observation_passed": True},
            {"session_ref": COLLECTOR_REF, "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))


class HistorySpanWorkerTests(unittest.TestCase):
    def invoke(self, *, failure=False):
        calls = []

        class FakeAdapter:
            submission_started = False

            def __init__(self, *_):
                self.dispatched = False

            def dispatch(self, *_):
                self.dispatched = True
                raise AssertionError("read_history_span must not use Adapter.dispatch")

        class FakeMonitor:
            def __init__(self, *_):
                pass

            def start(self):
                return self

            def stop(self):
                return {"background_observation_passed": True}

        def collect(adapter, **kwargs):
            calls.append(kwargs["deadline"])
            adapter.history_span_evidence = evidence()
            if failure:
                exc = AdapterError("context_conflict")
                exc.outcome_unknown = True
                raise exc
            return body()

        modules = {}
        for name, key, value in (
            ("wxbg.adapter", "Adapter", FakeAdapter),
            ("wxbg.monitor", "Monitor", FakeMonitor),
            ("wxbg.history_span", "collect_history_span", collect),
        ):
            module = types.ModuleType(name)
            setattr(module, key, value)
            modules[name] = module
        modules["wxbg.monitor"] = worker_monitor_module(FakeMonitor)
        request = {
            "action": "read_history_span", "args": ARGS,
            "target": {"pid": 1, "hwnd": 2, "created": 3}, "deadline": 12345.0,
        }
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(output):
            worker.main()
        response = json.loads(output.getvalue())
        self.assertEqual(calls, [12345.0])
        self.assertEqual(response["history_span_evidence"], evidence())
        self.assertNotIn("AssertionError", output.getvalue())
        if failure:
            self.assertFalse(response["ok"])
            self.assertNotIn("result", response)
            self.assertTrue(response["error"]["outcome_unknown"])
        else:
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"], body())

    def test_success_routes_to_span_collector_with_deadline(self):
        self.invoke()

    def test_failure_keeps_typed_evidence_and_does_not_leak_body(self):
        self.invoke(failure=True)


if __name__ == "__main__":
    unittest.main()
