import binascii
import struct
import sys
from pathlib import Path
import unittest
import zlib


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from wxbg import png_validation as validator
except ImportError:
    validator = None


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _ihdr(width, height, bit_depth=8, color_type=2, compression=0,
          filter_method=0, interlace=0):
    return struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        color_type,
        compression,
        filter_method,
        interlace,
    )


def _make_png(width=2, height=2, color_type=2, *, bit_depth=8,
              compression=0, filter_method=0, interlace=0,
              palette=None, filters=None, raw=None, idat_payloads=None,
              before=(), between=(), after=(), include_iend=True):
    chunks = [_chunk(
        b"IHDR",
        _ihdr(width, height, bit_depth, color_type, compression,
              filter_method, interlace),
    )]
    if palette is not None:
        chunks.append(_chunk(b"PLTE", palette))
    chunks.extend(before)
    if raw is None:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 1)
        row_bytes = width * channels
        selected_filters = [0] * height if filters is None else list(filters)
        raw = b"".join(
            bytes([selected_filters[row]]) + bytes(row_bytes)
            for row in range(height)
        )
    payloads = [zlib.compress(raw)] if idat_payloads is None else list(idat_payloads)
    for index, payload in enumerate(payloads):
        chunks.append(_chunk(b"IDAT", payload))
        if index + 1 < len(payloads):
            chunks.extend(between)
    chunks.extend(after)
    if include_iend:
        chunks.append(_chunk(b"IEND", b""))
    return PNG_SIGNATURE + b"".join(chunks)


class PngValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if validator is None:
            raise AssertionError("png_validation.py is missing")

    def assert_code(self, data, code):
        with self.assertRaises(validator.AdapterError) as context:
            validator.validate_png_bytes(data)
        self.assertEqual(context.exception.code, code)
        self.assertEqual(str(context.exception), code)

    def test_accepts_rgb_rgba_grayscale_and_indexed_fixtures(self):
        cases = (
            (2, _make_png(color_type=2), 2),
            (6, _make_png(color_type=6), 6),
            (0, _make_png(color_type=0), 0),
            (4, _make_png(color_type=4), 4),
            (3, _make_png(
                color_type=3,
                palette=b"\x00\x00\x00\xff\x00\x00",
            ), 3),
        )
        for color_type, data, expected_color_type in cases:
            with self.subTest(color_type=color_type):
                self.assertEqual(
                    validator.validate_png_bytes(data),
                    {
                        "width": 2,
                        "height": 2,
                        "bit_depth": 8,
                        "color_type": expected_color_type,
                    },
                )

    def test_accepts_all_non_interlaced_row_filter_tags(self):
        data = _make_png(width=1, height=5, color_type=2, filters=[0, 1, 2, 3, 4])
        self.assertEqual(validator.validate_png_bytes(data)["height"], 5)

    def test_accepts_bounded_ancillary_chunk_and_consecutive_idat_chunks(self):
        raw = b"\x00\x00\x00\x00"
        compressed = zlib.compress(raw)
        data = _make_png(
            width=1,
            height=1,
            color_type=2,
            before=[_chunk(b"tEXt", b"owned=fixture")],
            idat_payloads=[compressed[:2], compressed[2:]],
        )
        self.assertEqual(validator.validate_png_bytes(data)["width"], 1)

    def test_rejects_non_bytes_oversize_signature_and_truncation(self):
        self.assert_code("not bytes", "invalid_png")
        self.assert_code(b"x" * (1024 * 1024 + 1), "invalid_png")
        self.assert_code(b"not-a-png", "invalid_png")
        self.assert_code(_make_png()[:-1], "invalid_png")

    def test_rejects_crc_and_trailing_bytes_without_leaking_input(self):
        valid = bytearray(_make_png())
        valid[-1] ^= 1
        self.assert_code(bytes(valid), "invalid_png")
        self.assert_code(_make_png() + b"trailing-private-path-C:\\secret", "invalid_png")

    def test_rejects_ihdr_order_length_duplicate_and_missing_iend(self):
        ihdr = _ihdr(1, 1)
        self.assert_code(
            PNG_SIGNATURE + _chunk(b"tEXt", b"before") + _chunk(b"IHDR", ihdr),
            "invalid_png",
        )
        self.assert_code(
            PNG_SIGNATURE + _chunk(b"IHDR", b"short") + _chunk(b"IEND", b""),
            "invalid_png",
        )
        self.assert_code(
            _make_png(before=[_chunk(b"IHDR", ihdr)]),
            "invalid_png",
        )
        self.assert_code(_make_png(include_iend=False), "invalid_png")

    def test_rejects_unsupported_header_features_and_animation_chunks(self):
        self.assert_code(_make_png(bit_depth=16), "unsupported_png")
        self.assert_code(_make_png(color_type=1), "unsupported_png")
        self.assert_code(_make_png(interlace=1), "unsupported_png")
        self.assert_code(_make_png(compression=1), "unsupported_png")
        self.assert_code(_make_png(filter_method=1), "unsupported_png")
        for kind in (b"acTL", b"fcTL", b"fdAT"):
            with self.subTest(kind=kind):
                self.assert_code(_make_png(before=[_chunk(kind, b"animation")]), "unsupported_png")
        self.assert_code(_make_png(before=[_chunk(b"ABCD", b"critical")]), "unsupported_png")

    def test_rejects_dimension_pixel_and_input_bounds(self):
        self.assert_code(
            _make_png(width=4097, height=1, raw=b""),
            "invalid_png",
        )
        self.assert_code(
            PNG_SIGNATURE
            + _chunk(b"IHDR", _ihdr(4096, 1025))
            + _chunk(b"IEND", b""),
            "invalid_png",
        )

    def test_indexed_palette_is_required_bounded_and_unique(self):
        self.assert_code(_make_png(color_type=3), "invalid_png")
        self.assert_code(
            _make_png(color_type=3, palette=b"\x00\x00\x00" * 257),
            "invalid_png",
        )
        self.assert_code(
            _make_png(color_type=3, palette=b"\x00"),
            "invalid_png",
        )
        self.assert_code(
            _make_png(color_type=0, palette=b"\x00\x00\x00"),
            "invalid_png",
        )
        self.assert_code(
            _make_png(
                color_type=2,
                palette=b"\x00\x00\x00",
                before=[_chunk(b"PLTE", b"\x00\x00\x00")],
            ),
            "invalid_png",
        )

    def test_rejects_idat_empty_nonconsecutive_and_duplicate_iend(self):
        self.assert_code(_make_png(idat_payloads=[b""]), "invalid_png")
        raw = b"\x00\x00\x00\x00"
        compressed = zlib.compress(raw)
        self.assert_code(
            _make_png(
                width=1,
                height=1,
                idat_payloads=[compressed[:2], compressed[2:]],
                between=[_chunk(b"tEXt", b"gap")],
            ),
            "invalid_png",
        )
        self.assert_code(_make_png() + _chunk(b"IEND", b""), "invalid_png")
        self.assert_code(
            PNG_SIGNATURE
            + _chunk(b"IHDR", _ihdr(1, 1))
            + _chunk(b"IEND", b"x"),
            "invalid_png",
        )

    def test_rejects_zlib_truncation_unused_data_bomb_and_wrong_decoded_size(self):
        raw = b"\x00\x00\x00\x00"
        compressed = zlib.compress(raw)
        self.assert_code(
            _make_png(width=1, height=1, idat_payloads=[compressed[:-1]]),
            "invalid_png",
        )
        self.assert_code(
            _make_png(width=1, height=1, idat_payloads=[compressed + b"junk"]),
            "invalid_png",
        )
        self.assert_code(
            _make_png(
                width=1,
                height=1,
                idat_payloads=[zlib.compress(b"\x00" + b"x" * 10000)],
            ),
            "invalid_png",
        )
        self.assert_code(
            _make_png(width=1, height=1, idat_payloads=[zlib.compress(b"\x00")]),
            "invalid_png",
        )

    def test_rejects_invalid_scanline_filter(self):
        self.assert_code(
            _make_png(width=1, height=1, raw=b"\x05\x00\x00\x00"),
            "invalid_png",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
