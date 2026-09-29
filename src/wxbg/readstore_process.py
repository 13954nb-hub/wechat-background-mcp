"""Bounded, read-only Windows process reader for the readstore worker.

This module owns no client discovery and performs no writes.  The caller pins
the process identity (PID, creation time and main-window handle); this reader
opens that exact instance with ``PROCESS_QUERY_INFORMATION | PROCESS_VM_READ``
and rechecks the identity around every memory-region query.  A caller can use
the returned ``module_base``/``module_size`` to locate data in the already
validated image without accepting an address from an untrusted source.

The Windows backend is imported and initialized only when a reader is entered
without an injected backend.  Tests and non-Windows callers can therefore use
the same small interface without importing pywin32 or touching native APIs.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import math
import os
from pathlib import Path
import struct
import time

from .gate import DATA_RVA, DATA_SIZE, EXPECTED_DLL_SIZE
from .gate import EXPECTED_IMAGE_SIZE, EXPECTED_SHA
from .path_config import ClientPathError, load_client_paths


READ_ACCESS = 0x0410  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
MAX_ADDRESS = 0x800000000000
MAX_READ_SIZE = 1024 * 1024
MAX_REGIONS = 200000
READABLE_PROTECTS = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100


class _MODULEINFO(ctypes.Structure):
    """PSAPI MODULEINFO layout on the 64-bit Windows process we require."""

    _fields_ = [("lpBaseOfDll", ctypes.c_void_p),
                ("SizeOfImage", wintypes.DWORD),
                ("EntryPoint", ctypes.c_void_p)]


class _SYSTEM_INFO(ctypes.Structure):
    """64-bit SYSTEM_INFO prefix/layout used by GetNativeSystemInfo."""

    _fields_ = [("wProcessorArchitecture", wintypes.WORD),
                ("wReserved", wintypes.WORD),
                ("dwPageSize", wintypes.DWORD),
                ("lpMinimumApplicationAddress", ctypes.c_void_p),
                ("lpMaximumApplicationAddress", ctypes.c_void_p),
                ("dwActiveProcessorMask", ctypes.c_size_t),
                ("dwNumberOfProcessors", wintypes.DWORD),
                ("dwProcessorType", wintypes.DWORD),
                ("dwAllocationGranularity", wintypes.DWORD),
                ("wProcessorLevel", wintypes.WORD),
                ("wProcessorRevision", wintypes.WORD)]


class ProcessReaderError(RuntimeError):
    """Stable, non-sensitive failure code for the read-only reader."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _path_key(value) -> str:
    try:
        # casefold keeps comparisons strict on Windows while making fake
        # backends deterministic when the tests run on a non-Windows host.
        return os.path.normcase(os.path.realpath(os.fspath(value))).casefold()
    except (TypeError, ValueError, OSError):
        return ""


def _check_pe_header(header: bytes, *, image_size: int,
                    data_rva: int, data_size: int) -> None:
    """Validate the pinned AMD64 image header without following pointers."""
    try:
        if type(header) is not bytes or len(header) < 64 or header[:2] != b"MZ":
            raise ValueError
        pe = struct.unpack_from("<I", header, 60)[0]
        if pe < 64 or pe + 24 > len(header) or header[pe:pe + 4] != b"PE\0\0":
            raise ValueError
        if struct.unpack_from("<H", header, pe + 4)[0] != 0x8664:
            raise ValueError
        count = struct.unpack_from("<H", header, pe + 6)[0]
        optional_size = struct.unpack_from("<H", header, pe + 20)[0]
        optional = pe + 24
        if not 1 <= count <= 96 or optional_size < 60 or optional + optional_size > len(header):
            raise ValueError
        if struct.unpack_from("<H", header, optional)[0] != 0x20B:
            raise ValueError
        if struct.unpack_from("<I", header, optional + 56)[0] != image_size:
            raise ValueError
        section_table_end = optional + optional_size + count * 40
        if section_table_end > len(header):
            raise ValueError
        sections = []
        for index in range(count):
            offset = optional + optional_size + index * 40
            if header[offset:offset + 8].rstrip(b"\0") == b".data":
                virtual_size, virtual_address = struct.unpack_from("<II", header, offset + 8)
                flags = struct.unpack_from("<I", header, offset + 36)[0]
                sections.append((virtual_address, virtual_size, flags))
        if (len(sections) != 1 or sections[0][:2] != (data_rva, data_size)
                or not 0 < data_rva <= data_rva + data_size <= image_size
                or not sections[0][2] & 0x80000000
                or sections[0][2] & 0x20000000):
            raise ValueError
    except (ValueError, IndexError, struct.error, TypeError, OverflowError) as exc:
        raise ProcessReaderError("pe_invalid") from exc


