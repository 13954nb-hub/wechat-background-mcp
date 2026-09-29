"""Bounded sidebar navigation to read only currently exposed contact labels.

No contact row is activated, no draft is written, and no search occurs.
Optional bounded scrolling requires a verified top origin and its restoration.
Sequential UIA observations are not an atomic snapshot. The caller must keep
the existing background monitor and guardian active throughout this operation.
Soft deadlines reserve restoration time but cannot interrupt blocked COM calls;
a hard-killed worker cannot prove that its navigation finally block completed.
"""
import time

from wxbg.observed_adapter import rectangle, runtime_id
from wxbg.policy import AdapterError, exact_one

# Layouts are measured from current UIA controls. Window bounds vary with
# monitor DPI, window size and Qt's minimized-window representation.
POLL_ATTEMPTS = 6
POLL_SECONDS = .1
READ_SECONDS = 15.0
SCROLL_CLEANUP_SECONDS = 20.0
TOTAL_SECONDS = 24.0
MAX_PARENTS = 40


def _code(exc):
    code = exc.code if isinstance(exc, AdapterError) else 'observation_failed'
    if not isinstance(code, str) or not 1 <= len(code) <= 64 or not all(c in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in code):
        return 'observation_failed'
    return code


class _Guard:
    def __init__(self, adapter, deadline, identity):
        self.adapter, self.deadline, self.identity = adapter, deadline, identity

    def check(self):
        if time.monotonic() >= self.deadline:
            raise AdapterError('contacts_deadline')
        if self.adapter.identity != self.identity:
            raise AdapterError('context_conflict')

    def background(self):
        self.check()
        self.adapter.precondition()
        self.check()

    def nodes(self):
        self.background()
        nodes = tuple(self.adapter.nodes())
        if len(nodes) > 2500:
            raise AdapterError('tree_budget_exceeded')
        self.background()
        return nodes


def _root(guard):
    guard.background()
    root_rect = rectangle(guard.adapter.root())
    if (len(root_rect) != 4 or any(type(value) is not int for value in root_rect)
            or not 0 < root_rect[2] - root_rect[0] <= 32767
            or not 0 < root_rect[3] - root_rect[1] <= 32767):
        raise AdapterError('unverified_layout')
    guard.check()
    return root_rect


