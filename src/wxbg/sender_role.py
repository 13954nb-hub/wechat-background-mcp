"""Fail-closed sender-role fields shared by UI and read-store projections."""

from math import isfinite


_ROLE_EVIDENCE = {
    'self': frozenset({
        'database_account_match',
        'live_ui_sender_side',
        'local_outgoing_operation',
    }),
    'other': frozenset({
        'database_direct_contact_match',
        'database_group_member_match',
        'live_ui_sender_side',
    }),
    'unknown': frozenset({'ambiguous', 'unavailable'}),
}
SENDER_ROLE_KEYS = frozenset({
    'sender_role', 'sender_role_verified', 'sender_role_evidence',
})


def sender_role_fields(role: str, evidence: str) -> dict[str, object]:
    """Build the only supported role/evidence/verification combinations."""
    if (type(role) is not str or type(evidence) is not str
            or role not in _ROLE_EVIDENCE
            or evidence not in _ROLE_EVIDENCE[role]):
        raise ValueError('sender_role_contract_invalid')
    return {
        'sender_role': role,
        'sender_role_verified': role != 'unknown',
        'sender_role_evidence': evidence,
    }


def validate_sender_role_fields(value: object) -> dict[str, object]:
    """Validate and return a detached copy of a complete role triple."""
    if type(value) is not dict or set(value) != SENDER_ROLE_KEYS:
        raise ValueError('sender_role_contract_invalid')
    expected = sender_role_fields(value['sender_role'], value['sender_role_evidence'])
    if type(value['sender_role_verified']) is not bool:
        raise ValueError('sender_role_contract_invalid')
    if value['sender_role_verified'] is not expected['sender_role_verified']:
        raise ValueError('sender_role_contract_invalid')
    return expected


def unavailable_sender_role() -> dict[str, object]:
    return sender_role_fields('unknown', 'unavailable')


def classify_ui_center(
    center_x: int | float,
    viewport_left: int,
    viewport_width: int,
) -> dict[str, object]:
    """Classify a uniquely observed text element within the live message frame."""
    if (type(center_x) not in (int, float) or type(viewport_left) is not int
            or type(viewport_width) is not int or viewport_width <= 0):
        return unavailable_sender_role()
    try:
        if type(center_x) is float and not isfinite(center_x):
            return unavailable_sender_role()
        offset = center_x - viewport_left
        if offset < 0 or offset > viewport_width:
            return unavailable_sender_role()
        if type(offset) is int:
            if offset * 5 <= viewport_width * 2:
                return sender_role_fields('other', 'live_ui_sender_side')
            if offset * 5 >= viewport_width * 3:
                return sender_role_fields('self', 'live_ui_sender_side')
        else:
            ratio = offset / viewport_width
            if ratio <= 0.40:
                return sender_role_fields('other', 'live_ui_sender_side')
            if ratio >= 0.60:
                return sender_role_fields('self', 'live_ui_sender_side')
    except (OverflowError, ValueError):
        return unavailable_sender_role()
    return sender_role_fields('unknown', 'ambiguous')
