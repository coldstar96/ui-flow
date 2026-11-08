import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import records, sources
from uiflow.commands import record as record_command

APP = "com.example.Demo"
SETTINGS = "com.apple.Preferences"
WALK = [
    {"op": "launch", "app": SETTINGS},
    {"op": "tap", "sel": {"app": SETTINGS, "type": "button", "label": "General"}},
    {"op": "tap", "sel": {"app": SETTINGS, "type": "button", "label": "Keyboard"}},
]


class RecordSaveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-save-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        directory = os.path.join(self.dir, SETTINGS)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "open-keyboard-settings.json"), "w") as handle:
            json.dump({"app": SETTINGS, "name": "open-keyboard-settings",
                       "kind": "fragment", "steps": WALK}, handle)
        records.invalidate()

    def test_reuse_report_finds_a_whole_fragment_inside_new_steps(self):
        found = record_command.reuse_report(WALK + [{"op": "tap", "sel": {"label": "z"}}], APP)
        self.assertEqual(found[0]["ref"], "%s/open-keyboard-settings" % SETTINGS)
        self.assertEqual(found[0]["start"], 0)
        self.assertEqual(found[0]["length"], 3)

    def test_reuse_report_is_silent_when_nothing_matches(self):
        self.assertEqual(record_command.reuse_report([{"op": "tap", "sel": {"label": "z"}}], APP), [])

    def test_adopt_replaces_the_run_with_a_use_step(self):
        steps = record_command.adopt(
            WALK + [{"op": "tap", "sel": {"label": "z"}}], "%s/open-keyboard-settings" % SETTINGS, APP)
        self.assertEqual(steps[0], {"op": "use", "ref": "%s/open-keyboard-settings" % SETTINGS})
        self.assertEqual(steps[1], {"op": "tap", "sel": {"label": "z"}})

    def test_adopt_refuses_a_target_whose_requires_would_make_the_caller_unreplayable(self):
        with open(os.path.join(self.dir, SETTINGS, "needs-setup.json"), "w") as handle:
            json.dump({"app": SETTINGS, "name": "needs-setup", "kind": "fragment",
                       "steps": WALK, "requires": ["some-other-record"]}, handle)
        records.invalidate()
        with self.assertRaises(record_command.RecordRefused) as caught:
            record_command.build_record(
                APP, "caller", WALK + [{"op": "tap", "sel": {"label": "z"}}],
                verifies_why="because", use=["%s/needs-setup" % SETTINGS])
        self.assertIn("needs-setup", str(caught.exception))
        self.assertIn("some-other-record", str(caught.exception))

    def test_adopt_refuses_when_the_run_is_absent(self):
        with self.assertRaises(record_command.RecordRefused):
            record_command.adopt([{"op": "tap"}], "%s/open-keyboard-settings" % SETTINGS, APP)

    def test_a_fragment_does_not_need_verifies_why(self):
        built = record_command.build_record(
            APP, "frag", [{"op": "tap", "sel": {"label": "a"}}], kind=records.FRAGMENT)
        self.assertEqual(built["kind"], records.FRAGMENT)

    def test_a_flow_still_needs_verifies_why(self):
        with self.assertRaises(record_command.RecordRefused):
            record_command.build_record(APP, "flow", [{"op": "tap", "sel": {"label": "a"}}])

    def test_a_flow_record_has_no_kind_key(self):
        built = record_command.build_record(
            APP, "flow", [{"op": "tap", "sel": {"label": "a"}}], verifies_why="because")
        self.assertNotIn("kind", built)

    def test_the_post_save_report_never_matches_the_record_against_itself(self):
        found = record_command.reuse_report(
            WALK, APP, exclude=records.qualify(SETTINGS, "open-keyboard-settings"))
        self.assertEqual(found, [])

    def test_adopt_honours_a_fragment_shorter_than_min_run(self):
        directory = os.path.join(self.dir, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "tap-add.json"), "w") as handle:
            json.dump({"app": APP, "name": "tap-add", "kind": "fragment",
                       "steps": [{"op": "tap", "sel": {"label": "Add"}}]}, handle)
        records.invalidate()
        steps = record_command.adopt(
            [{"op": "tap", "sel": {"label": "Add"}}, {"op": "tap", "sel": {"label": "z"}}],
            "%s/tap-add" % APP, APP)
        self.assertEqual(steps[0], {"op": "use", "ref": "%s/tap-add" % APP})

    def test_build_record_adopts_the_named_refs(self):
        built = record_command.build_record(
            APP, "flow", WALK + [{"op": "tap", "sel": {"label": "z"}}],
            verifies_why="because", use=["%s/open-keyboard-settings" % SETTINGS])
        self.assertEqual(built["steps"][0], {"op": "use", "ref": "%s/open-keyboard-settings" % SETTINGS})


