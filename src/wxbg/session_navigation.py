"""Bounded Chats-list navigation to one exact, UIA-observed session.

One request may move the virtualized session list at most four wheel notches.
It never uses database keys, the search popover, focus, keyboard or clipboard.
"""
import math
import time

from .observed_adapter import rectangle, runtime_id
from .policy import AdapterError, exact_one, validate_ui_action


MAX_STEPS = 4
SETTLE_SECONDS = .16
MAX_SECONDS = 22.0
EXIT_RESERVE_SECONDS = 3.0
VIEW_RESERVE_SECONDS = 5.0
WHEEL_RESERVE_SECONDS = 1.2
OPEN_RESERVE_SECONDS = 8.0


def _inside(row, table):
    return (row[0] == table[0] and row[2] == table[2]
            and max(row[1], table[1]) < min(row[3], table[3]))


def _adjacent_offscreen(row, table):
    """Qt may retain one whole row immediately outside the viewport."""
    if (row[0] != table[0] or row[2] != table[2]
            or not 0 < row[3] - row[1] <= table[3] - table[1]):
        return False
    return (0 <= table[1] - row[3] <= 2
            or 0 <= row[1] - table[3] <= 2)


def _observe(adapter, original_chat):
    adapter.precondition()
    root = adapter.root()
    root_rect = rectangle(root)
    if (root.element_info.class_name != 'mmui::MainWindow'
            or root_rect[0] >= root_rect[2] or root_rect[1] >= root_rect[3]):
        raise AdapterError('unverified_layout')
    root_id = runtime_id(root)
    nodes = tuple(adapter.nodes())
    tables = [node for node in nodes
              if (node.element_info.class_name == 'mmui::XTableView'
                  and node.element_info.control_type == 'List'
                  and node.parent() is not None
                  and node.parent().element_info.class_name == 'mmui::ChatSessionList'
                  and node.parent().element_info.control_type == 'Group')]
    table = exact_one(tables, 'session_table')
    parent = table.parent()
    table_rect = rectangle(table)
    if (parent is None or parent.element_info.class_name != 'mmui::ChatSessionList'
            or parent.element_info.control_type != 'Group'
            or rectangle(parent) != table_rect
            or table_rect[0] < root_rect[0] or table_rect[1] < root_rect[1]
            or table_rect[2] > root_rect[2] or table_rect[3] > root_rect[3]
            or table_rect[2] - table_rect[0] < 160
            or table_rect[3] - table_rect[1] < 160):
        raise AdapterError('session_table_unverified')
    table_id = runtime_id(table)
    fields = [node for node in nodes
              if node.element_info.automation_id == 'chat_input_field']
    if original_chat is None:
        # The logged-in Chats list can exist before any conversation is open.
        # Do not mistake a missing field in a selected or unreadable view for
        # that state: every exposed row must explicitly report unselected.
        if fields:
            raise AdapterError('context_conflict')
        field = None
    else:
        field = exact_one(fields, 'chat_input')
        if (field.element_info.class_name != 'mmui::ChatInputField'
                or field.element_info.control_type != 'Edit'
                or field.element_info.name != original_chat
                or field.iface_value.CurrentValue != ''):
            raise AdapterError('context_conflict')
    rows = []
    all_rows = []
    signatures = []
    for node in nodes:
        info = node.element_info
        if info.class_name != 'mmui::ChatSessionCell':
            continue
        if (info.control_type != 'ListItem' or type(info.automation_id) is not str
                or not info.automation_id.startswith('session_item_')
                or not info.automation_id[len('session_item_'):]):
            raise AdapterError('session_row_unverified')
        all_rows.append(node)
        if original_chat is None:
            try:
                selected = node.iface_selection_item.CurrentIsSelected
            except Exception:
                raise AdapterError('context_conflict') from None
            if type(selected) not in (bool, int) or selected not in (False, 0):
                raise AdapterError('context_conflict')
        owner = node.parent()
        if (owner is None or owner.element_info.class_name != 'mmui::XTableView'
                or runtime_id(owner) != table_id):
            raise AdapterError('session_row_outside_table')
        bounds = rectangle(node)
        if not _inside(bounds, table_rect):
            if _adjacent_offscreen(bounds, table_rect):
                continue
            raise AdapterError('session_row_outside_table')
        rows.append(node)
        signatures.append((info.automation_id, runtime_id(node), bounds))
    if not rows:
        raise AdapterError('session_view_empty')
    adapter.precondition()
    if original_chat is None:
        fresh_nodes = tuple(adapter.nodes())
        fresh_root = adapter.root()
        if (fresh_root.element_info.class_name != 'mmui::MainWindow'
                or runtime_id(fresh_root) != root_id
                or rectangle(fresh_root) != root_rect):
            raise AdapterError('unverified_layout')
        fresh_tables = [node for node in fresh_nodes
                        if (node.element_info.class_name == 'mmui::XTableView'
                            and node.element_info.control_type == 'List'
                            and node.parent() is not None
                            and node.parent().element_info.class_name == 'mmui::ChatSessionList'
                            and node.parent().element_info.control_type == 'Group')]
        fresh_table = exact_one(fresh_tables, 'session_table')
        if (runtime_id(fresh_table) != table_id
                or rectangle(fresh_table) != table_rect
                or rectangle(fresh_table.parent()) != table_rect):
            raise AdapterError('context_conflict')
        if any(node.element_info.automation_id == 'chat_input_field'
               for node in fresh_nodes):
            raise AdapterError('context_conflict')
        fresh_rows = [node for node in fresh_nodes
                      if node.element_info.class_name == 'mmui::ChatSessionCell']
        if ([runtime_id(node) for node in fresh_rows]
                != [runtime_id(node) for node in all_rows]):
            raise AdapterError('context_conflict')
        for node in fresh_rows:
            try:
                selected = node.iface_selection_item.CurrentIsSelected
            except Exception:
                raise AdapterError('context_conflict') from None
            if type(selected) not in (bool, int) or selected not in (False, 0):
                raise AdapterError('context_conflict')
    elif (field.element_info.name != original_chat
          or field.iface_value.CurrentValue != ''):
        raise AdapterError('context_conflict')
    return {'table': table, 'field': field, 'rows': tuple(rows),
            'all_rows': tuple(all_rows),
            'signature': tuple(signatures), 'table_id': table_id,
            'table_rect': table_rect, 'root_rect': root_rect, 'root_id': root_id}


