import os
import plistlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import simctl, strings


def write_plist(directory, name, payload):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "wb") as handle:
        plistlib.dump(payload, handle, fmt=plistlib.FMT_BINARY)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.swap(strings.paths, "STRINGS_DIR", os.path.join(self.root.name, "cache"))
        self.runtime = os.path.join(self.root.name, "RuntimeRoot")
        for subdir in ("System/Library/PreferenceBundles/P.bundle", "Applications/Settings.app"):
            base = os.path.join(self.runtime, *subdir.split("/"))
            write_plist(os.path.join(base, "en.lproj"), "K.strings",
                        {"A": "Allow Full Access", "G": "General"})
            write_plist(os.path.join(base, "ko.lproj"), "K.strings",
                        {"A": "전체 접근 허용", "G": "일반"})
        self.bundle = os.path.join(self.root.name, "Slyder.app")
        write_plist(os.path.join(self.bundle, "en.lproj"), "L.strings", {"F": "Folder Name"})
        write_plist(os.path.join(self.bundle, "ko.lproj"), "L.strings", {"F": "폴더 이름"})
        self.language = "ko-US"
        self.builds = 0
        self.swap(simctl, "runtime_for", lambda udid, listing=None, runtimes=None:
                  {"identifier": "iOS-26-5", "build": "23F77", "root": self.runtime})
        self.swap(simctl, "ui_language", lambda udid: self.language)
        self.swap(simctl, "app_bundle_path", lambda udid, app: self.bundle)
        real = strings.build_store
        def counted(path, roots, lang):
            self.builds += 1
            return real(path, roots, lang)
        self.swap(strings, "build_store", counted)

    def swap(self, module, name, value):
        saved = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, saved)

    def test_an_english_simulator_has_no_oracle(self):
        self.language = "en-US"
        self.assertIsNone(strings.oracle("AAAA-1111"))

    def test_no_udid_has_no_oracle(self):
        self.assertIsNone(strings.oracle(None))

    def test_no_resolvable_runtime_has_no_oracle(self):
        self.swap(simctl, "runtime_for", lambda udid, listing=None, runtimes=None: None)
        self.assertIsNone(strings.oracle("AAAA-1111"))

    def test_a_localized_simulator_translates_system_labels(self):
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertEqual(found.translate("Allow Full Access"), ["전체 접근 허용"])
        self.assertEqual(found.lang, "ko-US")

    def test_the_app_bundle_catalogue_is_consulted_too(self):
        found = strings.oracle("AAAA-1111", app="io.dtails.Slyder")
        self.addCleanup(found.close)
        self.assertEqual(found.translate("Folder Name"), ["폴더 이름"])
        self.assertEqual(found.translate("General"), ["일반"])

    def test_an_unknown_label_translates_to_nothing(self):
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertEqual(found.translate("Nope"), [])

    def test_a_localized_label_maps_back_to_its_english_original(self):
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertEqual(found.canonical("일반"), "General")
        self.assertIsNone(found.canonical("Nope"))

    def test_a_known_english_source_string_is_recognised(self):
        """This replaces isascii() as the 'already canonical' test — it works for fr and es too."""
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertTrue(found.knows_english("General"))
        self.assertFalse(found.knows_english("일반"))

    def test_the_store_is_built_once_and_reopened_after(self):
        strings.oracle("AAAA-1111").close()
        first = self.builds
        strings.oracle("AAAA-1111").close()
        self.assertEqual(self.builds, first)
        self.assertTrue(os.path.isfile(strings.store_path("23F77", "ko-US")))

    def test_a_new_runtime_build_builds_a_new_store(self):
        strings.oracle("AAAA-1111").close()
        before = self.builds
        self.swap(simctl, "runtime_for", lambda udid, listing=None, runtimes=None:
                  {"identifier": "iOS-26-6", "build": "23G01", "root": self.runtime})
        strings.oracle("AAAA-1111").close()
        self.assertGreater(self.builds, before)

    def test_a_corrupt_store_is_rebuilt_not_fatal(self):
        strings.oracle("AAAA-1111").close()
        with open(strings.store_path("23F77", "ko-US"), "wb") as handle:
            handle.write(b"not a database")
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertEqual(found.translate("General"), ["일반"])

    def test_a_store_from_an_older_schema_is_rebuilt(self):
        path = strings.store_path("23F77", "ko-US")
        strings.oracle("AAAA-1111").close()
        connection = sqlite3.connect(path)
        connection.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
        connection.commit()
        connection.close()
        before = self.builds
        strings.oracle("AAAA-1111").close()
        self.assertGreater(self.builds, before)

    def test_a_language_that_is_not_a_language_never_becomes_a_path(self):
        self.language = "../../../etc/passwd"
        self.assertIsNone(strings.oracle("AAAA-1111"))

    def test_an_ambiguous_localized_string_reports_every_candidate_and_picks_none(self):
        """`ORDER BY en` then `[0]` picked ALPHABETICALLY: 확인 -> Acknowledge, not OK."""
        write_plist(os.path.join(self.runtime, "Applications", "Settings.app", "en.lproj"),
                    "M.strings", {"O": "OK", "C": "Acknowledge"})
        write_plist(os.path.join(self.runtime, "Applications", "Settings.app", "ko.lproj"),
                    "M.strings", {"O": "확인", "C": "확인"})
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertEqual(found.originals("확인"), ["Acknowledge", "OK"])
        self.assertIsNone(found.canonical("확인"))
        self.assertEqual(found.canonical("일반"), "General")

    def test_a_known_localized_value_is_recognised_as_such(self):
        """The question the reverse pass must ask — `knows_english` refuses identity strings."""
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertTrue(found.knows_localized("일반"))
        self.assertFalse(found.knows_localized("General"))
        self.assertFalse(found.knows_localized("Bluetooth"))

    def test_a_failed_build_leaves_no_orphan_tmp_behind(self):
        def explode(roots, lang, cap=strings.VALUE_CAP):
            yield ("A", "B")
            raise UnicodeEncodeError("utf-8", "\ud83d", 0, 1, "surrogates not allowed")

        self.swap(strings, "iter_pairs", explode)
        path = os.path.join(strings.paths.STRINGS_DIR, "boom.db")
        with self.assertRaises(UnicodeEncodeError):
            strings.build_store(path, [self.runtime], "ko-US")
        left = [name for name in os.listdir(strings.paths.STRINGS_DIR) if name.endswith(".tmp")]
        self.assertEqual(left, [])

    def test_every_store_the_caller_opened_survives_the_prune_that_follows(self):
        """The app store is not the system store, and a read-only open refreshes no mtime."""
        real = strings.prune
        self.swap(strings, "prune", lambda keep=6, protect=(): real(keep=0, protect=protect))
        found = strings.oracle("AAAA-1111", app="io.dtails.Slyder")
        self.addCleanup(found.close)
        left = set(os.listdir(strings.paths.STRINGS_DIR))
        self.assertIn(os.path.basename(strings.store_path("23F77", "ko-US")), left)
        self.assertEqual(found.translate("Folder Name"), ["폴더 이름"])

    def test_a_strings_dir_whose_path_needs_uri_encoding_still_opens(self):
        """`file:` mode on a raw path: a legal `#` truncates it and every open fails forever."""
        self.swap(strings.paths, "STRINGS_DIR", os.path.join(self.root.name, "ca#che"))
        found = strings.oracle("AAAA-1111")
        self.addCleanup(found.close)
        self.assertEqual(found.translate("General"), ["일반"])
        self.assertEqual(self.builds, 1)

    def test_a_store_that_is_unusable_right_after_a_build_is_not_rebuilt_forever(self):
        self.swap(strings, "_usable", lambda path: None)
        self.assertIsNone(strings.oracle("AAAA-1111", app="io.dtails.Slyder"))
        self.assertLessEqual(self.builds, 1)

    def test_prune_reaps_an_orphan_tmp_from_a_dead_build(self):
        os.makedirs(strings.paths.STRINGS_DIR, exist_ok=True)
        orphan = os.path.join(strings.paths.STRINGS_DIR, "old.db.999999.tmp")
        live = os.path.join(strings.paths.STRINGS_DIR, "new.db.%d.tmp" % os.getpid())
        for path in (orphan, live):
            open(path, "wb").close()
        strings.prune(keep=6)
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(live))

    def test_prune_survives_a_store_vanishing_under_it(self):
        os.makedirs(strings.paths.STRINGS_DIR, exist_ok=True)
        path = os.path.join(strings.paths.STRINGS_DIR, "gone.db")
        open(path, "wb").close()
        real = strings.os.path.getmtime

        def vanishing(target):
            os.remove(target)
            return real(target)

        self.swap(strings.os.path, "getmtime", vanishing)
        strings.prune(keep=1)

    def test_prune_keeps_the_newest_stores_and_never_the_protected_one(self):
        os.makedirs(strings.paths.STRINGS_DIR, exist_ok=True)
        made = []
        for index in range(9):
            path = os.path.join(strings.paths.STRINGS_DIR, "old-%d.db" % index)
            open(path, "wb").close()
            os.utime(path, (index, index))
            made.append(path)
        strings.prune(keep=3, protect=(made[0],))
        left = set(os.listdir(strings.paths.STRINGS_DIR))
        self.assertIn(os.path.basename(made[0]), left)
        self.assertLessEqual(len(left), 4)


if __name__ == "__main__":
    unittest.main()
