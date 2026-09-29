"""One descendant enumeration per observation, with direct UIA rechecks.

This is an observation over sequential provider reads, NOT an atomic snapshot.
Concurrent user/automation changes remain unsupported. Rechecks reuse known
nodes only until the current boundary; every next phase enumerates fresh nodes.
No selected_target/current_chat/field/draft/one adapter methods are invoked.
"""
from dataclasses import dataclass

from wxbg.policy import AdapterError, exact_one


def runtime_id(node):
    value = node.element_info.runtime_id
    if value is None or value == () or value == [] or value == '':
        raise AdapterError('missing_runtime_id')
    return str(value)


def rectangle(node):
    try:
        rect = node.rectangle()
        return rect.left, rect.top, rect.right, rect.bottom
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError('observation_failed', type(exc).__name__) from exc


@dataclass(frozen=True)
class ObservedMessage:
    node: object
    runtime: str
    kind: str
    name: str


class Observation:
    def __init__(self, adapter, nodes, session_ref, title, expected_session, expected_field):
        self.adapter = adapter
        self.nodes = tuple(nodes)
        self.session_ref = session_ref
        self.title = title
        self.rows = tuple(n for n in self.nodes if n.element_info.class_name == 'mmui::ChatSessionCell')
        self.session = exact_one([n for n in self.rows if adapter.ref(n) == session_ref], 'session_ref')
        aid = self.session.element_info.automation_id
        if aid != 'session_item_' + title:
            raise AdapterError('context_conflict')
        exact_one([n for n in self.rows if n.element_info.automation_id == aid], 'recipient')
        self.session_identity = aid, runtime_id(self.session)
        self.field = exact_one([n for n in self.nodes if n.element_info.automation_id == 'chat_input_field'], 'chat_input')
        self.field_identity = runtime_id(self.field)
        if (expected_session is not None and self.session_identity != expected_session
                or expected_field is not None and self.field_identity != expected_field):
            raise AdapterError('context_conflict')

        messages = []
        for node in self.nodes:
            kind = node.element_info.class_name
            if kind.startswith('mmui::Chat') and kind.endswith('ItemView'):
                # Only attachment labels are needed. Do not copy text messages.
                name = node.element_info.name if kind == 'mmui::ChatBubbleItemView' else ''
                if not isinstance(name, str):
                    raise AdapterError('observation_failed', 'invalid card label')
                messages.append(ObservedMessage(node, runtime_id(node), kind, name))
        self.messages = tuple(messages)
        self.message_ids = frozenset(message.runtime for message in self.messages)
        self.draft = self.recheck()

    def _recipient_and_header(self):
        selected = []
        for row in self.rows:
            value = row.iface_selection_item.CurrentIsSelected
            if type(value) not in (int, bool) or value not in (0, 1):
                raise AdapterError('observation_failed', 'invalid selection value')
            if value:
                selected.append(row)
        if len(selected) != 1 or self.adapter.ref(selected[0]) != self.session_ref:
            raise AdapterError('context_conflict')
        if (self.session.element_info.automation_id, runtime_id(self.session)) != self.session_identity:
            raise AdapterError('context_conflict')
        info = self.field.element_info
        if (info.automation_id != 'chat_input_field' or info.class_name != 'mmui::ChatInputField'
                or info.control_type != 'Edit' or info.name != self.title
                or runtime_id(self.field) != self.field_identity):
            raise AdapterError('context_conflict')

    def recheck(self):
        """Fresh selection/value/property reads, without another tree walk."""
        try:
            self.adapter.precondition()
            self._recipient_and_header()
            value = self.field.iface_value.CurrentValue
            if not isinstance(value, str):
                raise AdapterError('observation_failed', 'invalid draft value')
            # A provider read can re-enter UI work. Recheck recipient/header
            # after reading Value too; this still does not create atomicity.
            self._recipient_and_header()
            return value
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError('observation_failed', type(exc).__name__) from exc

    def one(self, predicate, subject):
        try:
            return exact_one([node for node in self.nodes if predicate(node.element_info)], subject)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError('observation_failed', type(exc).__name__) from exc

    def new_file_cards(self, before, filename):
        matches = []
        seen = set()
        for message in self.messages:
            lines = message.name.splitlines()
            if (message.kind != 'mmui::ChatBubbleItemView' or message.runtime in before
                    or not lines or lines[0] != '檔案' or filename not in lines[1:]):
                continue
            if message.runtime in seen:
                raise AdapterError('ambiguous_attachment_card')
            seen.add(message.runtime)
            matches.append(message)
        return matches


def observe(adapter, session_ref, title, expected_session=None, expected_field=None):
    """Enumerate once; reject provider failures rather than infer selection."""
    try:
        adapter.precondition()
        return Observation(adapter, adapter.nodes(), session_ref, title, expected_session, expected_field)
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError('observation_failed', type(exc).__name__) from exc
