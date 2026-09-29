"""Synthetic, account-bound group image XML wrapper regressions."""

from __future__ import annotations

import hashlib
import sqlite3
import time
import unittest

from wxbg.readstore_attachment_query import query_attachment
from wxbg.readstore_image_declaration import (
    ImageDeclarationError,
    MAX_XML_BYTES,
    parse_image_declaration,
)
from wxbg.readstore_query import ReadStoreError


MD5 = "a" * 32
LOCATOR = b"\x1a\x22\x22\x20" + b"b" * 32
XML = f'<msg><img md5="{MD5}" length="2"/></msg>'
DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'
EPOCH = "synthetic-epoch"


def query_one(conversation_key: str, body: str) -> dict[str, object]:
    """Read a single synthetic image row through the real exact-row query."""

    chat_md5 = hashlib.md5(conversation_key.encode("utf-8")).hexdigest()
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            f'CREATE TABLE "Msg_{chat_md5}" ('
            "local_id INTEGER, local_type INTEGER, create_time INTEGER, "
            "message_content TEXT, server_id INTEGER, packed_info_data BLOB)"
        )
        connection.execute(
            f'INSERT INTO "Msg_{chat_md5}" '
            "(local_id, local_type, create_time, message_content, "
            "server_id, packed_info_data) VALUES (?, ?, ?, ?, ?, ?)",
            (7, 3, 123, body, 99, LOCATOR),
        )
        identity = {
            "chat_md5": chat_md5,
            "shard_id": "message_0",
            "local_id": 7,
            "server_id": 99,
            "rowid": 1,
        }
        return query_attachment(
            {"message_0": connection},
            account_epoch=EPOCH,
            expected_account_epoch=EPOCH,
            conversation_key=conversation_key,
            message_identity=identity,
            deadline=time.monotonic() + 3,
        )
    finally:
        connection.close()


class GroupImageXmlWrapperTests(unittest.TestCase):
    def assert_query_succeeds(self, key: str, body: str) -> dict[str, object]:
        try:
            return query_one(key, body)
        except ReadStoreError as error:
            self.fail(f"expected a group image declaration, got {error.code}")

    def assert_declaration_invalid(self, key: str, body: str) -> None:
        with self.assertRaises(ReadStoreError) as raised:
            query_one(key, body)
        self.assertEqual(raised.exception.code, "declaration_invalid")

    def test_group_image_accepts_one_sender_line_then_utf8_xml_declaration(self):
        result = self.assert_query_succeeds(
            "synthetic-group@chatroom", "synthetic_sender:\n" + DECLARATION + XML
        )
        self.assertEqual(result["declaration"]["content_md5"], MD5)
        self.assertEqual(result["declaration"]["size_bytes"], 2)
        self.assertEqual(result["cache_locator"], {"candidate_basename": "b" * 32})
        self.assertNotIn("sender", result["declaration"])

    def test_group_image_accepts_crlf_sender_line(self):
        result = self.assert_query_succeeds(
            "synthetic-group@chatroom", "synthetic_sender:\r\n" + DECLARATION + XML
        )
        self.assertEqual(result["declaration"]["content_md5"], MD5)

    def test_group_plain_xml_remains_valid(self):
        result = query_one("synthetic-group@chatroom", DECLARATION + XML)
        self.assertEqual(result["declaration"]["content_md5"], MD5)

    def test_direct_parser_stays_strict_without_group_context(self):
        with self.assertRaises(ImageDeclarationError) as raised:
            parse_image_declaration(
                "synthetic_sender:\n" + DECLARATION + XML,
                local_type=3,
            )
        self.assertEqual(raised.exception.code, "invalid_xml")

    def test_non_group_does_not_accept_sender_wrapper(self):
        self.assert_declaration_invalid(
            "synthetic-contact", "synthetic_sender:\n" + DECLARATION + XML
        )

    def test_missing_or_multiple_sender_lines_are_rejected(self):
        key = "synthetic-group@chatroom"
        for index, body in enumerate((
            "synthetic_sender\n" + DECLARATION + XML,
            "synthetic_sender:\nsecond_sender:\n" + DECLARATION + XML,
            "arbitrary introduction " + DECLARATION + XML,
            "synthetic_sender:\n" + XML,
        )):
            with self.subTest(index=index):
                self.assert_declaration_invalid(key, body)

    def test_overlong_or_angle_sender_is_rejected(self):
        key = "synthetic-group@chatroom"
        for sender in ("x" * 81, "synthetic<sender", "synthetic>sender", "synthetic\x01sender"):
            with self.subTest(kind="overlong" if len(sender) > 80 else "angle"):
                self.assert_declaration_invalid(key, sender + ":\n" + DECLARATION + XML)

    def test_xml_declaration_must_immediately_follow_sender_line(self):
        self.assert_declaration_invalid(
            "synthetic-group@chatroom", "synthetic_sender:\n \t" + DECLARATION + XML
        )

    def test_trailing_data_and_second_xml_document_are_rejected(self):
        key = "synthetic-group@chatroom"
        for suffix in ("junk", XML):
            with self.subTest(suffix_kind="junk" if suffix == "junk" else "second_doc"):
                self.assert_declaration_invalid(key, "synthetic_sender:\n" + DECLARATION + XML + suffix)

    def test_inner_security_and_declared_hash_bounds_remain_enforced(self):
        key = "synthetic-group@chatroom"
        bad_xmls = (
            '<!DOCTYPE msg [<!ENTITY x "y">]>' + XML,
            XML.replace("<msg>", '<msg xmlns="urn:synthetic">'),
            XML.replace(MD5, "not-an-md5"),
            XML.replace('length="2"', 'length="0"'),
        )
        for index, xml in enumerate(bad_xmls):
            with self.subTest(index=index):
                self.assert_declaration_invalid(key, "synthetic_sender:\n" + DECLARATION + xml)

    def test_non_utf8_xml_declaration_remains_rejected(self):
        self.assert_declaration_invalid(
            "synthetic-group@chatroom",
            "synthetic_sender:\n"
            + DECLARATION.replace("UTF-8", "GBK")
            + XML,
        )

    def test_full_body_bound_is_checked_before_sender_line_removal(self):
        suffix = DECLARATION + XML
        body = "s:\n" + suffix + " " * (MAX_XML_BYTES - len(suffix))
        with self.assertRaises(ImageDeclarationError) as raised:
            parse_image_declaration(body, local_type=3, allow_group_sender_line=True)
        self.assertEqual(raised.exception.code, "xml_too_large")

    def test_xml_security_codes_remain_specific_after_sender_line_removal(self):
        for xml, code in (
            ('<!DOCTYPE msg [<!ENTITY x "y">]>' + XML, "xml_dtd_forbidden"),
            (XML.replace("<msg>", '<msg xmlns="urn:synthetic">'),
             "xml_namespace_forbidden"),
            (XML.replace(MD5, "not-an-md5"), "md5_invalid"),
            (XML.replace('length="2"', 'length="0"'), "length_invalid"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ImageDeclarationError) as raised:
                    parse_image_declaration(
                        "synthetic_sender:\n" + DECLARATION + xml,
                        local_type=3,
                        allow_group_sender_line=True,
                    )
                self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
