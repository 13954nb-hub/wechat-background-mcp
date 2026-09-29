"""Worker-local image-key derivation with account/config lifetime checks.

The pinned client's config DWORD at +0x40 and account username define its V2
image key. The algorithm was independently checked against owned cache bytes;
reference semantics are attributed in THIRD_PARTY_READSTORE.md. Derived keys
are candidates until message checksum/size validation, never public results.
"""
from contextlib import contextmanager
from dataclasses import dataclass,field
import hashlib
import math
from pathlib import Path
import struct
import time
from .readstore_account import locate_account
from .readstore_locations import select_storage,discover_roots
from .readstore_process import ProcessReader

class ImageKeyError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)

@dataclass(frozen=True)
class ImageKeys:
    aes_key:bytes=field(repr=False)
    xor_key:int=field(repr=False)

@contextmanager
def image_keys(target,account_path,*,deadline):
    if type(deadline) not in (int,float) or not math.isfinite(deadline):
        raise ImageKeyError('attachment_image_key_unavailable')
    def check():
        if time.monotonic()>=deadline:raise ImageKeyError('attachment_deadline')
    check();body_failed=False
    try:
        with ProcessReader(target,deadline=deadline) as reader:
            account=locate_account(reader.read,reader.module_base,reader.module_size,deadline)
            location=select_storage(account.username,roots=discover_roots(deadline=deadline),deadline=deadline)
            if Path(location.account_path)!=Path(account_path):raise ImageKeyError('attachment_account_changed')
            cfg=reader.read(account.cfg_pointer+0x40,4)
            if type(cfg) is not bytes or len(cfg)!=4:raise ImageKeyError('attachment_image_key_unavailable')
            number=struct.unpack('<I',cfg)[0]
            aes=hashlib.md5((str(number)+account.username).encode('utf-8')).hexdigest()[:16].encode('ascii')
            check()
            try:yield ImageKeys(aes,number&255)
            except BaseException:
                body_failed=True
                raise
            check();reader.revalidate()
            if (locate_account(reader.read,reader.module_base,reader.module_size,deadline)!=account
                or reader.read(account.cfg_pointer+0x40,4)!=cfg):raise ImageKeyError('attachment_account_changed')
    except ImageKeyError:raise
    except Exception:
        if body_failed:raise
        raise ImageKeyError('attachment_image_key_unavailable') from None
