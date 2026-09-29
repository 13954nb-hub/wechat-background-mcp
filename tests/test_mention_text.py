"""Literal @username construction has no Windows or Weixin dependency."""

import unittest

from wxbg.policy import AdapterError
from wxbg.mention_text import build_at_username_text


class MentionTextTests(unittest.TestCase):
    def test_exact_username_without_body(self):
        self.assertEqual(build_at_username_text('寧', ''), '@寧')

    def test_optional_body_uses_one_separator(self):
        self.assertEqual(build_at_username_text('寧', '你好'), '@寧 你好')

    def test_unicode_name_is_preserved_without_normalization(self):
        self.assertEqual(build_at_username_text('A 🐱 B', ''), '@A 🐱 B')

    def test_rejects_ambiguous_or_control_username(self):
        for username in ('', ' 寧', '寧 ', '@寧', '寧@甲', '寧\n甲', '寧\x00甲', '\ufffc', 'x' * 129):
            with self.subTest(username=username), self.assertRaises(AdapterError):
                build_at_username_text(username, '')

    def test_rejects_non_string_and_oversize_message(self):
        for username, body in ((None, ''), ('寧', None), ('寧', 'x' * 10000)):
            with self.subTest(username=username), self.assertRaises(AdapterError):
                build_at_username_text(username, body)


if __name__ == '__main__':
    unittest.main()