class CanonicalSaveTests(unittest.TestCase):
    class FakeOracle:
        lang = "ko-US"
        REVERSE = {"일반": ["General"], "키보드": ["Keyboard"],
                   "확인": ["Acknowledge", "Confirm", "OK"]}

        def originals(self, label):
            return list(self.REVERSE.get(label) or [])

        def canonical(self, label):
            found = self.originals(label)
            return found[0] if len(found) == 1 else None

        def knows_english(self, label):
            return label in ("General", "Keyboard", "Slyder")

        def knows_localized(self, label):
            return label in self.REVERSE

    def steps(self):
        return [
            {"op": "tap", "sel": {"app": "com.apple.Preferences", "type": "button", "label": "일반"}},
            {"op": "tap", "sel": {"app": "com.apple.Preferences", "type": "button",
                                  "labelContains": "키보드"}},
        ]

    def build(self, steps, **kwargs):
        return record_command.build_record(app="io.dtails.Slyder", name="walk", steps=steps,
                                           verifies_why="because", **kwargs)

    def test_observed_localized_labels_are_saved_as_english(self):
        built = self.build(self.steps(), oracle=self.FakeOracle())
        self.assertEqual(built["steps"][0]["sel"]["label"], "General")
        self.assertEqual(built["steps"][1]["sel"]["labelContains"], "Keyboard")

    def test_an_any_of_journalled_from_a_localized_run_is_collapsed(self):
        steps = [{"op": "tap", "sel": {"type": "button", "labelAnyOf": ["General", "일반"]}}]
        built = self.build(steps, oracle=self.FakeOracle())
        self.assertEqual(built["steps"][0]["sel"], {"type": "button", "label": "General"})

    def test_the_provenance_line_survives_alongside_the_normalization_note(self):
        built = self.build(self.steps(), oracle=self.FakeOracle())
        self.assertTrue(any("learned from a journalled session" in line for line in built["history"]))
        self.assertTrue(any("normalized" in line and "ko-US" in line for line in built["history"]))

    def test_a_label_with_several_english_originals_is_kept_and_every_candidate_named(self):
        """Alphabetical order picked `Acknowledge` for 확인 and wrote it into unversioned user data."""
        warnings = []
        built = self.build([{"op": "tap", "sel": {"type": "button", "label": "확인"}}],
                           oracle=self.FakeOracle(), warn=warnings.append)
        self.assertEqual(built["steps"][0]["sel"]["label"], "확인")
        named = [line for line in warnings if "확인" in line]
        self.assertTrue(named)
        for candidate in ("Acknowledge", "Confirm", "OK"):
            self.assertIn(candidate, named[0])

    def test_a_string_the_catalogue_never_localized_is_saved_untouched_and_unwarned(self):
        warnings = []
        built = self.build([{"op": "tap", "sel": {"type": "button", "label": "Bluetooth"}}],
                           oracle=self.FakeOracle(), warn=warnings.append)
        self.assertEqual(built["steps"][0]["sel"]["label"], "Bluetooth")
        self.assertFalse([line for line in warnings if "Bluetooth" in line])

    def test_the_satisfied_when_and_verifies_payloads_are_canonicalized_too(self):
        built = self.build(
            self.steps(), oracle=self.FakeOracle(),
            satisfied_when=[{"op": "assert", "sel": {"label": "일반"}}],
            verifies=[{"op": "assert", "sel": {"descendant": {"label": "키보드"}}}])
        self.assertEqual(built["satisfiedWhen"][0]["sel"]["label"], "General")
        self.assertEqual(built["verifies"][0]["sel"]["descendant"]["label"], "Keyboard")

    def test_a_localized_simulator_with_no_oracle_warns_that_the_record_is_pinned(self):
        warnings = []
        built = self.build(self.steps(), lang="es-ES", warn=warnings.append)
        self.assertEqual(built["steps"][0]["sel"]["label"], "일반")
        self.assertTrue([line for line in warnings if "es-ES" in line])

    def test_an_english_simulator_with_no_oracle_says_nothing(self):
        warnings = []
        self.build([{"op": "tap", "sel": {"label": "General"}}], lang="en-US", warn=warnings.append)
        self.assertFalse([line for line in warnings if "pinned" in line])

    def test_without_an_oracle_nothing_changes(self):
        built = self.build(self.steps())
        self.assertEqual(built["steps"][0]["sel"]["label"], "일반")
        self.assertFalse(any("normalized" in line for line in built["history"]))

    def test_an_any_of_selector_demands_canonicalizing_wherever_it_is_nested(self):
        english = [{"op": "tap", "sel": {"type": "button", "label": "General"}}]
        self.assertFalse(record_command.needs_canonicalizing(english))
        self.assertTrue(record_command.needs_canonicalizing(
            [{"op": "tap", "sel": {"type": "button", "labelAnyOf": ["General", "일반"]}}]))
        self.assertTrue(record_command.needs_canonicalizing(
            [{"op": "tap", "sel": {"descendant": {"labelContainsAnyOf": ["Keyboard", "키보드"]}}}]))

    def test_a_latin_script_label_is_not_the_detector_s_business(self):
        """`Ajustes` and `Allgemein` are ASCII: only the simulator's language can decide this."""
        for label in ("Ajustes", "Allgemein", "Impostazioni", "일반"):
            self.assertFalse(record_command.needs_canonicalizing(
                [{"op": "tap", "sel": {"label": label}}]), label)


