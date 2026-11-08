import os
import plistlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import strings


def write_plist(directory, name, payload):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        plistlib.dump(payload, handle, fmt=plistlib.FMT_BINARY)
    return path


class NormalizeLangTests(unittest.TestCase):
    def test_a_region_qualified_tag_falls_back_to_its_subtag(self):
        self.assertEqual(strings.normalize_lang("ko-US"), ["ko-US", "ko_US", "ko"])

    def test_a_script_qualified_tag_tries_every_prefix_and_the_region_spelling(self):
        """The runtime ships zh-Hans.lproj and zh_CN.lproj and NO zh.lproj."""
        found = strings.normalize_lang("zh-Hans-CN")
        self.assertEqual(found[0], "zh-Hans-CN")
        for wanted in ("zh-Hans", "zh_Hans", "zh_CN", "zh"):
            self.assertIn(wanted, found)

    def test_a_script_subtag_reaches_the_region_directory_the_runtime_actually_ships(self):
        """iOS 26.5 ships zh-Hans.lproj in 3 bundles and zh_CN.lproj in 2,763. Resolving the script
        spelling alone yields a near-empty store, so the conventional region spelling must be tried."""
        hans = strings.normalize_lang("zh-Hans")
        self.assertIn("zh_CN", hans)
        self.assertIn("zh-Hans", hans)
        hant = strings.normalize_lang("zh-Hant")
        self.assertIn("zh_TW", hant)
        self.assertIn("zh_HK", hant)

    def test_a_script_and_region_tag_still_reaches_the_region_directory(self):
        self.assertIn("zh_CN", strings.normalize_lang("zh-Hans-CN"))

    def test_a_tag_with_no_script_gains_no_region_guesses(self):
        self.assertEqual(strings.normalize_lang("ko-US"), ["ko-US", "ko_US", "ko"])

    def test_a_bare_tag_stands_alone(self):
        self.assertEqual(strings.normalize_lang("ja"), ["ja"])

    def test_a_tag_that_is_not_a_language_is_refused(self):
        for bad in ("", None, "../../etc", "en;rm -rf", "x" * 40, "e n"):
            self.assertEqual(strings.normalize_lang(bad), [], bad)

    def test_english_is_recognised_with_or_without_a_region(self):
        self.assertTrue(strings.is_english("en"))
        self.assertTrue(strings.is_english("en-GB"))
        self.assertFalse(strings.is_english("ko-US"))
        self.assertFalse(strings.is_english(None))

    def test_a_script_and_region_qualified_english_is_still_english(self):
        """`candidates[-1]` is `en_US` at three subtags, so the tail never spelled the language."""
        self.assertTrue(strings.is_english("en-Latn-US"))
        self.assertTrue(strings.is_english("en_Latn_US"))
        self.assertFalse(strings.is_english("zh-Hans-CN"))


class UnescapeTests(unittest.TestCase):
    def test_a_surrogate_pair_becomes_one_astral_character(self):
        """Lone surrogates cannot be encoded, and sqlite raises on the whole executemany."""
        self.assertEqual(strings._unescape(r"\UD83D\UDE00"), "\U0001f600")
        self.assertEqual(strings._unescape(r"\UD83D\UDE00").encode("utf-8"), b"\xf0\x9f\x98\x80")

    def test_an_unpaired_surrogate_is_still_encodable(self):
        for text in (r"\UD83D", r"\UDE00x", r"\UD83Dz\UDE00"):
            strings._unescape(text).encode("utf-8")


class LoadStringsTests(unittest.TestCase):
    def test_a_binary_plist_loads(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_plist(root, "L.strings", {"A": "Allow"})
            self.assertEqual(strings.load_strings(path), {"A": "Allow"})

    def test_a_utf16_text_strings_file_keeps_its_non_ascii_intact(self):
        """App bundles ship UTF-16 text .strings; `unicode_escape` would render 일반 as mojibake."""
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "L.strings")
            with open(path, "w", encoding="utf-16") as handle:
                handle.write('/* c */\n"G" = "일반";\n"N"="a\\nb";\n"U"="\\U00ac";\n')
            loaded = strings.load_strings(path)
            self.assertEqual(loaded["G"], "일반")
            self.assertEqual(loaded["N"], "a\nb")
            self.assertEqual(loaded["U"], "\u00ac")

    def test_a_utf8_text_strings_file_loads(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "L.strings")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('"K" = "전체 접근 허용";\n')
            self.assertEqual(strings.load_strings(path), {"K": "전체 접근 허용"})

    def test_an_unreadable_file_is_an_empty_catalogue_not_a_crash(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "B.strings")
            with open(path, "wb") as handle:
                handle.write(b"\x00\x01not a plist")
            self.assertEqual(strings.load_strings(path), {})


class IterPairsTests(unittest.TestCase):
    def tree(self, root):
        bundle = os.path.join(root, "Some.bundle")
        write_plist(os.path.join(bundle, "en.lproj"), "K.strings", {
            "GENERAL": "General",
            "NET": "Allow Full Access",
            "SAME": "Slyder",
            "PROSE": "x" * (strings.VALUE_CAP + 1),
        })
        write_plist(os.path.join(bundle, "ko.lproj"), "K.strings", {
            "GENERAL": "일반",
            "NET": "전체 접근 허용",
            "SAME": "Slyder",
            "PROSE": "y" * (strings.VALUE_CAP + 1),
        })
        return bundle

    def pairs(self, root, lang):
        return sorted(set(strings.iter_pairs([root], lang)))

    def test_paired_lproj_files_yield_english_localized_tuples(self):
        with tempfile.TemporaryDirectory() as root:
            self.tree(root)
            self.assertIn(("Allow Full Access", "전체 접근 허용"), self.pairs(root, "ko"))

    def test_untranslated_and_oversized_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as root:
            self.tree(root)
            found = self.pairs(root, "ko")
            self.assertNotIn(("Slyder", "Slyder"), found)
            self.assertEqual([pair for pair in found if len(pair[0]) > strings.VALUE_CAP], [])

    def test_a_localized_file_with_no_english_sibling_is_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            write_plist(os.path.join(root, "O.bundle", "ko.lproj"), "O.strings", {"K": "홀로"})
            self.assertEqual(self.pairs(root, "ko"), [])

    def test_a_region_qualified_language_finds_the_subtag_directory(self):
        with tempfile.TemporaryDirectory() as root:
            self.tree(root)
            self.assertIn(("General", "일반"), self.pairs(root, "ko-US"))

    def test_an_english_string_with_two_translations_yields_both(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = self.tree(root)
            write_plist(os.path.join(bundle, "en.lproj"), "O.strings", {"G": "General"})
            write_plist(os.path.join(bundle, "ko.lproj"), "O.strings", {"G": "일반 설정"})
            found = self.pairs(root, "ko")
            self.assertIn(("General", "일반"), found)
            self.assertIn(("General", "일반 설정"), found)


if __name__ == "__main__":
    unittest.main()
