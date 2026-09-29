"""Bounded metadata-only UIA Notification/TextChanged subscription helper.

Target is {pid, hwnd, created}, with created in psutil.create_time() seconds.
The caller must call stop and inspect all cleanup flags before releasing its
own surrounding guard. This helper never operates a GUI control or a gate.
COM still marshals Notification BSTRs into the callback: ignoring them is not
equivalent to saying no text passed through this process.
"""
import math
import threading
import time

_LIVE = {}  # Keep pending owner/apartment/sinks alive until actual cleanup.


def _disable_auto_focus(client):
    client.AutoSetFocus = False
    if bool(client.AutoSetFocus):
        raise RuntimeError('cannot_disable_auto_focus')


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 30:
        raise ValueError('invalid_timeout')
    return float(value)


class EventWindow:
    def __init__(self, target, max_events=128, *, _backend_factory=None, _clock_ns=time.monotonic_ns):
        if (not isinstance(target, dict) or any(type(target.get(x)) is not int or target[x] <= 0 for x in ('pid', 'hwnd'))
                or type(target.get('created')) not in (int, float) or not math.isfinite(target['created']) or target['created'] <= 0):
            raise ValueError('invalid_target')
        if type(max_events) is not int or not 1 <= max_events <= 128:
            raise ValueError('invalid_max_events')
        self.target = {key: target[key] for key in ('pid', 'hwnd', 'created')}
        self.max_events = max_events
        self._factory = _backend_factory
        self._clock_ns = _clock_ns
        self._lock = threading.Lock(); self._wake = threading.Event(); self._ready = threading.Event()
        self._thread = None; self._state = 'new'; self._error = None; self._closing = False
        self._closing_ns = None; self._pending = []; self._refs_released = True
        self._events = []; self._received = 0; self._late = 0; self._overflow = 0; self._invalid = 0
        self._armed_ns = None; self._window_end_ns = None; self._startup = 0
        self._armed_counts = {'notification': 0, 'text': 0}; self._diagnostics = {}

    def start(self):
        with self._lock:
            if self._state != 'new': raise RuntimeError('already_started')
            self._state = 'starting'; self._refs_released = False
        if self._factory is None:
            try:
                # Import comtypes on the calling thread, as normal Python COM
                # code does. No UIA client/root/pointer is constructed here.
                # This avoids first-import apartment initialization occurring
                # accidentally inside the new MTA owner. Existing main STA
                # adapters keep their own independent UIA objects.
                import comtypes.client
                from comtypes.gen import UIAutomationClient
                self._factory = _WindowsBackend
            except Exception:
                with self._lock:
                    self._state = 'stopped'; self._error = 'bootstrap_failed'; self._refs_released = True
                self._ready.set()
                return self
        _LIVE[id(self)] = self
        self._thread = threading.Thread(target=self._run, name='BoundedUiaEventOwner', daemon=True)
        self._thread.start()
        return self

    def wait_ready(self, timeout):
        self._ready.wait(_timeout(timeout))
        with self._lock: return self._state == 'ready' and not self._closing

    def arm(self, timeout_seconds):
        """Begin one exclusive-end interval after both subscriptions are ready."""
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError('invalid_timeout')
        with self._lock:
            if self._state != 'ready' or self._closing or self._armed_ns is not None:
                raise RuntimeError('cannot_arm')
            self._armed_ns = self._clock_ns()
            self._window_end_ns = self._armed_ns + timeout_seconds * 1_000_000_000
        return self.snapshot()

    def _publish(self, kind, event_id):
        # No provider string, VARIANT, sender pointer or property is retained.
        with self._lock:
            if (type(kind) is not str or type(event_id) is not int
                    or (kind, event_id) not in (('notification', 20035), ('text', 20015))):
                self._invalid += 1; return
            now = self._clock_ns()
            self._received += 1
            if self._closing or (self._window_end_ns is not None and now >= self._window_end_ns):
                self._late += 1; return
            if self._armed_ns is None:
                self._startup += 1; return
            self._armed_counts[kind] += 1
            if len(self._events) >= self.max_events:
                self._overflow += 1; return
            self._events.append({'kind': kind, 'event_id': event_id,
                                 'time_ns': now, 'thread_id': threading.get_native_id()})

    def _close_publication(self):
        with self._lock:
            self._closing = True
            if self._closing_ns is None:
                now = self._clock_ns()
                self._closing_ns = min(now, self._window_end_ns) if self._window_end_ns is not None else now

    def stop(self, timeout=5):
        timeout = _timeout(timeout)
        self._close_publication()
        self._wake.set()
        thread = self._thread
        if thread is None:
            with self._lock: self._state = 'stopped'; self._refs_released = True
        elif thread is not threading.current_thread():
            thread.join(timeout)
        return self.snapshot()

    def snapshot(self):
        with self._lock:
            owner_exited = self._thread is None or not self._thread.is_alive()
            return {'state': self._state, 'ready': self._state == 'ready' and not self._closing,
                    'error_code': self._error, 'closing': self._closing, 'closing_ns': self._closing_ns,
                    'owner_exited': owner_exited, 'registrations_removed': not self._pending,
                    'handler_refs_released': self._refs_released,
                    'cleanup_pending': self._closing and (not owner_exited or bool(self._pending) or not self._refs_released),
                    'pending_registrations': list(self._pending), 'received': self._received,
                    'late_dropped': self._late, 'overflow_dropped': self._overflow, 'invalid_dropped': self._invalid,
                    'armed': self._armed_ns is not None, 'armed_ns': self._armed_ns,
                    'window_end_ns': self._window_end_ns, 'startup_dropped': self._startup,
                    'armed_counts': dict(self._armed_counts), 'max_events': self.max_events,
                    'events': [dict(x) for x in self._events], 'owner_diagnostics': dict(self._diagnostics)}

    def _run(self):
        backend = None; registered = []; released_owned_refs = False
        phase = 'open'
        try:
            backend = self._factory()
            diagnostics = backend.open(dict(self.target), self._publish)
            with self._lock:
                self._diagnostics = {k: v for k, v in diagnostics.items()
                                     if k in ('owner_tid', 'owner_mta', 'owner_windowless', 'identity_verified', 'auto_focus_disabled') and type(v) in (bool, int)}
            phase = 'subscribe'
            for kind in ('notification', 'text'):
                if self._closing: break
                backend.add(kind); registered.append(kind)
                with self._lock: self._pending = list(registered)
            with self._lock:
                self._state = 'stopping' if self._closing else 'ready'
        except Exception:
            with self._lock:
                self._error = 'open_failed' if phase == 'open' else 'subscribe_failed'
                self._state = 'stopping'
            self._close_publication(); self._wake.set()
        self._ready.set()
        if not self._closing: self._wake.wait()
        self._close_publication()
        if backend is None:
            with self._lock: self._refs_released = True; self._state = 'stopped'
            _LIVE.pop(id(self), None)
            return
        # Every backend/COM operation, including retries and release, stays on
        # this same MTA owner. Never hold the callback lock across these calls.
        while True:
            self._wake.clear()
            pending = []
            for kind in reversed(registered):
                try: backend.remove(kind)
                except Exception: pending.append(kind)
            registered = list(reversed(pending))
            with self._lock:
                self._pending = list(registered); self._state = 'cleanup_pending'
            if registered:
                self._wake.wait()  # Explicit subsequent stop() retries removal.
                continue
            try:
                if not released_owned_refs:
                    backend.release_handler_refs(); released_owned_refs = True
                if not backend.refs_released():
                    self._wake.wait(.05)
                    continue
                with self._lock: self._refs_released = True
                backend.close()
                with self._lock: self._state = 'stopped'
                _LIVE.pop(id(self), None)
                return
            except Exception:
                self._wake.wait()  # Retain owner/COM state; caller sees pending.


