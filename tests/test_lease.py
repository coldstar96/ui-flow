import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import lease

UDID = "AAAA-1111"


class LeaseTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-lease-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env("UIFLOW_LOCK_DIR", self.dir)
        self.env("UIFLOW_OWNER", "session-a")

    def env(self, key, value):
        saved = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, saved) if saved is not None
                        else os.environ.pop(key, None))


class OwnerTests(LeaseTestCase):
    def clear_session_vars(self, keep=()):
        for name in lease.SESSION_VARS:
            if name in keep:
                continue
            saved = os.environ.pop(name, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, name, saved)

    def test_explicit_owner_wins(self):
        self.assertEqual(lease.owner(), "session-a")

    def test_the_chain_is_ordered_most_specific_first(self):
        for index, name in enumerate(lease.SESSION_VARS):
            with self.subTest(var=name):
                self.clear_session_vars()
                self.env(name, "id-for-%s" % name)
                for weaker in lease.SESSION_VARS[index + 1:]:
                    self.env(weaker, "weaker")
                self.assertEqual(lease.owner(), "id-for-%s" % name)

    def test_the_conversation_id_outranks_the_per_subagent_one(self):
        # CLAUDE_CODE_SESSION_ID is minted per subagent, so preferring it makes an agent and the
        # subagents it fans work out to rivals that lock each other out of their own simulator.
        self.assertLess(lease.SESSION_VARS.index("CLAUDE_CODE_BRIDGE_SESSION_ID"),
                        lease.SESSION_VARS.index("CLAUDE_CODE_SESSION_ID"))
        self.clear_session_vars()
        self.env("CLAUDE_CODE_BRIDGE_SESSION_ID", "conversation-1")
        self.env("CLAUDE_CODE_SESSION_ID", "subagent-7")
        self.assertEqual(lease.owner(), "conversation-1")

    def test_a_parent_and_its_subagent_share_one_identity(self):
        self.clear_session_vars()
        self.env("CLAUDE_CODE_BRIDGE_SESSION_ID", "conversation-1")
        self.env("CLAUDE_CODE_SESSION_ID", "parent")
        parent = lease.owner()
        os.environ["CLAUDE_CODE_SESSION_ID"] = "subagent-7"
        self.assertEqual(lease.owner(), parent)

    def test_claude_codes_real_variable_name_is_honoured(self):
        # CLAUDE_SESSION_ID does not exist; CLAUDE_CODE_SESSION_ID is what Claude Code exports, and
        # assuming the wrong name left the chain falling through on the skill's own primary host.
        self.assertIn("CLAUDE_CODE_SESSION_ID", lease.SESSION_VARS)
        self.clear_session_vars()
        self.env("CLAUDE_CODE_SESSION_ID", "claude-abc")
        self.assertEqual(lease.owner(), "claude-abc")

    def test_the_last_resort_is_stable_not_process_derived(self):
        # Every `uiflow` command is a new process whose pid, ppid, pgrp and session id all differ
        # from the last one's. A process-derived identity cannot recognise its own lease.
        self.clear_session_vars()
        first = lease.owner()
        self.assertEqual(first, lease.owner())
        for value in (os.getpid(), os.getppid(), os.getpgrp()):
            self.assertNotIn(str(value), first)


