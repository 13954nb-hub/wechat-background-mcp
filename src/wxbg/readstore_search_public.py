"""Validate read-only search evidence and project only public result fields."""
import hashlib
import json
import re
from . import readstore_public as inbox
from .sender_role import SENDER_ROLE_KEYS, unavailable_sender_role, validate_sender_role_fields


def invalid():
    inbox._invalid()


def integer(value,*,nullable=False):
    if nullable and value is None:return None
    if type(value) is not int or not -(1<<63)<=value<(1<<63):invalid()
    return value


def shard(value):
    if type(value) is not str or re.fullmatch(r'message_(0|[1-9][0-9]{0,3})',value) is None:invalid()
    return value


def verified_response(response,*,messages=False):
    inbox._raise_verified_background_side_effect(response)
    inbox._raise_verified_readstore_error(response)
    if (type(response) is not dict or response.get('ok') is not True
            or response.get('worker_started') is not True
            or type(response.get('target_validation')) is not dict
            or response['target_validation'].get('status')!='stable'):invalid()
    background=inbox._background_projection(response.get('evidence'))
    cleanup=inbox._cleanup_projection(response.get('cleanup'))
    result=response.get('result')
    if type(result) is not dict:invalid()
    account_epoch=inbox._strict_epoch(result.get('readstore_account_epoch'))
    evidence=result.get('readstore_evidence')
    inbox._validate_readstore_evidence(evidence)
    captures=evidence['captures'];snapshots=evidence['snapshots']
    expected={'session','contact'}
    shards=[]
    if any(type(name) is not str for name in captures) or any(type(name) is not str for name in snapshots):invalid()
    if messages:
        shards=sorted(name for name in captures if name not in expected)
        if not 1<=len(shards)<=14:invalid()
        for name in shards:
            shard(name);inbox._validate_capture(captures[name])
            if name not in snapshots:invalid()
            inbox._validate_snapshot(snapshots[name])
        expected.update(shards)
    if set(captures)!=expected or set(snapshots)!=expected:invalid()
    return result,{'background':background,'cleanup':cleanup},shards,account_epoch


def message_row(value,chat,shards):
    if type(value) is not dict:invalid()
    name=shard(value.get('shard_id'))
    if name not in shards:invalid()
    row={'shard_id':name}
    for key in ('local_id','sort_seq'):
        row[key]=integer(value.get(key))
    for key in ('local_type','sender_id','create_time','server_id'):
        if key not in value:invalid()
        row[key]=integer(value[key],nullable=True)
    content=value.get('content')
    if content is not None:
        content=inbox._strict_text(content,maximum=4096)
    available=inbox._strict_bool(value.get('content_available'))
    truncated=inbox._strict_bool(value.get('content_truncated'))
    if available and (content is None or truncated):invalid()
    identity=value.get('message_identity')
    if (type(identity) is not dict or identity.get('chat_md5')!=chat
            or identity.get('shard_id')!=name
            or type(identity.get('local_id')) is not int
            or identity['local_id']!=row['local_id']
            or identity.get('server_id')!=row['server_id']):invalid()
    integer(identity.get('server_id'),nullable=True)
    rowid=integer(identity.get('rowid'))
    row.update(content=content,content_available=available,content_truncated=truncated,
        message_identity={'chat_md5':chat,'shard_id':name,'local_id':row['local_id'],
                          'server_id':row['server_id'],'rowid':rowid})
    present=SENDER_ROLE_KEYS.intersection(value)
    if present and present!=SENDER_ROLE_KEYS:invalid()
    if present:
        try:role_fields=validate_sender_role_fields({key:value[key] for key in SENDER_ROLE_KEYS})
        except (TypeError,ValueError):invalid()
    else:
        role_fields=unavailable_sender_role()
    row.update(role_fields)
    return row


def message_order(row):
    return (-row['sort_seq'],row['shard_id'],row['local_id'],row['message_identity']['rowid'])


