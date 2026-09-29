"""Record desktop effects; never repair focus to disguise a failed operation."""
import ctypes
import threading
import time
from ctypes import wintypes
import win32gui
import win32process
import win32clipboard

user32=ctypes.WinDLL('user32',use_last_error=True)

class GUITHREADINFO(ctypes.Structure):
    _fields_=[('cbSize',wintypes.DWORD),('flags',wintypes.DWORD),('hwndActive',wintypes.HWND),('hwndFocus',wintypes.HWND),('hwndCapture',wintypes.HWND),('hwndMenuOwner',wintypes.HWND),('hwndMoveSize',wintypes.HWND),('hwndCaret',wintypes.HWND),('rcCaret',wintypes.RECT)]

user32.GetGUIThreadInfo.argtypes=[wintypes.DWORD,ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype=wintypes.BOOL

user32.SetThreadDpiAwarenessContext.argtypes=[wintypes.HANDLE]
user32.SetThreadDpiAwarenessContext.restype=wintypes.HANDLE
user32.GetThreadDpiAwarenessContext.argtypes=[]
user32.GetThreadDpiAwarenessContext.restype=wintypes.HANDLE
user32.AreDpiAwarenessContextsEqual.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
user32.AreDpiAwarenessContextsEqual.restype=wintypes.BOOL

PM_V2=-4


class MonitorError(RuntimeError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


class CursorDpiError(RuntimeError):
    """Safe error codes; never expose provider exception text as evidence."""
    def __init__(self,code,primary_error_code=None,cleanup_error_code=None):
        self.code=code
        self.primary_error_code=primary_error_code
        self.cleanup_error_code=cleanup_error_code
        super().__init__(code)


def _dpi_context_matches(expected):
    current=user32.GetThreadDpiAwarenessContext()
    return bool(current and user32.AreDpiAwarenessContextsEqual(current,expected))


def cursor_snapshot():
    """Read in PMv2 on the calling thread, restoring its exact prior context.

    No cursor, window, process DPI, target process, or system DPI is changed.
    Even GetPhysicalCursorPos varied with context in the owned native probe;
    the fixed context and explicit labels are required for comparisons.
    A read or restoration failure produces no cursor snapshot.
    """
    previous=None
    position=None
    primary=cleanup=None
    stage='cursor_dpi_enter_failed'
    try:
        previous=user32.SetThreadDpiAwarenessContext(PM_V2)
        if not previous:
            raise CursorDpiError(stage)
        stage='cursor_dpi_context_unverified'
        if not _dpi_context_matches(PM_V2):
            raise CursorDpiError(stage)
        stage='cursor_read_failed'
        position=win32gui.GetCursorPos()
        if (not isinstance(position,(tuple,list)) or len(position)!=2
                or any(type(value) is not int for value in position)):
            raise CursorDpiError(stage)
        stage='cursor_dpi_context_unverified'
        if not _dpi_context_matches(PM_V2):
            raise CursorDpiError(stage)
    except Exception:
        primary=stage
    finally:
        if previous:
            cleanup_stage='cursor_dpi_restore_failed'
            try:
                if not user32.SetThreadDpiAwarenessContext(previous):
                    cleanup='cursor_dpi_restore_failed'
                else:
                    cleanup_stage='cursor_dpi_restore_unverified'
                    if not _dpi_context_matches(previous):
                        cleanup=cleanup_stage
            except Exception:
                cleanup=cleanup_stage
    if primary or cleanup:
        raise CursorDpiError(cleanup or primary,primary,cleanup)
    return {'cursor':list(position),'cursor_api':'GetCursorPos',
            'cursor_dpi_context':'per_monitor_v2',
            'cursor_coordinate_space':'screen_coordinates_under_pm_v2'}

def visible_windows(pid):
    found=[]
    def consider(hwnd,_):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1]==pid:
            found.append(hwnd)
    win32gui.EnumWindows(consider,None)
    return sorted(found)

def snapshot(pid,hwnd):
    thread=win32process.GetWindowThreadProcessId(hwnd)[0]
    info=GUITHREADINFO();info.cbSize=ctypes.sizeof(info)
    if not user32.GetGUIThreadInfo(thread,ctypes.byref(info)):raise ctypes.WinError(ctypes.get_last_error())
    return {'foreground':win32gui.GetForegroundWindow(),**cursor_snapshot(),
            'clipboard_sequence':win32clipboard.GetClipboardSequenceNumber(),'minimized':bool(win32gui.IsIconic(hwnd)),
            'visible_windows':visible_windows(pid),'capture':int(info.hwndCapture or 0)}

class Monitor:
    def __init__(self,pid,hwnd,phase_recorder=None):
        self.pid=pid;self.hwnd=hwnd;self.phase_recorder=phase_recorder
        self.before=snapshot(pid,hwnd)
        self.samples=[];self.errors=[];self._phase_recording_failed=False
        self.stop_event=threading.Event()
        self.thread=threading.Thread(target=self._observe,daemon=True)
        self._record_sample(self.before)
    def _phase_flags(self,sample,monitor_error=False):
        before_windows=set(self.before['visible_windows'])
        return {'foreground_changed':sample['foreground']!=self.before['foreground'],
                'clipboard_changed':sample['clipboard_sequence']!=self.before['clipboard_sequence'],
                'cursor_changed':sample['cursor']!=self.before['cursor'],
                'target_restored':not sample['minimized'],
                'capture_observed':bool(sample['capture']),
                'new_visible_window':bool(set(sample['visible_windows'])-before_windows),
                'monitor_error':monitor_error}
    def _record_phase(self,flags):
        if self.phase_recorder is None:return
        try:self.phase_recorder.observe(flags)
        except Exception:
            self._phase_recording_failed=True
            if 'phase_recorder_observe_failed' not in self.errors:
                self.errors.append('phase_recorder_observe_failed')
    def _record_sample(self,sample):
        self._record_phase(self._phase_flags(sample))
    def _record_monitor_error(self):
        self._record_phase({'foreground_changed':False,'clipboard_changed':False,
                            'cursor_changed':False,'target_restored':False,
                            'capture_observed':False,'new_visible_window':False,
                            'monitor_error':True})
    def _observe(self):
        while not self.stop_event.wait(.01):
            try:
                sample=snapshot(self.pid,self.hwnd)
                self.samples.append(sample)
                self._record_sample(sample)
            except Exception as exc:
                self.errors.append(exc.code if isinstance(exc,CursorDpiError) else type(exc).__name__)
                self._record_monitor_error()
                return
    def start(self):self.thread.start();return self
    def stop(self):
        self.stop_event.set();self.thread.join(1)
        if self.thread.is_alive():
            self.errors.append('monitor_join_timeout')
            self._record_monitor_error()
            raise MonitorError('monitor_join_timeout')
        try:after=snapshot(self.pid,self.hwnd)
        except Exception as exc:
            self.errors.append(exc.code if isinstance(exc,CursorDpiError) else type(exc).__name__)
            self._record_monitor_error()
            raise
        self._record_sample(after);samples=self.samples+[after]
        new_windows=sorted({h for s in samples for h in s['visible_windows'] if h not in self.before['visible_windows']})
        focus_changed=any(s['foreground']!=self.before['foreground'] for s in samples)
        clipboard_changed=any(s['clipboard_sequence']!=self.before['clipboard_sequence'] for s in samples)
        cursor_changed=any(s['cursor']!=self.before['cursor'] for s in samples)
        restored_window=any(not s['minimized'] for s in samples)
        capture_observed=any(bool(s['capture']) for s in [self.before]+samples)
        evidence={'observations':len(samples),'poll_interval_ms':10,'before':self.before,'after':after,
                'foreground_changed':focus_changed,'clipboard_changed':clipboard_changed,'cursor_changed':cursor_changed,
                'new_visible_windows':new_windows,'target_restored':restored_window,
                'capture_observed':capture_observed,'monitor_errors':self.errors,
                'background_observation_passed':not(focus_changed or clipboard_changed or new_windows or restored_window or capture_observed or self.errors),
                'verification_limit':'10ms sampling cannot prove absence of shorter transients; cursor movement can be user activity'}
        if self.phase_recorder is not None:
            try:evidence['phase_summary']=self.phase_recorder.snapshot()
            except Exception:raise MonitorError('phase_recorder_snapshot_failed') from None
        return evidence
