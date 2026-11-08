import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import cli, records, sources
from uiflow.commands import do as do_command
from uiflow.commands import find as find_command
from uiflow.commands import ls as ls_command
from uiflow.commands import record as record_command

APP = "com.example.Demo"


class CommandSourcesTests(unittest.TestCase):
    def setUp(self):
        self.builtin = tempfile.mkdtemp(prefix="uiflow-cmd-builtin-")
        self.external = tempfile.mkdtemp(prefix="uiflow-cmd-external-")
        self.addCleanup(shutil.rmtree, self.builtin, True)
        self.addCleanup(shutil.rmtree, self.external, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.builtin
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        sources.reset()
        self.addCleanup(sources.reset)
        self.addCleanup(records.invalidate)
        self.addCleanup(os.environ.pop, sources.ENV_VAR, None)

    def write_builtin(self, name, **extra):
        payload = {"app": APP, "name": name, "steps": [{"op": "tap", "sel": {"label": "b"}}]}
        payload.update(extra)
        directory = os.path.join(self.builtin, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump(payload, handle)
        records.invalidate()

    def write_external(self, name, **extra):
        payload = {"app": APP, "name": name, "description": "caller flow",
                   "steps": [{"op": "tap", "sel": {"label": "e"}}]}
        payload.update(extra)
        directory = os.path.join(self.external, APP)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, name + ".json")
        with open(target, "w") as handle:
            json.dump(payload, handle)
        return target

    def capture(self, module, argv):
        parser = argparse.ArgumentParser()
        module.add_arguments(parser)
        args = parser.parse_args(argv)
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            module.run(args)
        finally:
            sys.stdout = saved
        return buffer.getvalue()

    def test_do_ls_and_find_all_accept_file(self):
        for module in (do_command, ls_command, find_command):
            parser = argparse.ArgumentParser()
            module.add_arguments(parser)
            self.assertIn("--file", parser.format_help())

    def test_cli_registers_the_env_var_before_dispatch(self):
        self.write_external("onboarding")
        os.environ[sources.ENV_VAR] = self.external
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            cli.main(["ls", APP])
        finally:
            sys.stdout = saved
        self.assertIn("onboarding", buffer.getvalue())

    def test_cli_registers_file_after_the_env_var_so_the_flag_wins(self):
        first = os.path.join(self.external, "first")
        second = os.path.join(self.external, "second")
        for root, label in ((first, "from-env"), (second, "from-flag")):
            os.makedirs(os.path.join(root, APP), exist_ok=True)
            with open(os.path.join(root, APP, "x.json"), "w") as handle:
                json.dump({"app": APP, "name": "x", "description": label, "steps": []}, handle)
        os.environ[sources.ENV_VAR] = first
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            cli.main(["ls", APP, "--file", second])
        finally:
            sys.stdout = saved
        self.assertIn("from-flag", buffer.getvalue())

    def test_a_bad_file_path_exits_one_and_names_the_path(self):
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            self.assertEqual(cli.main(["ls", APP, "--file", "/nope/missing.json"]), 1)
            self.assertIn("missing.json", sys.stderr.getvalue())
        finally:
            sys.stderr = saved

    def test_ls_marks_an_external_record_and_prints_its_path(self):
        target = self.write_external("onboarding")
        sources.register(target)
        text = self.capture(ls_command, [APP])
        self.assertIn("onboarding", text)
        self.assertIn("external", text)
        self.assertIn(target, text)

    def test_ls_with_no_app_lists_an_app_that_only_has_external_records(self):
        sources.register(self.write_external("onboarding"))
        self.assertIn(APP, self.capture(ls_command, []))

    def test_find_hits_an_external_record_and_marks_it(self):
        sources.register(self.write_external("onboarding"))
        text = self.capture(find_command, ["onboarding"])
        self.assertIn("%s/onboarding" % APP, text)
        self.assertIn("external", text)

    def test_do_plan_summary_names_the_source_file_of_an_external_record(self):
        target = self.write_external("onboarding")
        sources.register(target)
        lines = do_command.plan_summary([records.load(APP, "onboarding")])
        self.assertTrue(any(target in line for line in lines))

    def test_do_plan_summary_says_nothing_extra_for_a_builtin(self):
        self.write_builtin("native")
        lines = do_command.plan_summary([records.load(APP, "native")])
        self.assertFalse(any("from " in line for line in lines))

    def test_plan_summary_surfaces_launch_args_and_source_through_a_use(self):
        target = self.write_external("frag", kind="fragment",
                                     steps=[{"op": "launch", "app": APP, "args": ["-EvilFlag", "1"]}])
        sources.register(target)
        self.write_builtin("caller", steps=[{"op": "use", "ref": "frag"}])
        line = do_command.plan_summary([records.load(APP, "caller")])[0]
        self.assertIn("launch args", line)
        self.assertIn(target, line)

    def test_plan_summary_survives_a_record_with_no_app(self):
        line = do_command.plan_summary([{"name": "x", "steps": [{"op": "tap"}]}])[0]
        self.assertIn("x: 1 ops", line)

    def test_cli_announces_a_shadowed_builtin(self):
        self.write_builtin("shared")
        target = self.write_external("shared")
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            cli.main(["ls", APP, "--file", target])
            printed = sys.stderr.getvalue()
        finally:
            sys.stderr = saved
        self.assertIn("shadowing builtin %s/shared" % APP, printed)
        self.assertIn(target, printed)

    def test_one_ref_from_both_the_env_var_and_a_flag_warns_once(self):
        self.write_builtin("shared")
        target = self.write_external("shared")
        os.environ[sources.ENV_VAR] = target
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            cli.main(["ls", APP, "--file", target])
            printed = sys.stderr.getvalue()
        finally:
            sys.stderr = saved
        self.assertEqual(printed.count("shadowing builtin"), 1)

    def test_factor_never_writes_into_a_registered_caller_file(self):
        from uiflow import factor

        target = self.write_external("collide")
        sources.register(target)
        before = open(target).read()
        self.assertTrue(factor.name_taken(APP, "collide"))
        self.assertNotEqual(factor.free_name(APP, "collide", [{"op": "tap"}]), "collide")
        self.assertEqual(open(target).read(), before)

    def test_record_save_without_to_never_writes_into_a_registered_caller_file(self):
        target = self.write_external("collide")
        sources.register(target)
        before = open(target).read()
        written = records.save({"app": APP, "name": "collide", "steps": [{"op": "tap"}]},
                               external=False)
        self.assertEqual(written, os.path.join(self.builtin, APP, "collide.json"))
        self.assertEqual(open(target).read(), before)

    def test_record_save_to_writes_under_the_given_directory(self):
        out = os.path.join(self.external, "out")
        record = {"app": APP, "name": "exported", "steps": [{"op": "tap"}]}
        path = record_command.save_to(out, record)
        self.assertEqual(path, os.path.join(out, APP, "exported.json"))
        with open(path) as handle:
            self.assertEqual(json.load(handle)["name"], "exported")
        self.assertFalse(os.path.exists(os.path.join(self.builtin, APP, "exported.json")))

    def test_record_save_to_registers_the_export_so_it_replays(self):
        out = os.path.join(self.external, "out")
        record_command.save_to(out, {"app": APP, "name": "exported", "steps": [{"op": "tap"}]})
        self.assertTrue(sources.is_external("%s/exported" % APP))
        self.assertEqual(records.load(APP, "exported")["name"], "exported")


if __name__ == "__main__":
    unittest.main()
