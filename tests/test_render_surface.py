import unittest

from wxbg.render_surface import enumerate_render_surfaces


class FakeGui:
    def __init__(self, classes):
        self.classes = classes

    def EnumChildWindows(self, parent, callback, _):
        for hwnd in self.classes:
            callback(hwnd, None)

    def GetClassName(self, hwnd):
        return self.classes[hwnd]


class RenderSurfaceTests(unittest.TestCase):
    def test_accepts_current_and_legacy_pinned_renderer_classes(self):
        gui = FakeGui({
            10: 'Qt51514QWindowIcon',
            11: 'MMUIRenderSubWindow',
            12: 'MMUIRenderSubWindowHW',
        })

        self.assertEqual(enumerate_render_surfaces(100, gui), [11, 12])

    def test_does_not_guess_unknown_renderer_classes(self):
        gui = FakeGui({10: 'MMUIRenderSubWindowVNext'})

        self.assertEqual(enumerate_render_surfaces(100, gui), [])


if __name__ == '__main__':
    unittest.main()
