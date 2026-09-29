"""Pure, bounded protocol checks for passive UI hints; no UI imports."""
import re

TRUE_FLAGS = ('started', 'armed', 'auto_focus_disabled', 'owner_mta',
              'owner_windowless', 'identity_verified', 'registrations_removed',
              'handler_refs_released', 'owner_exited')
BOOLEAN_FIELDS = TRUE_FLAGS + ('cleanup_pending',)
COUNT_FIELDS = ('startup_dropped', 'invalid_dropped')
ERROR_FIELDS = ('primary_error_code', 'cleanup_error_code')
EVENT_EVIDENCE_KEYS = frozenset(BOOLEAN_FIELDS + COUNT_FIELDS + ERROR_FIELDS)
RESULT_KEYS = frozenset(('status', 'scope', 'observed_kinds', 'counts',
                         'requested_wait_ms', 'armed_window_ms', 'coverage'))
RESULT_COUNT_KEYS = frozenset(('notification', 'text_changed', 'buffered',
                              'overflow_dropped', 'after_close_dropped'))
MAX_COUNTER = 2**53 - 1


def _count(value):
    return type(value) is int and 0 <= value <= MAX_COUNTER


def unknown_event_evidence():
    result = dict.fromkeys(EVENT_EVIDENCE_KEYS)
    result['primary_error_code'] = 'worker_event_evidence_missing'
    return result


def normalize_event_evidence(value):
    """Retain only the fixed public fields, with null for unproved evidence."""
    if not isinstance(value, dict):
        return unknown_event_evidence(), False
    result = dict.fromkeys(EVENT_EVIDENCE_KEYS)
    valid = set(value) == EVENT_EVIDENCE_KEYS
    for key in BOOLEAN_FIELDS:
        item = value.get(key)
        if item is None or type(item) is bool:
            result[key] = item
        else:
            valid = False
    for key in COUNT_FIELDS:
        item = value.get(key)
        if item is None or _count(item):
            result[key] = item
        else:
            valid = False
    for key in ERROR_FIELDS:
        item = value.get(key)
        if item is None or (type(item) is str and re.fullmatch('[a-z0-9_]{1,64}', item)):
            result[key] = item
        else:
            valid = False
    if not valid:
        result['primary_error_code'] = 'event_evidence_invalid'
    return result, valid


def valid_hint_result(value, duration):
    if type(duration) is not int or not 1 <= duration <= 60:
        return False
    if not isinstance(value, dict) or set(value) != RESULT_KEYS:
        return False
    if value['scope'] != 'main_window_subtree' or value['status'] not in ('hint_observed', 'timed_out'):
        return False
    requested, elapsed = value['requested_wait_ms'], value['armed_window_ms']
    if (not _count(requested) or requested != duration*1000 or not _count(elapsed)
            or elapsed > requested):
        return False
    counts = value['counts']
    if not isinstance(counts, dict) or set(counts) != RESULT_COUNT_KEYS or not all(_count(n) for n in counts.values()):
        return False
    total = counts['notification'] + counts['text_changed']
    if counts['buffered'] > 128 or total != counts['buffered'] + counts['overflow_dropped']:
        return False
    kinds = [kind for kind in ('notification', 'text_changed') if counts[kind]]
    if type(value['observed_kinds']) is not list or value['observed_kinds'] != kinds:
        return False
    if value['status'] == 'timed_out':
        if total or elapsed != requested:
            return False
    elif total == 0:
        return False
    coverage = value['coverage']
    return (isinstance(coverage, dict) and set(coverage) == {'continuous','complete','recipient_known','missed_count'}
            and all(coverage[key] is False for key in ('continuous','complete','recipient_known'))
            and coverage['missed_count'] is None)


def valid_hint_success(result, evidence, desktop, duration):
    clean, valid = normalize_event_evidence(evidence)
    return (valid and all(clean[key] is True for key in TRUE_FLAGS)
            and clean['cleanup_pending'] is False
            and all(_count(clean[key]) for key in COUNT_FIELDS)
            and all(clean[key] is None for key in ERROR_FIELDS)
            and isinstance(desktop, dict) and desktop.get('background_observation_passed') is True
            and valid_hint_result(result, duration))


def event_cleanup_verified(evidence):
    clean, valid = normalize_event_evidence(evidence)
    return (valid and all(clean[key] is True for key in
                         ('registrations_removed','handler_refs_released','owner_exited'))
            and clean['cleanup_pending'] is False and clean['cleanup_error_code'] is None)
