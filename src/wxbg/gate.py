"""One short, version-pinned accessibility lease; no UI or input operations.

The supervisor process is the only writer. Callers must hold SessionMutex for
recovery, activation, the worker lifetime, and restoration. Importing this module
does not open a process or mutate the desktop.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import struct
import time
import uuid
from .path_config import ClientPathError, DEFAULT_DLL, DEFAULT_EXE, load_client_paths

EXPECTED_EXE = DEFAULT_EXE
EXPECTED_DLL = DEFAULT_DLL
EXPECTED_SHA = "e3240bf8a4d00593a4b3e6ce6c8b6ac26897622c27f410f6655c4eee17cb3b6d"
EXPECTED_DLL_SIZE = 196834352
EXPECTED_IMAGE_SIZE = 0xBC2E000
DATA_RVA = 180502528
DATA_SIZE = 9469208
GATE_RVA = 0xAD19668
CODE_RVA = 0x82A4E2
CODE = bytes.fromhex("4885c90f8455060000803d76f14e0a000f8448060000")
JOURNAL_NAME = "gate-lease.json"
FINAL_STATES = {"restored", "target_exited"}


class GateError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def atomic_json(path: Path, value: dict) -> None:
    """Flush a full new record before replacement; leave the old record on error."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class GateLease:
    def __init__(self, state_dir: Path, backend, target: dict):
        self.path = Path(state_dir) / JOURNAL_NAME
        self.backend = backend
        self.target = dict(target)
        self.record = None
        self.modified = False

    def activate(self) -> dict:
        identity = self.backend.validate(self.target)
        original = self.backend.read_byte(identity)
        if type(original) is not int or original not in (0, 1):
            raise GateError("NON_BOOLEAN_GATE", "Pinned accessibility byte is not boolean")
        self.record = {
            "schema": 1, "lease_id": uuid.uuid4().hex,
            "owner": self.backend.owner_identity(), "target": self.target,
            "identity": identity, "original": original, "applied": 1,
            "state": "prepared", "prepared_at": time.time(),
        }
        # This record exists durably before the first possible write, including
        # crashes between WriteProcessMemory and the active-state journal update.
        atomic_json(self.path, self.record)
        if original != 1:
            if self.backend.read_byte(identity) != original:
                raise GateError("OWNERSHIP_CONFLICT", "Gate changed before activation")
            self.modified = True
            self.backend.write_byte(identity, 1)
            if self.backend.read_byte(identity) != 1:
                raise GateError("WRITE_VERIFY_FAILED", "Gate activation readback failed")
        self.record.update(state="active", active_at=time.time())
        atomic_json(self.path, self.record)
        return dict(identity)

    def restore(self) -> dict:
        evidence = {"status": "not_modified", "restored": True,
                    "modified": self.modified, "errors": []}
        if self.record is None:
            return evidence
        evidence["lease_id"] = self.record["lease_id"]
        try:
            if not self.backend.target_alive(self.target):
                evidence.update(status="target_exited", restored=None)
            else:
                identity = self.backend.validate(self.target)
                if identity != self.record["identity"]:
                    raise GateError("IDENTITY_CHANGED", "Loaded target identity changed; restore refused")
                current = self.backend.read_byte(identity)
                original = self.record["original"]
                if current == original:
                    evidence.update(status="restored", restored=True, observed=current)
                elif self.modified and current == self.record["applied"]:
                    self.backend.write_byte(identity, original)
                    observed = self.backend.read_byte(identity)
                    if observed != original:
                        raise GateError("RESTORE_VERIFY_FAILED", "Gate restoration readback failed")
                    evidence.update(status="restored", restored=True, observed=observed)
                else:
                    raise GateError("OWNERSHIP_CONFLICT", "Gate changed outside the lease; restore refused")
        except Exception as exc:
            evidence.update(status="recovery_unknown", restored=False)
            evidence["errors"].append({"code": getattr(exc, "code", "RESTORE_FAILED"), "message": str(exc)})
        self.record.update(state=evidence["status"], cleanup=evidence, completed_at=time.time())
        try:
            atomic_json(self.path, self.record)
        except Exception as exc:
            evidence["errors"].append({"code": "JOURNAL_COMMIT_FAILED", "message": str(exc)})
            evidence["journal_committed"] = False
        else:
            evidence["journal_committed"] = True
        return evidence


