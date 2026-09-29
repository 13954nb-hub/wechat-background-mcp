"""Bounded intentional navigation of the already selected conversation.

The requested effect is to leave the destination viewport in place. There is
no inverse input, recipient selection, message submission or draft write here.
All observations are sequential; a deadline cannot preempt a blocked provider.
"""
import math
import time

from .history_view import observe_history
from .history_wheel import send_history_wheel
from .policy import AdapterError


EXIT_RESERVE_SECONDS = 2.0
PAIR_RESERVE_SECONDS = 2.0
WHEEL_RESERVE_SECONDS = 1.12  # Native timeout plus the explicit settle pause.
from .history_contract import SAFE_HISTORY_ERRORS as _SAFE_ERRORS


def scroll_messages(adapter, session_ref, direction, steps, deadline, *,
                    observe=observe_history, wheel=send_history_wheel,
                    clock=time.monotonic, sleep=time.sleep):
    """Leave one verified destination viewport after one to four wheel notches.

    Evidence contains only fixed codes, booleans and counts. A successful input
    with an unchanged viewport is valid, but establishes no history boundary.
    The caller owns the guardian, operation journal and background observation.
    """
    started = getattr(adapter, 'navigation_started', False) is True
    evidence = {
        'mode': 'intentional_navigation',
        'direction': direction if type(direction) is str and direction in ('older', 'newer') else None,
        'requested_steps': steps if type(steps) is int and 1 <= steps <= 4 else None,
        'delivery_started': started, 'completed_steps': 0,
        'viewport_settled': False, 'conversation_preserved': False,
        'draft_preserved': False, 'viewport_changed': None,
        'primary_error_code': None, 'original_viewport_restoration_requested': False,
        'not_full_history': True, 'boundary_verified': False,
    }
    adapter.history_evidence = evidence
    adapter.navigation_started = started

    try:
        if started:
            raise AdapterError('history_navigation_already_started')
        if type(steps) is not int or not 1 <= steps <= 4:
            raise AdapterError('invalid_scroll_steps')
        if type(direction) is not str or direction not in ('older', 'newer'):
            raise AdapterError('invalid_scroll_direction')
        if type(session_ref) is not str or not session_ref:
            raise AdapterError('invalid_session_ref')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise AdapterError('invalid_deadline')
        operation_bound = deadline - EXIT_RESERVE_SECONDS

        def budget(required=0.0):
            now = clock()
            if not math.isfinite(now) or now + required >= operation_bound:
                raise AdapterError('history_budget_exhausted')

        def context_guard(view, required=0.0):
            budget(required)
            view.recheck()
            if view.session_ref != session_ref:
                raise AdapterError('context_conflict')
            # A provider read can block; check again before returning control
            # to the native helper, which validates the live frame and PID.
            budget(required)

        def stable(context=None, reserve_after=0.0):
            budget(PAIR_RESERVE_SECONDS + reserve_after)
            first = observe(adapter, expected_context=context)
            if first.session_ref != session_ref:
                raise AdapterError('context_conflict')
            context_guard(first, reserve_after)
            sleep(.08)
            budget(reserve_after)
            second = observe(adapter, expected_context=first.context)
            if second.session_ref != session_ref or second.context != first.context:
                raise AdapterError('context_conflict')
            if (first.signature != second.signature
                    or first.frame_identity != second.frame_identity
                    or first.row_identity != second.row_identity):
                raise AdapterError('history_view_not_settled')
            context_guard(second, reserve_after)
            return second

        # Minimum admission windows avoid beginning input with no room for a
        # final pair. These are soft reserves, not a bound on arbitrary COM.
        remaining = PAIR_RESERVE_SECONDS + steps * WHEEL_RESERVE_SECONDS
        original = stable(reserve_after=remaining)
        delta = 120 if direction == 'older' else -120
        for index in range(steps):
            required = PAIR_RESERVE_SECONDS + (steps - index) * WHEEL_RESERVE_SECONDS

            def guarded_context():
                context_guard(original, required)

            guarded_context()
            try:
                wheel(adapter, original.list_node, delta, check_context=guarded_context)
            except Exception as exc:
                if getattr(exc, 'wheel_delivery_started', False) is True:
                    adapter.navigation_started = evidence['delivery_started'] = True
                raise
            adapter.navigation_started = evidence['delivery_started'] = True
            evidence['completed_steps'] += 1
            budget(PAIR_RESERVE_SECONDS)
            sleep(.12)
            budget(PAIR_RESERVE_SECONDS)

        final = stable(original.context)
        changed = final.signature != original.signature
        evidence.update(viewport_settled=True, conversation_preserved=True,
                        draft_preserved=True, viewport_changed=changed)
        return {
            'ok': True, 'status': 'viewport_observed',
            'verification_level': 'settled_view_after_bounded_scroll',
            'background_mode': 'minimized',
            'counts': {'requested_steps': steps, 'completed_steps': steps,
                       'before_rows': len(original.rows), 'after_rows': len(final.rows),
                       'viewport_changed': int(changed)},
            'refs': {'conversation': session_ref},
        }
    except Exception as exc:
        code = exc.code if isinstance(exc, AdapterError) and exc.code in _SAFE_ERRORS else 'history_navigation_failed'
        evidence.update(primary_error_code=code, viewport_settled=False,
                        conversation_preserved=False, draft_preserved=False,
                        viewport_changed=None)
        failure = AdapterError(code)
        failure.outcome_unknown = adapter.navigation_started is True
        raise failure from None
