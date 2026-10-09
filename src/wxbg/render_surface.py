"""Recognize only the explicitly pinned Weixin render-child class variants."""

RENDER_SURFACE_CLASSES = frozenset({
    # Older 4.1 builds registered the HWND-suffixed native window class.
    'MMUIRenderSubWindowHW',
    # Current 4.1.13.12 registers the render child without that suffix.
    'MMUIRenderSubWindow',
})


def enumerate_render_surfaces(parent_hwnd, gui):
    """Return exact known renderer children; callers still validate PID and geometry."""
    found = []

    def consider(hwnd, _):
        if gui.GetClassName(hwnd) in RENDER_SURFACE_CLASSES:
            found.append(hwnd)

    gui.EnumChildWindows(parent_hwnd, consider, None)
    return found
