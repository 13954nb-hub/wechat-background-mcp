"""Parse one bounded, source-confirmed received-file XML declaration.

This is a pure parser.  It never opens a file, resolves a URL, accesses a
database, or performs a live-client operation.  The current Weixin source
type is ``(6 << 32) | 49``; legacy ``49`` is accepted only when the XML also
contains the exact direct ``appmsg/type`` value ``6``.  Only a safe leaf name,
declared size, and declared MD5 are returned.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any


FILE_MESSAGE_TYPE = (6 << 32) | 49
LEGACY_FILE_MESSAGE_TYPE = 49
MAX_XML_BYTES = 200_000
MIN_FILE_SIZE_BYTES = 1
MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024
MAX_FILENAME_UTF16_UNITS = 255

_MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")
_DECIMAL = re.compile(r"[0-9]+\Z")
_DTD_OR_ENTITY = re.compile(
    rb"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)\b", re.I
)
_NAMESPACE_DECLARATION = re.compile(
    rb"<[^>]*\bxmlns(?::[A-Za-z_][\w.-]*)?\s*=", re.I | re.S
)
_INVALID_FILENAME_CHARS = frozenset('/\\:*?<>|"')
_RESERVED_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL"})


class FileDeclarationError(ValueError):
    """Stable parser failure with no source content in the message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise FileDeclarationError(code)


def _xml_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        if str.__len__(value) > MAX_XML_BYTES:
            _fail("xml_too_large")
        try:
            payload = str.encode(value, "utf-8", "strict")
        except UnicodeEncodeError:
            _fail("invalid_xml")
    elif isinstance(value, memoryview):
        if value.nbytes > MAX_XML_BYTES:
            _fail("xml_too_large")
        try:
            payload = bytes(value)
        except (TypeError, ValueError):
            _fail("invalid_xml")
    elif isinstance(value, (bytes, bytearray)):
        if len(value) > MAX_XML_BYTES:
            _fail("xml_too_large")
        try:
            payload = bytes(value)
        except (TypeError, ValueError):
            _fail("invalid_xml")
    else:
        _fail("invalid_xml_input")
    if len(payload) > MAX_XML_BYTES:
        _fail("xml_too_large")
    try:
        payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail("invalid_xml")
    if b"\x00" in payload or not payload.strip():
        _fail("invalid_xml")
    if _DTD_OR_ENTITY.search(payload):
        _fail("xml_dtd_forbidden")
    if _NAMESPACE_DECLARATION.search(payload):
        _fail("xml_namespace_forbidden")
    return payload


def _parse(payload: bytes) -> ET.Element:
    try:
        root = ET.fromstring(payload.decode("utf-8", "strict"))
    except ET.ParseError:
        _fail("invalid_xml")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail("invalid_xml")
    return root


def _check_no_namespace(root: ET.Element) -> None:
    for node in root.iter():
        tag = node.tag
        if not isinstance(tag, str) or "{" in tag or "}" in tag or ":" in tag:
            _fail("xml_namespace_forbidden")
        for attr in node.attrib:
            if "{" in attr or "}" in attr or ":" in attr:
                _fail("xml_namespace_forbidden")


def _direct(parent: ET.Element, name: str, missing: str, ambiguous: str) -> ET.Element:
    nodes = [child for child in list(parent) if child.tag == name]
    if not nodes:
        _fail(missing)
    if len(nodes) != 1:
        _fail(ambiguous)
    return nodes[0]


def _leaf_text(node: ET.Element, code: str, *, strip: bool = True) -> str:
    if list(node):
        _fail(code)
    value = node.text or ""
    if not isinstance(value, str):
        _fail(code)
    return value.strip() if strip else value


def _safe_filename(value: str) -> str:
    if not value or value in (".", ".."):
        _fail("filename_invalid")
    if any(
        ch in _INVALID_FILENAME_CHARS
        or ord(ch) < 32
        or ord(ch) == 127
        or 0x80 <= ord(ch) <= 0x9F
        for ch in value
    ):
        _fail("filename_invalid")
    if value.endswith((" ", ".")):
        _fail("filename_invalid")
    try:
        units = len(value.encode("utf-16-le", "strict")) // 2
    except UnicodeEncodeError:
        _fail("filename_invalid")
    if not 1 <= units <= MAX_FILENAME_UTF16_UNITS:
        _fail("filename_invalid")
    stem = value.rstrip(" .").split(".", 1)[0].upper()
    if stem in _RESERVED_DEVICES or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
        _fail("filename_invalid")
    return value


def _parse_declared_type(node: ET.Element) -> None:
    value = _leaf_text(node, "type_invalid")
    if value != "6":
        _fail("type_mismatch")


def _parse_size(node: ET.Element) -> int:
    value = _leaf_text(node, "size_invalid")
    if _DECIMAL.fullmatch(value) is None:
        _fail("size_invalid")
    try:
        size = int(value, 10)
    except (TypeError, ValueError, OverflowError):
        _fail("size_invalid")
    if not MIN_FILE_SIZE_BYTES <= size <= MAX_FILE_SIZE_BYTES:
        _fail("size_invalid")
    return size


def _parse_md5(node: ET.Element) -> str:
    value = _leaf_text(node, "md5_invalid")
    if _MD5.fullmatch(value) is None:
        _fail("md5_invalid")
    return value.lower()


def parse_file_declaration(
    text: str | bytes | bytearray | memoryview,
    *,
    local_type: int,
) -> dict[str, object]:
    """Return a bounded original-file declaration from one XML message body.

    The declaration must have a direct ``msg/appmsg`` shape with exactly one
    direct ``type=6``, ``title``, ``md5``, and ``appattach/totallen`` node.
    Nested thumbnail hashes, URLs, keys, and ``recorditem`` payloads are never
    used as declarations.
    """

    if type(local_type) is not int or local_type not in (
        FILE_MESSAGE_TYPE,
        LEGACY_FILE_MESSAGE_TYPE,
    ):
        _fail("unsupported_local_type")
    payload = _xml_bytes(text)
    root = _parse(payload)
    _check_no_namespace(root)
    if root.tag != "msg":
        _fail("wrong_root")
    appmsg = _direct(root, "appmsg", "appmsg_missing", "appmsg_ambiguous")
    type_node = _direct(appmsg, "type", "type_missing", "type_ambiguous")
    title_node = _direct(appmsg, "title", "title_missing", "title_ambiguous")
    md5_node = _direct(appmsg, "md5", "md5_missing", "md5_ambiguous")
    appattach = _direct(
        appmsg,
        "appattach",
        "appattach_missing",
        "appattach_ambiguous",
    )
    totallen = _direct(
        appattach,
        "totallen",
        "totallen_missing",
        "totallen_ambiguous",
    )
    _parse_declared_type(type_node)
    filename = _safe_filename(
        _leaf_text(title_node, "filename_invalid", strip=False)
    )
    content_md5 = _parse_md5(md5_node)
    size_bytes = _parse_size(totallen)
    return {
        "filename": filename,
        "size_bytes": size_bytes,
        "content_md5": content_md5,
        "media_kind": "file",
        "representation": "original",
    }


__all__ = [
    "FILE_MESSAGE_TYPE",
    "FileDeclarationError",
    "LEGACY_FILE_MESSAGE_TYPE",
    "MAX_FILENAME_UTF16_UNITS",
    "MAX_FILE_SIZE_BYTES",
    "MAX_XML_BYTES",
    "parse_file_declaration",
]
