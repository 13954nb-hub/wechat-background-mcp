"""Bounded Contacts viewport reads with separately proven scroll restoration.

Snapshots and names live only in this operation's memory. No stable directory
identity, complete pagination or end-of-directory inference is made.
"""
import time

from . import contact_actions as ca
from .policy import AdapterError

STEP_SECONDS = .08
SETTLE_SECONDS = .2
KINDS = {'mmui::ContactsCellMangerBtnView', 'mmui::ContactsCellGroupView',
         'mmui::ContactsCellClassifyView', 'mmui::ContactsCellItemView'}


def _wheel(adapter, table, delta):
    from .contact_wheel import send_contact_wheel
    send_contact_wheel(adapter, table, delta)


def _snapshot(guard, table_id, nodes=None, *, expected_geometry=None):
    nodes = tuple(nodes) if nodes is not None else guard.nodes()
    table = ca._contacts_table(nodes)
    if table is None or ca.runtime_id(table) != table_id:
        raise AdapterError('contacts_page_changed')
    root_rect, table_rect = ca._table_geometry(guard, nodes, table)
    if expected_geometry is not None and (root_rect, table_rect) != expected_geometry:
        raise AdapterError('unverified_layout')
    signatures, identities, contacts, seen = [], [], [], set()
    item_height = None
    for row in nodes:
        guard.check()
        info = row.element_info
        kind = info.class_name
        if not kind.startswith('mmui::ContactsCell'):
            continue
        if kind not in KINDS or not isinstance(info.name, str):
            raise AdapterError('contact_changed')
        name, control = info.name, info.control_type
        rid, bounds = ca.runtime_id(row), ca.rectangle(row)
        if rid in seen:
            raise AdapterError('ambiguous_contact_identity')
        seen.add(rid)
        ca._belongs(guard, row, table_id)
        # Virtualized rows can straddle an edge; only the measured table width
        # and positive overlap authorize reading them. No row is activated.
        height = bounds[3] - bounds[1]
        if (bounds[0] != table_rect[0] or bounds[2] != table_rect[2]
                or not 0 < height <= min(512, table_rect[3] - table_rect[1])
                or max(bounds[1], table_rect[1]) >= min(bounds[3], table_rect[3])
                or bounds[1] < table_rect[1] - height
                or bounds[3] > table_rect[3] + height):
            raise AdapterError('contact_outside_bounds')
        if kind == 'mmui::ContactsCellItemView':
            if control != 'ListItem' or item_height not in (None, height):
                raise AdapterError('contact_outside_bounds')
            item_height = height
            ref = guard.adapter.ref(row, 'contacts')
            if not isinstance(ref, str) or not ref:
                raise AdapterError('ambiguous_contact_identity')
            contacts.append({'contact_ref': ref, 'display_text': name})
        info = row.element_info
        if (info.class_name, info.control_type, info.name, ca.runtime_id(row), ca.rectangle(row)) != (
                kind, control, name, rid, bounds):
            raise AdapterError('contact_changed')
        signatures.append((kind, control, name, bounds))
        identities.append(rid)
    guard.background()
    signature = tuple(signatures)
    top = sum(s[0] == 'mmui::ContactsCellMangerBtnView'
              and s[3][0] == table_rect[0] and s[3][1] == table_rect[1]
              and s[3][2] == table_rect[2] for s in signature) == 1
    return {'signature': signature, 'identities': tuple(identities), 'contacts': contacts,
            'top': top, 'table': table, 'geometry': (root_rect, table_rect)}


def _settled(guard, table_id, initial=None, *, expected_geometry=None):
    prior = initial
    for _ in range(3):
        if prior is not None:
            time.sleep(SETTLE_SECONDS)
        current = _snapshot(guard, table_id, expected_geometry=expected_geometry)
        if prior is not None and (current['signature'], current['identities'], current['geometry']) == (prior['signature'], prior['identities'], prior['geometry']):
            return current
        prior = current
    raise AdapterError('contacts_view_not_settled')


def _require_geometry(guard, table_id, expected_geometry):
    nodes = guard.nodes()
    table = ca._contacts_table(nodes)
    if table is None or ca.runtime_id(table) != table_id:
        raise AdapterError('contacts_page_changed')
    if ca._table_geometry(guard, nodes, table) != expected_geometry:
        raise AdapterError('unverified_layout')


def read_scrolled(guard, cleanup_guard, nodes, table, steps, evidence):
    """Read after N downward steps from observed top, then restore that view."""
    table_id = ca.runtime_id(table)
    baseline = _settled(guard, table_id, _snapshot(guard, table_id, nodes))
    if not baseline['top']:
        raise AdapterError('contacts_top_required')
    evidence['origin'] = 'observed_top'
    primary = cleanup_error = result = None
    try:
        for _ in range(steps):
            guard.background()
            _snapshot(guard, table_id, expected_geometry=baseline['geometry'])
            evidence['attempted_down_steps'] += 1
            # Count before delivery: timeout may still have delivered this
            # message. Cleanup is inverse navigation, never a down retry.
            _wheel(guard.adapter, baseline['table'], -120)
            time.sleep(STEP_SECONDS)
        time.sleep(SETTLE_SECONDS)
        result = _settled(guard, table_id, expected_geometry=baseline['geometry'])
        evidence['viewport_changed'] = result['signature'] != baseline['signature']
    except Exception as exc:
        primary = exc
        evidence['primary_error_code'] = ca._code(exc)
    finally:
        if evidence['attempted_down_steps']:
            try:
                for _ in range(evidence['attempted_down_steps']):
                    cleanup_guard.background()
                    _require_geometry(cleanup_guard, table_id, baseline['geometry'])
                    evidence['attempted_restore_steps'] += 1
                    _wheel(cleanup_guard.adapter, baseline['table'], 120)
                    time.sleep(STEP_SECONDS)
                time.sleep(SETTLE_SECONDS)
                restored = _settled(cleanup_guard, table_id, expected_geometry=baseline['geometry'])
                if not restored['top'] or restored['signature'] != baseline['signature']:
                    raise AdapterError('contacts_view_not_restored')
                evidence['restored'] = True
            except Exception as exc:
                cleanup_error = exc
                evidence['cleanup_error_code'] = ca._code(exc)
    if cleanup_error is not None:
        raise AdapterError('contacts_scroll_restore_failed') from (primary or cleanup_error)
    if primary is not None:
        raise AdapterError(evidence['primary_error_code']) from primary
    if result is None or not evidence['restored']:
        raise AdapterError('contacts_scroll_restore_failed')
    return result['contacts']
