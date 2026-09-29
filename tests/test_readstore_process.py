import ctypes
import hashlib
import time
import unittest

from wxbg.readstore_process import (_MODULEINFO, _WindowsBackend, ProcessReader,
                                    ProcessReaderError, READ_ACCESS)


EXE = r"C:\owned\Weixin.exe"
DLL = r"C:\owned\Weixin.dll"
PID = 31415
CREATED = 1234.5
HWND = 0x70123
SESSION = 9
BASE = 0x180000000
IMAGE_SIZE = 0x4000
DATA_RVA = 0x2000
DATA_SIZE = 0x800
PINNED = b"owned pinned dll"
PINNED_SHA = hashlib.sha256(PINNED).hexdigest()


def pe_header():
    header = bytearray(4096)
    header[:2] = b"MZ"
    pe = 0x80
    header[pe:pe + 4] = b"PE\0\0"
    header[pe + 4:pe + 6] = (0x8664).to_bytes(2, "little")
    header[pe + 6:pe + 8] = (1).to_bytes(2, "little")
    header[pe + 20:pe + 22] = (0xF0).to_bytes(2, "little")
    optional = pe + 24
    header[optional:optional + 2] = (0x20B).to_bytes(2, "little")
    header[optional + 56:optional + 60] = IMAGE_SIZE.to_bytes(4, "little")
    section = optional + 0xF0
    header[section:section + 5] = b".data"
    header[section + 8:section + 12] = DATA_SIZE.to_bytes(4, "little")
    header[section + 12:section + 16] = DATA_RVA.to_bytes(4, "little")
    header[section + 36:section + 40] = (0xC0000040).to_bytes(4, "little")
    header[60:64] = pe.to_bytes(4, "little")
    return bytes(header)


class FakeBackend:
    """Owned fake for the reader contract; no native or client access."""

    def __init__(self):
        self.info = {"pid": PID, "created": CREATED, "exe": EXE,
                     "session": SESSION, "running": True}
        self.window = {"exists": True, "pid": PID,
                       "class_name": "Qt51514QWindowIcon"}
        self.module = {"path": DLL, "base": BASE, "size": IMAGE_SIZE}
        self.pinned = {"size": len(PINNED), "sha256": PINNED_SHA,
                       "header": pe_header()}
        self._regions = [{"base": BASE, "size": 0x10000,
                          "state": 0x1000, "protect": 0x20,
                          "type": 0x1000000}]
        self.memory = {BASE: pe_header(), BASE + 0x1000: b"readable-data"}
        self.open_calls = []
        self.close_calls = []
        self.read_calls = []
        self.region_calls = 0
        self.handle = object()
        self.partial = False
        self.fail_open = False

    def current_session(self):
        return SESSION

    def process_info(self, pid):
        return dict(self.info)

    def window_info(self, hwnd):
        return dict(self.window)

    def pinned_file(self, path, max_bytes):
        return dict(self.pinned)

    def open_process(self, pid, access):
        self.open_calls.append((pid, access))
        if self.fail_open:
            raise PermissionError("access denied")
        return self.handle

    def close_process(self, handle):
        self.close_calls.append(handle)

    def modules(self, handle):
        return [dict(self.module)]

    def read_process_memory(self, handle, address, size):
        self.read_calls.append((address, size))
        for base, value in self.memory.items():
            if base <= address and address + size <= base + len(value):
                result = value[address - base:address - base + size]
                if self.partial:
                    return result[:-1]
                return result
        return None

    def query_regions(self, handle):
        self.region_calls += 1
        return [dict(region) for region in self._regions]


class FakeModuleEnumerator:
    def EnumProcessModules(self, handle):
        return [0xABC]

    def GetModuleFileNameEx(self, handle, module):
        return DLL

    def __getattr__(self, name):
        if name == "GetModuleInformation":
            raise AssertionError("pywin32 GetModuleInformation must not be used")
        raise AttributeError(name)


class FakeK32GetModuleInformation:
    def __init__(self, *, succeed=True):
        self.succeed = succeed
        self.calls = []

    def __call__(self, handle, module, output, output_size):
        self.calls.append((handle, module, output_size))
        if not self.succeed:
            return 0
        info = ctypes.cast(output, ctypes.POINTER(_MODULEINFO)).contents
        info.lpBaseOfDll = BASE
        info.SizeOfImage = IMAGE_SIZE
        info.EntryPoint = BASE + 0x1000
        return 1


