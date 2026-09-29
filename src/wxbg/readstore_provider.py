"""Internal read-store orchestration. Never use UIA or persist plaintext/keys."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import math
import os
import re
import time

from .readstore_account import AccountLookupError, locate_account
from .readstore_capture import CaptureError, capture_quiet
from .readstore_database import DatabaseError, open_snapshot
from .readstore_keys import KeyLookupError, find_database_keys
from .readstore_locations import LocationError, discover_roots, select_storage
from .readstore_process import ProcessReader, ProcessReaderError
from .path_config import ClientPathError, local_absolute_path


class ProviderError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_OPEN_DEADLINE_CODES = {
    ProcessReaderError: 'deadline',
    AccountLookupError: 'account_deadline',
    LocationError: 'storage_deadline',
    KeyLookupError: 'key_deadline',
    DatabaseError: 'database_deadline',
}


def _file_identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino


def _message_paths(storage, check):
    """Inventory only numbered message stores under the verified account."""
    directory = storage/'message'
    try:
        if not directory.is_dir():
            raise ProviderError('readstore_messages_unavailable')
        if directory.resolve(strict=True) != directory:
            raise ProviderError('readstore_source_changed')
        identity = _file_identity(directory)
        result = {}
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                check()
                if index >= 256:
                    raise ProviderError('readstore_size_limit')
                if re.fullmatch(r'message_(0|[1-9][0-9]{0,3})\.db',entry.name) is None:
                    continue
                path = directory/entry.name
                if (not entry.is_file(follow_symlinks=False)
                        or path.resolve(strict=True) != path):
                    raise ProviderError('readstore_source_changed')
                result[entry.name[:-3]] = path
                if len(result) > 14:
                    raise ProviderError('readstore_size_limit')
        check()
        if _file_identity(directory) != identity:
            raise ProviderError('readstore_source_changed')
        if not result:
            raise ProviderError('readstore_messages_unavailable')
        return dict(sorted(result.items())), identity
    except ProviderError:
        raise
    except OSError:
        raise ProviderError('readstore_source_changed') from None


@contextmanager
def open_stores(target, *, deadline, include_messages=False):
    """Bind process/account, capture and open authenticated session/contact DBs.

    Every source capture is an observed quiet window, not an atomic transaction
    across databases. Caller must leave this context successfully before
    publishing rows: exit rechecks account, directory and database identities.
    """
    if (type(include_messages) is not bool or type(deadline) not in (int,float)
            or not math.isfinite(deadline)):
        raise ProviderError('readstore_input_invalid')
    def check():
        if time.monotonic() >= deadline:
            raise ProviderError('readstore_deadline')
    check()
    try:
        with ExitStack() as stack:
            reader = stack.enter_context(ProcessReader(target, deadline=deadline))
            def account_now():
                check()
                return locate_account(reader.read, reader.module_base, reader.module_size, deadline)
            account = account_now()
            explicit_root = os.environ.get('WXBG_DATA_ROOT')
            if explicit_root is None:
                roots = discover_roots(deadline=deadline)
            else:
                try:
                    roots = [local_absolute_path(explicit_root)]
                except ClientPathError as exc:
                    raise ProviderError('readstore_input_invalid') from exc
            location = select_storage(account.username, roots=roots, deadline=deadline)
            paths = {'session': location.path/'session/session.db',
                     'contact': location.path/'contact/contact.db'}
            message_paths, message_directory_id = {}, None
            if include_messages:
                message_paths, message_directory_id = _message_paths(location.path,check)
                paths.update(message_paths)
            identities = {name: _file_identity(path) for name,path in paths.items()}
            if len(set(identities.values())) != len(identities):
                raise ProviderError('readstore_source_changed')
            first_pages = {}
            for name,path in paths.items():
                check()
                initial = capture_quiet(path, deadline=deadline, max_bytes=32*1024*1024)
                first_pages[name] = initial.database[:4096]
                del initial
            keys, key_evidence = find_database_keys(read=reader.read, regions=reader.regions,
                pages=first_pages, deadline=deadline)
            connections, captures, snapshots = {}, {}, {}
            retained = 0
            for name,path in paths.items():
                check()
                capture = capture_quiet(path, deadline=deadline, max_bytes=32*1024*1024)
                retained += len(capture.database)+len(capture.wal)+len(capture.wal_index)
                if retained > 64*1024*1024:
                    raise ProviderError('readstore_size_limit')
                if (_file_identity(path) != identities[name]
                        or capture.database[:16] != first_pages[name][:16]):
                    raise ProviderError('readstore_source_changed')
                connection, evidence = stack.enter_context(open_snapshot(capture,
                    key=keys[name], deadline=deadline))
                connections[name] = connection
                snapshots[name] = evidence
                captures[name] = capture.evidence
                del capture
            epoch_material = [target, account.username, account.cfg_pointer,
                location.account_id, location.storage_id, identities,
                {name:page[:16].hex() for name,page in first_pages.items()}]
            epoch = hashlib.sha256(json.dumps(epoch_material,sort_keys=True).encode()).hexdigest()
            yield {'connections':connections, 'account_epoch':epoch,
                   'account_path':location.account_path,
                   'account_username':account.username,
                   'message_shard_ids':list(message_paths),
                   'evidence':{'multi_database_atomic':False,
                       'source_consistency':'observed_quiet_window',
                       'captures':captures,'snapshots':snapshots,
                       'key_lookup':key_evidence}}
            check()
            reader.revalidate()
            if account_now() != account:
                raise ProviderError('readstore_account_changed')
            location.revalidate()
            if include_messages:
                after_paths, after_id = _message_paths(location.path,check)
                if after_paths != message_paths or after_id != message_directory_id:
                    raise ProviderError('readstore_source_changed')
            if any(_file_identity(path) != identities[name] for name,path in paths.items()):
                raise ProviderError('readstore_source_changed')
            check()
    except ProviderError:
        raise
    except CaptureError as exc:
        # Preserve only fixed, public-safe categories from the quiet capture.
        code = ({'capture_changed': 'readstore_source_changed',
                 'capture_replaced': 'readstore_source_changed',
                 'capture_deadline': 'readstore_deadline',
                 'capture_limit': 'readstore_size_limit'}
                .get(exc.code, 'readstore_unavailable'))
        raise ProviderError(code) from None
    except tuple(_OPEN_DEADLINE_CODES) as exc:
        expected = _OPEN_DEADLINE_CODES.get(type(exc))
        if expected is not None and exc.code == expected:
            code = 'readstore_deadline'
        elif type(exc) is DatabaseError and exc.code == 'database_index_invalid':
            code = 'readstore_index_invalid'
        else:
            code = 'readstore_unavailable'
        raise ProviderError(code) from None
    except Exception:
        raise ProviderError('readstore_unavailable') from None


def _query_args(action, args):
    if type(args) is not dict:
        raise ProviderError('readstore_input_invalid')
    allowed = ({'scope','keyword','conversation_key','limit','cursor'}
               if action == 'readstore_search' else {'conversation_key','limit','cursor','since_seq','start_from'})
    limit = args.get('limit',20)
    cursor = args.get('cursor')
    if (not set(args) <= allowed or type(limit) is not int or not 1 <= limit <= 100
            or (cursor is not None and type(cursor) is not dict)):
        raise ProviderError('readstore_input_invalid')
    try:
        if len(json.dumps(cursor,ensure_ascii=True,allow_nan=False).encode()) > 16384:
            raise ProviderError('readstore_input_invalid')
    except (TypeError,ValueError,RecursionError):
        raise ProviderError('readstore_input_invalid') from None
    scope = args.get('scope','conversations') if action == 'readstore_search' else 'messages'
    if type(scope) is not str or scope not in ('conversations','groups','messages'):
        raise ProviderError('readstore_input_invalid')
    if action == 'readstore_search':
        keyword = args.get('keyword')
        if type(keyword) is not str or not 1 <= len(keyword) <= 256 or '\x00' in keyword:
            raise ProviderError('readstore_input_invalid')
    key = args.get('conversation_key')
    if scope == 'messages':
        if type(key) is not str or not 1 <= len(key) <= 1024 or '\x00' in key:
            raise ProviderError('readstore_input_invalid')
    elif 'conversation_key' in args:
        raise ProviderError('readstore_input_invalid')
    if action=='readstore_read_new':
        start=args.get('start_from','now')
        if type(start) is not str or start not in ('now','beginning'):
            raise ProviderError('readstore_input_invalid')
        if start=='now' and cursor is None and args.get('since_seq') is not None:
            raise ProviderError('readstore_input_invalid')
    since = args.get('since_seq')
    if since is not None and (type(since) is not int or not -(1<<63) <= since < (1<<63)
                              or cursor is not None):
        raise ProviderError('readstore_input_invalid')
    return scope,limit,cursor


def run(action, args, *, target, deadline):
    """Internal worker entry. Publish results only after successful context exit."""
    if action=='readstore_get_attachment':
        from .readstore_attachment_provider import run_attachment
        return run_attachment(args,target=target,deadline=deadline)
    if action=='readstore_batch_read_new':
        from .readstore_batch_provider import run_batch_read
        return run_batch_read(args,target=target,deadline=deadline,open_stores=open_stores)
    if action in ('readstore_search','readstore_read_new'):
        scope,limit,cursor = _query_args(action,args)
        with open_stores(target,deadline=deadline,include_messages=scope=='messages') as opened:
            connections = opened['connections']
            if scope in ('conversations','groups'):
                from .readstore_conversation_search import search_conversations
                result=search_conversations(connections['session'],connections['contact'],
                    account_epoch=opened['account_epoch'],keyword=args['keyword'],
                    limit=limit,cursor=cursor,deadline=deadline,scope=scope)
            else:
                from .readstore_message_identity import resolve_conversation
                chat=resolve_conversation(connections['session'],args['conversation_key'],deadline=deadline)
                message_connections={name:connections[name] for name in opened['message_shard_ids']}
                if action == 'readstore_search':
                    from .readstore_message_search import search_messages
                    result=search_messages(message_connections,account_epoch=opened['account_epoch'],
                        chat_md5=chat,keyword=args['keyword'],limit=limit,cursor=cursor,deadline=deadline)
                else:
                    from .readstore_query import ReadStoreQuery
                    query=ReadStoreQuery(session_connection=connections['session'],
                        message_connections=message_connections,account_epoch=opened['account_epoch'],
                        query_timeout_seconds=5)
                    result=query.get_new_messages(chat,limit=limit,cursor=cursor,
                        since_seq=args.get('since_seq'),start_from=args.get('start_from','now'),deadline=deadline)
                if scope == 'messages':
                    from .readstore_sender_roles import classify_message_rows
                    result['items'] = classify_message_rows(
                        result.get('items', []),
                        message_connections=message_connections,
                        account_username=opened.get('account_username'),
                        conversation_key=args.get('conversation_key'),
                        contact_connection=connections.get('contact'),
                    )
            result={**result,'readstore_evidence':opened['evidence'],
                    'readstore_account_epoch':opened['account_epoch']}
        return result
    if action != 'readstore_read_inbox':
        raise ProviderError('readstore_action_unavailable')
    if (type(args) is not dict or not set(args) <= {'limit','unread_only','include_hidden','cursor'}
            or type(args.get('limit',50)) is not int or not 1 <= args.get('limit',50) <= 100
            or type(args.get('unread_only',True)) is not bool
            or type(args.get('include_hidden',False)) is not bool
            or (args.get('cursor') is not None and type(args['cursor']) is not dict)):
        raise ProviderError('readstore_input_invalid')
    from .readstore_inbox import read_inbox
    with open_stores(target, deadline=deadline) as opened:
        result = read_inbox(opened['connections']['session'],opened['connections']['contact'],
            account_epoch=opened['account_epoch'], deadline=deadline, **args)
        result = {**result, 'readstore_evidence':opened['evidence']}
    return result