def search_cursor(value,*,scope,keyword,chat,shards,account_epoch):
    if type(value) is not dict:invalid()
    epoch=inbox._strict_epoch(value.get('account_epoch'))
    if epoch != account_epoch:invalid()
    if type(value.get('keyword')) is not str or value['keyword']!=keyword:invalid()
    if scope=='conversations':
        if set(value)!={'version','account_epoch','keyword','inner_cursor'}:invalid()
        if type(value['version']) is not int or value['version']!=1:invalid()
        raw=value['inner_cursor']
        if type(raw) is not dict or set(raw)!=inbox._CURSOR_KEYS:invalid()
        inner=inbox._validate_cursor(raw,unread_only=False)
        if inner is None or inner['account_epoch']!=epoch:invalid()
        return {'version':1,'account_epoch':epoch,'keyword':keyword,'inner_cursor':inner}
    if scope=='groups':
        if type(value.get('version')) is not int or value['version']!=3 or value.get('scope')!='groups':invalid()
        phase=value.get('phase')
        base={'version','scope','account_epoch','keyword','phase'}
        if phase=='contact':
            if set(value)!=base|{'after_username'}:invalid()
            after=inbox._strict_text(value['after_username'],maximum=1024,nonempty=True)
            if not after.endswith('@chatroom'):invalid()
            return {'version':3,'scope':'groups','account_epoch':epoch,
                    'keyword':keyword,'phase':'contact','after_username':after}
        if phase!='session' or set(value)!=base|{'inner_cursor'}:invalid()
        raw=value['inner_cursor']
        if raw is None:
            inner=None
        else:
            if type(raw) is not dict or set(raw)!=inbox._CURSOR_KEYS|{'include_hidden'}:invalid()
            if (type(raw.get('version')) is not int or raw['version']!=3
                    or raw.get('include_hidden') is not True
                    or raw.get('unread_only') is not False
                    or raw.get('order')!='username_asc'
                    or raw.get('sort_timestamp') is not None):invalid()
            inner_epoch=inbox._strict_epoch(raw.get('account_epoch'))
            if inner_epoch!=epoch:invalid()
            inner={'version':3,'account_epoch':inner_epoch,'unread_only':False,
                   'include_hidden':True,'order':'username_asc','sort_timestamp':None,
                   'username':inbox._strict_text(raw.get('username'),maximum=1024,nonempty=True)}
        return {'version':3,'scope':'groups','account_epoch':epoch,
                'keyword':keyword,'phase':'session','inner_cursor':inner}
    if set(value)!={'version','account_epoch','chat_md5','keyword','direction','shard_ids','after'}:invalid()
    if (type(value['version']) is not int or value['version']!=4
            or value['chat_md5']!=chat or value['direction']!='desc'
            or value['shard_ids']!=shards):invalid()
    after=value['after']
    if type(after) is not dict or set(after)!={'sort_seq','shard_id','local_id','rowid'}:invalid()
    position={'sort_seq':integer(after['sort_seq']),'shard_id':shard(after['shard_id']),
              'local_id':integer(after['local_id']),'rowid':integer(after['rowid'])}
    if position['shard_id'] not in shards:invalid()
    return {'version':4,'account_epoch':epoch,'chat_md5':chat,'keyword':keyword,
            'direction':'desc','shard_ids':list(shards),'after':position}


