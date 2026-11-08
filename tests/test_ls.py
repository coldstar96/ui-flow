import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import index, records
from uiflow.commands import ls as ls_command

APP = "com.example.Demo"


class LsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-ls-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        self.write(APP, "frag", [{"op": "tap"}], kind="fragment")
        self.write(APP, "caller", [{"op": "use", "ref": "frag"}])

    def write(self, app, name, steps, **extra):
        payload = {"app": app, "name": name, "steps": steps}
        payload.update(extra)
        directory = os.path.join(self.dir, app)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump(payload, handle)
        records.invalidate()

    def capture(self, argv):
        parser = argparse.ArgumentParser()
        ls_command.add_arguments(parser)
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            ls_command.run(parser.parse_args(argv))
        finally:
            sys.stdout = saved
        return buffer.getvalue()

    def test_fragments_are_hidden_by_default(self):
        text = self.capture([APP])
        self.assertIn("caller", text)
        self.assertNotIn("frag ", text)

    def test_all_shows_fragments(self):
        self.assertIn("frag", self.capture([APP, "--all"]))

    def test_uses_and_used_by_are_printed(self):
        text = self.capture([APP, "--all"])
        self.assertIn("uses: %s" % records.qualify(APP, "frag"), text)
        self.assertIn("used-by: %s" % records.qualify(APP, "caller"), text)

    def test_a_cross_app_fragment_shows_its_caller(self):
        other = "com.example.Other"
        self.write(other, "shared", [{"op": "tap"}], kind="fragment")
        self.write(APP, "cross", [{"op": "use", "ref": "%s/shared" % other}])
        text = self.capture([other, "--all"])
        self.assertIn("used-by: %s" % records.qualify(APP, "cross"), text)

    def test_an_unreadable_kind_skips_one_row_not_the_listing(self):
        self.write(APP, "junk", [{"op": "tap"}], kind="Fragment")
        text = self.capture([APP, "--all"])
        self.assertIn("caller", text)
        self.assertIn("junk", text)

    def test_the_wiring_fallback_still_back_fills_used_by(self):
        saved = index.load
        index.load = lambda *args, **kwargs: {}
        self.addCleanup(setattr, index, "load", saved)
        text = self.capture([APP, "--all"])
        self.assertIn("used-by: %s" % records.qualify(APP, "caller"), text)

    def test_the_uses_walk_is_not_duplicated_in_the_command(self):
        self.assertFalse(hasattr(ls_command, "_uses_of"))
        self.assertEqual(index.uses_of({"steps": [{"op": "use", "ref": "frag"}]}, APP),
                         [records.qualify(APP, "frag")])

    def test_the_app_listing_still_counts_every_record(self):
        self.assertIn("2 records", self.capture([]))


if __name__ == "__main__":
    unittest.main()
