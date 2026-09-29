"""Bounded intentional history reads that retain the final destination view.

This work-only module composes the formal history observer and wheel primitive
through dependency-injection seams. It never sends messages, writes drafts,
opens conversations, inverses a wheel, or exposes UIA runtime IDs as message
identifiers.
"""
import math
import time

from wxbg.history_view import observe_history
from wxbg.history_wheel import send_history_wheel
from wxbg.history_span_contract import SAFE_HISTORY_SPAN_ERRORS
from wxbg.policy import AdapterError


EXIT_RESERVE_SECONDS = 2.0
PAIR_RESERVE_SECONDS = 2.0
WHEEL_RESERVE_SECONDS = 1.12
SETTLE_SECONDS = 0.08
CONVERGENCE_OBSERVE_RESERVE_SECONDS = 0.25
MAX_CONVERGENCE_OBSERVATIONS = 4
MAX_STEPS = 4
MAX_LIMIT = 200

_SAFE_ERRORS = SAFE_HISTORY_SPAN_ERRORS


def _safe_code(error):
    code = getattr(error, 'code', None)
    return code if type(code) is str and code in _SAFE_ERRORS else 'history_span_failed'


def _valid_int(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _row_payload(view, offset, limit, changed):
    rows = view.rows[-limit:]
    return {
        'offset': offset,
        'root_bounds': list(view.root_rect),
        'viewport_bounds': list(view.viewport_rect),
        'rows': [
            {'kind': row.kind, 'text': row.text, 'bounds': list(row.rect)}
            for row in rows
        ],
        'exposed_count': len(view.rows),
        'returned_count': len(rows),
        'viewport_changed': changed,
    }


def collect_history_span(adapter, session_ref, direction, steps, limit_per_view,
                         deadline, *, observe=observe_history,
                         wheel=send_history_wheel, clock=time.monotonic,
                         sleep=time.sleep):
    """Read the origin and each bounded destination viewport.

    ``steps`` means exact single wheel deliveries after the origin, not pages.
    The operation intentionally leaves the final destination in place. A wheel
    that may have been delivered makes every later failure outcome-unknown; no
    retry or inverse wheel is attempted.
    """
    prior_started = getattr(adapter, 'history_span_started', False) is True
    evidence = {
        'mode': 'intentional_navigation_span',
        'direction': direction if isinstance(direction, str) and direction in ('older', 'newer') else None,
        'requested_steps': steps if _valid_int(steps, 1, MAX_STEPS) else None,
        'delivery_started': prior_started,
        'completed_steps': 0,
        'observed_view_count': 0,
        'viewport_settled': None,
        'conversation_preserved': None,
        'draft_preserved': None,
        'final_view_retained': None,
        'not_full_history': True,
        'boundary_verified': False,
        'chronological_order_verified': False,
        'primary_error_code': None,
    }
    adapter.history_span_evidence = evidence
    adapter.history_span_started = prior_started
    started = prior_started

    try:
        if not _valid_int(steps, 1, MAX_STEPS):
            raise AdapterError('invalid_history_span_steps')
        if type(direction) is not str or direction not in ('older', 'newer'):
            raise AdapterError('invalid_history_span_direction')
        if type(session_ref) is not str or not session_ref:
            raise AdapterError('invalid_session_ref')
        if not _valid_int(limit_per_view, 1, MAX_LIMIT):
            raise AdapterError('invalid_history_span_limit')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise AdapterError('invalid_deadline')
        if prior_started:
            raise AdapterError('history_span_failed')

        operation_bound = deadline - EXIT_RESERVE_SECONDS

        def budget(required=0.0):
            now = clock()
            if not isinstance(now, (int, float)) or not math.isfinite(now):
                raise AdapterError('history_span_budget_exhausted')
            if now + required >= operation_bound:
                raise AdapterError('history_span_budget_exhausted')

        def context_guard(view, required=0.0):
            budget(required)
            view.recheck()
            if view.session_ref != session_ref:
                raise AdapterError('context_conflict')
            budget(required)

        def stable(expected_context=None, reserve_after=0.0):
            poll_cost = SETTLE_SECONDS + CONVERGENCE_OBSERVE_RESERVE_SECONDS
            remaining_polls = MAX_CONVERGENCE_OBSERVATIONS - 1
            budget(PAIR_RESERVE_SECONDS + reserve_after + remaining_polls * poll_cost)
            first = None
            previous = None
            saw_geometry_error = False
            for attempt in range(MAX_CONVERGENCE_OBSERVATIONS):
                if attempt:
                    polls_left = MAX_CONVERGENCE_OBSERVATIONS - attempt
                    budget(reserve_after + polls_left * poll_cost)
                    sleep(SETTLE_SECONDS)
                    budget(reserve_after + (polls_left - 1) * poll_cost
                           + CONVERGENCE_OBSERVE_RESERVE_SECONDS)
                try:
                    current = observe(adapter, expected_context=(
                        first.context if first is not None else expected_context))
                except AdapterError as error:
                    if error.code != 'history_row_geometry_invalid':
                        raise
                    # A transient Qt row layout is only an unsettled sample.
                    # It cannot count toward the required consecutive pair.
                    saw_geometry_error = True
                    previous = None
                    continue
                if current.session_ref != session_ref:
                    raise AdapterError('context_conflict')
                if first is None:
                    first = current
                elif current.context != first.context:
                    raise AdapterError('context_conflict')
                elif current.frame_identity != first.frame_identity:
                    raise AdapterError('history_frame_changed')
                elif (current.root_rect, current.viewport_rect) != (
                        first.root_rect, first.viewport_rect):
                    raise AdapterError('history_frame_changed')
                context_guard(current, reserve_after)
                if (previous is not None
                        and previous.signature == current.signature
                        and previous.frame_identity == current.frame_identity
                        and previous.row_identity == current.row_identity):
                    return current
                # A same-context signature/row-identity change is still in
                # motion. Keep observing within this fixed budget; never send
                # another wheel and never relax the identity checks.
                previous = current
            if saw_geometry_error and (first is None or previous is None):
                raise AdapterError('history_row_geometry_invalid')
            raise AdapterError('history_span_view_not_settled')

        # Reserve the initial pair, every wheel, and the final destination pair.
        remaining = PAIR_RESERVE_SECONDS + steps * WHEEL_RESERVE_SECONDS
        origin = stable(reserve_after=remaining)
        evidence['observed_view_count'] = 1
        views = [_row_payload(origin, 0, limit_per_view, False)]
        previous = origin
        delta = 120 if direction == 'older' else -120

        for index in range(steps):
            required = PAIR_RESERVE_SECONDS + (steps - index) * WHEEL_RESERVE_SECONDS
            context_guard(previous, required)
            try:
                wheel(adapter, previous.list_node, delta,
                      check_context=lambda: context_guard(previous, required))
            except Exception as error:
                if getattr(error, 'wheel_delivery_started', False) is True:
                    started = True
                    adapter.history_span_started = True
                    evidence['delivery_started'] = True
                raise
            started = True
            adapter.history_span_started = True
            evidence['delivery_started'] = True
            evidence['completed_steps'] = index + 1
            budget(PAIR_RESERVE_SECONDS)
            sleep(.12)
            budget(PAIR_RESERVE_SECONDS)
            current = stable(origin.context)
            evidence['observed_view_count'] = index + 2
            if (current.root_rect, current.viewport_rect) != (
                    origin.root_rect, origin.viewport_rect):
                raise AdapterError('history_frame_changed')
            changed = current.signature != previous.signature
            views.append(_row_payload(current, index + 1, limit_per_view, changed))
            previous = current

        evidence.update(
            viewport_settled=True,
            conversation_preserved=True,
            draft_preserved=True,
            final_view_retained=True,
            primary_error_code=None,
        )
        return {
            'ok': True,
            'status': 'history_span_observed',
            'verification_level': 'settled_visible_span_with_final_destination_retained',
            'background_mode': 'minimized',
            'direction': direction,
            'views': views,
            'counts': {
                'requested_steps': steps,
                'completed_steps': steps,
                'view_count': len(views),
                'changed_view_count': sum(view['viewport_changed'] for view in views),
            },
            'refs': {'conversation': session_ref},
            'not_full_history': True,
            'boundary_verified': False,
            'chronological_order_verified': False,
            'final_view_retained': True,
        }
    except Exception as error:
        code = _safe_code(error)
        started = started or getattr(error, 'wheel_delivery_started', False) is True
        started = started or getattr(adapter, 'history_span_started', False) is True
        evidence.update(
            delivery_started=started,
            viewport_settled=None,
            conversation_preserved=None,
            draft_preserved=None,
            final_view_retained=None,
            primary_error_code=code,
        )
        failure = AdapterError(code)
        failure.outcome_unknown = started
        raise failure from None
