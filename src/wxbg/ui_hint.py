"""One metadata-only, deadline-bounded observation. No navigation or message read."""
import math
import time

from .policy import AdapterError
from .uia_events import EventWindow

CLEANUP_RESERVE = 6.0
POLL_SECONDS = .05
EVENT_EVIDENCE_KEYS = (
    'started', 'armed', 'auto_focus_disabled', 'owner_mta', 'owner_windowless',
    'identity_verified', 'registrations_removed', 'handler_refs_released',
    'owner_exited', 'cleanup_pending', 'startup_dropped', 'invalid_dropped',
    'primary_error_code', 'cleanup_error_code',
)
_OWNER_FLAGS = ('auto_focus_disabled', 'owner_mta', 'owner_windowless', 'identity_verified')
_CLEANUP_FLAGS = ('registrations_removed', 'handler_refs_released', 'owner_exited')
_OPEN_CODES = {'bootstrap_failed': 'event_bootstrap_failed', 'open_failed': 'event_open_failed',
               'subscribe_failed': 'event_subscribe_failed'}


class _HintFailure(AdapterError):
    """Only internal, fixed error codes may reach the public evidence."""


def _initial_evidence():
    return {'started': False, 'armed': False, **dict.fromkeys(_OWNER_FLAGS),
            'registrations_removed': True, 'handler_refs_released': True,
            'owner_exited': True, 'cleanup_pending': False, 'startup_dropped': 0,
            'invalid_dropped': 0, 'primary_error_code': None, 'cleanup_error_code': None}


def _merge_evidence(evidence, snapshot):
    if not isinstance(snapshot, dict): raise ValueError('invalid_snapshot')
    diagnostics = snapshot.get('owner_diagnostics', {})
    if not isinstance(diagnostics, dict): raise ValueError('invalid_diagnostics')
    for key in _OWNER_FLAGS:
        value = diagnostics.get(key)
        evidence[key] = value if type(value) is bool else None
    for key in _CLEANUP_FLAGS + ('cleanup_pending',):
        value = snapshot.get(key)
        evidence[key] = value if type(value) is bool else None
    if type(snapshot.get('armed')) is not bool: raise ValueError('invalid_armed')
    evidence['armed'] = snapshot['armed']
    for key in ('startup_dropped', 'invalid_dropped'):
        value = snapshot.get(key)
        evidence[key] = value if type(value) is int and value >= 0 else None


def _counter(value):
    if type(value) is not int or value < 0: raise ValueError('invalid_counter')
    return value


def _aggregate(snapshot, timeout_seconds):
    counts = snapshot['armed_counts']
    note = _counter(counts['notification']); text = _counter(counts['text'])
    buffered = len(snapshot['events']); overflow = _counter(snapshot['overflow_dropped'])
    late = _counter(snapshot['late_dropped'])
    if buffered > 128 or note + text != buffered + overflow: raise ValueError('invalid_counts')
    start = snapshot['armed_ns']; end = snapshot['closing_ns']
    if type(start) is not int or type(end) is not int or end < start: raise ValueError('invalid_window')
    elapsed_ms = min(timeout_seconds * 1000, (end - start) // 1_000_000)
    if not note + text and end < snapshot['window_end_ns']: raise ValueError('short_quiet_window')
    return {'status': 'hint_observed' if note + text else 'timed_out', 'scope': 'main_window_subtree',
            'observed_kinds': [name for name, number in (('notification', note), ('text_changed', text)) if number],
            'counts': {'notification': note, 'text_changed': text, 'buffered': buffered,
                       'overflow_dropped': overflow, 'after_close_dropped': late},
            'requested_wait_ms': timeout_seconds * 1000, 'armed_window_ms': elapsed_ms,
            'coverage': {'continuous': False, 'complete': False, 'recipient_known': False, 'missed_count': None}}


def wait_for_ui_hint(target, timeout_seconds, deadline, *, listener_factory=EventWindow,
                     clock=time.monotonic, sleep=time.sleep, check_background=None):
    """Return result + lifecycle evidence, or AdapterError with event_evidence.

    deadline is the guardian's absolute time.monotonic() deadline. The native
    background checker is a required zero-argument callback; it must only
    verify identity/background preconditions, never operate or read chat UI.
    """
    evidence = _initial_evidence(); listener = None; final = None; primary = None; cleanup_error = None

    def fail(code):
        raise _HintFailure(code)

    def check():
        try: check_background()
        except Exception: fail('background_precondition_failed')

    try:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            fail('invalid_timeout')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            fail('invalid_deadline')
        if not callable(check_background): fail('background_checker_required')
        if deadline - clock() < timeout_seconds + CLEANUP_RESERVE: fail('observation_budget_insufficient')
        check()
        listener = listener_factory(target, max_events=128)
        evidence['started'] = True
        for key in _CLEANUP_FLAGS + ('cleanup_pending',): evidence[key] = None
        listener.start()
        ready_budget = max(0.0, min(30.0, deadline - clock() - CLEANUP_RESERVE))
        if not listener.wait_ready(ready_budget):
            snapshot = listener.snapshot()
            fail(_OPEN_CODES.get(snapshot.get('error_code'), 'event_ready_timeout'))
        snapshot = listener.snapshot(); _merge_evidence(evidence, snapshot)
        if not all(evidence[key] is True for key in _OWNER_FLAGS): fail('event_owner_unverified')
        check()
        if deadline - clock() < timeout_seconds + CLEANUP_RESERVE: fail('observation_budget_insufficient')
        snapshot = listener.arm(timeout_seconds); _merge_evidence(evidence, snapshot)
        if evidence['armed'] is not True: fail('event_listener_failed')
        end = snapshot['window_end_ns'] / 1_000_000_000
        while True:
            check()
            snapshot = listener.snapshot()
            if snapshot.get('error_code'): fail('event_listener_failed')
            counts = snapshot['armed_counts']
            if _counter(counts['notification']) + _counter(counts['text']): break
            now = clock()
            if now >= end: break
            if deadline - now <= CLEANUP_RESERVE: fail('observation_budget_exhausted')
            sleep(min(POLL_SECONDS, end - now))
    except _HintFailure as exc:
        primary = exc.code
    except Exception:
        primary = 'event_listener_failed'
    finally:
        if listener is not None:
            try:
                # Each stop both closes publication and asks the same owner to
                # remove/release. Never wait beyond this call's remaining budget.
                stop_budget = max(0.0, min(CLEANUP_RESERVE, deadline - clock()))
                final = listener.stop(stop_budget)
                _merge_evidence(evidence, final)
                if not all(evidence[key] is True for key in _CLEANUP_FLAGS) or evidence['cleanup_pending'] is not False:
                    cleanup_error = 'event_cleanup_pending'
            except Exception:
                cleanup_error = 'event_cleanup_failed'
                for key in _CLEANUP_FLAGS + ('cleanup_pending',): evidence[key] = None
            try: check()
            except _HintFailure:
                if primary is None: primary = 'background_precondition_failed'
        evidence['primary_error_code'] = primary; evidence['cleanup_error_code'] = cleanup_error

    if primary is None and cleanup_error is None:
        try: result = _aggregate(final, timeout_seconds)
        except Exception:
            primary = evidence['primary_error_code'] = 'event_result_invalid'
    if primary is not None or cleanup_error is not None:
        error = AdapterError(cleanup_error or primary)
        error.event_evidence = evidence
        error.outcome_unknown = bool(cleanup_error)
        raise error
    return {'result': result, 'event_evidence': evidence}
