"""Sequential history observation. No input or persistence APIs.

One descendant enumeration per observation, then bounded direct-property
rechecks. Signatures prove only an observed viewport equality, never absolute
message identity, complete history, stable pagination or atomic UI context.
"""
from dataclasses import dataclass

from wxbg.policy import AdapterError, validate_ui_action

ROW_KINDS = frozenset(('mmui::ChatTextItemView', 'mmui::ChatItemView',
                       'mmui::ChatBubbleItemView', 'mmui::ChatBubbleReferItemView'))
MAX_PARENTS = 40
MAX_NODES = 2500


def _runtime(node):
    value = node.element_info.runtime_id
    if value is None or value == '' or value == () or value == []:
        raise AdapterError('history_identity_missing')
    return str(value)


def _rect(node):
    value = node.rectangle()
    result = (value.left, value.top, value.right, value.bottom)
    if any(type(number) is not int for number in result):
        raise AdapterError('history_geometry_invalid')
    return result


def _inside(inner, outer):
    return (outer[0] <= inner[0] < inner[2] <= outer[2]
            and outer[1] <= inner[1] < inner[3] <= outer[3])


def _one(nodes, code):
    if len(nodes) != 1: raise AdapterError(code)
    return nodes[0]


def _is_direct(node, parent, kind, control):
    current = node.parent()
    if current is None: return False
    info = current.element_info
    return (info.class_name == kind and info.control_type == control
            and _runtime(current) == _runtime(parent))


@dataclass(frozen=True)
class HistoryRow:
    node: object
    kind: str
    runtime: str
    rect: tuple
    text: str


