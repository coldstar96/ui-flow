import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import lease
from uiflow.commands import lease as command


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update({"udid": "SIM-1", "as_json": False, "steal": False}, **kwargs)


class LeaseCommandTests(unittest.TestCase):
    """The shell suites drive the lease through this command, so it is the cross-language contract."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-lease-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env("UIFLOW_LOCK_DIR", self.dir)
        self.env("UIFLOW_OWNER", "session-a")

    def env(self, key, value):
        saved = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, saved) if saved is not None
                        else os.environ.pop(key, None))

    def run_command(self, action, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            code = command.run(Args(action=action, **kwargs))
        return code, out.getvalue().strip()

    def test_acquire_then_status_reports_mine(self):
        self.assertEqual(self.run_command("acquire")[0], 0)
        self.assertIn("mine", self.run_command("status")[1])

    def test_release_gives_it_up(self):
        self.run_command("acquire")
        self.run_command("release")
        self.assertIn("unheld", self.run_command("status")[1])

    def test_acquire_fails_when_another_session_holds_it(self):
        self.run_command("acquire")
        self.env("UIFLOW_OWNER", "session-b")
        with self.assertRaises(lease.LeaseHeld):
            self.run_command("acquire")

    def test_status_exits_nonzero_when_somebody_else_holds_it(self):
        self.run_command("acquire")
        self.env("UIFLOW_OWNER", "session-b")
        code, text = self.run_command("status")
        self.assertEqual(code, 1, "a shell caller needs a failing exit code to branch on")
        self.assertIn("session-a", text)

    def test_status_exits_zero_when_free(self):
        self.assertEqual(self.run_command("status")[0], 0)

    def test_steal_takes_it(self):
        self.run_command("acquire")
        self.env("UIFLOW_OWNER", "session-b")
        self.assertEqual(self.run_command("acquire", steal=True)[0], 0)
        self.assertIn("mine", self.run_command("status")[1])

    def test_release_does_not_take_another_sessions_lease(self):
        self.run_command("acquire")
        self.env("UIFLOW_OWNER", "session-b")
        self.run_command("release")
        self.env("UIFLOW_OWNER", "session-a")
        self.assertIn("mine", self.run_command("status")[1])

    def test_renew_keeps_the_holder_and_refreshes_it(self):
        self.run_command("acquire")
        lease.write("SIM-1", dict(lease.read("SIM-1"), renewedAt=1.0))
        self.run_command("renew")
        self.assertGreater(lease.read("SIM-1")["renewedAt"], 1.0)
        self.assertEqual(lease.read("SIM-1")["owner"], "session-a")

    def test_json_names_the_holder(self):
        self.run_command("acquire")
        payload = json.loads(self.run_command("status", as_json=True)[1])
        self.assertEqual(payload["udid"], "SIM-1")
        self.assertEqual(payload["held"]["owner"], "session-a")



class UdidValidationTests(LeaseCommandTests):
    def test_a_traversing_udid_is_refused_before_it_becomes_a_path(self):
        for bad in ("../../escape", "a/b", "with space", "x/../../y"):
            with self.subTest(udid=bad), self.assertRaises(lease.LeaseHeld):
                self.run_command("acquire", udid=bad)

    def test_a_real_udid_passes(self):
        self.assertEqual(self.run_command("acquire", udid="B7E49377-10BE-490B-8C9E-5E41B946C03F")[0], 0)


if __name__ == "__main__":
    unittest.main()