def recover_pending(state_dir: Path, backend) -> dict:
    """Under the session mutex, recover only a dead owner's exact target instance."""
    path = Path(state_dir) / JOURNAL_NAME
    if not path.exists():
        return {"status": "none", "restored": None}
    try:
        if path.stat().st_size > 65536:
            raise ValueError("oversized journal")
        record = json.loads(path.read_text(encoding="utf-8"))
        schema = record.get("schema")
        if (schema not in (1, 2) or not isinstance(record.get("owner"), dict)
                or not isinstance(record.get("target"), dict)
                or not isinstance(record.get("identity"), dict)
                or type(record.get("original")) is not int or record["original"] not in (0, 1)
                or record.get("applied") != 1 or not isinstance(record.get("lease_id"), str)):
            raise ValueError("invalid lease schema")
        target = record["target"]
        if set(target) != {"pid", "created", "hwnd"}:
            raise ValueError("invalid target identity")
        identity = record["identity"]
        if (identity.get("sha256") != EXPECTED_SHA or identity.get("rva") != GATE_RVA
                or identity.get("image_size") != EXPECTED_IMAGE_SIZE
                or identity.get("dll_size") != EXPECTED_DLL_SIZE
                or type(identity.get("module_base")) is not int
                or identity.get("address") != identity["module_base"] + GATE_RVA):
            raise ValueError("journal does not match the compiled target manifest")
        if schema == 2:
            # A newer gateway can leave a fully settled lease in this shared
            # directory. Accept only its readback-proved final state; this
            # version cannot recover a pending schema-2 write safely.
            profile = record.get("profile_id")
            digest = record.get("profile_digest")
            cleanup = record.get("cleanup")
            state = record.get("state")
            if (type(profile) is not str or not 1 <= len(profile) <= 256
                    or "\0" in profile or identity.get("profile_id") != profile
                    or type(digest) is not str or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                    or identity.get("profile_digest") != digest
                    or state not in FINAL_STATES
                    or type(cleanup) is not dict
                    or cleanup.get("status") != state
                    or cleanup.get("errors") != []):
                raise ValueError("unverified schema-2 final lease")
            if state == "restored":
                if (cleanup.get("restored") is not True
                        or type(cleanup.get("observed")) is not int
                        or cleanup["observed"] != record["original"]):
                    raise ValueError("unverified schema-2 restoration")
            elif cleanup.get("restored") is not None:
                raise ValueError("unverified schema-2 target exit")
            if backend.target_alive(target):
                fresh = backend.validate(target)
                if (any(identity.get(key) != value for key, value in fresh.items())
                        or state == "restored"
                        and backend.read_byte(fresh) != record["original"]):
                    raise ValueError("schema-2 target readback mismatch")
    except Exception as exc:
        raise GateError("JOURNAL_INVALID", "Recovery journal rejected: " + str(exc)) from exc
    if record.get("state") in FINAL_STATES:
        return {"status": "already_settled", "restored": record.get("state") == "restored"}
    if backend.owner_alive(record["owner"]):
        raise GateError("ACTIVE_OWNER", "A live owner still owns the pending gate lease")
    # A reused PID or restarted Weixin is not the old process. Do not open it
    # for writes using the journal's address or original byte.
    if backend.target_alive(target):
        fresh = backend.validate(target)
        if fresh != identity:
            raise GateError("RECOVERY_IDENTITY_MISMATCH", "Pending lease does not match the loaded image")
    lease = GateLease(state_dir, backend, target)
    lease.record = record
    lease.modified = record["original"] != 1
    evidence = lease.restore()
    if evidence["status"] not in FINAL_STATES or evidence.get("errors"):
        raise GateError("RECOVERY_FAILED", json.dumps(evidence, ensure_ascii=True))
    return evidence