def _result(status, title, evidence):
    opened = status == 'opened'
    return {'ok': True, 'status': status, 'title': title if opened else None,
            'verification_level': ('selected_row_and_header' if opened
                                   else 'bounded_viewport_observation'),
            'counts': {'wheel_steps': evidence['delivered_steps'],
                       'viewports': evidence['delivered_steps'] + 1},
            'background_mode': 'minimized'}


def scan_open_session(adapter, title, max_steps=4, *, direction='down',
                      deadline=None, wheel=None, sleep=time.sleep,
                      clock=time.monotonic):
    """Scan at most four notches in one direction; open one exact UIA hit."""
    if (type(title) is not str or not 1 <= len(title) <= 256
            or not title.strip() or '\0' in title):
        raise AdapterError('invalid_title')
    if type(max_steps) is not int or not 0 <= max_steps <= MAX_STEPS:
        raise AdapterError('invalid_scroll_steps')
    if type(direction) is not str or direction not in ('down', 'up'):
        raise AdapterError('invalid_scan_direction')
    validate_ui_action([title], 'ListItem')
    if wheel is None:
        from .session_wheel import send_session_wheel
        wheel = send_session_wheel
    evidence = {'direction': direction,
                'requested_steps': max_steps, 'attempted_steps': 0,
                'delivered_steps': 0, 'delivery_started': False,
                'activation_started': False, 'target_found': False,
                'selected_and_header_verified': False, 'reached_end': False,
                'primary_error_code': None}
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
        budget()

        def check_context():
            budget(VIEW_RESERVE_SECONDS)
            adapter.precondition()
            root = adapter.root()
            if (root.element_info.class_name != 'mmui::MainWindow'
                    or runtime_id(root) != view['root_id']
                    or rectangle(root) != view['root_rect']
                    or rectangle(view['table']) != view['table_rect']):
                raise AdapterError('context_conflict')
            if original_chat is None:
                current = _observe(adapter, None)
                if (current['table_id'] != view['table_id']
                        or current['table_rect'] != view['table_rect']):
                    raise AdapterError('context_conflict')
            else:
                info = view['field'].element_info
                if (info.automation_id != 'chat_input_field'
                        or info.class_name != 'mmui::ChatInputField'
                        or info.name != original_chat
                        or view['field'].iface_value.CurrentValue != ''):
                    raise AdapterError('context_conflict')
            budget(VIEW_RESERVE_SECONDS)

        for step in range(max_steps + 1):
            matches = [row for row in view['rows']
                       if row.element_info.automation_id == 'session_item_' + title]
            all_matches = [row for row in view['all_rows']
                           if row.element_info.automation_id == 'session_item_' + title]
            if len(all_matches) > 1:
                raise AdapterError('ambiguous_recipient')
            evidence['target_found'] = bool(matches)
            if matches:
                target = matches[0]
                bounds = rectangle(target)
                table_rect = view['table_rect']
                if bounds[1] < table_rect[1] or bounds[3] > table_rect[3]:
                    can_advance = ((direction == 'down'
                                    and bounds[1] >= table_rect[1]
                                    and bounds[3] > table_rect[3])
                                   or (direction == 'up'
                                       and bounds[1] < table_rect[1]
                                       and bounds[3] <= table_rect[3]))
                    if not can_advance or step == max_steps:
                        return _result('target_partially_visible', title, evidence)
                    # A clipped row becomes actionable only after a fresh
                    # observation verifies it moved fully inside the table.
                else:
                    budget(OPEN_RESERVE_SECONDS)
                    ref = adapter.ref(target)
                    if not isinstance(ref, str) or not ref:
                        raise AdapterError('invalid_session_ref')
                    # A selected row and matching header need no additional click.
                    if not (adapter.selected_target(ref) and adapter.current_chat() == title):
                        evidence['activation_started'] = True
                    try:
                        opened = adapter.open_session(ref)
                    finally:
                        # The nested open records its own evidence. Keep this
                        # request's scan evidence on both success and failure.
                        adapter.navigation_evidence = evidence
                    if (opened.get('status') != 'opened'
                            or opened.get('title') != title
                            or not adapter.selected_target(ref)
                            or adapter.current_chat() != title):
                        raise AdapterError('session_not_opened')
                    budget()
                    evidence['selected_and_header_verified'] = True
                    return _result('opened', title, evidence)
            if step == max_steps:
                budget()
                return _result('not_found_in_bounded_scan', title, evidence)
            check_context()
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
            budget()
            if view['signature'] == previous_signature:
                return _result(('target_partially_visible' if evidence['target_found']
                                else 'viewport_unchanged'), title, evidence)
        raise AdapterError('session_scan_failed')
    except Exception as exc:
        evidence['primary_error_code'] = (exc.code if isinstance(exc, AdapterError)
                                          else 'session_scan_failed')
        raise
