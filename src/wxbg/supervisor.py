"""Single-request guardian. The gateway may exit; this process owns rollback.

Protocol: one stdin JSON {action,args}; one stdout JSON response. Worker process
failure is never retried. UIA calls run only in the separately killable worker.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from .gate import GateError, GateLease, SessionMutex, WinBackend, recover_pending
from .event_contract import normalize_event_evidence, valid_hint_success, event_cleanup_verified
from .history_contract import normalize_history_evidence, valid_history_success
from .history_span_contract import normalize_history_span_evidence, valid_history_span_success
from .contact_span_contract import normalize_contact_span_evidence, valid_contact_span_success, safe_contact_span_error
from .session_scan_contract import normalize_scan_evidence, valid_scan_success, safe_scan_error
from .session_viewport_contract import valid_session_viewports_success
from .ui_read_contract import (
    UI_READ_ACTIONS, valid_ui_background, valid_ui_read_result,
    valid_open_session_evidence, valid_open_session_success,
)

MAX_WORKER_SECONDS = 30.0
MAX_HINT_WORKER_SECONDS = 105.0
SEND_ACTIONS = frozenset({'send_text', 'send_at_username'})
MAX_SEND_WORKER_SECONDS = 40.0
SCAN_ACTIONS = frozenset({'scan_open_session', 'scan_session_viewports'})
MAX_SCAN_WORKER_SECONDS = 70.0
MAX_REQUEST_BYTES = 1024 * 1024
READSTORE_ACTIONS = frozenset({
    "readstore_read_inbox",
    "readstore_search",
    "readstore_read_new",
    "readstore_batch_read_new",
    "readstore_get_attachment",
})
MUTATION_ACTIONS = frozenset({
    'send_text', 'send_at_username', 'send_file', 'set_draft',
})
MAX_READSTORE_RESULT_BYTES = 512 * 1024
MAX_ATTACHMENT_RESULT_BYTES = 24 * 1024 * 1024


def _readstore_cleanup():
    """Evidence for an action that never acquired or changed the UI gate."""
    return {
        "status": "read_only",
        "restored": True,
        "modified": False,
        "gate_touched": False,
        "errors": [],
    }


def _valid_mutation_background(evidence, target):
    """Independently check the worker's no-focus monitor before success."""
    if not valid_ui_background(evidence) or type(target) is not dict:
        return False
    if (evidence.get('observations') < 2
            or evidence.get('poll_interval_ms') != 10
            or type(evidence.get('cursor_changed')) is not bool):
        return False
    expected_labels = {
        'cursor_api': 'GetCursorPos',
        'cursor_dpi_context': 'per_monitor_v2',
        'cursor_coordinate_space': 'screen_coordinates_under_pm_v2',
    }
    snapshots = []
    for name in ('before', 'after'):
        value = evidence.get(name)
        if type(value) is not dict or value.get('minimized') is not True:
            return False
        if any(value.get(key) != expected for key, expected in expected_labels.items()):
            return False
        cursor = value.get('cursor')
        windows = value.get('visible_windows')
        if (type(value.get('foreground')) is not int
                or type(value.get('clipboard_sequence')) is not int
                or type(value.get('capture')) is not int or value['capture'] != 0
                or type(cursor) is not list or len(cursor) != 2
                or any(type(point) is not int for point in cursor)
                or type(windows) is not list or windows != [target.get('hwnd')]):
            return False
        snapshots.append(value)
    before, after = snapshots
    if (before['foreground'] != after['foreground']
            or before['clipboard_sequence'] != after['clipboard_sequence']
            or before['visible_windows'] != after['visible_windows']
            or not evidence['cursor_changed'] and before['cursor'] != after['cursor']):
        return False
    return True


def _normalize_contact_span_evidence(value):
    """Fail closed if a malformed/missing evidence shape trips the contract parser."""
    try:
        return normalize_contact_span_evidence(value)
    except Exception:
        return {"primary_error_code": "contact_span_evidence_invalid"}, False


def _sanitize_contact_error_items(value):
    """Publish only opaque allowlisted contact error codes."""
    items = value if type(value) is list else [value]
    return [{"code": safe_contact_span_error(item.get("code") if isinstance(item, dict) else None)}
            for item in items]


