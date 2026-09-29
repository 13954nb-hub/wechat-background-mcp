"""Version-pinned v4 selection driver; selection is not delivery proof.

Caller owns the guardian/session mutex, target hash/layout checks, background
monitor, journal, and VerifiedFile scope through any later Send. The deployed
DLL path must be immutable; this module never builds, copies or remotely
unloads a library. The packaged caller supplies the exact validated DLL.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import mmap
import ntpath
import os
from pathlib import Path
import re
import secrets
import time
from wxbg.policy import AdapterError

MAGIC, VERSION, DATA_SIZE = 0x57424134, 4, 736
MESSAGE = 'WxBg.AttachmentProbe.v4.8AB0B35C-A2EF-4F7C-ADDE-77A62178ECA7'
MAX_FILE_BYTES = 1024 * 1024
_RETAINED_TRANSPORTS = []


class Data(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in ('magic','version','size','command')] + [
        (n, ctypes.c_uint64) for n in ('nonce','request_deadline')] + [
        (n, ctypes.c_uint32) for n in ('pid','tid','kind','x','y','client_width','state','error','installed',
        'lease_code','protection_restored','matches','shows','live','resident','timer_expired',
        'restore_attempts','cleanup_unresolved','active_filter','cleanup_exhausted','accepted_shows',
        'selection_mode','grant_revoked','client_height')] + [
        (n, ctypes.c_uint64) for n in ('module','slot','current','original','replacement','lease_deadline','expected_size')] + [
        ('fixture_path', ctypes.c_wchar * 260), ('fixture_sha256', ctypes.c_ubyte * 32)]


def _path(value):
    value = os.fspath(value)
    if (not isinstance(value, str) or not re.match(r'^[A-Za-z]:\\', value)
            or len(value.encode('utf-16-le')) // 2 >= 260
            or any(ord(c) < 32 or c in ':*?"<>|' for c in value[2:])
            or any(p in ('.','..') or p.endswith((' ','.')) for p in value[3:].split('\\'))
            or '/' in value or not ntpath.basename(value)):
        raise AdapterError('invalid_local_file_path')
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise AdapterError('invalid_sha256')
    return value.lower()


def _fixture(value):
    if not isinstance(value, dict):
        raise AdapterError('invalid_fixture')
    path, size = _path(value.get('path', '')), value.get('size')
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES or value.get('name') != ntpath.basename(path):
        raise AdapterError('invalid_fixture')
    return dict(path=path, name=value['name'], size=size, sha256=_sha(value.get('sha256')))


def _attachment_geometry(point, size):
    """Accept measured client coordinates only, before opening the native hook."""
    if (type(point) is not tuple or len(point) != 2 or
            type(size) is not tuple or len(size) != 2 or
            any(type(v) is not int for v in (*point, *size))):
        raise AdapterError('invalid_attachment_layout')
    x, y = point
    width, height = size
    if (not 640 <= width <= 32767 or not 480 <= height <= 32767 or
            not 8 <= x <= width - 8 or not 8 <= y <= height - 8 or
            not 16 <= x <= width * 3 // 4 or
            y < height * 3 // 4):
        raise AdapterError('invalid_attachment_layout')
    return point, size


def _error(exc):
    return {'type': type(exc).__name__, 'code': getattr(exc, 'code', 'native_error'), 'detail': str(exc)[:1500]}


def _api(library, name, result, *args):
    fn = getattr(library, name); fn.restype = result; fn.argtypes = list(args)
    return fn


class _FileInformation(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
                ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
                ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
                ('links', wintypes.DWORD), ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]


class VerifiedFile:
    """A user-approved local path held FILE_SHARE_READ-only until scope exit.

    `with VerifiedFile(path, expected_sha256=...) as descriptor:` returns a dict
    with path/name/size/sha256. Hashing uses this held handle, not a second open.
    Approval is a caller responsibility, not inferred by this class.
    """
    def __init__(self, path, *, expected_sha256=None, expected_size=None):
        self.requested_path = _path(path)
        self.expected_sha256 = _sha(expected_sha256) if expected_sha256 is not None else None
        if expected_size is not None and (type(expected_size) is not int or not 0 <= expected_size <= MAX_FILE_BYTES):
            raise AdapterError('invalid_file_size')
        self.expected_size = expected_size
        self._handle = self._descriptor = self._k = None
        self._entered = False

    @property
    def closed(self):
        return self._handle is None

    @property
    def descriptor(self):
        if self.closed or self._descriptor is None:
            raise AdapterError('file_handle_not_held')
        return dict(self._descriptor)

    def _information(self):
        info = _FileInformation()
        if not self._k.GetFileInformationByHandle(self._handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    def __enter__(self):
        if self._entered:
            raise AdapterError('file_grant_already_used')
        self._entered = True
        k = self._k = ctypes.WinDLL('kernel32', use_last_error=True)
        _api(k,'GetDriveTypeW',wintypes.UINT,wintypes.LPCWSTR)
        _api(k,'CreateFileW',wintypes.HANDLE,wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
        _api(k,'GetFileType',wintypes.DWORD,wintypes.HANDLE)
        _api(k,'GetFileInformationByHandle',wintypes.BOOL,wintypes.HANDLE,ctypes.POINTER(_FileInformation))
        _api(k,'GetFinalPathNameByHandleW',wintypes.DWORD,wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD)
        _api(k,'ReadFile',wintypes.BOOL,wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p)
        _api(k,'CloseHandle',wintypes.BOOL,wintypes.HANDLE)
        try:
            if k.GetDriveTypeW(self.requested_path[:3]) not in (2,3,6):
                raise AdapterError('nonlocal_file')
            handle = k.CreateFileW(self.requested_path, 0x80000000, 1, None, 3, 0x00200000 | 0x08000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = handle
            before = self._information()
            size = (before.size_high << 32) | before.size_low
            if k.GetFileType(handle) != 1 or before.attributes & (0x10 | 0x400):
                raise AdapterError('nonregular_file')
            if size > MAX_FILE_BYTES or (self.expected_size is not None and size != self.expected_size):
                raise AdapterError('file_size_mismatch')
            final = ctypes.create_unicode_buffer(32768)
            length = k.GetFinalPathNameByHandleW(handle, final, len(final), 0)
            if not length or length >= len(final) or not final.value.startswith('\\\\?\\'):
                raise AdapterError('file_final_path_unverified')
            path = _path(final.value[4:])
            if k.GetDriveTypeW(path[:3]) not in (2,3,6):
                raise AdapterError('nonlocal_file')
            digest, total = hashlib.sha256(), 0
            while total < size:
                wanted = min(65536, size - total)
                buffer, received = ctypes.create_string_buffer(wanted), wintypes.DWORD()
                if not k.ReadFile(handle, buffer, wanted, ctypes.byref(received), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not 0 < received.value <= wanted:
                    raise AdapterError('file_read_length_changed')
                digest.update(buffer.raw[:received.value]); total += received.value
            after = self._information()
            if any(getattr(before, f) != getattr(after, f) for f in ('volume','index_high','index_low','size_high','size_low','attributes')):
                raise AdapterError('file_identity_changed')
            sha256 = digest.hexdigest()
            if self.expected_sha256 is not None and sha256 != self.expected_sha256:
                raise AdapterError('file_hash_mismatch')
            self._descriptor = dict(path=path, name=ntpath.basename(path), size=size, sha256=sha256)
            return self.descriptor
        except Exception as exc:
            try:
                self.close()
            except Exception as cleanup:
                exc.file_cleanup_error = str(cleanup)
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError('file_verification_failed', str(exc)) from exc

    def close(self):
        if self._handle is not None:
            if not self._k.CloseHandle(self._handle):
                raise AdapterError('file_handle_close_failed', str(ctypes.WinError(ctypes.get_last_error())))
            self._handle = None

    def __exit__(self, typ, exc, traceback):
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.file_cleanup_error = str(cleanup)
        return False


class _WinTransport:
    def __init__(self):
        self.library = self.hook = self.target = self._u = self._k = None

    @property
    def has_hook(self):
        return bool(self.hook)

    @staticmethod
    def _canonical(path):
        return os.path.normcase(os.path.realpath(path))

    def _check_target(self):
        import psutil
        import win32process
        if tuple(win32process.GetWindowThreadProcessId(self.target['hwnd'])) != (self.target['tid'],self.target['pid']):
            raise AdapterError('native_target_changed')
        if psutil.Process(self.target['pid']).create_time() != self.target['created']:
            raise AdapterError('native_process_instance_changed')

    def open(self, target, dll_path, expected_sha):
        if ctypes.sizeof(Data) != DATA_SIZE or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise AdapterError('native_abi_mismatch')
        self.target = dict(target)
        self._check_target()
        u = self._u = ctypes.WinDLL('user32',use_last_error=True)
        k = self._k = ctypes.WinDLL('kernel32',use_last_error=True)
        _api(u,'RegisterWindowMessageW',wintypes.UINT,wintypes.LPCWSTR)
        _api(u,'SetWindowsHookExW',wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.HINSTANCE,wintypes.DWORD)
        _api(u,'UnhookWindowsHookEx',wintypes.BOOL,wintypes.HANDLE)
        _api(u,'SendMessageTimeoutW',ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,ctypes.c_size_t,ctypes.c_ssize_t,wintypes.UINT,wintypes.UINT,ctypes.POINTER(ctypes.c_size_t))
        _api(k,'GetTickCount64',ctypes.c_uint64)
        _api(k,'FreeLibrary',wintypes.BOOL,wintypes.HMODULE)
        _api(k,'GetModuleFileNameW',wintypes.DWORD,wintypes.HMODULE,wintypes.LPWSTR,wintypes.DWORD)
        self.message = u.RegisterWindowMessageW(MESSAGE)
        if not self.message:
            raise ctypes.WinError(ctypes.get_last_error())
        self.library = ctypes.WinDLL(str(dll_path))
        loaded_path = ctypes.create_unicode_buffer(32768)
        length = k.GetModuleFileNameW(self.library._handle,loaded_path,len(loaded_path))
        if not length or length >= len(loaded_path) or self._canonical(loaded_path.value) != self._canonical(dll_path):
            raise AdapterError('native_loaded_path_mismatch')
        if hashlib.sha256(Path(dll_path).read_bytes()).hexdigest() != expected_sha:
            raise AdapterError('native_binary_changed')
        self._check_target()
        entry = ctypes.cast(self.library.WxBgAttachmentHook,ctypes.c_void_p)
        self.hook = u.SetWindowsHookExW(4,entry,self.library._handle,target['tid'])
        if not self.hook:
            raise ctypes.WinError(ctypes.get_last_error())

    def command(self, command, fixture=None, timeout_ms=3000,
                attachment_point=None, attachment_size=None):
        if not self.hook or command not in (1,3,4,5):
            raise AdapterError('native_command_refused')
        if command == 5:
            _attachment_geometry(attachment_point, attachment_size)
        elif attachment_point is not None or attachment_size is not None:
            raise AdapterError('invalid_attachment_layout')
        self._check_target()
        timeout_ms = min(3000,max(1,int(timeout_ms)))
        nonce = secrets.randbits(63) | 1
        memory = mmap.mmap(-1,DATA_SIZE,tagname=f'Local\\WxBgAttachment4.{self.target["pid"]}.{nonce:016x}')
        data = Data.from_buffer(memory)
        try:
            data.magic=MAGIC; data.version=VERSION; data.size=DATA_SIZE; data.command=command
            data.nonce=nonce; data.request_deadline=self._k.GetTickCount64()+timeout_ms
            data.pid=self.target['pid']; data.tid=self.target['tid']; data.kind=1
            if command == 5:
                data.x,data.y=attachment_point
                data.client_width,data.client_height=attachment_size
            if fixture is not None:
                data.fixture_path=fixture['path']; data.expected_size=fixture['size']
                data.fixture_sha256[:]=bytes.fromhex(fixture['sha256'])
            response=ctypes.c_size_t()
            ctypes.set_last_error(0)
            if not self._u.SendMessageTimeoutW(self.target['hwnd'],self.message,nonce,MAGIC,0x2|0x20,timeout_ms,ctypes.byref(response)):
                raise TimeoutError(f'native command {command} unconfirmed; winerror={ctypes.get_last_error()}')
            result={field:int(getattr(data,field)) for field,_ in Data._fields_ if field not in ('fixture_path','fixture_sha256')}
            if result['nonce'] != nonce:
                raise AdapterError('native_reply_nonce_mismatch')
            return result
        finally:
            del data
            memory.close()

    def close(self):
        errors=[]
        if self.hook:
            try:
                if not self._u.UnhookWindowsHookEx(self.hook):
                    errors.append('unhook: '+str(ctypes.WinError(ctypes.get_last_error())))
                else:
                    self.hook=None
            except Exception as exc:
                errors.append('unhook: '+str(exc))
        if self.library is not None and not self.hook:
            try:
                if not self._k.FreeLibrary(self.library._handle):
                    errors.append('local FreeLibrary failed')
                else:
                    self.library=None
            except Exception as exc:
                errors.append('local FreeLibrary: '+str(exc))
        if self.hook or self.library is not None:
            if self not in _RETAINED_TRANSPORTS:
                _RETAINED_TRANSPORTS.append(self)
        elif self in _RETAINED_TRANSPORTS:
            _RETAINED_TRANSPORTS.remove(self)
        return {'hook_removed':not self.hook,'local_module_released':self.library is None,'errors':errors}

    def module_present(self,target,path):
        import win32api,win32process,pywintypes
        for attempt in range(3):
            handle=win32api.OpenProcess(0x410,False,target['pid'])
            try:
                return any(self._canonical(win32process.GetModuleFileNameEx(handle,m))==self._canonical(path)
                           for m in win32process.EnumProcessModules(handle))
            except pywintypes.error as exc:
                if exc.args[0] not in (6,126,299) or attempt==2:
                    raise
                time.sleep(.01)
            finally:
                handle.Close()


class NativeAttachmentDriver:
    def __init__(self,target,dll_path,expected_sha,*,_transport=None,_clock=time.monotonic,
                 _sleep=time.sleep,poll_timeout=3.0,cleanup_timeout=3.0):
        self.target=dict(target); self.dll_path=Path(dll_path); self.expected_sha=expected_sha
        self.transport=_transport; self.clock=_clock; self.sleep=_sleep
        self.poll_timeout=min(3.0,max(.01,float(poll_timeout)))
        self.cleanup_timeout=min(3.0,max(.01,float(cleanup_timeout)))
        self._used=False

    def _validate(self,report,command,identity=None):
        if not isinstance(report,dict):
            raise AdapterError('native_reply_invalid')
        expected={'magic':MAGIC,'version':VERSION,'size':DATA_SIZE,'state':2,'command':command,
                  'pid':self.target['pid'],'tid':self.target['tid'],'kind':1,'error':0}
        if any(type(report.get(k)) is not int or report[k]!=v for k,v in expected.items()):
            raise AdapterError('native_reply_invalid',f'command={command}; error={report.get("error")}')
        for key in ('module','slot','original','replacement'):
            if type(report.get(key)) is not int or report[key]<=0 or (identity and report[key]!=identity[key]):
                raise AdapterError('native_address_identity_changed',key)
        for key in ('current','installed','protection_restored','matches','shows','accepted_shows','live','resident',
                    'active_filter','cleanup_unresolved','cleanup_exhausted','selection_mode','grant_revoked'):
            if type(report.get(key)) is not int or report[key]<0:
                raise AdapterError('native_reply_invalid',key)
        return report

    @staticmethod
    def _clean(report):
        return bool(report and report['installed']==0 and report['current']==report['original']
                    and report['protection_restored']==1 and report['cleanup_unresolved']==0
                    and report['cleanup_exhausted']==0 and report['active_filter']==0
                    and report['grant_revoked']==1 and report['live']==0)

    @staticmethod
    def _terminal(report):
        return bool(NativeAttachmentDriver._clean(report) and report['matches']==1 and report['accepted_shows']==1
                    and report['shows']==0 and report['selection_mode']==1 and report['resident']==1)

    def select_file(self,fixture,*,attachment_point=None,attachment_size=None):
        _attachment_geometry(attachment_point,attachment_size)
        if self._used:
            exc=AdapterError('outcome_unknown','driver is single-use; never automatically resend')
            exc.evidence={'passed':False,'primary_error':{'code':'driver_already_used'},'cleanup_errors':[]}
            raise exc
        self._used=True
        before=observed=final=primary=None
        cleanup_errors=[]; attempted=opened=False; remote=None
        detached={'hook_removed':False,'local_module_released':False}
        try:
            fixture=_fixture(fixture); _path(self.dll_path); self.expected_sha=_sha(self.expected_sha)
            if any(type(self.target.get(k)) is not int or self.target[k]<=0 for k in ('pid','tid','hwnd')):
                raise AdapterError('native_target_invalid')
            if type(self.target.get('created')) not in (int,float) or self.target['created']<=0:
                raise AdapterError('native_target_instance_missing')
            if hashlib.sha256(self.dll_path.read_bytes()).hexdigest()!=self.expected_sha:
                raise AdapterError('native_binary_hash_mismatch')
            if self.transport is None:
                self.transport=_WinTransport()
            opened=True
            self.transport.open(self.target,self.dll_path,self.expected_sha)
            before=self._validate(self.transport.command(1),1)
            if (before['current']!=before['original'] or before['installed'] or before['active_filter']
                    or before['live'] or before['cleanup_unresolved'] or before['cleanup_exhausted']):
                raise AdapterError('native_inspection_not_idle')
            attempted=True  # arm5 itself can queue a click, even if its reply times out.
            armed=self._validate(self.transport.command(
                5,fixture=fixture,attachment_point=attachment_point,
                attachment_size=attachment_size),5,before)
            expected_geometry = dict(zip(
                ('x','y','client_width','client_height'),
                (*attachment_point,*attachment_size)))
            if any(type(armed.get(key)) is not int or armed[key] != value
                   for key,value in expected_geometry.items()):
                raise AdapterError('native_layout_changed')
            if (armed['installed']!=1 or armed['current']!=armed['replacement'] or armed['active_filter']!=1
                    or armed['selection_mode']!=1 or armed['grant_revoked']!=0
                    or armed['protection_restored']!=1 or armed['resident']!=1):
                raise AdapterError('native_arm_unverified')
            deadline=self.clock()+self.poll_timeout
            while self.clock()<deadline:
                timeout=max(1,min(3000,int((deadline-self.clock())*1000)))
                observed=self._validate(self.transport.command(3,timeout_ms=timeout),3,before)
                if observed['matches']>1 or observed['accepted_shows']>1 or observed['shows']:
                    raise AdapterError('native_duplicate_or_cancelled_selection')
                if observed['matches']==1 and observed['accepted_shows']==1 and observed['live']==0:
                    break
                self.sleep(min(.05,max(0,deadline-self.clock())))
            else:
                raise AdapterError('native_selection_timeout','queued action unknown; no retry')
        except Exception as exc:
            primary=_error(exc)
        finally:
            if opened:
                if attempted and self.transport.has_hook:
                    deadline=self.clock()+self.cleanup_timeout
                    for attempt in range(3):
                        if self.clock()>=deadline:
                            break
                        try:
                            timeout=max(1,min(1000,int((deadline-self.clock())*1000)))
                            final=self._validate(self.transport.command(4,timeout_ms=timeout),4,before)
                            if self._clean(final):
                                break
                        except Exception as exc:
                            cleanup_errors.append({'stage':'restore',**_error(exc)})
                        self.sleep(min(.05,max(0,deadline-self.clock())))
                    if not self._clean(final):
                        cleanup_errors.append({'stage':'restore','code':'native_cleanup_unverified'})
                try:
                    detached=self.transport.close()
                    cleanup_errors.extend({'stage':'detach','detail':e} for e in detached.get('errors',[]))
                except Exception as exc:
                    cleanup_errors.append({'stage':'detach',**_error(exc)})
                try:
                    remote=self.transport.module_present(self.target,self.dll_path)
                    if attempted and remote is not True:
                        cleanup_errors.append({'stage':'module','code':'resident_module_missing'})
                except Exception as exc:
                    cleanup_errors.append({'stage':'module',**_error(exc)})
        released=bool(attempted and self._clean(final) and detached.get('hook_removed') is True
                      and detached.get('local_module_released') is True)
        passed=bool(primary is None and not cleanup_errors and released and self._terminal(final) and remote is True)
        report=dict(final or {})
        report.update(passed=passed,status='selected' if passed else 'unknown',released=released,
                      grant_revoked=bool(final and final['grant_revoked']==1),hook_removed=detached.get('hook_removed') is True,
                      local_module_released=detached.get('local_module_released') is True,remote_module_present=remote,
                      module_path=str(self.dll_path),submission_started=attempted,remote_receipt_verified=False,
                      primary_error=primary,cleanup_errors=cleanup_errors)
        if not passed:
            if primary is None:
                report['primary_error']={'code':'native_terminal_evidence_unverified'}
            detail=json.dumps({'primary_error':report['primary_error'],'cleanup_errors':cleanup_errors},ensure_ascii=True)
            exc=AdapterError('outcome_unknown',detail+'; never automatically resend'); exc.evidence=report
            raise exc
        return report