class HistoryView:
    def __init__(self, adapter, nodes, expected_context):
        self._adapter = adapter
        self._identity = adapter.identity
        self._root = adapter.root()
        self._root_runtime = _runtime(self._root)
        self._root_rect = _rect(self._root)
        if (self._root.handle != adapter.hwnd
                or self._root.element_info.class_name != 'mmui::MainWindow'
                or self._root.element_info.control_type != 'Window'
                or not 0 < self._root_rect[2] - self._root_rect[0] <= 32767
                or not 0 < self._root_rect[3] - self._root_rect[1] <= 32767):
            raise AdapterError('unverified_layout')
        self._sessions = tuple(n for n in nodes if n.element_info.class_name == 'mmui::ChatSessionCell')
        self._session = _one(self._selected(), 'context_conflict')
        aid = self._session.element_info.automation_id
        if type(aid) is not str or not aid.startswith('session_item_') or not aid[len('session_item_'):]:
            raise AdapterError('context_conflict')
        _one([node for node in self._sessions if node.element_info.automation_id == aid], 'ambiguous_recipient')
        self._field = _one([node for node in nodes if node.element_info.automation_id == 'chat_input_field'], 'context_conflict')
        self.title = aid[len('session_item_'):]
        validate_ui_action([self.title], 'ListItem')
        self.context = (aid, _runtime(self._session), _runtime(self._field))
        if expected_context is not None and self.context != expected_context:
            raise AdapterError('context_conflict')
        self.session_ref = adapter.ref(self._session)
        if type(self.session_ref) is not str or not self.session_ref:
            raise AdapterError('context_conflict')
        self.view_node = _one([node for node in nodes if node.element_info.class_name == 'mmui::MessageView'], 'history_frame_ambiguous')
        self.list_node = _one([node for node in nodes if node.element_info.class_name == 'mmui::RecyclerListView'
            and _is_direct(node, self.view_node, 'mmui::MessageView', 'Group')], 'history_frame_ambiguous')
        self._view_rect = _rect(self.view_node)
        if not _inside(self._view_rect, self._root_rect):
            raise AdapterError('history_frame_changed')
        self.root_rect = self._root_rect
        self.viewport_rect = self._view_rect
        self._view_runtime, self._list_runtime = _runtime(self.view_node), _runtime(self.list_node)
        self.recheck()

        rows, seen = [], set()
        for node in nodes:
            info = node.element_info
            kind = info.class_name
            if not (isinstance(kind, str) and kind.startswith('mmui::Chat') and kind.endswith('ItemView')):
                continue
            # Capture before walking parent; COM parent reads can reenter UI.
            text, control, runtime = info.name, info.control_type, _runtime(node)
            if not _is_direct(node, self.list_node, 'mmui::RecyclerListView', 'List'):
                continue
            if kind not in ROW_KINDS or control != 'ListItem' or type(text) is not str:
                raise AdapterError('history_row_unsupported')
            if runtime in seen: raise AdapterError('history_row_identity_invalid')
            seen.add(runtime)
            bounds = _rect(node)
            if (bounds[0] != self._view_rect[0] or bounds[2] != self._view_rect[2]
                    or bounds[3] <= bounds[1]
                    or max(bounds[1], self._view_rect[1]) >= min(bounds[3], self._view_rect[3])):
                raise AdapterError('history_row_geometry_invalid')
            row = HistoryRow(node, kind, runtime, bounds, text)
            self._row_unchanged(row)
            rows.append(row)
        if not rows: raise AdapterError('history_rows_missing')
        rows.sort(key=lambda row: row.rect[1])
        if any(a.rect[3] > b.rect[1] for a, b in zip(rows, rows[1:])):
            raise AdapterError('history_rows_overlap')
        self.rows = tuple(rows)
        self.frame_identity = (self._view_runtime, self._list_runtime)
        self.row_identity = tuple(row.runtime for row in rows)
        self.signature = tuple((row.kind, row.text, row.rect) for row in rows)
        # Detect changes to earlier rows caused by reading a later provider.
        for row in self.rows: self._row_unchanged(row)
        self.recheck()

    def _selected(self):
        selected = []
        for node in self._sessions:
            value = node.iface_selection_item.CurrentIsSelected
            if type(value) not in (bool, int) or value not in (0, 1):
                raise AdapterError('history_observation_failed')
            if value: selected.append(node)
        return selected

    def _context_unchanged(self):
        selected = _one(self._selected(), 'context_conflict')
        aid, session_runtime, field_runtime = self.context
        session_info = self._session.element_info
        if (session_info.class_name != 'mmui::ChatSessionCell' or session_info.control_type != 'ListItem'
                or selected.element_info.class_name != 'mmui::ChatSessionCell'
                or selected.element_info.control_type != 'ListItem'
                or selected.element_info.automation_id != aid or _runtime(selected) != session_runtime
                or session_info.automation_id != aid or _runtime(self._session) != session_runtime
                or len([node for node in self._sessions if node.element_info.automation_id == aid]) != 1):
            raise AdapterError('context_conflict')
        info = self._field.element_info
        if ((info.automation_id, info.class_name, info.control_type, info.name, _runtime(self._field))
                != ('chat_input_field', 'mmui::ChatInputField', 'Edit', self.title, field_runtime)):
            raise AdapterError('context_conflict')

    def _ancestry(self):
        current = self.view_node
        visited = set()
        for _ in range(MAX_PARENTS + 1):
            if current is None: break
            runtime = _runtime(current)
            if runtime in visited: break
            visited.add(runtime)
            handle = int(current.handle or 0)
            if handle:
                if handle == self._adapter.hwnd and runtime == self._root_runtime:
                    return
                break
            current = current.parent()
        raise AdapterError('history_ancestry_invalid')

    def _frame_unchanged(self):
        root = self._adapter.root()
        if (root.handle != self._adapter.hwnd or _runtime(root) != self._root_runtime
                or root.element_info.class_name != 'mmui::MainWindow'
                or root.element_info.control_type != 'Window' or _rect(root) != self._root_rect):
            raise AdapterError('unverified_layout')
        for node, runtime, kind, control in ((self.view_node, self._view_runtime, 'mmui::MessageView', 'Group'),
                                             (self.list_node, self._list_runtime, 'mmui::RecyclerListView', 'List')):
            info = node.element_info
            if ((info.class_name, info.control_type, _runtime(node), _rect(node))
                    != (kind, control, runtime, self._view_rect)):
                raise AdapterError('history_frame_changed')
        if not _is_direct(self.list_node, self.view_node, 'mmui::MessageView', 'Group'):
            raise AdapterError('history_frame_changed')
        self._ancestry()

    def _row_unchanged(self, row):
        info = row.node.element_info
        if ((info.class_name, info.control_type, info.name, _runtime(row.node), _rect(row.node))
                != (row.kind, 'ListItem', row.text, row.runtime, row.rect)
                or not _is_direct(row.node, self.list_node, 'mmui::RecyclerListView', 'List')):
            raise AdapterError('history_row_changed')

    def recheck(self):
        """Direct context/frame/draft checks; no walk and no baseline-row equality.

        Rows may legitimately move during a caller's scroll. The caller must
        take fresh observations to compare destination/restoration signatures.
        A retained wrapper check is not proof that a provider never changed.
        """
        try:
            adapter = self._adapter
            adapter.precondition()
            if adapter.identity != self._identity: raise AdapterError('context_conflict')
            self._frame_unchanged()
            self._context_unchanged()
            draft = self._field.iface_value.CurrentValue
            self._context_unchanged()
            if type(draft) is not str: raise AdapterError('history_observation_failed')
            if draft != '': raise AdapterError('draft_conflict')
            adapter.precondition()
            if adapter.identity != self._identity: raise AdapterError('context_conflict')
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError('history_observation_failed') from exc


def observe_history(adapter, *, expected_context=None):
    """Observe one current viewport without persistence or any UI input."""
    try:
        adapter.precondition()
        nodes = tuple(adapter.nodes())
        if len(nodes) > MAX_NODES: raise AdapterError('tree_budget_exceeded')
        adapter.precondition()
        return HistoryView(adapter, nodes, expected_context)
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError('history_observation_failed') from exc
