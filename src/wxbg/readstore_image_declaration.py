"""Parse one bounded declaration for a message image.

The parser deliberately reports declarations only.  A ``msg/img`` attribute
is evidence about what the message XML declares; it is not proof that a cache
file exists, that the bytes are an original image, or that a rendition role
has been verified.  No file, client, database, URL, key, or process is
accessed here.

The current image shape supplies the primary ``md5`` and ``length``
attributes.  The optional ``hdlength`` and ``originsourcemd5`` attributes are
kept as separate declared values.  They never change ``representation`` from
``message_image`` to ``original``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any


IMAGE_LOCAL_TYPE = 3
MAX_XML_BYTES = 200_000
MIN_IMAGE_SIZE_BYTES = 1
MAX_IMAGE_SIZE_BYTES = 16 * 1024 * 1024

_MD5 = re.compile(r"[0-9a-fA-F]{32}\Z")
_DECIMAL = re.compile(r"[0-9]+\Z")
_DTD_OR_ENTITY = re.compile(
    rb"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)\b", re.I
)
_NAMESPACE_DECLARATION = re.compile(
    rb"<[^>]*\bxmlns(?::[A-Za-z_][\w.-]*)?\s*=", re.I | re.S
)
_XML_DECLARATION = re.compile(
    r"\A(?:\ufeff)?\s*<\?xml\b(?P<body>[^>]*)\?>", re.I | re.S
)
_XML_ENCODING = re.compile(
    r"(?:^|\s)encoding\s*=\s*(['\"])([^'\"]+)\1", re.I
)
_GROUP_SENDER_LINE = re.compile(
    r"\A(?P<sender>[^\r\n<>:&\x00]{1,80}):\r?\n(?=<\?xml\b)"
)


class ImageDeclarationError(ValueError):
    """Stable parser failure that never includes source XML or secrets."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ImageDeclarationError(code)


def _xml_bytes(value: Any) -> bytes:
    """Return strict UTF-8 XML bytes after applying the byte-size bound."""

    if isinstance(value, str):
        # Check both the cheap character bound and the required UTF-8 byte
        # bound.  The latter matters for CJK and astral characters.
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
        xml_text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail("invalid_xml")
    # ET.fromstring(bytes) auto-detects UTF-16 from NUL placement, which would
    # let a UTF-16 entity declaration evade the UTF-8 DTD scan above.  Keep a
    # single encoding contract and pass decoded text to ET instead.
    if "\x00" in xml_text:
        _fail("invalid_xml")
    declaration = _XML_DECLARATION.match(xml_text)
    if declaration is not None:
        encoding = _XML_ENCODING.search(declaration.group("body"))
        if encoding is not None:
            normalized = encoding.group(2).lower().replace("_", "-")
            if normalized not in ("utf-8", "utf8"):
                _fail("invalid_xml")
    if not payload.strip():
        _fail("invalid_xml")
    if _DTD_OR_ENTITY.search(payload):
        _fail("xml_dtd_forbidden")
    if _NAMESPACE_DECLARATION.search(payload):
        _fail("xml_namespace_forbidden")
    return payload


def _parse(payload: bytes) -> ET.Element:
    try:
        # _xml_bytes has already validated UTF-8 and the byte bound.  Parsing
        # the decoded string prevents ElementTree's bytes auto-detection from
        # accepting a different XML encoding.
        return ET.fromstring(payload.decode("utf-8", "strict"))
    except ET.ParseError:
        _fail("invalid_xml")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail("invalid_xml")
    raise AssertionError("unreachable")


def _group_xml_payload(payload: bytes) -> bytes:
    """Unwrap one exact group sender line before a complete XML declaration.

    The full body has already passed ``_xml_bytes``.  Rechecking the suffix is
    essential: its XML encoding declaration was not at the beginning of the
    original body and therefore was not checked by the first pass.
    """

    text = payload.decode("utf-8", "strict")
    match = _GROUP_SENDER_LINE.match(text)
    if match is None:
        return payload
    sender = match.group("sender")
    if sender != sender.strip() or not all(ch.isprintable() for ch in sender):
        return payload
    return _xml_bytes(text[match.end():])


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


def _parse_md5(value: str, code: str) -> str:
    if _MD5.fullmatch(value) is None:
        _fail(code)
    return value.lower()


def _parse_size(value: str, code: str) -> int:
    if _DECIMAL.fullmatch(value) is None:
        _fail(code)
    try:
        size = int(value, 10)
    except (TypeError, ValueError, OverflowError):
        _fail(code)
    if not MIN_IMAGE_SIZE_BYTES <= size <= MAX_IMAGE_SIZE_BYTES:
        _fail(code)
    return size


def _optional_attribute(node: ET.Element, name: str, parser, code: str):
    if name not in node.attrib:
        return None
    return parser(node.attrib[name], code)


def parse_image_declaration(
    text: str | bytes | bytearray | memoryview,
    *,
    local_type: int,
    allow_group_sender_line: bool = False,
) -> dict[str, object]:
    """Return bounded, declaration-only metadata for one message image.

    ``local_type`` is intentionally strict: only the integer ``3`` is
    accepted, and ``True`` is not treated as an integer image type.  The XML
    root must be ``msg`` with exactly one direct ``img`` child.  A trusted
    exact-row group query may explicitly allow one bounded sender line
    immediately before an XML declaration; the sender text is discarded.
    Only the direct ``img`` child's primary ``md5``/``length`` and optional
    ``hdlength``/``originsourcemd5`` attributes are used; nested thumbnail,
    live, URL, and AES-looking fields are ignored and never returned.

    Optional output names are normalized to ``hd_size_bytes`` and
    ``origin_source_md5``.  Their presence remains declaration evidence only.
    """

    if type(local_type) is not int or local_type != IMAGE_LOCAL_TYPE:
        _fail("unsupported_local_type")
    if type(allow_group_sender_line) is not bool:
        _fail("invalid_xml_input")

    payload = _xml_bytes(text)
    if allow_group_sender_line:
        payload = _group_xml_payload(payload)
    root = _parse(payload)
    _check_no_namespace(root)
    if root.tag != "msg":
        _fail("wrong_root")
    image = _direct(root, "img", "img_missing", "img_ambiguous")

    if "md5" not in image.attrib:
        _fail("md5_missing")
    content_md5 = _parse_md5(image.attrib["md5"], "md5_invalid")
    if "length" not in image.attrib:
        _fail("length_missing")
    size_bytes = _parse_size(image.attrib["length"], "length_invalid")

    result: dict[str, object] = {
        "status": "declared_only",
        "content_md5": content_md5,
        "size_bytes": size_bytes,
        "media_kind": "image",
        "representation": "message_image",
    }
    hd_size_bytes = _optional_attribute(
        image, "hdlength", _parse_size, "hdlength_invalid"
    )
    if hd_size_bytes is not None:
        result["hd_size_bytes"] = hd_size_bytes
    origin_source_md5 = _optional_attribute(
        image,
        "originsourcemd5",
        _parse_md5,
        "originsourcemd5_invalid",
    )
    if origin_source_md5 is not None:
        result["origin_source_md5"] = origin_source_md5
    return result


__all__ = [
    "IMAGE_LOCAL_TYPE",
    "ImageDeclarationError",
    "MAX_IMAGE_SIZE_BYTES",
    "MAX_XML_BYTES",
    "MIN_IMAGE_SIZE_BYTES",
    "parse_image_declaration",
]
