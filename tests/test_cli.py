import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import cli


class CliTests(unittest.TestCase):
    def test_help_lists_every_command(self):
        buffer = io.StringIO()
        cli._help(buffer)
        text = buffer.getvalue()
        for name in cli.COMMANDS:
            self.assertIn(name, text)
        self.assertNotIn("unavailable", text)

    def test_unknown_command_exits_two(self):
        saved, sys.stderr = sys.stderr, io.StringIO()
        try:
            self.assertEqual(cli.main(["nope"]), 2)
        finally:
            sys.stderr = saved

    def test_dispatch_imports_only_the_named_command(self):
        for module in [m for m in sys.modules if m.startswith("uiflow.commands.")]:
            del sys.modules[module]
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            cli.main([])
        finally:
            sys.stdout = saved
        loaded = sorted(m for m in sys.modules if m.startswith("uiflow.commands."))
        self.assertTrue(loaded)

    def test_the_composition_commands_are_registered(self):
        for name in ("find", "factor", "index"):
            self.assertIn(name, cli.COMMANDS)

    def test_every_command_module_has_the_contract(self):
        import importlib

        for name, path in cli.COMMANDS.items():
            module = importlib.import_module(path)
            self.assertEqual(module.NAME, name)
            self.assertTrue(module.HELP)
            self.assertTrue(callable(module.add_arguments))
            self.assertTrue(callable(module.run))


if __name__ == "__main__":
    unittest.main()