class WinBackend:
    """Windows implementation, initialized lazily; excludes all UIA/input APIs."""
    def __init__(self):
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise GateError("UNSUPPORTED_PLATFORM", "A 64-bit Windows Python process is required")
        try:
            self.client_paths = load_client_paths()
        except ClientPathError as exc:
            raise GateError("CLIENT_PATH_INVALID", "Invalid configured Weixin installation path") from exc
        import psutil
        import win32api
        import win32gui
        import win32process
        self.psutil = psutil
        self.api = win32api
        self.gui = win32gui
        self.proc = win32process
        self.handles = {}
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.ReadProcessMemory.restype = wintypes.BOOL
        self.kernel.WriteProcessMemory.argtypes = self.kernel.ReadProcessMemory.argtypes
        self.kernel.WriteProcessMemory.restype = wintypes.BOOL
        self.kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self.kernel.VirtualQueryEx.restype = ctypes.c_size_t
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.ProcessIdToSessionId.restype = wintypes.BOOL

    def session(self, pid):
        value = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(pid, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.value

    def owner_identity(self):
        import win32security
        token = win32security.OpenProcessToken(self.api.GetCurrentProcess(), 0x0008)
        try:
            sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        finally:
            token.Close()
        pid = os.getpid()
        return {"pid": pid, "created": self.psutil.Process(pid).create_time(),
                "session": self.session(pid), "sid": sid}

    def owner_alive(self, owner):
        try:
            return (self.psutil.Process(int(owner["pid"])).create_time() == owner["created"]
                    and self.session(int(owner["pid"])) == owner["session"])
        except self.psutil.NoSuchProcess:
            return False
        except Exception:
            # Failure to prove death does not authorize taking another lease.
            return True

    def discover(self):
        targets = []
        def consider(hwnd, _):
            if not self.gui.IsWindowVisible(hwnd) or self.gui.GetClassName(hwnd) != "Qt51514QWindowIcon":
                return
            # Fixed main-window titles exclude Qt popup/settings windows without
            # reading chat contents or activating the application.
            if self.gui.GetWindowText(hwnd) not in {"微信", "Weixin", "WeChat"}:
                return
            pid = self.proc.GetWindowThreadProcessId(hwnd)[1]
            try:
                process = self.psutil.Process(pid)
                if self._path(process.exe()) == self._path(self.client_paths.exe):
                    targets.append({"pid": pid, "created": process.create_time(), "hwnd": int(hwnd)})
            except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                return
        self.gui.EnumWindows(consider, None)
        if not targets:
            # EnumWindows only sees windows on this caller's desktop.  A
            # separate automation desktop is one possible cause, but absence
            # alone cannot prove it (the client may also be closed, logged
            # out, or have a different window identity).
            raise GateError(
                "TARGET_NOT_VISIBLE",
                "No matching Weixin main window is visible to the MCP process on its current desktop",
            )
        if len(targets) > 1:
            raise GateError("TARGET_AMBIGUOUS", "Multiple matching Weixin main windows are visible on this desktop")
        return targets[0]

    @staticmethod
    def _path(path):
        return os.path.normcase(os.path.realpath(path))

    def target_alive(self, target):
        try:
            process = self.psutil.Process(int(target["pid"]))
            return process.create_time() == target["created"] and process.is_running()
        except self.psutil.NoSuchProcess:
            return False

    def _handle(self, target):
        key = (target["pid"], target["created"])
        if key not in self.handles:
            if not self.target_alive(target):
                raise GateError("TARGET_EXITED", "Target process instance has exited")
            self.handles[key] = self.api.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008 | 0x00100000, False, target["pid"])
            if not self.target_alive(target):
                self.handles.pop(key).Close()
                raise GateError("TARGET_EXITED", "Target changed while opening the process")
        return self.handles[key]

    def _read(self, handle, address, size):
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(int(handle), address, buffer, size, ctypes.byref(count)) or count.value != size:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw

    def _page(self, handle, base, address):
        class MemoryInfo(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                        ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
        page = MemoryInfo()
        returned = self.kernel.VirtualQueryEx(int(handle), address, ctypes.byref(page), ctypes.sizeof(page))
        if returned != ctypes.sizeof(page):
            raise GateError("PAGE_QUERY_FAILED", "VirtualQueryEx returned incomplete page information")
        if (page.State != 0x1000 or page.Type != 0x1000000 or page.AllocationBase != base
                or not page.BaseAddress <= address < page.BaseAddress + page.RegionSize
                or page.Protect not in (0x04, 0x08)):
            raise GateError("UNSAFE_GATE_PAGE", "Gate is not committed writable non-executable image data")

    def validate(self, target):
        if set(target) != {"pid", "created", "hwnd"} or not self.target_alive(target):
            raise GateError("TARGET_EXITED", "Target process instance is unavailable")
        process = self.psutil.Process(target["pid"])
        if self._path(process.exe()) != self._path(self.client_paths.exe):
            raise GateError("TARGET_PATH_MISMATCH", "Unexpected executable")
        target_session = self.session(target["pid"])
        if target_session != self.session(os.getpid()):
            raise GateError("SESSION_MISMATCH", "Cross-session control is disabled")
        if (not self.gui.IsWindow(target["hwnd"])
                or self.proc.GetWindowThreadProcessId(target["hwnd"])[1] != target["pid"]
                or self.gui.GetClassName(target["hwnd"]) != "Qt51514QWindowIcon"):
            raise GateError("WINDOW_IDENTITY_MISMATCH", "Main window identity changed")
        # Read and hash the pinned file; never discover or scan for new offsets.
        with self.client_paths.dll.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != EXPECTED_DLL_SIZE:
                raise GateError("UNSUPPORTED_BINARY", "Pinned DLL size mismatch")
            header = stream.read(4096)
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != EXPECTED_SHA:
            raise GateError("UNSUPPORTED_BINARY", "Pinned DLL SHA-256 mismatch")
        self._check_pe(header)
        handle = self._handle(target)
        modules = [int(module) for module in self.proc.EnumProcessModules(handle)
                   if self._path(self.proc.GetModuleFileNameEx(handle, module)) == self._path(self.client_paths.dll)]
        if len(modules) != 1:
            raise GateError("LOADED_DLL_MISMATCH", "Exactly one pinned loaded module is required")
        base = modules[0]
        self._check_pe(self._read(handle, base, 4096))
        if self._read(handle, base + CODE_RVA, len(CODE)) != CODE:
            raise GateError("CODE_SIGNATURE_MISMATCH", "Loaded accessibility code signature mismatch")
        address = base + GATE_RVA
        self._page(handle, base, address)
        value = self._read(handle, address, 1)[0]
        if value not in (0, 1):
            raise GateError("NON_BOOLEAN_GATE", "Loaded gate byte is not boolean")
        return {**target, "sha256": digest, "module_base": base, "address": address,
                "rva": GATE_RVA, "image_size": EXPECTED_IMAGE_SIZE,
                "dll_size": EXPECTED_DLL_SIZE, "session": target_session}

    @staticmethod
    def _check_pe(header):
        try:
            if header[:2] != b"MZ":
                raise ValueError("not PE")
            pe = struct.unpack_from("<I", header, 60)[0]
            if header[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", header, pe + 4)[0] != 0x8664:
                raise ValueError("not AMD64 PE")
            count = struct.unpack_from("<H", header, pe + 6)[0]
            optional_size = struct.unpack_from("<H", header, pe + 20)[0]
            optional = pe + 24
            if struct.unpack_from("<H", header, optional)[0] != 0x20B:
                raise ValueError("not PE32+")
            if struct.unpack_from("<I", header, optional + 56)[0] != EXPECTED_IMAGE_SIZE:
                raise ValueError("image size mismatch")
            sections = []
            for index in range(count):
                offset = optional + optional_size + index * 40
                if header[offset:offset + 8].rstrip(b"\0") == b".data":
                    size, rva = struct.unpack_from("<II", header, offset + 8)
                    flags = struct.unpack_from("<I", header, offset + 36)[0]
                    sections.append((rva, size, flags))
            if (len(sections) != 1 or sections[0][:2] != (DATA_RVA, DATA_SIZE)
                    or not DATA_RVA <= GATE_RVA < DATA_RVA + DATA_SIZE
                    or not sections[0][2] & 0x80000000 or sections[0][2] & 0x20000000):
                raise ValueError("pinned writable non-executable data section mismatch")
        except (ValueError, struct.error, IndexError) as exc:
            raise GateError("PE_MANIFEST_MISMATCH", str(exc)) from exc

    def read_byte(self, identity):
        target = {key: identity[key] for key in ("pid", "created", "hwnd")}
        handle = self._handle(target)
        self._page(handle, identity["module_base"], identity["address"])
        return self._read(handle, identity["address"], 1)[0]

    def write_byte(self, identity, value):
        if type(value) is not int or value not in (0, 1):
            raise GateError("WRITE_REFUSED", "Only a boolean value is permitted")
        target = {key: identity[key] for key in ("pid", "created", "hwnd")}
        # Recompute rather than accept arbitrary addresses from a journal.
        if self.validate(target) != identity:
            raise GateError("IDENTITY_CHANGED", "Target changed before write")
        handle = self._handle(target)
        buffer = ctypes.create_string_buffer(bytes([value]))
        written = ctypes.c_size_t()
        if not self.kernel.WriteProcessMemory(int(handle), identity["address"], buffer, 1, ctypes.byref(written)) or written.value != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        if self.read_byte(identity) != value:
            raise GateError("WRITE_VERIFY_FAILED", "Runtime boolean write did not read back")

    def close(self):
        errors = []
        for handle in list(self.handles.values()):
            try:
                handle.Close()
            except Exception as exc:
                errors.append(str(exc))
        self.handles.clear()
        if errors:
            raise GateError("HANDLE_CLOSE_FAILED", "; ".join(errors))


class SessionMutex:
    """One Local-session/SID lease, acquired and released by the same thread."""
    def __init__(self, backend, timeout=2.0):
        self.backend = backend
        self.timeout = timeout
        self.handle = None
        self.owned = False

    def __enter__(self):
        import win32event
        import win32security
        import pywintypes
        owner = self.backend.owner_identity()
        sid_hash = hashlib.sha256(owner["sid"].encode("ascii")).hexdigest()[:20]
        name = "Local\\WxBg_" + str(owner["session"]) + "_" + sid_hash
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            "D:P(A;;GA;;;" + owner["sid"] + ")", win32security.SDDL_REVISION_1)
        self.handle = win32event.CreateMutex(attributes, False, name)
        waited = win32event.WaitForSingleObject(self.handle, max(0, int(self.timeout * 1000)))
        if waited not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            self.handle.Close()
            self.handle = None
            raise GateError("BUSY", "Another guardian owns this Windows-session gate lease")
        self.owned = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        import win32event
        try:
            if self.owned:
                win32event.ReleaseMutex(self.handle)
        finally:
            self.owned = False
            if self.handle is not None:
                self.handle.Close()
                self.handle = None
        return False
