"""Owned, offline cache-image fixtures; no WeChat database or process access."""

import hashlib
import io
from pathlib import Path
import struct
import tempfile
import time
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from wxbg.readstore_attachment_provider import inspect_image
from wxbg.readstore_downloaded_file import DownloadError
from wxbg.readstore_downloaded_image import read_downloaded_image
from wxbg.readstore_image_container import V2_MAGIC


_CHAT = "a" * 32
_LOCATOR = "b" * 32
_AES_KEY = bytes(range(16))
_XOR_KEY = 0x5A


def _image_bytes(image_format, *, lossless=False):
    image = Image.new("RGB", (3, 2), (35, 90, 160))
    output = io.BytesIO()
    options = {"lossless": lossless} if image_format == "WEBP" else {}
    image.save(output, format=image_format, **options)
    return output.getvalue()


def _v2_container(plain):
    aes_size = min(32, len(plain))
    xor_size = min(17, len(plain) - aes_size)
    first = plain[:aes_size]
    pad = 16 - len(first) % 16
    encryptor = Cipher(algorithms.AES(_AES_KEY), modes.ECB()).encryptor()
    cipher = encryptor.update(first + bytes([pad]) * pad) + encryptor.finalize()
    middle = plain[aes_size:len(plain) - xor_size] if xor_size else plain[aes_size:]
    tail = bytes(value ^ _XOR_KEY for value in plain[-xor_size:]) if xor_size else b""
    return V2_MAGIC + struct.pack("<II", aes_size, xor_size) + b"\x00" + cipher + middle + tail


class DownloadedImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="owned-image-cache-")
        self.addCleanup(self.temp.cleanup)
        self.account = Path(self.temp.name) / "owned-account"
        self.folder = self.account / "msg" / "attach" / _CHAT / "2026-09" / "Img"
        self.folder.mkdir(parents=True)
        # Windows tempfile may use an 8.3 parent name; the production reader
        # intentionally requires canonical paths before opening a cache file.
        self.account = self.account.resolve(strict=True)
        self.folder = self.folder.resolve(strict=True)

    def _cache(self, data, *, suffix=".dat"):
        path = self.folder / (_LOCATOR + suffix)
        path.write_bytes(_v2_container(data))
        return path

    def _read(self, data):
        declaration = {
            "size_bytes": len(data),
            "content_md5": hashlib.md5(data).hexdigest(),
            "media_kind": "image",
            "representation": "message_image",
        }
        return read_downloaded_image(
            self.account, _CHAT, _LOCATOR, declaration,
            aes_key=_AES_KEY, xor_key=_XOR_KEY, deadline=time.monotonic() + 5,
        )

    def test_cached_static_jpeg_webp_and_png_decode_and_validate(self):
        cases = (
            ("JPEG", False, ".jpg", "image/jpeg"),
            ("WEBP", False, ".webp", "image/webp"),
            ("WEBP", True, ".webp", "image/webp"),
            ("PNG", False, ".png", "image/png"),
        )
        for image_format, lossless, extension, mime_type in cases:
            with self.subTest(image_format=image_format, lossless=lossless):
                source = _image_bytes(image_format, lossless=lossless)
                self._cache(source)
                downloaded = self._read(source)
                self.assertEqual(downloaded.data, source)
                self.assertEqual(downloaded.sha256, hashlib.sha256(source).hexdigest())
                self.assertTrue(downloaded.evidence["image_container_decoded"])
                self.assertTrue(downloaded.evidence["declared_size_matched"])
                self.assertTrue(downloaded.evidence["declared_md5_matched"])
                self.assertFalse(downloaded.evidence["sender_authenticity_verified"])
                image = inspect_image(downloaded.data)
                self.assertEqual(image["format"], image_format)
                self.assertEqual(image["mime_type"], mime_type)
                self.assertEqual(image["extension"], extension)
                self.assertEqual((image["width"], image["height"]), (3, 2))

    def test_same_image_in_both_cache_renditions_is_unambiguous(self):
        source = _image_bytes("JPEG")
        self._cache(source, suffix=".dat")
        self._cache(source, suffix="_h.dat")
        downloaded = self._read(source)
        self.assertEqual(downloaded.data, source)
        self.assertEqual(inspect_image(downloaded.data)["format"], "JPEG")

    def test_different_message_bytes_do_not_pass_checksum_binding(self):
        declared = _image_bytes("JPEG")
        self._cache(_image_bytes("PNG"))
        with self.assertRaises(DownloadError) as caught:
            self._read(declared)
        self.assertEqual(caught.exception.code, "attachment_not_downloaded")

    def test_checksum_bound_non_image_is_rejected_by_format_gate(self):
        declared = b"owned non-image bytes"
        self._cache(declared)
        downloaded = self._read(declared)
        with self.assertRaises(DownloadError) as caught:
            inspect_image(downloaded.data)
        self.assertEqual(caught.exception.code, "attachment_image_invalid")

    def test_checksum_bound_animated_webp_is_rejected_by_format_gate(self):
        first = Image.new("RGB", (3, 2), "red")
        second = Image.new("RGB", (3, 2), "blue")
        output = io.BytesIO()
        first.save(output, format="WEBP", save_all=True,
                   append_images=[second], duration=20, loop=0)
        declared = output.getvalue()
        self._cache(declared)
        downloaded = self._read(declared)
        with self.assertRaises(DownloadError) as caught:
            inspect_image(downloaded.data)
        self.assertEqual(caught.exception.code, "attachment_image_invalid")

    def test_checksum_bound_truncated_jpeg_is_rejected_by_format_gate(self):
        declared = _image_bytes("JPEG")[:-8]
        self._cache(declared)
        downloaded = self._read(declared)
        with self.assertRaises(DownloadError) as caught:
            inspect_image(downloaded.data)
        self.assertEqual(caught.exception.code, "attachment_image_invalid")


if __name__ == "__main__":
    unittest.main()