class FakeVirtualQuery:
    class MemoryInfo(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p),
                    ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", ctypes.c_uint32),
                    ("PartitionId", ctypes.c_uint16),
                    ("RegionSize", ctypes.c_size_t),
                    ("State", ctypes.c_uint32),
                    ("Protect", ctypes.c_uint32),
                    ("Type", ctypes.c_uint32)]

    def __init__(self, maximum):
        self.maximum = maximum
        self.calls = []
        self.VirtualQueryEx = self

    def __call__(self, handle, address, output, output_size):
        self.calls.append(address)
        if address > self.maximum:
            raise AssertionError("VirtualQueryEx called above maximum application address")
        info = ctypes.cast(output, ctypes.POINTER(self.MemoryInfo)).contents
        info.BaseAddress = address
        info.AllocationBase = address
        info.AllocationProtect = 0
        info.PartitionId = 0
        info.State = 0x1000
        info.Protect = 0x20
        info.Type = 0x1000000
        if address == 0x10000:
            info.RegionSize = 0x1000
        elif address == 0x11000:
            info.RegionSize = self.maximum + 1 - address
        else:
            raise AssertionError("unexpected region address")
        return output_size


class ProcessReaderTests(unittest.TestCase):
    def target(self):
        return {"pid": PID, "created": CREATED, "hwnd": HWND}

    def reader(self, backend=None, **kwargs):
        deadline = kwargs.pop("deadline", time.monotonic() + 2)
        return ProcessReader(self.target(), backend=backend or FakeBackend(),
                              deadline=deadline,
                              expected_exe=EXE, expected_dll=DLL,
                              expected_sha=PINNED_SHA,
                              expected_dll_size=len(PINNED),
                              expected_image_size=IMAGE_SIZE,
                              expected_data_rva=DATA_RVA,
                              expected_data_size=DATA_SIZE, **kwargs)

    def test_read_only_access_identity_module_and_cleanup(self):
        backend = FakeBackend()
        with self.reader(backend) as reader:
            snapshot = reader.revalidate()
            self.assertEqual(snapshot["module_base"], BASE)
            self.assertEqual(snapshot["module_size"], IMAGE_SIZE)
            self.assertEqual(reader.module_base, BASE)
            self.assertEqual(reader.module_size, IMAGE_SIZE)
            self.assertEqual(reader.read(BASE + 0x1000, 7), b"readabl")
        self.assertEqual(backend.open_calls, [(PID, READ_ACCESS)])
        self.assertEqual(backend.close_calls, [backend.handle])
        self.assertEqual(READ_ACCESS, 0x0410)

    def test_partial_read_returns_none_and_never_exposes_partial_bytes(self):
        backend = FakeBackend()
        with self.reader(backend) as reader:
            backend.partial = True
            self.assertIsNone(reader.read(BASE + 0x1000, 7))

    def test_repeated_reads_do_not_rescan_all_memory_regions(self):
        backend = FakeBackend()
        with self.reader(backend) as reader:
            for _ in range(5):
                self.assertEqual(reader.read(BASE + 0x1000, 7), b"readabl")
        self.assertEqual(backend.region_calls, 0)

    def test_read_is_bounded_to_a_fresh_readable_region(self):
        backend = FakeBackend()
        with self.reader(backend) as reader:
            self.assertIsNone(reader.read(BASE + 0x10000, 1))
            self.assertEqual(reader.regions(), [(BASE, 0x10000)])

    def test_process_create_time_drift_is_rejected_on_revalidate(self):
        backend = FakeBackend()
        with self.reader(backend) as reader:
            backend.info["created"] = CREATED + 1
            with self.assertRaisesRegex(ProcessReaderError, "^create_time_drift$"):
                reader.revalidate()

    def test_pid_drift_is_rejected_on_revalidate(self):
        backend = FakeBackend()
        with self.reader(backend) as reader:
            backend.info["pid"] = PID + 1
            with self.assertRaisesRegex(ProcessReaderError, "^pid_drift$"):
                reader.revalidate()

    def test_window_session_and_exe_identity_are_strict(self):
        for field, value, code in (("session", SESSION + 1, "session_mismatch"),
                                   ("exe", r"C:\other\Weixin.exe", "exe_mismatch")):
            with self.subTest(field=field):
                backend = FakeBackend()
                backend.info[field] = value
                with self.assertRaisesRegex(ProcessReaderError, "^" + code + "$"):
                    self.reader(backend).__enter__()
                self.assertEqual(len(backend.close_calls), 0)
        backend = FakeBackend()
        backend.window["pid"] = PID + 1
        with self.assertRaisesRegex(ProcessReaderError, "^window_mismatch$"):
            self.reader(backend).__enter__()

    def test_duplicate_pinned_modules_are_rejected_and_handle_is_closed(self):
        backend = FakeBackend()
        original = backend.modules
        backend.modules = lambda handle: original(handle) * 2
        with self.assertRaisesRegex(ProcessReaderError, "^module_ambiguous$"):
            self.reader(backend).__enter__()
        self.assertEqual(backend.close_calls, [backend.handle])

    def test_pinned_dll_size_and_sha_are_checked_before_open(self):
        for field, value, code in (("size", len(PINNED) + 1, "dll_size_mismatch"),
                                   ("sha256", "0" * 64, "dll_hash_mismatch")):
            with self.subTest(field=field):
                backend = FakeBackend()
                backend.pinned[field] = value
                with self.assertRaisesRegex(ProcessReaderError, "^" + code + "$"):
                    self.reader(backend).__enter__()
                self.assertEqual(backend.open_calls, [])

    def test_loaded_module_size_is_pinned_to_image_manifest(self):
        backend = FakeBackend()
        backend.module["size"] = IMAGE_SIZE + 1
        with self.assertRaisesRegex(ProcessReaderError, "^module_mismatch$"):
            self.reader(backend).__enter__()
        self.assertEqual(backend.close_calls, [backend.handle])

    def test_open_access_denied_closes_nothing_and_reports_error(self):
        backend = FakeBackend()
        backend.fail_open = True
        with self.assertRaisesRegex(ProcessReaderError, "^access_denied$"):
            self.reader(backend).__enter__()
        self.assertEqual(backend.close_calls, [])

    def test_cleanup_failure_is_visible(self):
        backend = FakeBackend()
        def fail_close(handle):
            backend.close_calls.append(handle)
            raise OSError("close failed")
        backend.close_process = fail_close
        reader = self.reader(backend)
        reader.__enter__()
        with self.assertRaisesRegex(ProcessReaderError, "^cleanup_failed$"):
            reader.__exit__(None, None, None)

    def test_deadline_is_checked_before_native_calls(self):
        backend = FakeBackend()
        reader = self.reader(backend, deadline=time.monotonic() - 1)
        with self.assertRaisesRegex(ProcessReaderError, "^deadline$"):
            reader.__enter__()
        self.assertEqual(backend.open_calls, [])

    def test_invalid_pe_is_rejected(self):
        backend = FakeBackend()
        backend.memory[BASE] = b"not a pe" + b"\0" * 4088
        with self.assertRaisesRegex(ProcessReaderError, "^pe_invalid$"):
            self.reader(backend).__enter__()
        self.assertEqual(backend.close_calls, [backend.handle])

    def test_windows_modules_uses_ctypes_k32_moduleinfo_when_pywin32_lacks_api(self):
        backend = object.__new__(_WindowsBackend)
        backend.proc = FakeModuleEnumerator()
        api = FakeK32GetModuleInformation()
        backend._module_info_api = api
        modules = backend.modules(123)
        self.assertEqual(modules, [{"path": DLL, "base": BASE, "size": IMAGE_SIZE}])
        self.assertEqual(api.calls, [(123, 0xABC, ctypes.sizeof(_MODULEINFO))])

    def test_windows_moduleinfo_api_failure_is_not_size_zero_fallback(self):
        backend = object.__new__(_WindowsBackend)
        backend.proc = FakeModuleEnumerator()
        backend._module_info_api = FakeK32GetModuleInformation(succeed=False)
        with self.assertRaises(OSError):
            backend.modules(123)

    def test_reader_maps_module_api_failure_to_module_query_failed(self):
        backend = FakeBackend()
        def fail_modules(handle):
            raise OSError("K32GetModuleInformation failed")
        backend.modules = fail_modules
        with self.assertRaisesRegex(ProcessReaderError, "^module_query_failed$"):
            self.reader(backend).__enter__()
        self.assertEqual(backend.close_calls, [backend.handle])

    def test_virtual_query_stops_at_native_terminal_region(self):
        backend = object.__new__(_WindowsBackend)
        maximum = 0x12FFF
        api = FakeVirtualQuery(maximum)
        backend.kernel = api
        backend.max_application_address = maximum
        regions = list(backend.query_regions(123))
        self.assertEqual([region["base"] for region in regions], [0x10000, 0x11000])
        self.assertEqual(api.calls, [0x10000, 0x11000])


if __name__ == "__main__":
    unittest.main()
