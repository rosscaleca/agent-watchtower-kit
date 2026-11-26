#!/usr/bin/env python3
"""``_fields.who_field``, the one helper every daemon passes a person's name through.

Run: `just test` (or `python3 -m unittest discover -s tests`).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "daemons"))

import _fields  # noqa: E402


class WhoField(unittest.TestCase):
    def test_an_ordinary_name_is_unchanged(self):
        for name in ("Bob Reviewer", "Alice C", "José O'Neil-Smith", "x" * _fields.WHO_CAP):
            self.assertEqual(_fields.who_field(name), name)

    def test_whitespace_runs_and_newlines_collapse_to_one_space(self):
        self.assertEqual(_fields.who_field('a"b\n c'), "a'b c")
        self.assertEqual(_fields.who_field("a\r\n\t b"), "a b")

    def test_every_line_separator_is_flattened(self):
        for sep in ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            out = _fields.who_field("a%sb" % sep)
            self.assertEqual(out, "a b", repr(sep))
            self.assertEqual(len(out.splitlines()), 1, repr(sep))

    def test_double_quotes_become_single(self):
        self.assertEqual(_fields.who_field('say "hi"'), "say 'hi'")

    def test_it_is_capped(self):
        self.assertEqual(len(_fields.who_field("x" * 200)), _fields.WHO_CAP)

    def test_nothing_is_a_question_mark(self):
        self.assertEqual(_fields.who_field(None), "?")
        self.assertEqual(_fields.who_field(""), "?")
        self.assertEqual(_fields.who_field("   "), "?")
        self.assertEqual(_fields.who_field("\x1b\x00"), "?")

    def test_a_name_of_only_joiners_and_whitespace_is_a_question_mark(self):
        # U+200C and U+200D are kept inside a name, but a name with nothing else in it shows nothing.
        for name in ("‌", "‍", "‌‍", "‍‌‍", " ‍ ", "‌ ‍\t\n",
                     "‌ ‌ ‌", "\x1b‍​"):
            self.assertEqual(_fields.who_field(name), "?", repr(name))
        # a joiner beside a visible character is still kept
        self.assertEqual(_fields.who_field("‍a"), "‍a")

    def test_a_name_of_joiners_and_default_ignorables_is_a_question_mark(self):
        # U+3164 and U+115F are Lo, U+034F and U+FE0F are Mn: not Cc or Cf, yet they show nothing.
        zwj, zwnj = chr(0x200D), chr(0x200C)
        for cp in (0x3164, 0x115F, 0x034F, 0xFE0F):
            ch = chr(cp)
            for name in (ch, ch * 3, zwj + ch, ch + zwj, zwj + ch + zwnj, " " + zwj + ch + " "):
                self.assertEqual(_fields.who_field(name), "?", repr(name))
        self.assertEqual(_fields.who_field(chr(0x3164) + chr(0x034F) + chr(0xFE0F) + chr(0x200D)), "?")

    def test_a_default_ignorable_beside_a_readable_character_stays(self):
        emoji = chr(0x2764) + chr(0xFE0F)                  # a heart written with VARIATION SELECTOR-16
        self.assertEqual(_fields.who_field(emoji), emoji)
        self.assertEqual(_fields.who_field("Alice " + emoji), "Alice " + emoji)
        accent = "e" + chr(0x034F) + chr(0x0301)           # a letter with COMBINING GRAPHEME JOINER
        self.assertEqual(_fields.who_field(accent), accent)
        self.assertEqual(_fields.who_field("a" + chr(0x034F)), "a" + chr(0x034F))
        # unshown_dropped still drops only Cc and Cf
        self.assertEqual(_fields.unshown_dropped(emoji), emoji)

    def test_is_default_ignorable(self):
        for cp in (0xAD, 0x034F, 0x061C, 0x115F, 0x1160, 0x180B, 0x200B, 0x2060, 0x3164, 0xFE0F, 0xFEFF,
                   0xFFA0, 0xE0001, 0xE0FFF):
            self.assertTrue(_fields.is_default_ignorable(chr(cp)), hex(cp))
        for ch in ("a", " ", "1", chr(0x2764), chr(0x0301), chr(0x1161)):
            self.assertFalse(_fields.is_default_ignorable(ch), repr(ch))

    def test_controls_and_bidi_marks_are_dropped(self):
        for mark in ("\x1b", "\x00", "\x7f", "\u200e", "\u200f", "\u202a", "\u202e", "\u2066", "\u2069",
                     "\x9b", "\x80", "\u200b", "\u2060", "\ufeff", "\xad"):
            self.assertEqual(_fields.who_field("Mal%slory" % mark), "Mallory", repr(mark))
        self.assertEqual(_fields.who_field("\x1b[31mRoss\x1b[0m"), "[31mRoss[0m")

    def test_the_two_joiners_are_kept(self):
        persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"            # U+200C joins nothing: it keeps two letters apart
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"   # an emoji ZWJ sequence
        for name in (persian, family, "a\u200db"):
            self.assertEqual(_fields.who_field(name), name, repr(name))
            self.assertEqual(_fields.unshown_dropped(name), name, repr(name))

    def test_everything_else_in_the_two_categories_is_still_dropped(self):
        self.assertEqual(_fields.unshown_dropped("a\u202eb\u200bc\u200cd\u200de"), "abc\u200cd\u200de")
        self.assertEqual(_fields.unshown_dropped("a\U000e0067b"), "ab")   # a tag character


if __name__ == "__main__":
    unittest.main()
