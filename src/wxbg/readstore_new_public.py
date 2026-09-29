"""Safe projection for account-bound, on-demand incremental message reads."""
import hashlib
import json
from . import readstore_public as inbox
from .readstore_search_public import invalid,integer,verified_response,message_row
from .readstore_query import IncrementalCursor,ReadStoreError


def new_result_or_error(response,*,conversation_key,limit):
    inbox._strict_text(conversation_key,maximum=1024,nonempty=True)
    if type(limit) is not int or not 1<=limit<=100:invalid()
    try:chat=hashlib.md5(conversation_key.encode('utf-8')).hexdigest()
    except UnicodeError:invalid()
    result,evidence,shards,account_epoch=verified_response(response,messages=True)
    raw=result.get('items')
    if type(raw) is not list or len(raw)>limit:invalid()
    count=integer(result.get('count'))
    if count!=len(raw):invalid()
    if (result.get('bounded') is not True or result.get('full_history') is not False
            or result.get('exact_once') is not False
            or result.get('coverage')!='bounded_message_table_query'
            or result.get('ordering')!='rowid_asc_per_shard_merge'):invalid()
    more=inbox._strict_bool(result.get('has_more'))
    gap=inbox._strict_bool(result.get('gap_detected'))
    rows=[message_row(row,chat,shards) for row in raw]
    if len({(row['shard_id'],row['message_identity']['rowid']) for row in rows})!=len(rows):invalid()
    order=[(row['message_identity']['rowid'],row['shard_id'],row['local_id']) for row in rows]
    if any(a>=b for a,b in zip(order,order[1:])):invalid()
    try:
        parsed=IncrementalCursor.from_value(result.get('next_cursor'))
    except (ReadStoreError,TypeError,ValueError,OverflowError):invalid()
    if (parsed.chat_md5!=chat or list(parsed.shard_ids)!=shards
            or parsed.account_epoch!=account_epoch
            or parsed.filter_key not in (None,'start_from:now')
            or parsed.gap_detected is not gap):invalid()
    cursor=parsed.as_dict()
    if cursor['shard_highwater']!=cursor['shard_boundary']:invalid()
    positions={item['shard_id']:(item['sort_seq'],item['local_id'],item['rowid'])
               for item in cursor['shard_highwater']}
    last_by_shard={}
    for row in rows:
        last_by_shard[row['shard_id']]=(row['sort_seq'],row['local_id'],
                                        row['message_identity']['rowid'])
    if any(positions[shard_id]!=last for shard_id,last in last_by_shard.items()):invalid()
    projected={'items':rows,'count':count,'has_more':more,'next_cursor':cursor,
       'account_epoch':account_epoch,
       'gap_detected':gap,'bounded':True,'full_history':False,'exact_once':False,
       'coverage':'bounded_message_table_query','ordering':'rowid_continuation_not_chronological',
       'unsupported_content':'reported_per_message'}
    try:
        if len(json.dumps(projected,ensure_ascii=True,allow_nan=False).encode())>384*1024:invalid()
    except (TypeError,ValueError,RecursionError):invalid()
    return {'ok':True,'result':projected,'evidence':evidence}
