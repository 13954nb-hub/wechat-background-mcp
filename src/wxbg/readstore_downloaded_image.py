"""Locked account-cache image candidates, bound to a message checksum and size."""
from contextlib import ExitStack
import hashlib
import math
import ntpath
import os
from pathlib import Path
import re
import stat
import time
from .readstore_downloaded_file import (
    DownloadError,VerifiedDownload,_directory,_identity,_canonical,_read_locked,
    MAX_FILE_BYTES,MAX_TOTAL_BYTES,
)
from .readstore_image_container import decode_v2,ContainerError

def _fail(code):raise DownloadError(code)

def _inventory(account,chat,locator,check):
    base=account/'msg/attach'/chat
    directories={p:_directory(p) for p in (account,account/'msg',account/'msg/attach',base)}
    files={}
    with os.scandir(base) as entries:
        for index,entry in enumerate(entries):
            check()
            if index>=256:_fail('attachment_size_limit')
            if not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])',entry.name):continue
            month=base/entry.name;directories[month]=_directory(month)
            folder=month/'Img'
            try:directories[folder]=_directory(folder)
            except FileNotFoundError:continue
            for suffix in ('.dat','_h.dat'):
                path=folder/(locator+suffix)
                try:info=path.lstat()
                except FileNotFoundError:continue
                if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                    _fail('attachment_path_changed')
                files[path]=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
    return directories,files

def read_downloaded_image(account_path,chat_md5,cache_locator,declaration,*,
                          aes_key,xor_key,deadline,max_total_bytes=MAX_TOTAL_BYTES):
    """Return exact message-image bytes; no thumbnail fallback or original claim.

    Locator values select candidates only. Every accepted rendition must match
    the authenticated message's primary MD5 and length. Equal copies are allowed;
    different content sharing that checksum is ambiguous. All candidate handles
    stay locked until the complete inventory has been revalidated.
    """
    if (type(deadline) not in (int,float) or not math.isfinite(deadline)
        or type(max_total_bytes) is not int or not 1<=max_total_bytes<=MAX_TOTAL_BYTES
        or type(declaration) is not dict or type(aes_key) is not bytes or len(aes_key)!=16
        or type(xor_key) is not int or not 0<=xor_key<=255):_fail('attachment_declaration_invalid')
    for value in (chat_md5,cache_locator):
        if type(value) is not str or not re.fullmatch('[0-9a-f]{32}',value):_fail('attachment_declaration_invalid')
    size=declaration.get('size_bytes');md5=declaration.get('content_md5')
    if (type(size) is not int or not 1<=size<=MAX_FILE_BYTES or type(md5) is not str
        or not re.fullmatch('[0-9a-f]{32}',md5) or declaration.get('media_kind')!='image'
        or declaration.get('representation')!='message_image'): _fail('attachment_declaration_invalid')
    try:account=Path(account_path)
    except (TypeError,ValueError):raise DownloadError('attachment_path_changed') from None
    if not account.is_absolute() or not re.match(r'^[A-Za-z]:[\\/]',str(account)):_fail('attachment_path_changed')
    def check():
        if time.monotonic()>=deadline:_fail('attachment_deadline')
    check();held=ExitStack()
    try:
        import win32file
        inventory=_inventory(account,chat_md5,cache_locator,check)
        total=0;found=None
        for path,info in sorted(inventory[1].items()):
            check();size_on_disk=info[2];total+=size_on_disk
            if not 1<=size_on_disk<=MAX_FILE_BYTES or total>max_total_bytes:_fail('attachment_size_limit')
            handle=win32file.CreateFile(str(path),0x80000000,1,None,3,0x00200000|0x08000000,None)
            held.callback(handle.Close);before=_identity(handle)
            expected=ntpath.normcase(ntpath.normpath(str(path)))
            if before[-1]!=size_on_disk:_fail('attachment_file_changed')
            if _canonical(handle)!=expected or path.resolve(strict=True)!=path:_fail('attachment_path_changed')
            raw=_read_locked(handle,size_on_disk,check)
            if _identity(handle)!=before or _canonical(handle)!=expected:_fail('attachment_file_changed')
            try:data=decode_v2(raw,aes_key=aes_key,xor_key=xor_key,max_output_bytes=MAX_FILE_BYTES)
            except ContainerError:continue
            check()
            if len(data)!=size or hashlib.md5(data).hexdigest()!=md5:continue
            if found is not None and found.data!=data:_fail('attachment_ambiguous')
            found=VerifiedDownload(data,hashlib.sha256(data).hexdigest(),{
                'declared_size_matched':True,'declared_md5_matched':True,
                'held_without_write_delete_sharing':True,'file_id_128_verified':True,
                'canonical_path_verified':True,'observed_sha256':True,
                'image_container_decoded':True,'sender_authenticity_verified':False})
        check()
        if _inventory(account,chat_md5,cache_locator,check)!=inventory:_fail('attachment_path_changed')
        if found is None:_fail('attachment_not_downloaded')
        return found
    except DownloadError:raise
    except FileNotFoundError:_fail('attachment_not_downloaded')
    except Exception:raise DownloadError('attachment_file_unavailable') from None
    finally:
        try:held.close()
        except Exception:raise DownloadError('attachment_close_failed') from None
