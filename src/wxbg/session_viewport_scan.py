"""Enumerate at most five verified Chats viewports without opening a row.

The current sidebar viewport remains at the final position. Returned titles
are observations, not durable session refs or proof of full-list coverage.
"""

import math
import time

from .policy import AdapterError
from .session_navigation import (
    EXIT_RESERVE_SECONDS,
    MAX_SECONDS,
    MAX_STEPS,
    SETTLE_SECONDS,
    VIEW_RESERVE_SECONDS,
    WHEEL_RESERVE_SECONDS,
    _observe,
)


MAX_ROWS_PER_VIEW = 32


def _viewport(view, index):
    signatures = view['signature']
    table_rect = view['table_rect']
    if len(signatures) > MAX_ROWS_PER_VIEW:
        raise AdapterError('session_view_overflow')
    public_rows = []
    for automation_id, _runtime_id, bounds in signatures:
        title = automation_id[len('session_item_'):]
        if not 1 <= len(title) <= 256 or not title.strip() or '\0' in title:
            raise AdapterError('session_row_unverified')
        public_rows.append({
            'title': title,
            'fully_visible': (bounds[1] >= table_rect[1]
                              and bounds[3] <= table_rect[3]),
        })
    return {'index': index, 'rows': public_rows}


def _result(status, viewports, evidence):
    return {
        'ok': True,
        'status': status,
        'viewports': viewports,
        'counts': {'wheel_steps': evidence['delivered_steps'],
                   'viewports': len(viewports)},
        'coverage': 'bounded_ui_viewports',
        'end_verified': False,
        'background_mode': 'minimized',
    }


def scan_session_viewports(adapter, max_steps=4, *, direction='down',
                           deadline=None, wheel=None, sleep=time.sleep,
                           clock=time.monotonic):
    """Collect origin plus 0..4 successive, target-local wheel destinations."""
    if type(max_steps) is not int or not 0 <= max_steps <= MAX_STEPS:
        raise AdapterError('invalid_scroll_steps')
    if type(direction) is not str or direction not in ('down', 'up'):
        raise AdapterError('invalid_scan_direction')
    if wheel is None:
        from .session_wheel import send_session_wheel
        wheel = send_session_wheel

    evidence = {
        'direction': direction,
        'requested_steps': max_steps,
        'attempted_steps': 0,
        'delivered_steps': 0,
        'delivery_started': False,
        'activation_started': False,
        'target_found': False,
        'selected_and_header_verified': False,
        'reached_end': False,
        'primary_error_code': None,
    }
    adapter.navigation_evidence = evidence
    try:
        if deadline is None:
            deadline = clock() + MAX_SECONDS
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise AdapterError('invalid_deadline')
        operation_bound = deadline - EXIT_RESERVE_SECONDS

        def budget(required=0.0):
            now = clock()
            if not math.isfinite(now) or now + required >= operation_bound:
                raise AdapterError('session_scan_budget_exhausted')

        budget(VIEW_RESERVE_SECONDS)
        adapter.precondition()
        original_chat = adapter.current_chat()
        if original_chat is not None and (type(original_chat) is not str or not original_chat):
            raise AdapterError('context_conflict')
        budget(VIEW_RESERVE_SECONDS)
        view = _observe(adapter, original_chat)
        viewports = []

        def check_context():
            budget(VIEW_RESERVE_SECONDS)
            adapter.precondition()
            if original_chat is None:
                current = _observe(adapter, None)
                if (current['root_id'] != view['root_id']
                        or current['root_rect'] != view['root_rect']
                        or current['table_id'] != view['table_id']
                        or current['table_rect'] != view['table_rect']):
                    raise AdapterError('context_conflict')
            else:
                info = view['field'].element_info
                if (info.automation_id != 'chat_input_field'
                        or info.class_name != 'mmui::ChatInputField'
                        or info.name != original_chat
                        or view['field'].iface_value.CurrentValue != ''
                        or adapter.current_chat() != original_chat):
                    raise AdapterError('context_conflict')
            budget(VIEW_RESERVE_SECONDS)

        for step in range(max_steps + 1):
            budget()
            viewports.append(_viewport(view, step))
            check_context()
            if step == max_steps:
                return _result('bounded_complete', viewports, evidence)

            budget(WHEEL_RESERVE_SECONDS + VIEW_RESERVE_SECONDS)
            previous_signature = view['signature']
            evidence['attempted_steps'] += 1
            evidence['delivery_started'] = True
            wheel(adapter, view['table'], -120 if direction == 'down' else 120,
                  check_context=check_context)
            evidence['delivered_steps'] += 1
            budget(VIEW_RESERVE_SECONDS)
            sleep(SETTLE_SECONDS)
            view = _observe(adapter, original_chat)
            if view['signature'] == previous_signature:
                budget()
                viewports.append(_viewport(view, step + 1))
                check_context()
                return _result('viewport_unchanged', viewports, evidence)
        raise AdapterError('session_scan_failed')
    except Exception as exc:
        evidence['primary_error_code'] = (exc.code if isinstance(exc, AdapterError)
                                          else 'session_scan_failed')
        raise
