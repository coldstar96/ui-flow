import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import sources

APP = "com.example.Demo"


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-sources-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        sources.reset()
        self.addCleanup(sources.reset)

    def write(self, relative, payload):
        target = os.path.join(self.dir, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            json.dump(payload, handle)
        return target

    def test_registers_a_single_file_by_its_own_app_and_name(self):
        path = self.write("one.json", {"app": APP, "name": "onboarding", "steps": []})
        self.assertEqual(sources.register(path), ["%s/onboarding" % APP])
        self.assertEqual(sources.path_for("%s/onboarding" % APP), path)
        self.assertTrue(sources.is_external("%s/onboarding" % APP))

    def test_a_single_file_falls_back_to_the_default_app_and_the_filename(self):
        path = self.write("login.json", {"steps": []})
        self.assertEqual(sources.register(path, default_app=APP), ["%s/login" % APP])

    def test_a_file_with_no_app_and_no_default_is_refused(self):
        path = self.write("login.json", {"steps": []})
        with self.assertRaises(sources.SourceError):
            sources.register(path)

    def test_a_directory_record_is_keyed_by_its_folder_not_its_json_app(self):
        self.write("flows/%s/x.json" % APP, {"app": "com.other.App", "steps": []})
        self.assertEqual(sources.register(os.path.join(self.dir, "flows")), ["%s/x" % APP])
        self.assertIsNone(sources.path_for("com.other.App/x"))

    def test_a_loose_file_still_takes_its_app_from_its_json(self):
        path = self.write("loose.json", {"app": "com.other.App", "name": "y", "steps": []})
        self.assertEqual(sources.register(path, default_app=APP), ["com.other.App/y"])

    def test_a_loose_file_without_a_json_suffix_keeps_its_whole_stem(self):
        path = self.write("login.txt", {"steps": []})
        self.assertEqual(sources.register(path, default_app=APP), ["%s/login" % APP])

    def test_registers_a_directory_of_app_subdirectories(self):
        self.write("flows/%s/a.json" % APP, {"steps": []})
        self.write("flows/%s/b.json" % APP, {"steps": []})
        self.write("flows/com.other.App/c.json", {"steps": []})
        added = sources.register(os.path.join(self.dir, "flows"))
        self.assertEqual(sorted(added), ["%s/a" % APP, "%s/b" % APP, "com.other.App/c"])
        self.assertEqual(sources.apps(), [APP, "com.other.App"])

    def test_a_directory_skips_index_and_keymap(self):
        self.write("flows/%s/a.json" % APP, {"steps": []})
        self.write("flows/%s/keymap.json" % APP, {"keys": {}})
        self.write("flows/%s/index.json" % APP, {})
        self.assertEqual(sources.register(os.path.join(self.dir, "flows")), ["%s/a" % APP])

    def test_a_directory_with_no_records_is_refused(self):
        os.makedirs(os.path.join(self.dir, "empty"))
        with self.assertRaises(sources.SourceError):
            sources.register(os.path.join(self.dir, "empty"))

    def test_a_missing_path_is_refused_by_name(self):
        with self.assertRaises(sources.SourceError) as caught:
            sources.register(os.path.join(self.dir, "nope.json"))
        self.assertIn("nope.json", str(caught.exception))

    def test_unparseable_json_is_refused_by_name(self):
        target = os.path.join(self.dir, "bad.json")
        with open(target, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(sources.SourceError) as caught:
            sources.register(target, default_app=APP)
        self.assertIn("bad.json", str(caught.exception))

    def test_a_bad_name_is_refused(self):
        path = self.write("evil.json", {"app": APP, "name": "../escape", "steps": []})
        with self.assertRaises(sources.SourceError):
            sources.register(path)

    def test_later_registration_of_the_same_ref_wins_and_moves_to_the_end(self):
        first = self.write("a/%s/x.json" % APP, {"steps": []})
        second = self.write("b/%s/x.json" % APP, {"steps": []})
        sources.register(os.path.join(self.dir, "a"))
        sources.register(os.path.join(self.dir, "b"))
        self.assertEqual(sources.path_for("%s/x" % APP), second)
        self.assertNotEqual(first, second)
        self.assertEqual(sources.refs(), ["%s/x" % APP])

    def test_record_for_defaults_app_and_name_and_caches_on_mtime(self):
        path = self.write("one.json", {"app": APP, "steps": [{"op": "tap"}]})
        sources.register(path)
        loaded = sources.record_for("%s/one" % APP)
        self.assertEqual(loaded["app"], APP)
        self.assertEqual(loaded["name"], "one")
        self.assertIs(sources.record_for("%s/one" % APP), loaded)

    def test_record_for_forces_the_payload_to_agree_with_its_ref(self):
        self.write("flows/%s/x.json" % APP, {"app": "com.other.App", "steps": []})
        ref = sources.register(os.path.join(self.dir, "flows"))[0]
        self.assertEqual(ref, "%s/x" % APP)
        loaded = sources.record_for(ref)
        self.assertEqual(loaded["app"], APP)
        self.assertEqual(loaded["name"], "x")

    def test_add_argument_collects_repeated_file_flags(self):
        import argparse

        parser = argparse.ArgumentParser()
        sources.add_argument(parser)
        self.assertEqual(parser.parse_args([]).files, [])
        self.assertEqual(parser.parse_args(["--file", "a", "--file", "b"]).files, ["a", "b"])

    def test_is_external_is_false_for_an_unregistered_ref(self):
        self.assertFalse(sources.is_external("%s/never" % APP))

    def test_a_directory_record_still_takes_its_name_from_its_json(self):
        self.write("flows/%s/x.json" % APP, {"name": "renamed", "steps": []})
        self.assertEqual(sources.register(os.path.join(self.dir, "flows")), ["%s/renamed" % APP])

    def test_record_for_rereads_after_the_file_changes(self):
        path = self.write("one.json", {"app": APP, "steps": []})
        sources.register(path)
        self.assertEqual(sources.record_for("%s/one" % APP)["steps"], [])
        sources.forget_cache()
        self.write("one.json", {"app": APP, "steps": [{"op": "tap"}]})
        self.assertEqual(len(sources.record_for("%s/one" % APP)["steps"]), 1)

    def test_records_for_returns_one_app_keyed_by_name(self):
        self.write("flows/%s/a.json" % APP, {"steps": []})
        self.write("flows/com.other.App/b.json", {"steps": []})
        sources.register(os.path.join(self.dir, "flows"))
        self.assertEqual(sorted(sources.records_for(APP)), ["a"])

    def test_register_env_splits_on_the_path_separator(self):
        self.write("one/%s/a.json" % APP, {"steps": []})
        self.write("two/%s/b.json" % APP, {"steps": []})
        value = os.pathsep.join([os.path.join(self.dir, "one"), os.path.join(self.dir, "two")])
        self.assertEqual(sorted(sources.register_env(value)), ["%s/a" % APP, "%s/b" % APP])

    def test_register_env_ignores_empty_pieces(self):
        self.assertEqual(sources.register_env(""), [])
        self.assertEqual(sources.register_env(os.pathsep), [])

    def test_adopt_registers_a_path_that_does_not_exist_yet(self):
        target = os.path.join(self.dir, "out", APP, "new.json")
        sources.adopt("%s/new" % APP, target)
        self.assertEqual(sources.path_for("%s/new" % APP), target)
        self.assertIsNone(sources.record_for("%s/new" % APP))

    def test_refs_named_finds_the_same_name_under_another_app(self):
        self.write("flows/com.other.App/onboarding.json", {"steps": []})
        sources.register(os.path.join(self.dir, "flows"))
        self.assertEqual(sources.refs_named("onboarding"), ["com.other.App/onboarding"])

    def test_reset_clears_everything(self):
        self.write("flows/%s/a.json" % APP, {"steps": []})
        sources.register(os.path.join(self.dir, "flows"))
        sources.reset()
        self.assertEqual(sources.refs(), [])
        self.assertIsNone(sources.path_for("%s/a" % APP))


if __name__ == "__main__":
    unittest.main()