def _tab_rect(guard, node):
    root = _root(guard)
    bounds = rectangle(node)
    if (not _inside(bounds, root) or bounds[0] != root[0]
            or bounds[2] >= root[2] or bounds[2] - bounds[0] >= (root[2] - root[0]) // 2):
        raise AdapterError('unverified_layout')
    return bounds


def _tab(guard, nodes, name, expected=None):
    node = exact_one([n for n in nodes if n.element_info.class_name == 'mmui::XTabBarItem'
                      and n.element_info.control_type == 'Button' and n.element_info.name == name], 'sidebar_tab')
    _check_tab(guard, node, name, expected)
    return node


def _check_tab(guard, node, name, expected):
    info = node.element_info
    identity = runtime_id(node)
    if (info.class_name, info.control_type, info.name) != ('mmui::XTabBarItem', 'Button', name):
        raise AdapterError('sidebar_tab_changed')
    bounds = _tab_rect(guard, node)
    if expected is not None and bounds != expected:
        raise AdapterError('unverified_layout')
    info = node.element_info
    if (info.class_name, info.control_type, info.name, runtime_id(node)) != ('mmui::XTabBarItem', 'Button', name, identity):
        raise AdapterError('sidebar_tab_changed')
    guard.check()


class _Chat:
    """Keep only the selected session and input identities for direct rechecks."""
    def __init__(self, guard, nodes, expected=None):
        self.guard = guard
        if any(n.element_info.class_name == 'mmui::ContactsTableBaseView' for n in nodes):
            raise AdapterError('context_conflict')
        self.rows = tuple(n for n in nodes if n.element_info.class_name == 'mmui::ChatSessionCell')
        selected = self._selected()
        if len(selected) != 1:
            raise AdapterError('context_conflict')
        self.session = selected[0]
        info = self.session.element_info
        aid = info.automation_id
        if not isinstance(aid, str) or not aid.startswith('session_item_') or not aid[len('session_item_'):]:
            raise AdapterError('context_conflict')
        self.title = aid[len('session_item_'):]
        if len([n for n in self.rows if n.element_info.automation_id == aid]) != 1:
            raise AdapterError('ambiguous_recipient')
        self.session_ref = guard.adapter.ref(self.session)
        self.session_identity = aid, runtime_id(self.session)
        if not isinstance(self.session_ref, str) or not self.session_ref:
            raise AdapterError('context_conflict')
        if expected and (self.session_ref, self.title, self.session_identity) != expected:
            raise AdapterError('context_conflict')
        self.field = exact_one([n for n in nodes if n.element_info.automation_id == 'chat_input_field'], 'chat_input')
        self.field_identity = runtime_id(self.field)
        self.recheck()

    @property
    def expected(self):
        return self.session_ref, self.title, self.session_identity

    def _selected(self):
        selected = []
        for row in self.rows:
            value = row.iface_selection_item.CurrentIsSelected
            if type(value) not in (bool, int) or value not in (0, 1):
                raise AdapterError('observation_failed')
            if value:
                selected.append(row)
        return selected

    def _recipient(self):
        selected = self._selected()
        if len(selected) != 1 or self.guard.adapter.ref(selected[0]) != self.session_ref:
            raise AdapterError('context_conflict')
        if (self.session.element_info.automation_id, runtime_id(self.session)) != self.session_identity:
            raise AdapterError('context_conflict')
        info = self.field.element_info
        if (info.automation_id, info.class_name, info.control_type, info.name, runtime_id(self.field)) != (
                'chat_input_field', 'mmui::ChatInputField', 'Edit', self.title, self.field_identity):
            raise AdapterError('context_conflict')

    def recheck(self):
        self.guard.background()
        self._recipient()
        draft = self.field.iface_value.CurrentValue
        self._recipient()
        if not isinstance(draft, str):
            raise AdapterError('observation_failed')
        if draft != '':
            raise AdapterError('draft_conflict')
        self.guard.check()


def _contacts_table(nodes):
    tables = [n for n in nodes if n.element_info.class_name == 'mmui::ContactsTableBaseView']
    fields = [n for n in nodes if n.element_info.automation_id == 'chat_input_field']
    if len(tables) > 1:
        raise AdapterError('ambiguous_contacts_table')
    if tables and fields:
        raise AdapterError('contacts_page_changed')
    if not tables:
        return None
    if tables[0].element_info.control_type != 'Group':
        raise AdapterError('contacts_page_changed')
    return tables[0]


def _inside(rect, parent):
    left, top, right, bottom = rect
    return parent[0] <= left < right <= parent[2] and parent[1] <= top < bottom <= parent[3]


def _table_geometry(guard, nodes, table):
    root_rect = _root(guard)
    if (table.element_info.class_name != 'mmui::ContactsTableBaseView'
            or table.element_info.control_type != 'Group'):
        raise AdapterError('contacts_page_changed')
    tabs = {}
    for name in ('微信', '通訊錄'):
        node = exact_one([n for n in nodes if n.element_info.class_name == 'mmui::XTabBarItem'
                          and n.element_info.control_type == 'Button' and n.element_info.name == name],
                         'sidebar_tab')
        tabs[name] = _tab_rect(guard, node)
    wechat, contacts = tabs['微信'], tabs['通訊錄']
    if (wechat[0] != contacts[0] or wechat[2] != contacts[2]
            or wechat[3] > contacts[1]):
        raise AdapterError('unverified_layout')
    table_rect = rectangle(table)
    if (not _inside(table_rect, root_rect) or table_rect[0] != wechat[2]
            or table_rect[2] <= table_rect[0]):
        raise AdapterError('contact_outside_bounds')
    if _root(guard) != root_rect or rectangle(table) != table_rect:
        raise AdapterError('unverified_layout')
    return root_rect, table_rect


def _belongs(guard, row, table_id):
    parent = row.parent()
    visited = set()
    for _ in range(MAX_PARENTS):
        guard.check()
        if parent is None:
            break
        rid = runtime_id(parent)
        kind = parent.element_info.class_name
        if kind == 'mmui::ContactsTableBaseView' and rid == table_id:
            return
        key = kind, rid
        if key in visited:
            break
        visited.add(key)
        parent = parent.parent()
    raise AdapterError('contact_outside_table')


def _read_rows(guard, nodes, table):
    table_id = runtime_id(table)
    root_rect, table_rect = _table_geometry(guard, nodes, table)
    result, seen = [], set()
    for row in nodes:
        guard.check()
        info = row.element_info
        if info.class_name != 'mmui::ContactsCellItemView':
            continue
        if info.control_type != 'ListItem' or not isinstance(info.name, str):
            raise AdapterError('contact_changed')
        rid, name = runtime_id(row), info.name
        ref = guard.adapter.ref(row, 'contacts')
        if rid in seen or not isinstance(ref, str) or not ref:
            raise AdapterError('ambiguous_contact_identity')
        seen.add(rid)
        _belongs(guard, row, table_id)
        bounds = rectangle(row)
        # Qt can retain one virtualized cell past the visible table. It is
        # read-only and must keep the measured table width and positive overlap.
        height = bounds[3] - bounds[1]
        if (bounds[0] != table_rect[0] or bounds[2] != table_rect[2]
                or not 0 < height <= min(512, table_rect[3] - table_rect[1])
                or max(bounds[1], table_rect[1]) >= min(bounds[3], table_rect[3])
                or bounds[1] < table_rect[1] - height
                or bounds[3] > table_rect[3] + height
                or bounds[3] > root_rect[3] + height // 3):
            raise AdapterError('contact_outside_bounds')
        info = row.element_info
        if (info.class_name, info.control_type, info.name, runtime_id(row), guard.adapter.ref(row, 'contacts')) != (
                'mmui::ContactsCellItemView', 'ListItem', name, rid, ref):
            raise AdapterError('contact_changed')
        result.append({'contact_ref': ref, 'display_text': name})
    guard.check()
    final_nodes = guard.nodes()
    final_table = _contacts_table(final_nodes)
    if final_table is None or runtime_id(final_table) != table_id:
        raise AdapterError('contacts_page_changed')
    if _table_geometry(guard, final_nodes, final_table) != (root_rect, table_rect):
        raise AdapterError('contacts_page_changed')
    final_ids = [runtime_id(n) for n in final_nodes if n.element_info.class_name == 'mmui::ContactsCellItemView']
    if len(final_ids) != len(seen) or set(final_ids) != seen:
        raise AdapterError('contacts_page_changed')
    guard.check()
    return result


def _prove_chat(guard, nodes, expected):
    chat = _Chat(guard, nodes, expected)
    _root(guard)
    # Geometry/provider work precedes the final direct selection/header/Value
    # reads. A tab switch can legitimately recreate the input runtime identity.
    chat.recheck()


def _restore(guard, original, known_tab, known_tab_rect, evidence):
    guard.check()
    _root(guard)
    nodes = None
    try:
        nodes = guard.nodes()
        has_field = any(n.element_info.automation_id == 'chat_input_field' for n in nodes)
    except AdapterError:
        raise
    except Exception:
        # Reading a contact provider can fail while the retained sidebar node
        # remains usable. Revalidate that exact node and its geometry below;
        # this is a single cleanup tab activation, not a row/search fallback.
        has_field = False
        nodes = None
    if has_field:
        _prove_chat(guard, nodes, original)
        evidence['restored'] = True
        return
    tab = _tab(guard, nodes, '微信', known_tab_rect) if nodes is not None else known_tab
    _check_tab(guard, tab, '微信', known_tab_rect)
    evidence['restore_attempted'] = True
    guard.adapter.click(tab)
    for attempt in range(POLL_ATTEMPTS):
        nodes = guard.nodes()
        if any(n.element_info.automation_id == 'chat_input_field' for n in nodes):
            _prove_chat(guard, nodes, original)
            evidence['restored'] = True
            return
        if attempt + 1 < POLL_ATTEMPTS:
            time.sleep(POLL_SECONDS)
    raise AdapterError('original_chat_not_restored')


def list_contacts(adapter, query='', limit=100, scroll_steps=0):
    """Visit Contacts once, read exposed rows, and prove original chat restored.

    On failure, adapter.navigation_evidence retains bounded primary/cleanup
    codes and attempt/restoration flags, without contact labels or draft text.
    Any failed cleanup fails the whole request. The caller must forward this
    evidence even when the function raises AdapterError.
    """
    evidence = {'attempted': False, 'entered': False, 'restore_attempted': False,
                'restored': False, 'primary_error_code': None, 'cleanup_error_code': None}
    adapter.navigation_evidence = evidence
    if not isinstance(query, str) or len(query) > 256 or '\0' in query:
        evidence['primary_error_code'] = 'invalid_query'
        raise AdapterError('invalid_query')
    if type(limit) is not int or not 1 <= limit <= 100:
        evidence['primary_error_code'] = 'invalid_limit'
        raise AdapterError('invalid_limit')
    if type(scroll_steps) is not int or not 0 <= scroll_steps <= 24:
        evidence['primary_error_code'] = 'invalid_scroll_steps'
        raise AdapterError('invalid_scroll_steps')
    evidence['scroll'] = {'requested_steps': scroll_steps, 'attempted_down_steps': 0,
                          'attempted_restore_steps': 0, 'restored': scroll_steps == 0,
                          'origin': 'current_view', 'viewport_changed': False,
                          'primary_error_code': None, 'cleanup_error_code': None}
    start = time.monotonic()
    identity = adapter.identity
    read = _Guard(adapter, start + READ_SECONDS, identity)
    cleanup = _Guard(adapter, start + TOTAL_SECONDS, identity)
    scroll_cleanup = _Guard(adapter, start + SCROLL_CLEANUP_SECONDS, identity)
    primary = cleanup_error = result = None
    original = wechat = wechat_rect = None
    try:
        nodes = read.nodes()
        entry_root = _root(read)
        chat = _Chat(read, nodes)
        original = chat.expected
        wechat = _tab(read, nodes, '微信')
        wechat_rect = _tab_rect(read, wechat)
        contacts = _tab(read, nodes, '通訊錄')
        contacts_rect = _tab_rect(read, contacts)
        if (contacts_rect[0] != wechat_rect[0]
                or contacts_rect[2] != wechat_rect[2]
                or wechat_rect[3] > contacts_rect[1]
                or _root(read) != entry_root):
            raise AdapterError('unverified_layout')
        chat.recheck()
        _check_tab(read, contacts, '通訊錄', contacts_rect)
        if _root(read) != entry_root:
            raise AdapterError('unverified_layout')
        evidence['attempted'] = True
        adapter.click(contacts)
        for attempt in range(POLL_ATTEMPTS):
            nodes = read.nodes()
            table = _contacts_table(nodes)
            if table is not None:
                evidence['entered'] = True
                break
            if attempt + 1 < POLL_ATTEMPTS:
                time.sleep(POLL_SECONDS)
        else:
            raise AdapterError('contacts_not_opened')
        if scroll_steps:
            from .contact_scroll import read_scrolled
            rows = read_scrolled(read, scroll_cleanup, nodes, table, scroll_steps, evidence['scroll'])
        else:
            rows = _read_rows(read, nodes, table)
        matched = [row for row in rows if query.casefold() in row['display_text'].casefold()][:limit]
        result = {'contacts': matched, 'query': query, 'limit': limit, 'count': len(matched),
                  'exposed_count': len(rows), 'visible_only': True, 'not_full_directory': True,
                  'pagination_supported': False, 'contact_ref_scope': 'temporary_ui_contacts',
                  'query_scope': 'exposed_contacts_only', 'background_mode': 'minimized'}
        result.update(scroll_steps=scroll_steps, bounded_scroll_supported=True,
                      scroll_steps_limit=24, original_contacts_view_restored=evidence['scroll']['restored'])
        if scroll_steps:
            result['query_scope'] = 'requested_contact_view_only'
            result['scroll_origin'] = 'observed_top'
            result['viewport_changed'] = evidence['scroll']['viewport_changed']
    except Exception as exc:
        primary = exc
        evidence['primary_error_code'] = _code(exc)
    finally:
        if evidence['attempted']:
            try:
                _restore(cleanup, original, wechat, wechat_rect, evidence)
            except Exception as exc:
                cleanup_error = exc
                evidence['cleanup_error_code'] = _code(exc)
    if cleanup_error is not None:
        raise AdapterError('navigation_restore_failed',
                           f"primary={evidence['primary_error_code'] or 'none'}; cleanup={evidence['cleanup_error_code']}") from (primary or cleanup_error)
    if primary is not None:
        raise AdapterError(evidence['primary_error_code']) from primary
    if not evidence['restored'] or result is None:
        raise AdapterError('navigation_restore_failed')
    result['original_conversation_restored'] = True
    return result
