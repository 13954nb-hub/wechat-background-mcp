"""Authenticate the worker envelope before admitting attachment bytes to resources."""
import base64
import binascii
import hashlib
import json
import re
from mcp.server.fastmcp.exceptions import ToolError
from .readstore_search_public import verified_response,integer,shard
from .readstore_downloaded_file import _leaf,DownloadError,MAX_FILE_BYTES
from .readstore_attachment_provider import SAFE_ATTACHMENT_ERRORS
from . import readstore_public as inbox


def invalid(code='attachment_result_invalid'):
    raise ToolError(json.dumps({'code':code,'outcome_unknown':False,'retry':False}))


def validate_attachment_request(conversation_key,account_epoch,message_identity):
    if (type(conversation_key) is not str or not 1<=len(conversation_key)<=1024 or '\x00' in conversation_key
            or type(account_epoch) is not str or re.fullmatch('[0-9a-f]{64}',account_epoch) is None
            or type(message_identity) is not dict
            or set(message_identity)!={'chat_md5','shard_id','local_id','server_id','rowid'}):
        invalid('attachment_input_invalid')
    try:chat=hashlib.md5(conversation_key.encode('utf-8')).hexdigest()
    except UnicodeError:invalid('attachment_input_invalid')
    if message_identity['chat_md5']!=chat:invalid('attachment_input_invalid')
    try:
        shard(message_identity['shard_id'])
        for key in ('local_id','rowid'):integer(message_identity[key])
        integer(message_identity['server_id'],nullable=True)
    except ToolError:invalid('attachment_input_invalid')
    return dict(message_identity)


def attachment_result_or_error(response,*,conversation_key,account_epoch,message_identity):
    identity=validate_attachment_request(conversation_key,account_epoch,message_identity)
    if type(response) is not dict:invalid()
    if response.get('ok') is not True:
        error=response.get('error')
        if (response.get('ok') is not False or response.get('worker_started') is not True
                or type(response.get('target_validation')) is not dict
                or response['target_validation'].get('status')!='stable'
                or 'result' in response or type(error) is not dict
                or error.get('outcome_unknown') is not False
                or error.get('submission_started',False) is not False):invalid()
        if error.get('code')=='background_side_effect':
            if (type(response.get('evidence')) is not dict
                    or response['evidence'].get('background_observation_passed') is not False):invalid()
            try:inbox._cleanup_projection(response.get('cleanup'))
            except ToolError:invalid()
            inbox._raise_verified_background_side_effect(response)
            invalid()
        try:
            inbox._background_projection(response.get('evidence'))
            inbox._cleanup_projection(response.get('cleanup'))
        except ToolError:invalid()
        inbox._raise_verified_readstore_error(
            response,
            allowed_codes=SAFE_ATTACHMENT_ERRORS | inbox._PUBLIC_READSTORE_ERRORS,
        )
        invalid()
    try:result,evidence,shards,epoch=verified_response(response,messages=True)
    except ToolError:invalid()
    kind=result.get('media_kind');representation=result.get('representation')
    if (epoch!=account_epoch or result.get('message_identity')!=identity
            or identity['shard_id'] not in shards
            or (kind,representation) not in (('file','original'),('image','message_image'))):invalid()
    # Compare types as well as values: bool must never pass for numeric identities.
    actual=result['message_identity']
    if type(actual) is not dict or any(type(actual.get(k)) is not type(v) for k,v in identity.items()):invalid()
    try:name=_leaf(result.get('filename'))
    except DownloadError:invalid()
    size=result.get('size_bytes');sha=result.get('sha256');encoded=result.get('content_base64')
    if (type(size) is not int or not 1<=size<=MAX_FILE_BYTES or type(sha) is not str
            or re.fullmatch('[0-9a-f]{64}',sha) is None or type(encoded) is not str
            or len(encoded)>4*((MAX_FILE_BYTES+2)//3)):invalid()
    try:data=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error):invalid()
    if len(data)!=size or hashlib.sha256(data).hexdigest()!=sha:invalid()
    file_evidence=result.get('file_evidence')
    required=('declared_size_matched','declared_md5_matched','held_without_write_delete_sharing',
              'file_id_128_verified','canonical_path_verified','observed_sha256')
    if (type(file_evidence) is not dict or any(file_evidence.get(k) is not True for k in required)
            or file_evidence.get('sender_authenticity_verified') is not False):invalid()
    evidence['file']={k:True for k in required}
    evidence['file']['sender_authenticity_verified']=False
    projected={'filename':name,'size_bytes':size,'sha256':sha,
       'account_epoch':epoch,'message_identity':identity,'media_kind':kind,'representation':representation,
       'download_triggered':False,'snapshot_only':True,'mime_type':'application/octet-stream'}
    if kind=='image':
        from .readstore_image_format import inspect_image,ImageFormatError
        if any(file_evidence.get(k) is not True for k in ('image_container_decoded','image_key_revalidated')):invalid()
        try:image=inspect_image(data)
        except ImageFormatError:invalid()
        actual=result.get('image')
        if (type(actual) is not dict or actual!=image
            or any(type(actual.get(k)) is not type(v) for k,v in image.items())
            or name!=sha+image['extension']):invalid()
        projected.update(image=image,mime_type=image['mime_type'])
        evidence['file'].update(image_container_decoded=True,image_key_revalidated=True)
    return {'ok':True,'result':projected,'evidence':evidence},data
