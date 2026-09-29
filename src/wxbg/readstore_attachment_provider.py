"""Internal attachment transport; only release bytes after source revalidation."""
import base64
import json
import re
from .readstore_attachment_query import query_attachment
from .readstore_downloaded_file import read_downloaded_file,DownloadError
from .readstore_downloaded_image import read_downloaded_image
from .readstore_image_keys import image_keys,ImageKeyError
from .readstore_message_identity import resolve_conversation
from .readstore_query import ReadStoreError

SAFE_ATTACHMENT_ERRORS=frozenset({
    'attachment_not_downloaded','attachment_ambiguous','attachment_size_limit',
    'attachment_deadline','attachment_path_changed','attachment_file_changed',
    'attachment_file_unavailable','attachment_identity_unavailable',
    'attachment_declaration_invalid','attachment_close_failed',
    'attachment_account_changed','attachment_message_unavailable',
    'attachment_image_key_unavailable','attachment_image_invalid'})

_DECLARATION_QUERY_ERRORS=frozenset({
    'declaration_invalid','declaration_too_large','image_locator_invalid',
    'image_packed_info_missing','packed_info_missing','packed_info_unsupported',
    'packed_info_too_large','body_missing','body_unsupported',
    'body_unavailable','body_too_large'})

def inspect_image(data):
    from .readstore_image_format import inspect_image as inspect,ImageFormatError
    try:return inspect(data)
    except ImageFormatError:raise DownloadError('attachment_image_invalid') from None


def run_attachment(args,*,target,deadline):
    from .readstore_provider import open_stores,ProviderError
    if (type(args) is not dict or set(args)!={'conversation_key','account_epoch','message_identity'}
            or type(args.get('conversation_key')) is not str or not 1<=len(args['conversation_key'])<=1024
            or '\x00' in args['conversation_key'] or type(args.get('account_epoch')) is not str
            or re.fullmatch('[0-9a-f]{64}',args['account_epoch']) is None
            or type(args.get('message_identity')) is not dict):
        raise ProviderError('readstore_input_invalid')
    try:
        if len(json.dumps(args,ensure_ascii=True,allow_nan=False).encode())>16384:raise ValueError()
    except (TypeError,ValueError,RecursionError):raise ProviderError('readstore_input_invalid') from None
    with open_stores(target,deadline=deadline,include_messages=True) as opened:
        if opened['account_epoch']!=args['account_epoch']:raise ProviderError('attachment_account_changed')
        try:
            chat=resolve_conversation(opened['connections']['session'],args['conversation_key'],deadline=deadline)
            if args['message_identity'].get('chat_md5')!=chat:raise ProviderError('attachment_message_unavailable')
            query=query_attachment({name:opened['connections'][name] for name in opened['message_shard_ids']},
                account_epoch=opened['account_epoch'],expected_account_epoch=args['account_epoch'],
                conversation_key=args['conversation_key'],message_identity=args['message_identity'],deadline=deadline)
            declaration=query['declaration']
            image=None
            if declaration.get('media_kind')=='image':
                with image_keys(target,opened['account_path'],deadline=deadline) as keys:
                    downloaded=read_downloaded_image(opened['account_path'],chat,query['cache_locator']['candidate_basename'],declaration,
                        aes_key=keys.aes_key,xor_key=keys.xor_key,deadline=deadline)
                downloaded.evidence['image_key_revalidated']=True
                image=inspect_image(downloaded.data)
                filename=downloaded.sha256+image['extension']
            else:
                downloaded=read_downloaded_file(opened['account_path'],declaration,deadline=deadline)
                filename=declaration['filename']
        except ImageKeyError as error:
            code=error.code if error.code in SAFE_ATTACHMENT_ERRORS else 'attachment_image_key_unavailable'
            raise ProviderError(code) from None
        except DownloadError as error:
            code=error.code if error.code in SAFE_ATTACHMENT_ERRORS else 'attachment_file_unavailable'
            raise ProviderError(code) from None
        except ReadStoreError as error:
            if error.code in _DECLARATION_QUERY_ERRORS:
                code='attachment_declaration_invalid'
            elif error.code=='query_budget_exceeded':
                code='attachment_deadline'
            else:
                code='attachment_message_unavailable'
            raise ProviderError(code) from None
        result={'message_identity':query['message_identity'],'filename':filename,
            'size_bytes':len(downloaded.data),'sha256':downloaded.sha256,
            'content_base64':base64.b64encode(downloaded.data).decode('ascii'),
            'media_kind':'image' if image else 'file','representation':'message_image' if image else 'original','file_evidence':downloaded.evidence,
            'readstore_account_epoch':opened['account_epoch'],'readstore_evidence':opened['evidence']}
        if image is not None:result['image']=image
    return result
