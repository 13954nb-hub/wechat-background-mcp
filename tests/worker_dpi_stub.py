"""Success-shaped DPI context for worker tests with a fake Weixin adapter."""
from types import ModuleType, SimpleNamespace


def worker_monitor_module(monitor_class):
    module = ModuleType("wxbg.monitor")
    module.Monitor = monitor_class
    module.PM_V2 = -4
    module.user32 = SimpleNamespace(
        SetThreadDpiAwarenessContext=lambda _context: 1,
        AreDpiAwarenessContextsEqual=lambda current, expected: current == expected,
        GetThreadDpiAwarenessContext=lambda: -4,
    )
    return module