class RecordSaveWithCallerSourcesTests(unittest.TestCase):
    def setUp(self):
        self.builtin = tempfile.mkdtemp(prefix="uiflow-save-builtin-")
        self.external = tempfile.mkdtemp(prefix="uiflow-save-external-")
        self.addCleanup(shutil.rmtree, self.builtin, True)
        self.addCleanup(shutil.rmtree, self.external, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.builtin
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        sources.reset()
        self.addCleanup(sources.reset)
        self.addCleanup(records.invalidate)

    def journalled(self, steps):
        journal = record_command.journal
        for name, replacement in (
            ("mark_is_stale", lambda channel=None, udid=None: False),
            ("since_mark", lambda channel=None, udid=None: []),
            ("to_steps", lambda entries: [dict(step) for step in steps]),
        ):
            self.addCleanup(setattr, journal, name, getattr(journal, name))
            setattr(journal, name, replacement)

    def register_external(self, name, steps):
        directory = os.path.join(self.external, APP)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, name + ".json")
        with open(target, "w") as handle:
            json.dump({"app": APP, "name": name, "kind": "fragment", "steps": steps}, handle)
        sources.register(target)
        return target

    def save(self, argv):
        parser = argparse.ArgumentParser()
        record_command.add_arguments(parser)
        args = parser.parse_args(argv)
        buffer, stdout = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            return record_command.run(args)
        finally:
            sys.stdout = stdout

    def test_save_use_adopts_a_run_that_lives_in_a_caller_file(self):
        head = [{"op": "tap", "sel": {"label": "A"}}, {"op": "tap", "sel": {"label": "B"}}]
        self.register_external("onboarding", head)
        self.journalled(head + [{"op": "tap", "sel": {"label": "z"}}])
        self.save(["save", APP, "caller", "--verifies-why", "because",
                   "--use", "%s/onboarding" % APP])
        with open(os.path.join(self.builtin, APP, "caller.json")) as handle:
            self.assertEqual(json.load(handle)["steps"][0],
                             {"op": "use", "ref": "%s/onboarding" % APP})

    def test_save_registers_a_caller_file_of_its_own(self):
        parser = argparse.ArgumentParser()
        record_command.add_arguments(parser)
        args = parser.parse_args(["save", APP, "caller", "--file", "/nope/records.json"])
        self.assertEqual(args.files, ["/nope/records.json"])


if __name__ == "__main__":
    unittest.main()
