"""Read bounded, checksum-bound account cache bytes through locked Windows handles."""
from dataclasses import dataclass,field
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import math
import ntpath
import os
from pathlib import Path
import re
import stat
import time

MAX_FILE_BYTES=16*1024*1024
MAX_TOTAL_BYTES=64*1024*1024


class DownloadError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


@dataclass(frozen=True)
class VerifiedDownload:
    data:bytes=field(repr=False)
    sha256:str
    evidence:dict


def _fail(code):raise DownloadError(code)


def _leaf(name):
    if (type(name) is not str or not name or name in ('.','..') or name[-1] in '. '
            or any(ord(c)<32 or c in '<>:"/\\|?*' for c in name)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?',name,re.I)):
        _fail('attachment_declaration_invalid')
    try:
        if len(name.encode('utf-16-le'))>510:_fail('attachment_declaration_invalid')
    except UnicodeError:_fail('attachment_declaration_invalid')
    return name


def _directory(path):
    info=path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
            or path.resolve(strict=True)!=path):_fail('attachment_path_changed')
    return info.st_dev,info.st_ino


def _canonical(handle):
    import win32file
    value=win32file.GetFinalPathNameByHandle(handle,0)
    if not value.startswith('\\\\?\\') or value.startswith('\\\\?\\UNC\\'):_fail('attachment_path_changed')
    return ntpath.normcase(ntpath.normpath(value[4:]))


def _identity(handle):
    import win32file
    class FileIdInfo(ctypes.Structure):
        _fields_=[('volume',ctypes.c_ulonglong),('file_id',ctypes.c_ubyte*16)]
    api=ctypes.WinDLL('kernel32',use_last_error=True).GetFileInformationByHandleEx
    api.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    api.restype=wintypes.BOOL
    value=FileIdInfo()
    if not api(int(handle),18,ctypes.byref(value),ctypes.sizeof(value)):_fail('attachment_identity_unavailable')
    info=win32file.GetFileInformationByHandle(handle)
    if info[0]&(0x10|0x400):_fail('attachment_path_changed')
    return (value.volume,bytes(value.file_id),info[0],info[3],(info[5]<<32)|info[6])


def _read_locked(handle,size,check):
    import win32file
    parts=[];remaining=size
    while remaining:
        check();_,data=win32file.ReadFile(handle,min(65536,remaining))
        if not data or len(data)>remaining:_fail('attachment_file_changed')
        parts.append(data);remaining-=len(data)
    return b''.join(parts)


def _inventory(account,name,check):
    directories={p:_directory(p) for p in (account,account/'msg',account/'msg/file')}
    base=account/'msg/file';files={}
    with os.scandir(base) as entries:
        for index,entry in enumerate(entries):
            check()
            if index>=256:_fail('attachment_size_limit')
            if re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])',entry.name) is None:continue
            month=base/entry.name;directories[month]=_directory(month)
            path=month/name
            try:info=path.lstat()
            except FileNotFoundError:continue
            if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:_fail('attachment_path_changed')
            files[path]=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
    return directories,files


def read_downloaded_file(account_path,declaration,*,deadline,max_total_bytes=MAX_TOTAL_BYTES):
    """Internal account path only; match exact leaf, declared size and client MD5.

    MD5 matches the message's format checksum, not sender authenticity. SHA256
    identifies observed bytes. No caller paths, recursive walk or remote access.
    Every successful file handle excludes write/delete sharing during the read.
    """
    if (type(deadline) not in (int,float) or not math.isfinite(deadline)
            or type(max_total_bytes) is not int or not 1<=max_total_bytes<=MAX_TOTAL_BYTES
            or type(declaration) is not dict):_fail('attachment_declaration_invalid')
    def check():
        if time.monotonic()>=deadline:_fail('attachment_deadline')
    check()
    if set(declaration)!={'filename','size_bytes','content_md5','media_kind','representation'}:_fail('attachment_declaration_invalid')
    name=_leaf(declaration['filename']);size=declaration['size_bytes'];checksum=declaration['content_md5']
    if (type(size) is not int or not 1<=size<=MAX_FILE_BYTES or type(checksum) is not str
            or re.fullmatch('[0-9a-f]{32}',checksum) is None or declaration['media_kind']!='file'
            or declaration['representation']!='original'):_fail('attachment_declaration_invalid')
    account=Path(account_path)
    if not account.is_absolute() or re.match(r'^[A-Za-z]:[\\/]',str(account)) is None:_fail('attachment_path_changed')
    held=ExitStack()
    try:
        import win32file
        inventory=_inventory(account,name,check)
        candidates=[p for p,info in inventory[1].items() if info[2]==size]
        total=0;found=None
        for path in sorted(candidates):
            check();total+=size
            if total>max_total_bytes:_fail('attachment_size_limit')
            handle=win32file.CreateFile(str(path),0x80000000,1,None,3,0x00200000|0x08000000,None)
            held.callback(handle.Close)
            before=_identity(handle)
            if before[-1]!=size:_fail('attachment_file_changed')
            expected=ntpath.normcase(ntpath.normpath(str(path)))
            if _canonical(handle)!=expected or path.resolve(strict=True)!=path:_fail('attachment_path_changed')
            data=_read_locked(handle,size,check)
            if _identity(handle)!=before or _canonical(handle)!=expected:_fail('attachment_file_changed')
            if hashlib.md5(data).hexdigest()!=checksum:continue
            if found is not None:_fail('attachment_ambiguous')
            found=VerifiedDownload(data,hashlib.sha256(data).hexdigest(),{
                'declared_size_matched':True,'declared_md5_matched':True,
                'held_without_write_delete_sharing':True,'file_id_128_verified':True,
                'canonical_path_verified':True,'observed_sha256':True,
                'sender_authenticity_verified':False})
        check()
        if _inventory(account,name,check)!=inventory:_fail('attachment_path_changed')
        if found is None:_fail('attachment_not_downloaded')
        return found
    except DownloadError:raise
    except FileNotFoundError:_fail('attachment_not_downloaded')
    except Exception:raise DownloadError('attachment_file_unavailable') from None
    finally:
        try:held.close()
        except Exception:raise DownloadError('attachment_close_failed') from None
