import io
import json
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from wxbg import worker
from wxbg.policy import AdapterError
from worker_dpi_stub import worker_monitor_module


READSTORE_ACTIONS = (
    "readstore_read_inbox",
    "readstore_search",
    "readstore_read_new",
    "readstore_batch_read_new",
)
TARGET = {"pid": 123, "created": 456.5, "hwnd": 789}


class _FakeMonitor:
    instances = []
    stop_error = None
    evidence = {"background_observation_passed": True, "monitor_errors": []}

    def __init__(self, pid, hwnd):
        self.pid = pid
        self.hwnd = hwnd
        self.started = False
        type(self).instances.append(self)

    def start(self):
        self.started = True
        return self

    def stop(self):
        if type(self).stop_error is not None:
            raise type(self).stop_error
        return dict(type(self).evidence)


class ReadstoreWorkerTests(unittest.TestCase):
    def setUp(self):
        _FakeMonitor.instances = []
        _FakeMonitor.stop_error = None
        _FakeMonitor.evidence = {"background_observation_passed": True, "monitor_errors": []}
        self.deadline = 10_000_000_000.0

    def invoke(self, action, *, provider=None, adapter=None, check_background=None,
               monitor_class=_FakeMonitor, args=None, deadline=None):
        provider_module = types.ModuleType("wxbg.readstore_provider")
        if provider is not None:
            provider_module.run = provider
        adapter_module = types.ModuleType("wxbg.adapter")
        if adapter is not None:
            adapter_module.Adapter = adapter
        monitor_module = worker_monitor_module(monitor_class)
        request = {
            "action": action,
            "args": {} if args is None else args,
            "target": dict(TARGET),
            "deadline": self.deadline if deadline is None else deadline,
        }
        output = io.StringIO()
        modules = {
            "wxbg.monitor": monitor_module,
            "wxbg.adapter": adapter_module,
            "wxbg.readstore_provider": provider_module,
        }
        with patch.dict(sys.modules, modules), \
                patch.object(worker, "_check_hint_background", side_effect=check_background), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(request))), \
                redirect_stdout(output):
            worker.main()
        return json.loads(output.getvalue())

    def test_allowlisted_actions_route_to_provider_without_adapter(self):
        seen = []

        def provider(action, args, *, target, deadline):
            seen.append((action, args, target, deadline))
            return {"bounded": True, "action": action, "count": 0}

        class ExplodingAdapter:
            def __init__(self, target):
                raise AssertionError("readstore must not construct Adapter")

        for action in READSTORE_ACTIONS:
            with self.subTest(action=action):
                result = self.invoke(
                    action,
                    provider=provider,
                    adapter=ExplodingAdapter,
                    check_background=lambda target: None,
                    args={"limit": 3},
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["result"]["action"], action)
        self.assertEqual([item[0] for item in seen], list(READSTORE_ACTIONS))
        self.assertTrue(all(item[1] == {"limit": 3} for item in seen))
        self.assertTrue(all(item[2] == TARGET for item in seen))
        self.assertTrue(all(item[3] == self.deadline for item in seen))

    def test_pre_background_failure_blocks_provider_and_returns_no_result(self):
        called = []

        def provider(*args, **kwargs):
            called.append(True)
            return {"bounded": True}

        result = self.invoke(
            "readstore_search",
            provider=provider,
            check_background=AdapterError("stale_process"),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "readstore_background_failed")
        self.assertNotIn("result", result)
        self.assertEqual(called, [])
        self.assertIn("evidence", result)

    def test_post_background_failure_withholds_provider_result(self):
        called = []

        def provider(*args, **kwargs):
            called.append(True)
            return {"bounded": True, "secret": "must not be returned after drift"}

        result = self.invoke(
            "readstore_read_inbox",
            provider=provider,
            check_background=[None, AdapterError("stale_process")],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "readstore_background_failed")
        self.assertNotIn("result", result)
        self.assertEqual(called, [True])

    def test_provider_exception_is_fixed_code_without_private_message(self):
        private = "C:\\Users\\owner\\session.db key=super-secret"

        def provider(*args, **kwargs):
            raise RuntimeError(private)

        result = self.invoke(
            "readstore_search",
            provider=provider,
            check_background=lambda target: None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "readstore_provider_failed")
        self.assertEqual(result["error"]["detail"], "readstore_provider_failed")
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn("session.db", json.dumps(result))
        self.assertIn("evidence", result)

    def test_missing_provider_never_falls_back_to_adapter(self):
        class ExplodingAdapter:
            def __init__(self, target):
                raise AssertionError("missing provider must not fall back to Adapter")

        result = self.invoke(
            "readstore_read_new",
            adapter=ExplodingAdapter,
            check_background=lambda target: None,
        )
        self.assertFalse(result["ok"])
        self.assertIn(result["error"]["code"], {
            "readstore_provider_unavailable", "readstore_provider_failed",
        })
        self.assertNotIn("result", result)

    def test_provider_result_must_be_bounded_dict(self):
        result = self.invoke(
            "readstore_search",
            provider=lambda *args, **kwargs: ["unbounded"],
            check_background=lambda target: None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "readstore_result_invalid")
        self.assertNotIn("result", result)

    def test_monitor_cleanup_failure_cannot_return_success_data(self):
        _FakeMonitor.stop_error = RuntimeError("private monitor cleanup")
        result = self.invoke(
            "readstore_read_new",
            provider=lambda *args, **kwargs: {"bounded": True, "rows": []},
            check_background=lambda target: None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "monitor_failed")
        self.assertNotIn("result", result)
        self.assertNotIn("private monitor cleanup", json.dumps(result))

    def test_unsupported_action_uses_existing_adapter_path(self):
        calls = []

        class Adapter:
            submission_started = False

            def __init__(self, target):
                calls.append(("init", target))

            def dispatch(self, action, args):
                calls.append(("dispatch", action, args))
                return {"legacy": True}

        result = self.invoke(
            "readstore_future",
            adapter=Adapter,
            check_background=None,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], {"legacy": True})
        self.assertEqual(calls, [("init", TARGET), ("dispatch", "readstore_future", {})])

    def test_expired_readstore_deadline_does_not_call_provider(self):
        called = []

        def provider(*args, **kwargs):
            called.append(True)
            return {"bounded": True}

        result = self.invoke(
            "readstore_search",
            provider=provider,
            check_background=lambda target: None,
            deadline=0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "readstore_deadline")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