def _bounded_readstore_result(value, *, max_bytes=MAX_READSTORE_RESULT_BYTES):
    """Recheck the worker result cap before forwarding it to the gateway."""
    if type(value) is not dict:
        raise WorkerFailure("WORKER_PROTOCOL_ERROR", "Read-store result is not an object")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ATTACHMENT_RESULT_BYTES:
        raise WorkerFailure("WORKER_PROTOCOL_ERROR", "Read-store result bound is invalid")
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except Exception as exc:
        raise WorkerFailure("WORKER_PROTOCOL_ERROR", "Read-store result is not JSON") from exc
    if len(encoded) > max_bytes:
        raise WorkerFailure("WORKER_PROTOCOL_ERROR", "Read-store result exceeds its transport bound")
    return value


class WorkerFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.cleanup_errors = []


class _WorkerJob:
    """A guardian crash closes this non-inherited handle and kills its worker."""
    def __init__(self):
        self.handle = None
        if os.name == "nt":
            import win32job
            self.api = win32job
            self.handle = win32job.CreateJobObject(None, "")
            try:
                information = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
                information["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, information)
            except BaseException:
                self.close()
                raise

    def attach(self, process):
        if self.handle is not None:
            self.api.AssignProcessToJobObject(self.handle, process._handle)

    def close(self):
        if self.handle is not None:
            self.handle.Close()
            self.handle = None


def _worker_command():
    """Spawn the actual interpreter, loading only this install's trusted site.

    A Windows venv python.exe may spawn its child before Job assignment. Direct
    launch makes the Popen process the worker itself. Explicit addsitedir runs
    the installed venv .pth files, including pywin32's DLL bootstrap.
    """
    if os.name != 'nt':
        return [sys.executable, '-m', 'wxbg.worker']
    executable = Path(sys._base_executable).resolve(strict=True)
    site_directory = Path(sys.prefix) / 'Lib/site-packages'
    source_directory = Path(__file__).resolve().parents[1]
    if not executable.is_file() or not site_directory.is_dir() or not source_directory.is_dir():
        raise WorkerFailure('WORKER_RUNTIME_UNAVAILABLE', 'Worker installation paths are unavailable')
    bootstrap = (
        "import runpy,site,sys;"
        "site.addsitedir(sys.argv[1]);sys.path.insert(0,sys.argv[2]);"
        "sys.argv=['wxbg.worker'];runpy.run_module('wxbg.worker',run_name='__main__')"
    )
    return [str(executable), '-I', '-S', '-X', 'utf8', '-c', bootstrap,
            str(site_directory), str(source_directory)]


def _run_worker(request, timeout):
    began = time.monotonic()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    job = _WorkerJob()
    process = None
    failure = None
    cleanup_errors = []
    try:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        environment.pop('__PYVENV_LAUNCHER__', None)
        process = subprocess.Popen(
            _worker_command(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
            errors="replace", env=environment, creationflags=flags,
        )
        # No request is dispatched until assignment succeeds. stdin reads in
        # worker.main block meanwhile. The gateway does not own this job.
        job.attach(process)
        try:
            remaining = timeout - (time.monotonic() - began)
            if remaining <= 0:
                raise WorkerFailure("TIMEOUT", "Worker deadline expired before dispatch")
            stdout, stderr = process.communicate(json.dumps(request, ensure_ascii=True), timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise WorkerFailure("TIMEOUT", "UIA worker exceeded its deadline; result is unknown and must not be retried") from exc
    except BaseException as exc:
        failure = exc if isinstance(exc, WorkerFailure) else WorkerFailure("CRASH", "Worker launch or execution failed: " + str(exc))
    finally:
        try:
            if process is not None and process.poll() is None:
                process.kill()
        except BaseException as exc:
            cleanup_errors.append({"code": "WORKER_KILL_FAILED", "message": str(exc)})
        try:
            job.close()
        except BaseException as exc:
            cleanup_errors.append({"code": "WORKER_JOB_CLOSE_FAILED", "message": str(exc)})
        if process is not None and process.returncode is None:
            try:
                process.communicate(timeout=2)
            except BaseException as exc:
                cleanup_errors.append({"code": "WORKER_REAP_FAILED", "message": str(exc)})
                # Do not let inherited pipe handles defeat the guardian's bound.
                for stream in (process.stdin, process.stdout, process.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
    if cleanup_errors and failure is None:
        failure = WorkerFailure("WORKER_CLEANUP_FAILED", "Worker lifecycle cleanup was incomplete")
    if failure is not None:
        failure.cleanup_errors = cleanup_errors
        raise failure
    if process.returncode != 0:
        # Do not leak worker stderr, which could include UI text or a traceback
        # containing request content. The diagnostic code is enough for callers.
        raise WorkerFailure("CRASH", "UIA worker exited with code " + str(process.returncode))
    try:
        value = json.loads(stdout)
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise ValueError("missing boolean ok")
        if value["ok"] and "result" not in value:
            raise ValueError("missing result")
        if not value["ok"] and "error" not in value:
            raise ValueError("missing error")
        return value
    except (ValueError, TypeError) as exc:
        raise WorkerFailure("WORKER_PROTOCOL_ERROR", "UIA worker did not return one valid response") from exc


def _error(code, message, unknown=False):
    return {"code": code, "message": message, "outcome_unknown": bool(unknown)}


def run_request(request, timeout=None, *, state_dir=None, backend=None,
                worker_runner=None, mutex_factory=None):
    """Run once and always emit independent cleanup evidence; injection aids fake tests."""
    contact_request = isinstance(request, dict) and request.get("action") == "read_contact_span"
    scan_request = (isinstance(request, dict)
                    and type(request.get("action")) is str
                    and request["action"] in SCAN_ACTIONS)
    readstore_request = (isinstance(request, dict)
                         and isinstance(request.get("action"), str)
                         and request.get("action") in READSTORE_ACTIONS)
    result = {"ok": False, "worker_started": False,
              "cleanup": {"status": "not_started", "restored": True, "errors": []}}
    lease = None
    owned_backend = backend
    started = time.monotonic()
    try:
        if (not isinstance(request, dict) or not isinstance(request.get("action"), str)
                or not request["action"] or not isinstance(request.get("args", {}), dict)):
            raise GateError("INVALID_REQUEST", "Expected {action: nonempty string, args: object}")
        if timeout is None:
            timeout = (MAX_HINT_WORKER_SECONDS if request['action'] == 'wait_for_ui_hint'
                       else 30)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise GateError("INVALID_TIMEOUT", "Timeout must be positive and finite")
        worker_limit = (MAX_SEND_WORKER_SECONDS if request['action'] in SEND_ACTIONS
                        else MAX_SCAN_WORKER_SECONDS if request['action'] in SCAN_ACTIONS
                        else MAX_HINT_WORKER_SECONDS if request['action'] == 'wait_for_ui_hint'
                        else MAX_WORKER_SECONDS)
        deadline = started + min(float(timeout), worker_limit)
        directory = state_dir or os.environ.get("WXBG_STATE_DIR")
        if not directory:
            raise GateError("STATE_DIR_REQUIRED", "WXBG_STATE_DIR must identify the persistent guardian state directory")
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        owned_backend = owned_backend if owned_backend is not None else WinBackend()
        factory = mutex_factory or SessionMutex
        with factory(owned_backend, min(2.0, max(0.0, deadline - time.monotonic()))):
            try:
                result["recovery"] = recover_pending(directory, owned_backend)
                target = owned_backend.discover()
                readstore_identity = None
                if readstore_request:
                    result["cleanup"] = _readstore_cleanup()
                    result["target_validation"] = {"status": "not_started"}
                    try:
                        readstore_identity = owned_backend.validate(target)
                    except BaseException:
                        result["target_validation"] = {"status": "before_failed"}
                        raise
                    result["target_validation"] = {"status": "before_validated"}
                else:
                    lease = GateLease(directory, owned_backend, target)
                    lease.activate()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GateError("DEADLINE_BEFORE_DISPATCH", "Guardian deadline expired before worker dispatch")
                worker_request = {"action": request["action"], "args": request.get("args", {}),
                                  "target": {key: target[key] for key in ("pid", "created", "hwnd")},
                                  "deadline": deadline}
                result["worker_started"] = True
                if readstore_request:
                    worker_error = None
                    reply = None
                    try:
                        reply = (worker_runner or _run_worker)(worker_request, remaining)
                    except BaseException as exc:
                        worker_error = exc
                    try:
                        after_identity = owned_backend.validate(target)
                    except BaseException as exc:
                        result["target_validation"] = {"status": "after_failed"}
                        raise GateError(
                            "TARGET_VALIDATION_FAILED",
                            "Read-only target could not be revalidated",
                        ) from exc
                    if after_identity != readstore_identity:
                        result["target_validation"] = {"status": "changed"}
                        raise GateError(
                            "TARGET_IDENTITY_CHANGED",
                            "Read-only target identity changed during the worker",
                        )
                    result["target_validation"] = {"status": "stable"}
                    if worker_error is not None:
                        raise worker_error
                else:
                    reply = (worker_runner or _run_worker)(worker_request, remaining)
                if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                    raise WorkerFailure("WORKER_PROTOCOL_ERROR", "Worker returned an invalid response")
                if "evidence" in reply:
                    result["evidence"] = reply["evidence"]
                if "native_evidence" in reply:
                    result["native_evidence"] = reply["native_evidence"]
                if request['action'] in ('probe_image_entry','send_image'):
                    from .image_observation_contract import normalize_image_evidence
                    result['image_evidence'], image_valid = normalize_image_evidence(reply.get('image_evidence'))
                    result['image_evidence_valid'] = image_valid and reply.get('image_evidence_valid') is True
                if "navigation_evidence" in reply:
                    result["navigation_evidence"] = reply["navigation_evidence"]
                if request['action'] == 'wait_for_ui_hint':
                    result['event_evidence'] = normalize_event_evidence(reply.get('event_evidence'))[0]
                if request['action'] == 'scroll_messages':
                    result['history_evidence'] = normalize_history_evidence(reply.get('history_evidence'))[0]
                if request['action'] == 'read_history_span':
                    result['history_span_evidence'] = normalize_history_span_evidence(
                        reply.get('history_span_evidence'))[0]
                if request['action'] == 'read_contact_span':
                    result['contact_span_evidence'] = _normalize_contact_span_evidence(
                        reply.get('contact_span_evidence'))[0]
                if reply["ok"]:
                    if "result" not in reply:
                        raise WorkerFailure("WORKER_PROTOCOL_ERROR", "Worker success lacks result")
                    if readstore_request:
                        reply["result"] = _bounded_readstore_result(
                            reply["result"],
                            max_bytes=(MAX_ATTACHMENT_RESULT_BYTES
                                       if request["action"] == "readstore_get_attachment"
                                       else MAX_READSTORE_RESULT_BYTES),
                        )
                    result.update(ok=True, result=reply["result"])
                else:
                    error = reply.get("error", {})
                    if isinstance(error, str):
                        error = {"code": "WORKER_ERROR", "message": error}
                    if not isinstance(error, dict):
                        raise WorkerFailure("WORKER_PROTOCOL_ERROR", "Worker error has invalid shape")
                    unknown = error.get("outcome_unknown", False) or error.get("submission_started", False)
                    if contact_request:
                        code = safe_contact_span_error(error.get("code", "contact_span_failed"))
                        result["error"] = _error(code, code, unknown)
                    elif scan_request:
                        code = safe_scan_error(error.get('code'))
                        result['error'] = _error(code, code, unknown)
                    else:
                        result["error"] = _error(error.get("code", "WORKER_ERROR"),
                                                 error.get("message", error.get("detail", "Worker reported an error")),
                                                 unknown)
                    if "submission_started" in error:
                        result["error"]["submission_started"] = bool(error["submission_started"])
            except BaseException as exc:
                if contact_request:
                    code = safe_contact_span_error(getattr(exc, "code", type(exc).__name__))
                    result["error"] = _error(code, code, result["worker_started"])
                elif scan_request:
                    code = safe_scan_error(getattr(exc, 'code', None))
                    result['error'] = _error(code, code, result['worker_started'])
                else:
                    result["error"] = _error(getattr(exc, "code", "SUPERVISOR_ERROR"), str(exc),
                                              result["worker_started"])
                if getattr(exc, "cleanup_errors", None):
                    if contact_request:
                        result["worker_cleanup_errors"] = _sanitize_contact_error_items(exc.cleanup_errors)
                    else:
                        result["worker_cleanup_errors"] = exc.cleanup_errors
            finally:
                if lease is not None:
                    try:
                        result["cleanup"] = lease.restore()
                    except BaseException as exc:
                        result["cleanup"] = {"status": "recovery_unknown", "restored": False,
                                             "errors": [{"code": "CLEANUP_CRASH", "message": str(exc)}]}
    except BaseException as exc:
        if contact_request:
            code = safe_contact_span_error(getattr(exc, "code", type(exc).__name__))
            result.pop("result", None)
            result["ok"] = False
            result["error"] = _error(code, code, result["worker_started"])
        elif scan_request:
            code = safe_scan_error(getattr(exc, 'code', None))
            result.pop('result', None)
            result['ok'] = False
            result['error'] = _error(code, code, result['worker_started'])
        elif result["ok"]:
            result["cleanup"]["errors"].append({"code": getattr(exc, "code", "MUTEX_CLEANUP_FAILED"), "message": str(exc)})
        elif "error" not in result:
            result["error"] = _error(getattr(exc, "code", "SUPERVISOR_ERROR"), str(exc), result["worker_started"])
        else:
            result["cleanup"]["errors"].append({"code": getattr(exc, "code", "MUTEX_CLEANUP_FAILED"), "message": str(exc)})
    finally:
        if owned_backend is not None:
            try:
                owned_backend.close()
            except BaseException as exc:
                result["cleanup"]["errors"].append({"code": "HANDLE_CLOSE_FAILED", "message": str(exc)})
    if isinstance(request, dict) and request.get('action') == 'wait_for_ui_hint' and result['worker_started']:
        result['event_evidence'] = normalize_event_evidence(result.get('event_evidence'))[0]
        original = lease.record.get('original') if lease and isinstance(lease.record,dict) else None
        cleanup = result['cleanup']
        gate_verified = (cleanup.get('restored') is True and type(cleanup.get('observed')) is int
                         and cleanup['observed'] == original and type(cleanup.get('errors')) is list
                         and not cleanup['errors'])
        if result['ok'] and (not valid_hint_success(result.get('result'), result['event_evidence'],
                result.get('evidence'), request.get('args',{}).get('timeout_seconds'))
                or not gate_verified):
            result.pop('result', None)
            result.update(ok=False,error=_error('EVENT_OBSERVATION_UNVERIFIED',
                'UI hint, event cleanup, background observation or gate restoration was not proved',True))
        if not result['ok']:
            result.setdefault('error',_error('EVENT_OBSERVATION_UNVERIFIED','UI hint was not verified',True))
            result['error']['submission_started'] = False
            if not event_cleanup_verified(result['event_evidence']) or not gate_verified:
                result['error']['outcome_unknown'] = True
    if isinstance(request, dict) and request.get('action') == 'scroll_messages' and result['worker_started']:
        history, shape_valid = normalize_history_evidence(result.get('history_evidence'))
        result['history_evidence'] = history
        original = lease.record.get('original') if lease and isinstance(lease.record,dict) else None
        cleanup = result['cleanup']
        gate_verified = (cleanup.get('restored') is True and type(cleanup.get('observed')) is int
                         and cleanup['observed'] == original and type(cleanup.get('errors')) is list
                         and not cleanup['errors'])
        if result['ok'] and (not valid_history_success(result.get('result'),history,
                result.get('evidence'),request.get('args',{})) or not gate_verified):
            result.pop('result',None)
            result.update(ok=False,error=_error('HISTORY_OBSERVATION_UNVERIFIED',
                'Destination viewport, conversation, draft, background or gate restoration was not proved',True))
        if not result['ok']:
            result.setdefault('error',_error('HISTORY_OBSERVATION_UNVERIFIED','History navigation was not verified',True))
            result['error']['submission_started'] = False
            result['error']['navigation_started'] = history['delivery_started']
            if (history['delivery_started'] is not False or not shape_valid or not gate_verified
                    or history['primary_error_code'] in ('history_evidence_invalid','worker_history_evidence_missing')):
                result['error']['outcome_unknown'] = True
    if isinstance(request, dict) and request.get('action') == 'read_history_span' and result['worker_started']:
        span_evidence, shape_valid = normalize_history_span_evidence(result.get('history_span_evidence'))
        result['history_span_evidence'] = span_evidence
        original = lease.record.get('original') if lease and isinstance(lease.record, dict) else None
        cleanup = result['cleanup']
        gate_verified = (cleanup.get('restored') is True and type(cleanup.get('observed')) is int
                         and cleanup['observed'] == original and type(cleanup.get('errors')) is list
                         and not cleanup['errors'])
        if result['ok'] and (not valid_history_span_success(result.get('result'), span_evidence,
                result.get('evidence'), request.get('args', {})) or not gate_verified):
            result.pop('result', None)
            result.update(ok=False, error=_error('HISTORY_SPAN_OBSERVATION_UNVERIFIED',
                'History span body, typed evidence, background observation or gate restoration was not proved', True))
        if not result['ok']:
            result.setdefault('error', _error('HISTORY_SPAN_OBSERVATION_UNVERIFIED',
                                               'History span was not verified', True))
            result['error']['submission_started'] = False
            result['error']['navigation_started'] = span_evidence['delivery_started']
            if (span_evidence['delivery_started'] is not False or not shape_valid or not gate_verified
                    or span_evidence['primary_error_code'] in (
                        'history_span_evidence_invalid', 'worker_history_span_evidence_missing')):
                result['error']['outcome_unknown'] = True
    if contact_request:
        shape_valid = True
        contact_evidence = None
        if result['worker_started']:
            contact_evidence, shape_valid = _normalize_contact_span_evidence(
                result.get('contact_span_evidence'))
            result['contact_span_evidence'] = contact_evidence
        cleanup = result.get('cleanup')
        if not isinstance(cleanup, dict):
            cleanup = result['cleanup'] = {
                'status': 'recovery_unknown', 'restored': False,
                'errors': [{'code': 'contact_span_failed'}],
            }
        original_cleanup_errors = cleanup.get('errors')
        lease_side_effect = bool(lease is not None and isinstance(lease.record, dict))
        if lease_side_effect:
            original = lease.record.get('original')
            cleanup_verified = (cleanup.get('restored') is True
                                and type(cleanup.get('observed')) is int
                                and cleanup['observed'] == original
                                and type(original_cleanup_errors) is list
                                and not original_cleanup_errors)
        else:
            cleanup_verified = (cleanup.get('restored') is True
                                and type(original_cleanup_errors) is list
                                and not original_cleanup_errors)
        cleanup['errors'] = _sanitize_contact_error_items(original_cleanup_errors)
        gate_verified = cleanup_verified
        if result['ok'] and (not result['worker_started']
                or not valid_contact_span_success(result.get('result'), contact_evidence,
                    result.get('evidence'), request.get('args', {}))
                or not gate_verified):
            result.pop('result', None)
            result.update(ok=False, error=_error('CONTACT_SPAN_OBSERVATION_UNVERIFIED',
                'Contact span body, typed evidence, background observation or gate restoration was not proved', True))
        if not result['ok']:
            result.pop('result', None)
            result.setdefault('error', _error('CONTACT_SPAN_OBSERVATION_UNVERIFIED',
                                               'Contact span was not verified', True))
            result['error']['submission_started'] = False
            if result['worker_started']:
                result['error']['navigation_started'] = contact_evidence.get('delivery_started')
                if (contact_evidence.get('delivery_started') is not False or not shape_valid
                        or contact_evidence.get('cleanup_error_code') is not None
                        or contact_evidence.get('primary_error_code') in (
                            'contact_span_evidence_invalid', 'worker_contact_span_evidence_missing')):
                    result['error']['outcome_unknown'] = True
            else:
                result['error'].pop('navigation_started', None)
            if not gate_verified:
                result['error']['outcome_unknown'] = True
    if scan_request:
        scan = None
        shape_valid = True
        if result['worker_started']:
            scan, shape_valid = normalize_scan_evidence(result.get('navigation_evidence'))
            result['navigation_evidence'] = scan
        cleanup = result.get('cleanup')
        original = lease.record.get('original') if lease and isinstance(lease.record, dict) else None
        gate_verified = (isinstance(cleanup, dict) and lease is not None
                         and cleanup.get('restored') is True
                         and type(cleanup.get('observed')) is int
                         and cleanup['observed'] == original
                         and type(cleanup.get('errors')) is list
                         and not cleanup['errors'])
        valid_scan_result = (valid_session_viewports_success
                             if request.get('action') == 'scan_session_viewports'
                             else valid_scan_success)
        if result['ok'] and (not result['worker_started']
                or not valid_scan_result(result.get('result'), scan,
                                         result.get('evidence'), request.get('args', {}))
                or not gate_verified):
            result.pop('result', None)
            result.update(ok=False, error=_error('SESSION_SCAN_OBSERVATION_UNVERIFIED',
                'Session scan, background observation or gate restoration was not proved', True))
        if not result['ok']:
            result.pop('result', None)
            result.setdefault('error', _error('SESSION_SCAN_OBSERVATION_UNVERIFIED',
                                               'Session scan was not verified', True))
            result['error']['submission_started'] = False
            if result['worker_started']:
                navigation_started = (scan['delivery_started'] is True
                                      or scan['activation_started'] is True)
                result['error']['navigation_started'] = navigation_started
                if (not shape_valid or scan['delivery_started'] is not False
                        or scan['activation_started'] is not False):
                    result['error']['outcome_unknown'] = True
            if not gate_verified:
                result['error']['outcome_unknown'] = True
    if isinstance(request, dict) and request.get("action") == "list_contacts" and result["worker_started"]:
        navigation = result.get("navigation_evidence")
        if not isinstance(navigation, dict):
            navigation = result["navigation_evidence"] = {
                "attempted": None, "entered": None, "restore_attempted": None, "restored": None,
                "restoration_status": "unknown",
                "primary_error_code": result.get("error", {}).get("code", "WORKER_PROTOCOL_ERROR"),
                "cleanup_error_code": "worker_navigation_evidence_missing",
            }
            if request.get("args", {}).get("scroll_steps", 0):
                navigation['scroll'] = {'requested_steps': request['args']['scroll_steps'],
                                        'restored': None, 'restoration_status': 'unknown'}
        # A restored accessibility byte is independent of UI-tab restoration.
        # Killing a blocked worker prevents its finally block from running.
        contact_result = result.get("result")
        if result["ok"] and (navigation.get("restored") is not True
                or not isinstance(contact_result, dict)
                or contact_result.get("original_conversation_restored") is not True):
            result.pop("result", None)
            result.update(ok=False, error=_error("CONTACT_RESTORATION_UNVERIFIED",
                "Worker did not prove restoration of the original conversation; gate cleanup is separate", True))
        if result["ok"] and request.get("args", {}).get("scroll_steps", 0):
            scroll = navigation.get("scroll")
            if (not isinstance(scroll, dict) or scroll.get("restored") is not True
                    or contact_result.get("original_contacts_view_restored") is not True):
                result.pop("result", None)
                result.update(ok=False, error=_error("CONTACT_SCROLL_RESTORATION_UNVERIFIED",
                    "Worker did not prove restoration of the original Contacts viewport; chat and gate cleanup are separate", True))
        if not result["ok"]:
            attempted = navigation.get("attempted")
            scroll = navigation.get("scroll")
            if (attempted is not False and navigation.get("restored") is not True
                    or attempted is False and navigation.get("entered") is True
                    or request.get("args", {}).get("scroll_steps", 0)
                    and attempted is not False
                    and (not isinstance(scroll, dict) or scroll.get("restored") is not True)
                    or not valid_ui_background(result.get("evidence"))):
                result["error"]["outcome_unknown"] = True
    if (isinstance(request, dict) and type(request.get("action")) is str
            and request["action"] in UI_READ_ACTIONS
            and result["ok"]):
        cleanup = result.get("cleanup")
        original = lease.record.get("original") if lease and isinstance(lease.record, dict) else None
        gate_verified = (lease is not None and isinstance(cleanup, dict)
                         and cleanup.get("restored") is True
                         and type(cleanup.get("observed")) is int
                         and cleanup["observed"] == original
                         and cleanup.get("errors") == [])
        if (not result["worker_started"] or not gate_verified
                or not valid_ui_background(result.get("evidence"))
                or not valid_ui_read_result(
                    request["action"], result.get("result"), request.get("args", {}), target)):
            result.pop("result", None)
            result.update(ok=False, error=_error("UI_READ_OBSERVATION_UNVERIFIED",
                "UI read body, desktop observation or gate restoration was not proved", True))
    if (isinstance(request, dict) and type(request.get('action')) is str
            and request['action'] in MUTATION_ACTIONS
            and result['ok']):
        cleanup = result.get('cleanup')
        original = lease.record.get('original') if lease and isinstance(lease.record, dict) else None
        gate_verified = (lease is not None and isinstance(cleanup, dict)
                         and cleanup.get('restored') is True
                         and type(cleanup.get('observed')) is int
                         and cleanup['observed'] == original
                         and cleanup.get('journal_committed') is True
                         and cleanup.get('errors') == [])
        if (result.get('worker_started') is not True or not gate_verified
                or not _valid_mutation_background(result.get('evidence'), target)):
            result.pop('result', None)
            result.update(ok=False, error=_error('MUTATION_OBSERVATION_UNVERIFIED',
                'Mutation desktop observation or gate restoration was not proved', True))
    if isinstance(request, dict) and request.get('action') == 'open_session':
        args = request.get('args', {})
        navigation = result.get('navigation_evidence')
        navigation_valid = (type(args) is dict
                            and valid_open_session_evidence(
                                navigation, args.get('session_ref')))
        if not navigation_valid:
            result.pop('navigation_evidence', None)
        cleanup = result.get('cleanup')
        original = lease.record.get('original') if lease and isinstance(lease.record, dict) else None
        gate_verified = (lease is not None and isinstance(cleanup, dict)
                         and cleanup.get('restored') is True
                         and type(cleanup.get('observed')) is int
                         and cleanup['observed'] == original
                         and cleanup.get('errors') == [])
        if result['ok'] and (result.get('worker_started') is not True
                or not gate_verified or not valid_ui_background(result.get('evidence'))
                or not valid_open_session_success(result.get('result'), navigation, args)):
            result.pop('result', None)
            result.update(ok=False, error=_error('SESSION_OPEN_OBSERVATION_UNVERIFIED',
                'Session selection, header, desktop or gate restoration was not proved', True))
        if not result['ok']:
            result.pop('result', None)
            result.setdefault('error', _error('SESSION_OPEN_OBSERVATION_UNVERIFIED',
                                              'Session opening was not verified', True))
            navigation_started = navigation['activation_started'] if navigation_valid else None
            result['error']['navigation_started'] = navigation_started
            if (result.get('worker_started') is True
                    and (navigation_started is not False or not gate_verified
                         or not valid_ui_background(result.get('evidence')))):
                result['error']['outcome_unknown'] = True
    cleanup_failed = result["cleanup"].get("restored") is False or bool(result["cleanup"].get("errors"))
    if cleanup_failed and result["ok"]:
        if readstore_request or (isinstance(request, dict) and request.get('action') in ('read_history_span', 'read_contact_span', 'scan_open_session', 'scan_session_viewports')):
            result.pop('result', None)
        else:
            result["worker_result"] = result.pop("result")
        if contact_request:
            result.update(ok=False, error=_error("contact_span_observation_unverified",
                                                 "contact_span_observation_unverified", True))
        else:
            result.update(ok=False, error=_error("CLEANUP_FAILED", "Worker completed but guardian cleanup did not fully succeed", True))
    result["elapsed_seconds"] = round(time.monotonic() - started, 4)
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds size limit")
        request = json.loads(raw)
        timeout = (MAX_SEND_WORKER_SECONDS if isinstance(request, dict)
                   and request.get('action') in SEND_ACTIONS else
                   MAX_SCAN_WORKER_SECONDS if isinstance(request, dict)
                   and request.get('action') in SCAN_ACTIONS else
                   MAX_HINT_WORKER_SECONDS if isinstance(request, dict)
                   and request.get('action') == 'wait_for_ui_hint' else MAX_WORKER_SECONDS)
        response = run_request(request, timeout=timeout)
    except BaseException as exc:
        response = {"ok": False, "error": _error("INVALID_REQUEST", str(exc)),
                    "worker_started": False, "cleanup": {"status": "not_started", "restored": True, "errors": []}}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