def search_result_or_error(response,*,scope,keyword,limit,conversation_key=None):
    if type(scope) is not str or scope not in ('conversations','groups','messages'):invalid()
    if type(keyword) is not str or not 1<=len(keyword)<=256 or '\x00' in keyword:invalid()
    if type(limit) is not int or not 1<=limit<=100:invalid()
    chat=None
    if scope=='messages':
        inbox._strict_text(conversation_key,maximum=1024,nonempty=True)
        try:chat=hashlib.md5(conversation_key.encode('utf-8')).hexdigest()
        except UnicodeError:invalid()
    result,evidence,shards,account_epoch=verified_response(response,messages=scope=='messages')
    items=result.get('items')
    if type(items) is not list or len(items)>limit:invalid()
    count=integer(result.get('count'))
    scanned=integer(result.get('scanned'))
    if count!=len(items) or not count<=scanned<=500:invalid()
    more=inbox._strict_bool(result.get('has_more'))
    scan_limit=inbox._strict_bool(result.get('scan_limit_reached'))
    if more and scanned==0:invalid()
    cursor=result.get('next_cursor')
    if more != (cursor is not None):invalid()
    if cursor is not None:
        cursor=search_cursor(cursor,scope=scope,keyword=keyword,chat=chat,
                             shards=shards,account_epoch=account_epoch)
    if scope in ('conversations','groups'):
        groups=scope=='groups'
        if groups and result.get('membership_unverified') is not True:invalid()
        rows=[]
        for value in items:
            if groups and (type(value) is not dict or value.get('membership_unverified') is not True):invalid()
            row=inbox._validate_row(value,include_hidden=groups)
            if groups:
                if not row['conversation_key'].endswith('@chatroom'):invalid()
                source=value.get('candidate_source')
                if type(source) is not str or source not in ('contact','session'):invalid()
                if source=='contact' and (
                    row['unread_count'] is not None or row['unread_known'] is not False
                    or row['summary'] is not None or row['summary_available'] is not False
                    or row['summary_truncated'] is not False
                    or row['last_timestamp'] is not None or row['sort_timestamp'] is not None
                    or row['is_hidden'] is not None
                ):invalid()
                row['membership_unverified']=True
                row['candidate_source']=source
            rows.append(row)
        if any(keyword.casefold() not in r['display_name'].casefold() and keyword.casefold() not in r['conversation_key'].casefold() for r in rows):invalid()
        if len({r['conversation_key'] for r in rows})!=len(rows):invalid()
        if groups and len({r['candidate_source'] for r in rows})>1:invalid()
        if groups:
            if any(a['conversation_key']>=b['conversation_key'] for a,b in zip(rows,rows[1:])):invalid()
        elif any(not inbox._row_after(a,b) for a,b in zip(rows,rows[1:])):invalid()
        if groups and rows and cursor is not None:
            if rows[0]['candidate_source']=='session' and cursor['phase']!='session':invalid()
            if (rows[0]['candidate_source']=='contact' and cursor['phase']=='contact'
                    and cursor['after_username']<rows[-1]['conversation_key']):invalid()
        coverage=('contact_only_then_sessiontable_bounded_group_candidates' if groups
                  else 'sessiontable_contact_bounded_nonhidden')
        matching=('chatroom_suffix_and_display_name_or_conversation_key_casefold_literal_substring'
                  if groups else 'display_name_or_conversation_key_casefold_literal_substring')
    else:
        rows=[message_row(v,chat,shards) for v in items]
        folded_keyword=keyword.casefold()
        for row in rows:
            content=row['content']
            if content is None:invalid()
            if row['content_available']:
                if folded_keyword not in content.casefold():invalid()
            elif not row['content_truncated'] or len(content)!=4096:
                # Search only returns decoded matches.  An unavailable body
                # must be the exact bounded preview of a longer match.
                invalid()
        identities={(r['shard_id'],r['message_identity']['rowid']) for r in rows}
        if len(identities)!=len(rows):invalid()
        if any(message_order(a)>=message_order(b) for a,b in zip(rows,rows[1:])):invalid()
        coverage='bounded_decoded_message_search'
        matching='decoded_text_casefold_literal_substring'
        if (result.get('bounded') is not True or result.get('full_history') is not False
                or result.get('exact_once') is not False):invalid()
    if result.get('coverage')!=coverage or result.get('matching')!=matching:invalid()
    projected={'items':rows,'count':count,'has_more':more,'next_cursor':cursor,
        'scanned':scanned,'scan_limit_reached':scan_limit,'coverage':coverage,'matching':matching}
    if scope=='groups':
        projected['membership_unverified']=True
    if scope=='messages':
        unsupported=integer(result.get('unsupported_count'))
        if not 0<=unsupported<=scanned-count:invalid()
        projected.update(unsupported_count=unsupported,bounded=True,full_history=False,exact_once=False)
        projected['account_epoch']=account_epoch
        if rows and cursor is not None:
            a=cursor['after'];key=(-a['sort_seq'],a['shard_id'],a['local_id'],a['rowid'])
            if key<message_order(rows[-1]):invalid()
    try:
        if len(json.dumps(projected,ensure_ascii=True,allow_nan=False).encode())>384*1024:invalid()
    except (ValueError,TypeError,RecursionError):invalid()
    return {'ok':True,'result':projected,'evidence':evidence}