class AcquireTests(LeaseTestCase):
    def test_acquire_records_us_as_the_holder(self):
        held = lease.acquire(UDID)
        self.assertEqual(held["owner"], "session-a")
        self.assertEqual(lease.read(UDID)["owner"], "session-a")

    def test_our_own_lease_is_not_a_collision(self):
        lease.acquire(UDID)
        self.assertIsNone(lease.held_by_other(UDID))
        self.assertEqual(lease.acquire(UDID)["owner"], "session-a")

    def test_another_live_owner_blocks_us(self):
        lease.acquire(UDID)
        self.env("UIFLOW_OWNER", "session-b")
        held = lease.held_by_other(UDID)
        self.assertEqual(held["owner"], "session-a")
        with self.assertRaises(lease.LeaseHeld) as caught:
            lease.acquire(UDID)
        self.assertIn("session-a", str(caught.exception))
        self.assertIn("--steal", str(caught.exception))

    def test_a_stale_lease_is_free_to_take(self):
        lease.acquire(UDID)
        lease.write(UDID, dict(lease.read(UDID), renewedAt=0.0))
        self.env("UIFLOW_OWNER", "session-b")
        self.assertIsNone(lease.held_by_other(UDID))
        self.assertEqual(lease.acquire(UDID)["owner"], "session-b")

    def test_steal_takes_a_live_lease(self):
        lease.acquire(UDID)
        self.env("UIFLOW_OWNER", "session-b")
        self.assertEqual(lease.acquire(UDID, steal=True)["owner"], "session-b")

    def test_release_frees_it(self):
        lease.acquire(UDID)
        lease.release(UDID)
        self.assertIsNone(lease.read(UDID))

    def test_release_leaves_another_owners_lease_alone(self):
        lease.acquire(UDID)
        self.env("UIFLOW_OWNER", "session-b")
        lease.release(UDID)
        self.assertEqual(lease.read(UDID)["owner"], "session-a")

    def test_renew_moves_the_clock_without_changing_the_holder(self):
        first = lease.acquire(UDID)
        lease.write(UDID, dict(first, renewedAt=first["renewedAt"] - 50))
        renewed = lease.renew(UDID)
        self.assertEqual(renewed["owner"], "session-a")
        self.assertGreater(renewed["renewedAt"], first["renewedAt"] - 50)

    def test_renew_does_not_steal_from_another_owner(self):
        lease.acquire(UDID)
        self.env("UIFLOW_OWNER", "session-b")
        lease.renew(UDID)
        self.assertEqual(lease.read(UDID)["owner"], "session-a")


class DescribeTests(LeaseTestCase):
    def test_ours_reads_as_mine(self):
        lease.acquire(UDID)
        self.assertEqual(lease.describe(UDID), "mine")

    def test_another_owner_is_named_with_its_idle_time(self):
        lease.acquire(UDID)
        self.env("UIFLOW_OWNER", "session-b")
        self.assertRegex(lease.describe(UDID), r"^held by session-a \d+s ago$")

    def test_unheld_is_blank(self):
        self.assertEqual(lease.describe(UDID), "")

    def test_a_stale_lease_is_blank(self):
        lease.acquire(UDID)
        lease.write(UDID, dict(lease.read(UDID), renewedAt=0.0))
        self.env("UIFLOW_OWNER", "session-b")
        self.assertEqual(lease.describe(UDID), "")



class OwnerStabilityTests(LeaseTestCase):
    """The owner must survive across processes: every `uiflow` command is a new one."""

    def setUp(self):
        super().setUp()
        for name in lease.SESSION_VARS:
            saved = os.environ.pop(name, None)
            if saved is not None:
                self.addCleanup(os.environ.__setitem__, name, saved)

    def test_the_owner_is_not_the_pid(self):
        self.assertNotIn(str(os.getpid()), lease.owner())

    def test_a_second_process_recognises_the_first_ones_lease_as_its_own(self):
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = (
            "import sys, os; sys.path.insert(0, %r);"
            "from uiflow import lease;"
            "print(lease.%s)"
        )
        env = {k: v for k, v in os.environ.items() if k not in lease.SESSION_VARS}

        def run(expression):
            return subprocess.run(
                [sys.executable, "-c", script % (os.path.join(root, "lib"), expression)],
                capture_output=True, text=True, env=env, check=True,
            ).stdout.strip()

        self.assertEqual(run("acquire('SIM-X')['owner']"), run("owner()"))
        self.assertEqual(run("describe('SIM-X')"), "mine")
        self.assertEqual(run("held_by_other('SIM-X')"), "None")
        run("release('SIM-X')")
        self.assertEqual(run("read('SIM-X')"), "None")


if __name__ == "__main__":
    unittest.main()
