import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import records
from uiflow.commands import find as find_command
from uiflow.commands import index as index_command

APP = "com.example.Demo"


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-find-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        directory = os.path.join(self.dir, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "grant-full-access.json"), "w") as handle:
            json.dump({"app": APP, "name": "grant-full-access", "aliases": ["full access"],
                       "description": "Turn on Allow Full Access", "steps": [{"op": "tap"}]}, handle)
        records.invalidate()

    def parse(self, module, argv):
        parser = argparse.ArgumentParser()
        module.add_arguments(parser)
        return parser.parse_args(argv)

    def capture(self, module, argv):
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            code = module.run(self.parse(module, argv))
        finally:
            sys.stdout = saved
        return code, buffer.getvalue()

    def test_find_prints_a_matching_ref(self):
        code, text = self.capture(find_command, ["full access"])
        self.assertEqual(code, 0)
        self.assertIn("%s/grant-full-access" % APP, text)
        self.assertIn("flow", text)

    def test_find_reaches_a_record_only_its_alias_names(self):
        directory = os.path.join(self.dir, APP)
        with open(os.path.join(directory, "switch-to-slyder.json"), "w") as handle:
            json.dump({"app": APP, "name": "switch-to-slyder", "aliases": ["use slyder keyboard"],
                       "description": "Cycle the globe", "steps": [{"op": "tap"}]}, handle)
        records.invalidate()
        code, text = self.capture(find_command, ["slyder keyboard"])
        self.assertEqual(code, 0)
        self.assertIn("%s/switch-to-slyder" % APP, text)
        self.assertIn("alias", text)

    def test_find_reports_no_match_and_exits_one(self):
        code, text = self.capture(find_command, ["nothing-like-this"])
        self.assertEqual(code, 1)
        self.assertIn("no record", text)

    def test_find_json_is_machine_readable(self):
        _, text = self.capture(find_command, ["full access", "--json"])
        self.assertEqual(json.loads(text)[0]["ref"], "%s/grant-full-access" % APP)

    def test_index_stats_prints_table_sizes(self):
        code, text = self.capture(index_command, ["--stats"])
        self.assertEqual(code, 0)
        self.assertIn("records", text)

    def test_index_rebuild_writes_the_file(self):
        from uiflow import index

        code, _ = self.capture(index_command, ["--rebuild"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(index.index_path()))


if __name__ == "__main__":
    unittest.main()
