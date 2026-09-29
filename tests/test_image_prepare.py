from __future__ import annotations

import binascii
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from wxbg import image_prepare
except ImportError:
    image_prepare = None

try:
    from PIL import Image
except ImportError:
    Image = None


class ImagePrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if image_prepare is None:
            raise AssertionError("image_prepare.py is missing")
        if Image is None:
            raise AssertionError("Pillow is required by these owned fixtures")

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _save_rgb(self, name, fmt, size=(3, 2), **kwargs):
        path = self.root / name
        image = Image.new("RGB", size)
        for y in range(size[1]):
            for x in range(size[0]):
                image.putpixel((x, y), ((x * 47) % 256, (y * 83) % 256, 91))
        image.save(path, format=fmt, **kwargs)
        return path

    def _save_rgba(self, name, fmt="PNG", size=(3, 2), **kwargs):
        path = self.root / name
        image = Image.new("RGBA", size)
        for y in range(size[1]):
            for x in range(size[0]):
                image.putpixel((x, y), (x * 40, y * 70, 150, 40 + x + y * 10))
        image.save(path, format=fmt, **kwargs)
        return path

    def _read_chunks(self, path):
        data = path.read_bytes()
        offset = 8
        chunks = []
        while offset < len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            kind = data[offset + 4:offset + 8]
            chunks.append(kind)
            offset += 12 + length
        return chunks

    def test_jpeg_exif_orientation_and_private_metadata_are_normalized(self):
        source = self.root / "owned-fixture.jpg"
        image = Image.new("RGB", (2, 3), "red")
        image.putpixel((0, 0), (255, 0, 0))
        image.putpixel((1, 0), (0, 255, 0))
        exif = image.getexif()
        exif[274] = 6
        image.save(
            source,
            format="JPEG",
            quality=95,
            exif=exif.tobytes(),
            icc_profile=b"owned-private-icc",
        )
        destination = self.root / "normalized.png"

        result = image_prepare.prepare_image(source, destination)

        self.assertEqual(result["format"], "PNG")
        self.assertEqual(result["original_size"], (3, 2))
        self.assertEqual(result["output_size"], (3, 2))
        self.assertFalse(result["resized"])
        self.assertFalse(result["lossy_resize"])
        self.assertEqual(result["size_bytes"], destination.stat().st_size)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(image_prepare.validate_png(destination.read_bytes())["width"], 3)
        self.assertNotIn(b"eXIf", destination.read_bytes())
        self.assertNotIn(b"iCCP", destination.read_bytes())
        self.assertEqual(self._read_chunks(destination), [b"IHDR", b"IDAT", b"IEND"])

    def test_webp_content_is_accepted_even_with_nonmatching_extension(self):
        source = self._save_rgb("owned-fixture.bin", "WEBP")
        destination = self.root / "converted.png"

        result = image_prepare.prepare_image(source, destination)

        self.assertEqual(result["original_size"], (3, 2))
        self.assertEqual(result["output_size"], (3, 2))
        self.assertFalse(result["resized"])
        self.assertEqual(destination.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(image_prepare.validate_png(destination.read_bytes())["color_type"], 2)

    def test_png_alpha_is_preserved_and_result_hash_matches_new_bytes(self):
        source = self._save_rgba("owned-alpha.png")
        destination = self.root / "alpha-output.png"

        result = image_prepare.prepare_image(source, destination)

        with Image.open(destination) as output:
            self.assertEqual(output.mode, "RGBA")
            self.assertEqual(output.getpixel((2, 1))[3], 52)
        import hashlib

        self.assertEqual(
            result["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest()
        )
        self.assertEqual(image_prepare.validate_png(destination.read_bytes())["color_type"], 6)

    def test_rgb_png_trns_transparency_is_converted_to_alpha(self):
        source = self.root / "owned-trns.png"
        image = Image.new("RGB", (2, 1))
        image.putpixel((0, 0), (10, 20, 30))
        image.putpixel((1, 0), (1, 2, 3))
        image.save(source, format="PNG", transparency=(10, 20, 30))
        destination = self.root / "trns-output.png"

        image_prepare.prepare_image(source, destination)

        with Image.open(destination) as output:
            self.assertEqual(output.mode, "RGBA")
            self.assertEqual(output.getpixel((0, 0))[3], 0)
            self.assertEqual(output.getpixel((1, 0))[3], 255)

    def test_dimension_limit_uses_explicit_lossy_resize_and_remains_validator_compatible(self):
        source = self.root / "large.jpg"
        Image.new("RGB", (5000, 1000), (18, 52, 86)).save(
            source, format="JPEG", quality=90
        )
        destination = self.root / "scaled.png"

        result = image_prepare.prepare_image(source, destination)

        self.assertEqual(result["original_size"], (5000, 1000))
        self.assertEqual(result["output_size"], (4096, 819))
        self.assertTrue(result["resized"])
        self.assertTrue(result["lossy_resize"])
        checked = image_prepare.validate_png(destination.read_bytes())
        self.assertEqual((checked["width"], checked["height"]), result["output_size"])

    def test_animated_webp_is_rejected(self):
        first = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
        second = Image.new("RGBA", (2, 2), (0, 0, 255, 255))
        for extension, image_format in (("webp", "WEBP"), ("png", "PNG")):
            with self.subTest(image_format=image_format):
                source = self.root / f"animated.{extension}"
                first.save(
                    source,
                    format=image_format,
                    save_all=True,
                    append_images=[second],
                    duration=10,
                    loop=0,
                )

                with self.assertRaises(image_prepare.ImagePrepareError) as caught:
                    image_prepare.prepare_image(source, self.root / f"animated-{extension}.png")
                self.assertEqual(caught.exception.code, "animated_image")

    def test_single_frame_apng_control_chunks_are_rejected(self):
        base = self.root / "base.png"
        Image.new("RGBA", (2, 2), (1, 2, 3, 4)).save(base, format="PNG")

        data = base.read_bytes()
        offset = 8
        chunks = []
        while offset < len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            chunks.append((kind, payload))
            offset += length + 12
        ihdr = next(payload for kind, payload in chunks if kind == b"IHDR")
        idat = b"".join(payload for kind, payload in chunks if kind == b"IDAT")

        def chunk(kind, payload):
            return (
                len(payload).to_bytes(4, "big")
                + kind
                + payload
                + (binascii.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big")
            )

        source = self.root / "single-frame.apng"
        source.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"acTL", struct.pack(">II", 1, 0))
            + chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 2, 2, 0, 0, 1, 10, 0, 0))
            + chunk(b"IDAT", idat)
            + chunk(b"IEND", b"")
        )

        with Image.open(source) as opened:
            self.assertFalse(getattr(opened, "is_animated", False))
            self.assertEqual(getattr(opened, "n_frames", 1), 1)
        with self.assertRaises(image_prepare.ImagePrepareError) as caught:
            image_prepare.prepare_image(source, self.root / "single-frame.png")
        self.assertEqual(caught.exception.code, "animated_image")

    def test_content_validation_rejects_unsupported_and_truncated_inputs(self):
        unsupported = self.root / "owned.gif"
        Image.new("RGB", (2, 2), "purple").save(unsupported, format="GIF")
        with self.assertRaises(image_prepare.ImagePrepareError) as unsupported_error:
            image_prepare.prepare_image(unsupported, self.root / "unsupported.png")
        self.assertEqual(unsupported_error.exception.code, "unsupported_format")

        valid = self._save_rgb("valid.jpg", "JPEG")
        truncated = self.root / "truncated.jpg"
        truncated.write_bytes(valid.read_bytes()[:-8])
        with self.assertRaises(image_prepare.ImagePrepareError) as truncated_error:
            image_prepare.prepare_image(truncated, self.root / "truncated.png")
        self.assertEqual(truncated_error.exception.code, "invalid_image")

    def test_input_pixel_and_destination_bounds_are_clear_and_non_destructive(self):
        source = self._save_rgb("owned.jpg", "JPEG")
        existing = self.root / "existing.png"
        existing.write_bytes(b"keep-me")

        with self.assertRaises(image_prepare.ImagePrepareError) as too_small:
            image_prepare.prepare_image(source, self.root / "tiny.png", max_input_bytes=10)
        self.assertEqual(too_small.exception.code, "input_too_large")

        with self.assertRaises(image_prepare.ImagePrepareError) as pixels:
            image_prepare.prepare_image(
                source,
                self.root / "pixel-limit.png",
                max_input_pixels=1,
            )
        self.assertEqual(pixels.exception.code, "pixel_limit")

        with self.assertRaises(image_prepare.ImagePrepareError) as collision:
            image_prepare.prepare_image(source, existing)
        self.assertEqual(collision.exception.code, "destination_exists")
        self.assertEqual(existing.read_bytes(), b"keep-me")

        with self.assertRaises(image_prepare.ImagePrepareError) as same_path:
            image_prepare.prepare_image(source, source)
        self.assertEqual(same_path.exception.code, "destination_exists")
        self.assertTrue(source.exists())

    def test_output_limit_fails_without_overwriting_destination(self):
        source = self._save_rgb("owned.png", "PNG")
        destination = self.root / "too-small.png"

        with self.assertRaises(image_prepare.ImagePrepareError) as caught:
            image_prepare.prepare_image(source, destination, max_output_bytes=20)
        self.assertEqual(caught.exception.code, "output_too_large")
        self.assertFalse(destination.exists())

    def test_input_growth_after_initial_stat_is_bounded_and_rejected(self):
        source = self.root / "growing.bin"
        source.write_bytes(b"1234")
        original_open = Path.open
        original_read_bytes = Path.read_bytes
        growth_done = False

        def grow_then_open(path, *args, **kwargs):
            nonlocal growth_done
            if path == source and not growth_done:
                growth_done = True
                with open(source, "ab") as handle:
                    handle.write(b"56789")
            return original_open(path, *args, **kwargs)

        def forbid_unbounded_read(path, *args, **kwargs):
            if path == source:
                raise OSError("source.read_bytes() is unbounded")
            return original_read_bytes(path, *args, **kwargs)

        with patch.object(Path, "open", grow_then_open), patch.object(
            Path, "read_bytes", forbid_unbounded_read
        ):
            with self.assertRaises(image_prepare.ImagePrepareError) as caught:
                image_prepare._read_bounded_source(source, 4)
        self.assertEqual(caught.exception.code, "input_too_large")
        self.assertTrue(growth_done)

    def test_permission_failure_does_not_remove_file_created_by_other_actor(self):
        destination = self.root / "race.png"
        original_open = Path.open

        def permission_open(path, *args, **kwargs):
            if path == destination:
                with open(destination, "wb") as handle:
                    handle.write(b"other-actor")
                raise PermissionError("owned race fixture")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", permission_open):
            with self.assertRaises(image_prepare.ImagePrepareError) as caught:
                image_prepare._write_new_file(destination, b"new-output")
        self.assertEqual(caught.exception.code, "destination_write_failed")
        self.assertTrue(destination.exists(), "race-created file was deleted")
        self.assertEqual(destination.read_bytes(), b"other-actor")

    def test_cli_returns_json_summary_and_nonzero_json_error(self):
        source = self._save_rgb("cli.jpg", "JPEG")
        destination = self.root / "cli.png"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SRC) + os.pathsep + environment.get("PYTHONPATH", "")
        command = [sys.executable, "-m", "wxbg.image_prepare", str(source), str(destination)]

        completed = subprocess.run(command, capture_output=True, text=True, env=environment)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["output_size"], [3, 2])
        self.assertEqual(summary["size_bytes"], destination.stat().st_size)

        failed = subprocess.run(
            [sys.executable, "-m", "wxbg.image_prepare", str(source), str(destination)],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(failed.stderr)["error"]["code"], "destination_exists")

    def test_module_import_does_not_import_pillow(self):
        code = """
import builtins
import sys
sys.path.insert(0, sys.argv[1])
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'PIL' or name.startswith('PIL.'):
        raise AssertionError('Pillow imported at module import time')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import wxbg.image_prepare
print('ok')
"""
        completed = subprocess.run(
            [sys.executable, "-c", code, str(SRC)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
