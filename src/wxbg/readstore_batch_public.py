"""Public boundary for one bounded authenticated multi-conversation read."""
import json

from mcp.server.fastmcp.exceptions import ToolError

from . import readstore_public as inbox
from .batch_read_contract import (
    BatchReadContractError, validate_batch_read_request, valid_batch_read_result,
)
from .readstore_new_public import new_result_or_error
from .readstore_search_public import invalid, verified_response

MAX_BATCH_PUBLIC_BYTES = 384 * 1024
MAX_BATCH_REQUEST_BYTES = 288 * 1024
_SPEC_KEYS = {'conversation_key', 'limit', 'cursor', 'start_from'}
_PUBLIC_READSTORE_ERRORS = frozenset({
    'readstore_provider_unavailable', 'readstore_provider_failed',
    'readstore_result_invalid', 'readstore_invalid_deadline',
    'readstore_background_failed',
    'readstore_deadline', 'readstore_input_invalid', 'readstore_size_limit',
    'readstore_source_changed', 'readstore_account_changed',
    'readstore_unavailable', 'readstore_index_invalid',
    'query_budget_exceeded', 'unsupported',
    'invalid_cursor', 'cursor_context_conflict', 'invalid_chat_md5',
    'missing_epoch', 'schema_unsupported',
})


def _propagate_verified_error(response):
    """Expose only fixed read-store codes from a verified failed worker reply."""
    if (type(response) is not dict or response.get('ok') is not False
            or response.get('worker_started') is not True
            or type(response.get('target_validation')) is not dict
            or response['target_validation'].get('status') != 'stable'
            or 'result' in response):
        invalid()
    inbox._background_projection(response.get('evidence'))
    inbox._cleanup_projection(response.get('cleanup'))
    error = response.get('error')
    if type(error) is not dict:
        invalid()
    code = error.get('code')
    if (type(code) is not str or code not in _PUBLIC_READSTORE_ERRORS
            or error.get('outcome_unknown') is not False
            or error.get('submission_started', False) is not False):
        invalid()
    raise ToolError(json.dumps({'code': code, 'outcome_unknown': False, 'retry': False},
                               ensure_ascii=True, separators=(',', ':')))


def normalize_batch_request(account_epoch, conversations, *, max_total=256):
    """Validate and detach arguments before a worker can read any store."""
    try:
        if type(conversations) is not list or not 1 <= len(conversations) <= 16:
            raise ValueError
        specs = []
        for spec in conversations:
            if type(spec) is not dict or not set(spec) <= _SPEC_KEYS:
                raise ValueError
            cursor = spec.get('cursor')
            specs.append({'conversation_key': spec.get('conversation_key'),
                          'limit': spec.get('limit', 50), 'cursor': cursor,
                          'start_from': spec.get('start_from', 'cursor' if cursor is not None else 'now')})
        request = validate_batch_read_request(account_epoch, specs, max_total=max_total)
        encoded = json.dumps(request, ensure_ascii=True, allow_nan=False)
        if len(encoded.encode()) > MAX_BATCH_REQUEST_BYTES:
            raise ValueError
        # Nested cursors must not retain references to mutable caller data.
        return json.loads(encoded)
    except (BatchReadContractError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ToolError(json.dumps({'code': 'invalid_batch_read_request',
                                    'outcome_unknown': False, 'retry': False})) from None


def _continuation_valid(spec, state):
    previous = spec['cursor']
    current = state['next_cursor']
    if previous is None:
        expected_filter = 'start_from:now' if spec['start_from'] == 'now' else None
        if current['filter'] != expected_filter:
            invalid()
        if spec['start_from'] == 'now' and (state['items'] or state['has_more']):
            invalid()
        if spec['start_from'] == 'now':
            if current['shard_boundary'] != current['shard_highwater']:
                invalid()
            return
        expected = {shard: None for shard in current['shard_ids']}
    else:
        if previous['gap_detected'] and not state['gap_detected']:
            invalid()
        if (previous['shard_ids'] != current['shard_ids']
                or previous['filter'] != current['filter']):
            invalid()
        expected = {item['shard_id']: dict(item)
                    for item in previous['shard_highwater']}
    for row in state['items']:
        shard = row['shard_id']
        rowid = row['message_identity']['rowid']
        highwater = expected[shard]
        if (highwater is not None and highwater['rowid'] is not None
                and rowid <= highwater['rowid']):
            invalid()
        expected[shard] = {'shard_id': shard, 'sort_seq': row['sort_seq'],
                           'local_id': row['local_id'], 'rowid': rowid}
    positions = [expected[shard] if expected[shard] is not None else
                 {'shard_id': shard, 'sort_seq': None, 'local_id': None,
                  'rowid': None} for shard in current['shard_ids']]
    if (current['shard_highwater'] != positions
            or current['shard_boundary'] != positions):
        invalid()


def batch_result_or_error(response, *, request):
    if type(response) is dict and response.get('ok') is False:
        _propagate_verified_error(response)
    result, evidence, _, epoch = verified_response(response, messages=True)
    if epoch != request['account_epoch']:
        invalid()
    batch = {key: value for key, value in result.items()
             if key not in ('readstore_evidence', 'readstore_account_epoch')}
    if not valid_batch_read_result(batch, request):
        invalid()
    states_out = []
    for spec, state in zip(request['conversations'], batch['conversations']):
        # Reuse the established public validation of actual shard inventory,
        # cursor grammar, output boundaries and exact message identity.
        single = {'items': state['items'], 'count': len(state['items']),
                  'has_more': state['has_more'], 'gap_detected': state['gap_detected'],
                  'next_cursor': state['next_cursor'], 'bounded': True,
                  'full_history': False, 'exact_once': False,
                  'ordering': 'rowid_asc_per_shard_merge',
                  'coverage': 'bounded_message_table_query',
                  'readstore_account_epoch': epoch,
                  'readstore_evidence': result['readstore_evidence']}
        checked = new_result_or_error({**response, 'result': single},
                                      conversation_key=spec['conversation_key'], limit=spec['limit'])
        _continuation_valid(spec, state)
        states_out.append({**state, 'items': [
            {**checked_row,
             'conversation_key': raw_row['conversation_key'],
             'account_epoch': raw_row['account_epoch']}
            for raw_row, checked_row in zip(state['items'], checked['result']['items'])]})
    batch = {**batch, 'conversations': states_out, 'multi_database_atomic': False,
             'source_consistency': 'observed_quiet_window',
             'snapshot_scope': 'one_authenticated_store_set'}
    try:
        encoded = json.dumps(batch, ensure_ascii=True, allow_nan=False)
        if len(encoded.encode()) > MAX_BATCH_PUBLIC_BYTES:
            invalid()
        batch = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        invalid()
    return {'ok': True, 'result': batch, 'evidence': evidence}
