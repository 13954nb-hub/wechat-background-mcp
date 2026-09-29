"""Geometry predicates for minimized Qt top-level windows."""


def main_client_size_matches_or_minimized(hwnd, expected, win32gui):
    """A minimized shell may collapse while its verified render child persists.

    The caller must independently validate render client dimensions and that
    the main and render client origins match. This only exempts the collapsed
    *outer client size* when Windows confirms the top-level window is iconic;
    it never accepts a mismatched render child or native client origins.
    """
    try:
        actual = tuple(win32gui.GetClientRect(hwnd))
        if actual == tuple(expected):
            return True
        return bool(win32gui.IsIconic(hwnd))
    except Exception:
        return False