class _WindowsBackend:
    def __init__(self):
        self.initialized = False; self.handle = None; self.client = None; self.client5 = None
        self.root = None; self.sink = None; self.note_pointer = None; self.text_pointer = None
        self.final = threading.Event()

    def open(self, target, emit):
        import ctypes
        from ctypes import wintypes
        import comtypes
        import comtypes.client
        from comtypes.gen import UIAutomationClient as U
        import psutil
        self.ctypes = ctypes; self.comtypes = comtypes; self.U = U; self.psutil = psutil; self.target = target
        comtypes.CoInitializeEx(0); self.initialized = True
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        ole = ctypes.OleDLL('ole32'); ole.CoGetApartmentType.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        apt = ctypes.c_int(-9); qualifier = ctypes.c_int(-9)
        ole.CoGetApartmentType(ctypes.byref(apt), ctypes.byref(qualifier))
        if apt.value != 1: raise RuntimeError('not_mta')
        self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, target['pid'])
        if not self.handle: raise RuntimeError('identity_unavailable')
        self._verify_native()
        self.client = comtypes.client.CreateObject(U.CUIAutomation8, interface=U.IUIAutomation2)
        _disable_auto_focus(self.client)
        self.client.ConnectionTimeout = 2000; self.client.TransactionTimeout = 2000
        self.client5 = self.client.QueryInterface(U.IUIAutomation5)
        self.root = self.client.ElementFromHandle(target['hwnd'])
        self._verify_uia()
        windows = []; callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumThreadWindows.argtypes = [wintypes.DWORD, callback_type, wintypes.LPARAM]
        callback = callback_type(lambda hwnd, _: (windows.append(int(hwnd)), True)[1])
        self.user.EnumThreadWindows(threading.get_native_id(), callback, 0)
        if windows: raise RuntimeError('owner_has_window')
        final = self.final
        class Sink(comtypes.COMObject):
            _com_interfaces_ = [U.IUIAutomationNotificationEventHandler, U.IUIAutomationEventHandler]
            def HandleNotificationEvent(self, this, sender, kind, processing, display, activity):
                emit('notification', 20035)
                return 0
            def HandleAutomationEvent(self, this, sender, event_id):
                emit('text', event_id)
                return 0
            def _final_release_(self): final.set()
        self.sink = Sink()
        self.note_pointer = self.sink.QueryInterface(U.IUIAutomationNotificationEventHandler)
        self.text_pointer = self.sink.QueryInterface(U.IUIAutomationEventHandler)
        return {'owner_tid': threading.get_native_id(), 'owner_mta': True, 'owner_windowless': True,
                'identity_verified': True, 'auto_focus_disabled': True}

    def _verify_native(self):
        from ctypes import wintypes
        c = self.ctypes; target = self.target
        pid = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(target['hwnd'], c.byref(pid)) or pid.value != target['pid']:
            raise RuntimeError('native_pid_mismatch')
        if self.kernel.WaitForSingleObject(self.handle, 0) != 258: raise RuntimeError('process_exited')
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(c.byref(x) for x in times)):
            raise RuntimeError('creation_unavailable')
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        native_created = ticks / 10000000.0 - 11644473600.0
        if abs(native_created - target['created']) > .00001 or self.psutil.Process(target['pid']).create_time() != target['created']:
            raise RuntimeError('creation_mismatch')

    def _verify_uia(self):
        self._verify_native()
        if self.root.CurrentProcessId != self.target['pid'] or int(self.root.CurrentNativeWindowHandle) != self.target['hwnd']:
            raise RuntimeError('uia_identity_mismatch')
        self._verify_native()

    def add(self, kind):
        self._verify_uia()
        if kind == 'notification':
            self.client5.AddNotificationEventHandler(self.root, self.U.TreeScope_Subtree, None, self.note_pointer)
        else:
            self.client.AddAutomationEventHandler(20015, self.root, self.U.TreeScope_Subtree, None, self.text_pointer)

    def remove(self, kind):
        if kind == 'notification': self.client5.RemoveNotificationEventHandler(self.root, self.note_pointer)
        else: self.client.RemoveAutomationEventHandler(20015, self.root, self.text_pointer)

    def release_handler_refs(self):
        self.note_pointer = None; self.text_pointer = None

    def refs_released(self):
        return self.sink is None or (self.final.is_set() and self.sink._refcnt.value == 0
                                    and self.sink not in self.comtypes.COMObject._instances_)

    def close(self):
        self.root = None; self.client5 = None; self.client = None; self.sink = None
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle = None
        if self.initialized:
            self.comtypes.CoUninitialize(); self.initialized = False