class ProcessReader:
    """Read a caller-pinned process through a minimal bounded interface.

    ``target`` must contain exactly ``pid``, ``created`` and ``hwnd``.  The
    optional backend is deliberately structural: it supplies process/window
    identity, ``open_process``/``close_process``, module enumeration, bounded
    file metadata, ``read_process_memory`` and ``query_regions``.  The public
    ``read`` method returns an exact byte string or ``None`` when Windows
    cannot provide a complete read; it never returns partial bytes.
    """

    def __init__(self, target: dict, *, deadline: float, backend=None,
                 expected_exe=None, expected_dll=None,
                 expected_sha: str = EXPECTED_SHA,
                 expected_dll_size: int = EXPECTED_DLL_SIZE,
                 expected_image_size: int = EXPECTED_IMAGE_SIZE,
                 expected_data_rva: int = DATA_RVA,
                 expected_data_size: int = DATA_SIZE,
                 max_read_size: int = MAX_READ_SIZE,
                 max_regions: int = MAX_REGIONS):
        if (type(target) is not dict or set(target) != {"pid", "created", "hwnd"}
                or type(target.get("pid")) is not int or target["pid"] <= 0
                or type(target.get("created")) not in (int, float)
                or not math.isfinite(target["created"])
                or type(target.get("hwnd")) is not int or target["hwnd"] <= 0):
            raise ProcessReaderError("target_invalid")
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or type(expected_sha) is not str or len(expected_sha) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in expected_sha)
                or type(expected_dll_size) is not int or expected_dll_size <= 0
                or type(expected_image_size) is not int or expected_image_size <= 0
                or type(expected_data_rva) is not int or expected_data_rva <= 0
                or type(expected_data_size) is not int or expected_data_size <= 0
                or type(max_read_size) is not int or not 0 < max_read_size <= MAX_READ_SIZE
                or type(max_regions) is not int or not 0 < max_regions <= MAX_REGIONS):
            raise ProcessReaderError("reader_input_invalid")
        if expected_exe is None or expected_dll is None:
            try:
                client_paths = load_client_paths()
            except ClientPathError as exc:
                raise ProcessReaderError("client_path_invalid") from exc
            if expected_exe is None:
                expected_exe = client_paths.exe
            if expected_dll is None:
                expected_dll = client_paths.dll
        self.target = dict(target)
        self.deadline = float(deadline)
        self.backend = backend
        self.expected_exe = expected_exe
        self.expected_dll = expected_dll
        self.expected_sha = expected_sha.lower()
        self.expected_dll_size = expected_dll_size
        self.expected_image_size = expected_image_size
        self.expected_data_rva = expected_data_rva
        self.expected_data_size = expected_data_size
        self.max_read_size = max_read_size
        self.max_regions = max_regions
        self._handle = None
        self._snapshot = None
        self.module_base = None
        self.module_size = None
        self._last_read_error = None

    def __enter__(self):
        if self._handle is not None:
            raise ProcessReaderError("already_open")
        self._check_deadline()
        if self.backend is None:
            self.backend = _WindowsBackend()
        try:
            self._validate_identity()
            pinned = self._validate_pinned_file()
            self._check_deadline()
            try:
                handle = self.backend.open_process(self.target["pid"], READ_ACCESS)
            except PermissionError as exc:
                raise ProcessReaderError("access_denied") from exc
            except OSError as exc:
                raise ProcessReaderError("access_denied") from exc
            if handle is None:
                raise ProcessReaderError("access_denied")
            self._handle = handle
            self._validate_identity()
            self._snapshot = self._validate_loaded(pinned)
            self.module_base = self._snapshot["module_base"]
            self.module_size = self._snapshot["module_size"]
            return self
        except ProcessReaderError as exc:
            self._abort_open(exc)
            raise
        except (PermissionError, OSError) as exc:
            self._abort_open(exc)
            raise ProcessReaderError("reader_failed") from exc

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self) -> None:
        """Close the exact read-only handle; repeated close is harmless."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self.backend.close_process(handle)
        except Exception as exc:
            raise ProcessReaderError("cleanup_failed") from exc

    def read(self, address: int, size: int):
        """Return an exact read or ``None``; never return partial bytes."""
        self._require_open()
        self._check_deadline()
        if (type(address) is not int or type(size) is not int
                or not 0x10000 <= address < MAX_ADDRESS
                or not 0 < size <= self.max_read_size
                or address + size > MAX_ADDRESS):
            raise ProcessReaderError("read_invalid")
        try:
            value = self.backend.read_process_memory(self._handle, address, size)
        except (PermissionError, OSError):
            self._last_read_error = "read_failed"
            return None
        self._check_deadline()
        # A process can be reused while ReadProcessMemory is in flight.  Check
        # the identity once more before exposing the result to the caller.
        self._validate_identity()
        if type(value) is not bytes or len(value) != size:
            self._last_read_error = "partial_read"
            return None
        self._last_read_error = None
        return value

    def regions(self):
        """Return fresh, sorted readable committed regions bounded by MAX_ADDRESS."""
        self._require_open()
        self._check_deadline()
        self._validate_identity()
        try:
            raw_regions = iter(self.backend.query_regions(self._handle))
        except (PermissionError, OSError) as exc:
            raise ProcessReaderError("regions_failed") from exc
        except Exception as exc:
            raise ProcessReaderError("regions_failed") from exc
        normalized = []
        count = 0
        try:
            for raw in raw_regions:
                self._check_deadline()
                count += 1
                if count > self.max_regions:
                    raise ProcessReaderError("region_limit")
                base, length, state, protect = self._region_fields(raw)
                if state != MEM_COMMIT or not self._readable(protect):
                    continue
                if (type(base) is not int or type(length) is not int
                        or base < 0x10000 or length <= 0
                        or base >= MAX_ADDRESS or base + length > MAX_ADDRESS):
                    raise ProcessReaderError("region_invalid")
                normalized.append((base, length))
        except ProcessReaderError:
            raise
        except (PermissionError, OSError) as exc:
            raise ProcessReaderError("regions_failed") from exc
        except Exception as exc:
            raise ProcessReaderError("regions_failed") from exc
        finally:
            close = getattr(raw_regions, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        normalized.sort()
        previous_end = 0
        for base, length in normalized:
            if base < previous_end:
                raise ProcessReaderError("region_invalid")
            previous_end = base + length
        self._check_deadline()
        self._validate_identity()
        return normalized

    def revalidate(self) -> dict:
        """Recheck target, pinned DLL and loaded PE identity, then return it."""
        self._require_open()
        self._check_deadline()
        self._validate_identity()
        pinned = self._validate_pinned_file()
        fresh = self._validate_loaded(pinned)
        self._check_deadline()
        self._validate_identity()
        if self._snapshot is not None and any(
                fresh.get(key) != self._snapshot.get(key)
                for key in ("module_base", "module_size", "sha256", "dll_size")):
            raise ProcessReaderError("module_drift")
        self._snapshot = fresh
        self.module_base = fresh["module_base"]
        self.module_size = fresh["module_size"]
        return dict(fresh)

    def _abort_open(self, original: Exception) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self.backend.close_process(handle)
        except Exception as cleanup:
            try:
                original.add_note("cleanup_failed")
            except Exception:
                pass

    def _check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ProcessReaderError("deadline")

    def _require_open(self) -> None:
        if self._handle is None:
            raise ProcessReaderError("handle_closed")

    def _validate_identity(self) -> dict:
        self._check_deadline()
        try:
            info = self.backend.process_info(self.target["pid"])
            if type(info) is not dict:
                raise ProcessReaderError("target_invalid")
            if info.get("pid") != self.target["pid"]:
                raise ProcessReaderError("pid_drift")
            if info.get("created") != self.target["created"]:
                raise ProcessReaderError("create_time_drift")
            if info.get("running") is not True:
                raise ProcessReaderError("target_exited")
            if _path_key(info.get("exe")) != _path_key(self.expected_exe):
                raise ProcessReaderError("exe_mismatch")
            session = info.get("session")
            if session != self.backend.current_session():
                raise ProcessReaderError("session_mismatch")
            window = self.backend.window_info(self.target["hwnd"])
        except ProcessReaderError:
            raise
        except (PermissionError, OSError, KeyError, TypeError, ValueError) as exc:
            raise ProcessReaderError("target_unavailable") from exc
        if (type(window) is not dict or window.get("exists") is not True
                or window.get("pid") != self.target["pid"]
                or window.get("class_name") != "Qt51514QWindowIcon"):
            raise ProcessReaderError("window_mismatch")
        self._check_deadline()
        return {**self.target, "exe": info.get("exe"), "session": session}

    def _validate_pinned_file(self) -> dict:
        self._check_deadline()
        try:
            value = self.backend.pinned_file(self.expected_dll, 4096)
        except FileNotFoundError as exc:
            raise ProcessReaderError("pinned_dll_unavailable") from exc
        except (PermissionError, OSError) as exc:
            raise ProcessReaderError("pinned_dll_unavailable") from exc
        if type(value) is not dict:
            raise ProcessReaderError("pinned_dll_invalid")
        size = value.get("size")
        digest = value.get("sha256")
        header = value.get("header")
        if type(size) is not int or size != self.expected_dll_size:
            raise ProcessReaderError("dll_size_mismatch")
        if type(digest) is not str or digest.lower() != self.expected_sha:
            raise ProcessReaderError("dll_hash_mismatch")
        if type(header) is not bytes or len(header) < 4096:
            raise ProcessReaderError("pinned_dll_invalid")
        _check_pe_header(header[:4096], image_size=self.expected_image_size,
                         data_rva=self.expected_data_rva, data_size=self.expected_data_size)
        self._check_deadline()
        return {"size": size, "sha256": digest.lower(), "header": header[:4096]}

    def _validate_loaded(self, pinned: dict) -> dict:
        self._check_deadline()
        try:
            modules = list(self.backend.modules(self._handle))
        except (PermissionError, OSError) as exc:
            raise ProcessReaderError("module_query_failed") from exc
        matches = [module for module in modules
                   if type(module) is dict and _path_key(module.get("path")) == _path_key(self.expected_dll)]
        if len(matches) == 0:
            raise ProcessReaderError("module_missing")
        if len(matches) != 1:
            raise ProcessReaderError("module_ambiguous")
        module = matches[0]
        base, size = module.get("base"), module.get("size")
        if (type(base) is not int or type(size) is not int or base < 0x10000
                or size <= 0 or base + size > MAX_ADDRESS
                or size != self.expected_image_size):
            raise ProcessReaderError("module_mismatch")
        header = self._read_exact(base, 4096)
        _check_pe_header(header, image_size=self.expected_image_size,
                         data_rva=self.expected_data_rva, data_size=self.expected_data_size)
        self._check_deadline()
        identity = self._validate_identity()
        return {**identity, "sha256": pinned["sha256"],
                "dll_size": pinned["size"], "module_base": base,
                "module_size": size, "image_size": self.expected_image_size}

    def _read_exact(self, address: int, size: int) -> bytes:
        self._check_deadline()
        try:
            value = self.backend.read_process_memory(self._handle, address, size)
        except (PermissionError, OSError) as exc:
            raise ProcessReaderError("read_failed") from exc
        self._check_deadline()
        if type(value) is not bytes or len(value) != size:
            raise ProcessReaderError("partial_read")
        return value

    @staticmethod
    def _readable(protect) -> bool:
        if type(protect) is not int or protect & PAGE_GUARD:
            return False
        return protect & 0xFF in READABLE_PROTECTS

    @staticmethod
    def _region_fields(raw):
        if type(raw) is dict:
            return (raw.get("base", raw.get("address")),
                    raw.get("size", raw.get("length")),
                    raw.get("state"), raw.get("protect"))
        if type(raw) in (tuple, list) and len(raw) >= 4:
            return raw[0], raw[1], raw[2], raw[3]
        raise ProcessReaderError("region_invalid")


class _WindowsBackend:
    """Small native backend; it has no write, input or UI automation calls."""

    def __init__(self):
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ProcessReaderError("unsupported_platform")
        try:
            import psutil
            import win32gui
            import win32process
        except ImportError as exc:
            raise ProcessReaderError("windows_dependencies_missing") from exc
        self.psutil = psutil
        self.gui = win32gui
        self.proc = win32process
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                                  ctypes.c_void_p, ctypes.c_size_t,
                                                  ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.ReadProcessMemory.restype = wintypes.BOOL
        self.kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_size_t]
        self.kernel.VirtualQueryEx.restype = ctypes.c_size_t
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        try:
            self._module_info_api = self.kernel.K32GetModuleInformation
        except AttributeError:
            try:
                self._module_info_library = ctypes.WinDLL("psapi", use_last_error=True)
                self._module_info_api = self._module_info_library.GetModuleInformation
            except (AttributeError, OSError) as exc:
                raise ProcessReaderError("module_query_failed") from exc
        try:
            self._module_info_api.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                              ctypes.POINTER(_MODULEINFO), wintypes.DWORD]
            self._module_info_api.restype = wintypes.BOOL
        except (AttributeError, TypeError) as exc:
            raise ProcessReaderError("module_query_failed") from exc
        try:
            self.kernel.GetNativeSystemInfo.argtypes = [ctypes.POINTER(_SYSTEM_INFO)]
            self.kernel.GetNativeSystemInfo.restype = None
        except (AttributeError, TypeError) as exc:
            raise ProcessReaderError("system_info_failed") from exc
        system_info = _SYSTEM_INFO()
        self.kernel.GetNativeSystemInfo(ctypes.byref(system_info))
        self.max_application_address = _native_handle(system_info.lpMaximumApplicationAddress)
        if (type(self.max_application_address) is not int
                or not 0x10000 < self.max_application_address < MAX_ADDRESS):
            raise ProcessReaderError("system_info_failed")

    def current_session(self):
        return self.session(os.getpid())

    def session(self, pid):
        value = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(pid, ctypes.byref(value)):
            raise OSError(ctypes.get_last_error(), "ProcessIdToSessionId")
        return value.value

    def process_info(self, pid):
        try:
            process = self.psutil.Process(pid)
            return {"pid": pid, "created": process.create_time(),
                    "exe": process.exe(), "session": self.session(pid),
                    "running": process.is_running()}
        except (self.psutil.NoSuchProcess, self.psutil.AccessDenied) as exc:
            raise OSError("process unavailable") from exc

    def window_info(self, hwnd):
        exists = bool(self.gui.IsWindow(hwnd))
        if not exists:
            return {"exists": False, "pid": None, "class_name": None}
        pid = self.proc.GetWindowThreadProcessId(hwnd)[1]
        return {"exists": True, "pid": pid, "class_name": self.gui.GetClassName(hwnd)}

    def pinned_file(self, path, max_bytes):
        path = Path(path)
        with path.open("rb") as stream:
            descriptor = stream.fileno()
            status = os.fstat(descriptor)
            if not stat_is_regular(status.st_mode):
                raise OSError("pinned DLL is not a regular file")
            size = status.st_size
            header = stream.read(max_bytes)
            digest = hashlib.sha256()
            stream.seek(0)
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        return {"size": size, "sha256": digest.hexdigest(), "header": header}

    def open_process(self, pid, access):
        value = self.kernel.OpenProcess(access, False, pid)
        if not value:
            raise PermissionError(ctypes.get_last_error(), "OpenProcess")
        return value

    def close_process(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle")

    def read_process_memory(self, handle, address, size):
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count)):
            return None
        return bytes(buffer.raw[:count.value])

    def modules(self, handle):
        modules = []
        process_handle = _native_handle(handle)
        try:
            enumerated = self.proc.EnumProcessModules(process_handle)
            for module in enumerated:
                info = _MODULEINFO()
                if not self._module_info_api(
                        process_handle, _native_handle(module), ctypes.byref(info),
                        ctypes.sizeof(info)):
                    raise OSError(ctypes.get_last_error(), "K32GetModuleInformation")
                base = _native_handle(info.lpBaseOfDll)
                size = int(info.SizeOfImage)
                if type(base) is not int or base <= 0 or size <= 0:
                    raise OSError("invalid MODULEINFO")
                modules.append({"path": self.proc.GetModuleFileNameEx(process_handle, module),
                                "base": base, "size": size})
        except OSError:
            raise
        except Exception as exc:
            raise OSError("module query failed") from exc
        return modules

    def query_regions(self, handle):
        class MemoryInfo(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p),
                        ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wintypes.DWORD),
                        ("PartitionId", wintypes.WORD),
                        ("RegionSize", ctypes.c_size_t),
                        ("State", wintypes.DWORD),
                        ("Protect", wintypes.DWORD),
                        ("Type", wintypes.DWORD)]
        address = 0x10000
        maximum = self.max_application_address
        count = 0
        while address <= maximum:
            info = MemoryInfo()
            returned = self.kernel.VirtualQueryEx(handle, address, ctypes.byref(info), ctypes.sizeof(info))
            if returned != ctypes.sizeof(info):
                raise OSError("VirtualQueryEx failed")
            base = int(info.BaseAddress or 0)
            length = int(info.RegionSize)
            end = base + length
            if (base < 0 or base > address or length <= 0 or end <= address
                    or base >= MAX_ADDRESS):
                raise OSError("invalid VirtualQueryEx progress")
            # VirtualQueryEx may report an allocation whose base is below the
            # caller's minimum address when the query starts at 0x10000.  Clip
            # that first record before handing it to the bounded public API.
            visible_base = max(base, 0x10000)
            visible_end = min(end, maximum + 1)
            if visible_base < visible_end:
                count += 1
                if count > MAX_REGIONS:
                    raise OSError("VirtualQueryEx region limit")
                yield {"base": visible_base, "size": visible_end - visible_base,
                               "state": int(info.State),
                               "protect": int(info.Protect), "type": int(info.Type)}
            if end > maximum:
                break
            address = end


def stat_is_regular(mode: int) -> bool:
    """Avoid importing ``stat`` in the reader's hot path."""
    return (mode & 0o170000) == 0o100000


def _native_handle(handle):
    value = getattr(handle, "value", None)
    return int(value) if value is not None else handle


__all__ = ["ProcessReader", "ProcessReaderError", "READ_ACCESS"]
