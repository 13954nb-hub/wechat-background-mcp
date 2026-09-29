"""Synthetic dynamic-resolution matrix; no real Weixin window is manipulated."""
import unittest

from wxbg.adapter import _control_point
from wxbg.policy import AdapterError


class DynamicGeometryMatrixTests(unittest.TestCase):
    def test_send_control_is_measured_for_common_resolutions_dpi_and_origins(self):
        resolutions = ((800, 600), (1280, 720), (1920, 1080),
                       (2560, 1440), (3240, 2160), (3840, 2160))
        dpi_scales = (100, 125, 150, 200, 250)
        origins = ((0, 0), (96, 48), (-1920, 120))
        for width, height in resolutions:
            for dpi in dpi_scales:
                for left, top in origins:
                    with self.subTest(resolution=(width, height), dpi=dpi,
                                      origin=(left, top)):
                        root = (left, top, left + width, top + height)
                        # UIA gives physical desktop bounds after DPI awareness;
                        # scale control placement while keeping the measured
                        # control size plausible at each display scale.
                        control_width = round(72 * dpi / 100)
                        control_height = round(36 * dpi / 100)
                        cx, cy = round(width * .84), round(height * .84)
                        control = (left + cx - control_width // 2,
                                   top + cy - control_height // 2,
                                   left + cx + (control_width + 1) // 2,
                                   top + cy + (control_height + 1) // 2)
                        point = _control_point(root, control, send=True)
                        self.assertEqual(point, (cx, cy))

    def test_window_below_supported_minimum_fails_closed(self):
        with self.assertRaisesRegex(AdapterError, 'unverified_layout'):
            _control_point((0, 0, 503, 300), (420, 240, 480, 280), send=True)


if __name__ == '__main__':
    unittest.main()
