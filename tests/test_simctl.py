import datetime
import json
import os
import plistlib
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import simctl

_SAVED_ENV = {}


def setUpModule():
    """SKILL.md and the LeaseHeld message both tell users to export these, so a developer with
    either set would otherwise see this file resolve a different simulator and fail."""
    for name in ("UIFLOW_SIM_NAME", "UIFLOW_UDID"):
        _SAVED_ENV[name] = os.environ.pop(name, None)


def tearDownModule():
    for name, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

LISTING = json.loads("""
{
  "devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
      {"name": "uiflow-iphone17pro", "udid": "AAAA-1111", "state": "Shutdown", "isAvailable": true},
      {"name": "slyder-marketing-iphone", "udid": "CCCC-3333", "state": "Shutdown", "isAvailable": true},
      {"name": "twin", "udid": "DDDD-4444", "state": "Shutdown", "isAvailable": true},
      {"name": "twin", "udid": "EEEE-5555", "state": "Booted", "isAvailable": true},
      {"name": "gone", "udid": "FFFF-6666", "state": "Shutdown", "isAvailable": false}
    ],
    "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
      {"name": "slyder-uitest", "udid": "BBBB-2222", "state": "Booted", "isAvailable": true}
    ]
  }
}
""")


class ListingTests(unittest.TestCase):
    def test_name_to_udid(self):
        self.assertEqual(simctl.udid_from_listing(LISTING, "uiflow-iphone17pro"), "AAAA-1111")

    def test_booted_wins_over_shutdown_of_the_same_name(self):
        self.assertEqual(simctl.udid_from_listing(LISTING, "twin"), "EEEE-5555")

    def test_unknown_name_is_none(self):
        self.assertIsNone(simctl.udid_from_listing(LISTING, "nope"))

    def test_unavailable_device_is_ignored(self):
        self.assertIsNone(simctl.udid_from_listing(LISTING, "gone"))

    def test_bad_name_raises(self):
        for name in ("../evil", "has space", 'quote"', "", "a/b"):
            with self.assertRaises(simctl.SimctlError):
                simctl.validate_name(name)

    def test_good_name_passes(self):
        self.assertEqual(simctl.validate_name("slyder-uitest"), "slyder-uitest")


class FakeRun:
    def __init__(self, booted=True):
        self.calls = []
        self.booted = booted

    def __call__(self, args, check=True, capture=False):
        self.calls.append(list(args))
        if args[:3] == ["simctl", "list", "devices"] and "-j" in args:
            return json.dumps(LISTING)
        if args[:2] == ["simctl", "list"]:
            return "    twin (EEEE-5555) (%s)\n" % ("Booted" if self.booted else "Shutdown")
        return ""

    def ops(self):
        return [call[1] for call in self.calls if call and call[0] == "simctl"]


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun()
        saved_run, simctl._run = simctl._run, self.run
        self.addCleanup(setattr, simctl, "_run", saved_run)
        self.device = tempfile.mkdtemp(prefix="uiflow-device-")
        self.addCleanup(shutil.rmtree, self.device, True)
        os.makedirs(os.path.join(self.device, "Library", "Preferences"))
        saved_root = simctl.paths.device_root
        simctl.paths.device_root = lambda udid: self.device
        self.addCleanup(setattr, simctl.paths, "device_root", saved_root)
        saved_probe, simctl._system_domain_answers = simctl._system_domain_answers, lambda udid: True
        self.addCleanup(setattr, simctl, "_system_domain_answers", saved_probe)
        self.app = os.path.join(tempfile.mkdtemp(prefix="uiflow-app-"), "Demo.app")
        os.makedirs(self.app)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.app), True)

    def test_order_is_install_shutdown_prefs_boot(self):
        written = []
        saved = simctl.write_prefs
        simctl.write_prefs = lambda *a, **k: written.append(len(self.run.calls))
        self.addCleanup(setattr, simctl, "write_prefs", saved)
        simctl.prepare("EEEE-5555", app_path=self.app, keyboard_ids=["io.dtails.Slyder.SliderKeyboard"])
        ops = self.run.ops()
        self.assertIn("install", ops)
        self.assertIn("shutdown", ops)
        self.assertIn("boot", ops)
        self.assertLess(ops.index("install"), ops.index("shutdown"))
        prefs_at = written[0]  # number of simctl calls made before write_prefs ran
        install_at = next(i for i, c in enumerate(self.run.calls) if c[1] == "install")
        shutdown_at = next(i for i, c in enumerate(self.run.calls) if c[1] == "shutdown")
        boot_after = [i for i, c in enumerate(self.run.calls) if c[1] == "boot" and i > shutdown_at]
        self.assertLess(install_at, shutdown_at)
        self.assertLess(shutdown_at, prefs_at)
        self.assertTrue(boot_after and prefs_at <= boot_after[0])

    def test_never_calls_bootstatus(self):
        simctl.prepare("EEEE-5555", app_path=self.app)
        for call in self.run.calls:
            self.assertNotIn("bootstatus", call)

    def test_write_prefs_sets_keyboards_and_autolock(self):
        simctl.write_prefs("EEEE-5555", keyboard_ids=["io.dtails.Slyder.SliderKeyboard"], locale="en-US")
        prefs = os.path.join(self.device, "Library", "Preferences")
        with open(os.path.join(prefs, ".GlobalPreferences.plist"), "rb") as handle:
            globals_plist = plistlib.load(handle)
        with open(os.path.join(prefs, "com.apple.springboard.plist"), "rb") as handle:
            springboard = plistlib.load(handle)
        self.assertIn("io.dtails.Slyder.SliderKeyboard", globals_plist["AppleKeyboards"])
        self.assertEqual(globals_plist["AppleLocale"], "en-US")
        self.assertEqual(springboard["SBAutoLockTime"], 0.0)

    def test_write_prefs_shows_the_software_keyboard(self):
        # A device that has seen a hardware keyboard docks the software one off-screen, so its keys
        # are present but never hittable and no globe is vended — `prepare` must undo that.
        simctl.write_prefs("EEEE-5555", keyboard_ids=["io.dtails.Slyder.SliderKeyboard"])
        prefs = os.path.join(self.device, "Library", "Preferences")
        with open(os.path.join(prefs, "com.apple.keyboard.preferences.plist"), "rb") as handle:
            keyboard = plistlib.load(handle)
        self.assertIs(keyboard["AutomaticMinimizationEnabled"], False)

    def test_write_prefs_keeps_existing_keyboard_prefs(self):
        prefs = os.path.join(self.device, "Library", "Preferences")
        path = os.path.join(prefs, "com.apple.keyboard.preferences.plist")
        with open(path, "wb") as handle:
            plistlib.dump({"KeyboardAutocorrection": True, "AutomaticMinimizationEnabled": True}, handle)
        simctl.write_prefs("EEEE-5555")
        with open(path, "rb") as handle:
            keyboard = plistlib.load(handle)
        self.assertIs(keyboard["AutomaticMinimizationEnabled"], False)
        self.assertIs(keyboard["KeyboardAutocorrection"], True)

    def test_write_prefs_keeps_existing_keys(self):
        prefs = os.path.join(self.device, "Library", "Preferences")
        with open(os.path.join(prefs, ".GlobalPreferences.plist"), "wb") as handle:
            plistlib.dump({"KeepMe": 7}, handle)
        simctl.write_prefs("EEEE-5555", keyboard_ids=["k"])
        with open(os.path.join(prefs, ".GlobalPreferences.plist"), "rb") as handle:
            self.assertEqual(plistlib.load(handle)["KeepMe"], 7)


class EraseTests(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun()
        saved_run, simctl._run = simctl._run, self.run
        self.addCleanup(setattr, simctl, "_run", saved_run)
        self.downs = []
        saved_down, simctl.session.down = simctl.session.down, lambda udid=None: self.downs.append(udid)
        self.addCleanup(setattr, simctl.session, "down", saved_down)

    def test_erases_the_owned_simulator(self):
        simctl.erase(name="uiflow-iphone17pro", udid="AAAA-1111")
        self.assertIn("erase", self.run.ops())
        self.assertEqual(self.downs, ["AAAA-1111"])

    def test_refuses_another_sessions_simulator(self):
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.erase(name="slyder-marketing-iphone", udid="CCCC-3333")
        self.assertIn("--force", str(caught.exception))
        self.assertNotIn("erase", self.run.ops())

    def test_force_still_requires_the_name_repeated(self):
        with self.assertRaises(simctl.SimctlError):
            simctl.erase(name="slyder-marketing-iphone", udid="CCCC-3333", force=True)
        simctl.erase(name="slyder-marketing-iphone", udid="CCCC-3333", force=True, confirm="slyder-marketing-iphone")
        self.assertIn("erase", self.run.ops())


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun()
        saved_run, simctl._run = simctl._run, self.run
        self.addCleanup(setattr, simctl, "_run", saved_run)

    def test_resolve_defaults_to_the_structural_name(self):
        self.assertEqual(simctl.resolve(), "AAAA-1111")

    def test_a_non_default_device_type_resolves_under_its_own_name(self):
        # Not AAAA-1111: that device is an iPhone 17 Pro, and asking for an iPhone Air must not
        # hand back (or later overwrite) it.
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.resolve(device_type="iPhone Air")
        self.assertIn("uiflow-iphoneair", str(caught.exception))

    def test_the_create_error_points_at_renaming_an_older_device(self):
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.resolve("uiflow-iphoneair")
        self.assertIn("simctl rename", str(caught.exception))

    def test_resolve_finds_by_name(self):
        self.assertEqual(simctl.resolve("uiflow-iphone17pro"), "AAAA-1111")

    def test_resolve_refuses_to_create_without_the_flag(self):
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.resolve("brand-new")
        self.assertIn("--create", str(caught.exception))
        self.assertNotIn("create", self.run.ops())



REAP_LISTING = json.loads("""
{
  "devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
      {"name": "uiflow-iphone17pro", "udid": "DEFAULT-1", "state": "Booted", "isAvailable": true,
       "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"},
      {"name": "uiflow-iphoneair", "udid": "SPARE-1", "state": "Shutdown", "isAvailable": true,
       "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-Air"},
      {"name": "uiflow-leased", "udid": "LEASED-1", "state": "Booted", "isAvailable": true,
       "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-Air"},
      {"name": "slyder-test", "udid": "FOREIGN-1", "state": "Booted", "isAvailable": true,
       "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17"}
    ]
  }
}
""")


class ReapTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="uiflow-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.locks = tempfile.mkdtemp(prefix="uiflow-locks-")
        self.addCleanup(shutil.rmtree, self.locks, True)
        self.env("UIFLOW_HOME", self.home)
        self.env("UIFLOW_LOCK_DIR", self.locks)
        self.env("UIFLOW_OWNER", "me")
        self.deleted = []
        self.swap(simctl, "list_devices", lambda: REAP_LISTING)
        self.swap(simctl, "_run", self.fake_run)
        self.swap(simctl.session, "down", lambda udid=None, **k: self.deleted.append(("down", udid)))
        self.swap(simctl.session, "find_channel", lambda *a, **k: None)

    def fake_run(self, args, check=True, capture=False):
        self.deleted.append(tuple(args))
        return ""

    def swap(self, target, name, value):
        saved = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, saved)

    def env(self, key, value):
        saved = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, saved) if saved is not None
                        else os.environ.pop(key, None))

    def shutdown(self, udid):
        listing = json.loads(json.dumps(REAP_LISTING))
        for entry in listing["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"]:
            if entry["udid"] == udid:
                entry["state"] = "Shutdown"
        self.swap(simctl, "list_devices", lambda: listing)

    def deletes(self):
        return [call[2] for call in self.deleted if call[:2] == ("simctl", "delete")]


class ReapableTests(ReapTestCase):
    def test_the_working_device_is_never_reapable(self):
        self.assertNotIn("DEFAULT-1", [d["udid"] for d in simctl.reapable()])

    def test_a_foreign_device_is_never_reapable(self):
        self.assertNotIn("FOREIGN-1", [d["udid"] for d in simctl.reapable()])

    def test_a_spare_uiflow_device_is_reapable(self):
        self.assertIn("SPARE-1", [d["udid"] for d in simctl.reapable()])

    def test_a_device_another_session_holds_is_not_reapable(self):
        simctl.lease.write("LEASED-1", {"udid": "LEASED-1", "owner": "someone-else",
                                        "renewedAt": time.time()})
        self.assertNotIn("LEASED-1", [d["udid"] for d in simctl.reapable()])

    def test_our_own_stale_lease_does_not_protect_a_spare(self):
        simctl.lease.write("SPARE-1", {"udid": "SPARE-1", "owner": "me", "renewedAt": 0.0})
        self.assertIn("SPARE-1", [d["udid"] for d in simctl.reapable()])

    def test_a_device_with_a_live_driver_is_not_reapable(self):
        self.swap(simctl.session, "find_channel", lambda udid=None, **k: "/chan" if udid == "SPARE-1" else None)
        self.swap(simctl.session, "channel_is_live", lambda *a, **k: True)
        self.assertNotIn("SPARE-1", [d["udid"] for d in simctl.reapable()])

    def test_a_recorded_device_outside_the_namespace_is_still_ours(self):
        self.shutdown("FOREIGN-1")
        simctl.registry.record("FOREIGN-1", name="slyder-test", device_type="iPhone 17",
                               created_at=time.time() - 90 * 3600)
        self.assertIn("FOREIGN-1", [d["udid"] for d in simctl.reapable()])

    def test_a_booted_device_is_never_reaped(self):
        # lastBootedAt is stamped at boot and never advances, so a device open in Simulator.app for
        # days reads as days idle. Booted is the only signal somebody may still be looking at it.
        self.assertEqual([d["udid"] for d in simctl.reapable() if d["udid"] == "LEASED-1"], [])
        self.shutdown("LEASED-1")
        self.assertIn("LEASED-1", [d["udid"] for d in simctl.reapable()])


class SweepTests(ReapTestCase):
    def test_a_dry_run_deletes_nothing(self):
        planned = simctl.sweep(yes=False)
        self.assertIn("SPARE-1", [d["udid"] for d in planned])
        self.assertEqual(self.deletes(), [])

    def test_confirmed_sweep_deletes_exactly_what_it_planned(self):
        simctl.registry.record("SPARE-1", name="uiflow-iphoneair", device_type="iPhone Air",
                               created_at=time.time() - 90 * 3600)
        planned = simctl.sweep(yes=True)
        self.assertEqual(self.deletes(), [entry["udid"] for entry in planned])
        self.assertIn("SPARE-1", self.deletes())
        self.assertFalse(simctl.registry.is_recorded("SPARE-1"))

    def test_sweep_spares_the_working_device_and_foreign_ones(self):
        simctl.sweep(yes=True)
        self.assertNotIn("DEFAULT-1", self.deletes())
        self.assertNotIn("FOREIGN-1", self.deletes())


class DeleteTests(ReapTestCase):
    def test_delete_takes_the_session_down_first(self):
        simctl.delete(udid="SPARE-1")
        self.assertIn(("down", "SPARE-1"), self.deleted)
        self.assertEqual(self.deletes(), ["SPARE-1"])

    def test_delete_refuses_a_foreign_device(self):
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.delete(udid="FOREIGN-1")
        self.assertIn("did not create", str(caught.exception))
        self.assertEqual(self.deletes(), [])

    def test_force_deletes_a_foreign_device_only_with_confirmation(self):
        with self.assertRaises(simctl.SimctlError):
            simctl.delete(udid="FOREIGN-1", force=True)
        simctl.delete(udid="FOREIGN-1", force=True, confirm="slyder-test")
        self.assertEqual(self.deletes(), ["FOREIGN-1"])

    def test_delete_refuses_a_device_another_session_holds(self):
        simctl.lease.write("SPARE-1", {"udid": "SPARE-1", "owner": "someone-else",
                                       "renewedAt": time.time()})
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.delete(udid="SPARE-1")
        self.assertIn("someone-else", str(caught.exception))
        self.assertEqual(self.deletes(), [])


class IdleAgeTests(ReapTestCase):
    """A lease goes stale between runs, so age is what separates an orphan from a paused session."""

    def recent_boot(self, hours):
        stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        listing = json.loads(json.dumps(REAP_LISTING))
        for entry in listing["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"]:
            if entry["udid"] == "SPARE-1":
                entry["lastBootedAt"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.swap(simctl, "list_devices", lambda: listing)

    def test_a_recently_booted_device_is_spared(self):
        self.recent_boot(1)
        self.assertNotIn("SPARE-1", [d["udid"] for d in simctl.reapable()])

    def test_a_long_idle_device_is_reaped(self):
        self.recent_boot(48)
        self.assertIn("SPARE-1", [d["udid"] for d in simctl.reapable()])

    def test_the_threshold_is_adjustable(self):
        self.recent_boot(1)
        self.assertIn("SPARE-1", [d["udid"] for d in simctl.reapable(older_than_hours=0)])

    def test_a_just_created_device_is_spared_even_before_its_first_boot(self):
        simctl.registry.record("SPARE-1", name="uiflow-iphoneair", device_type="iPhone Air")
        self.assertNotIn("SPARE-1", [d["udid"] for d in simctl.reapable()])

    def test_a_never_booted_unregistered_device_is_inert_and_reapable(self):
        self.assertIn("SPARE-1", [d["udid"] for d in simctl.reapable()])


class RefusalIsInertTests(ReapTestCase):
    """A refusal must not have already touched the simulator on its way to saying no.

    The sibling session hit exactly this: its udid was recorded before the ownership guard, so a
    polite refusal still ran `simctl terminate` against the other session's device.
    """

    def setUp(self):
        super().setUp()
        self.swap(simctl.session, "down", self.real_down_guard)

    def real_down_guard(self, udid=None, **kwargs):
        held = simctl.lease.held_by_other(udid)
        if held:
            raise simctl.lease.LeaseHeld("simulator %s is in use by %s" % (udid, held.get("owner")))
        self.deleted.append(("down", udid))

    def hold(self, udid):
        simctl.lease.write(udid, {"udid": udid, "owner": "someone-else", "renewedAt": time.time()})

    def destructive(self):
        return [c for c in self.deleted
                if c[:2] in (("simctl", "erase"), ("simctl", "delete"), ("simctl", "shutdown"),
                             ("simctl", "terminate"))]

    def test_erase_refused_by_the_lease_erases_nothing(self):
        self.hold("DEFAULT-1")
        with self.assertRaises(simctl.lease.LeaseHeld):
            simctl.erase(name="uiflow-iphone17pro", udid="DEFAULT-1")
        self.assertEqual(self.destructive(), [])

    def test_delete_refused_by_the_lease_touches_nothing(self):
        self.hold("SPARE-1")
        with self.assertRaises(simctl.SimctlError):
            simctl.delete(udid="SPARE-1")
        self.assertEqual(self.destructive(), [])

    def test_delete_refused_by_provenance_touches_nothing(self):
        with self.assertRaises(simctl.SimctlError):
            simctl.delete(udid="FOREIGN-1")
        self.assertEqual(self.destructive(), [])

    def test_sweep_skips_a_device_claimed_after_the_plan_and_keeps_going(self):
        self.hold("LEASED-1")
        planned = simctl.sweep(yes=True)
        outcomes = {entry["name"]: entry.get("deleted") for entry in planned}
        self.assertFalse(outcomes.get("uiflow-leased"), "a claimed device must be skipped")
        self.assertTrue(outcomes.get("uiflow-iphoneair"), "and must not abort the rest")


class WorkingDeviceTests(ReapTestCase):
    def test_delete_with_no_arguments_refuses_the_working_device(self):
        # `--name` and `--udid` are both optional, so a bare `uiflow sim delete` resolves to the
        # device you are working on — ~3GB plus the installed app, prefs and keyboard setup.
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.delete()
        self.assertIn("not a spare", str(caught.exception))
        self.assertEqual(self.deletes(), [])

    def test_the_working_device_can_still_be_deleted_deliberately(self):
        simctl.delete(udid="DEFAULT-1", force=True, confirm="uiflow-iphone17pro")
        self.assertEqual(self.deletes(), ["DEFAULT-1"])

    def test_sweep_never_offers_the_working_device(self):
        self.assertNotIn("DEFAULT-1", [d["udid"] for d in simctl.reapable()])



class ForceGateTests(ReapTestCase):
    def test_force_alone_does_not_destroy_the_working_device(self):
        # The --confirm check used to sit only in the "not ours" branch, so for a uiflow-namespaced
        # device — every device the guard protects — --force alone was enough.
        with self.assertRaises(simctl.SimctlError):
            simctl.delete(udid="DEFAULT-1", force=True)
        self.assertEqual(self.deletes(), [])

    def test_force_with_the_matching_confirm_goes_through(self):
        simctl.delete(udid="DEFAULT-1", force=True, confirm="uiflow-iphone17pro")
        self.assertEqual(self.deletes(), ["DEFAULT-1"])

    def test_a_wrong_confirm_is_refused(self):
        with self.assertRaises(simctl.SimctlError):
            simctl.delete(udid="DEFAULT-1", force=True, confirm="uiflow-typo")
        self.assertEqual(self.deletes(), [])


class DeviceTypeOverrideTests(ReapTestCase):
    def test_a_pinned_name_that_agrees_is_not_a_conflict(self):
        # The documented escape hatch creates with --device-type then exports the resulting name,
        # so from then on the two AGREE — refusing that broke the workflow the code recommends.
        self.env("UIFLOW_SIM_NAME", "uiflow-iphoneair")
        self.assertEqual(simctl.default_name("iPhone Air"), "uiflow-iphoneair")

    def test_a_pinned_name_and_an_explicit_device_type_are_refused_as_a_conflict(self):
        # Either silent winner is a trap: the flag wins and `prepare` reinstalls and reboots a
        # DIFFERENT simulator than the pinned one; the env var wins and `--create` makes an
        # iPhone Air called uiflow-iphone17pro.
        self.env("UIFLOW_SIM_NAME", "uiflow-iphone17pro")
        with self.assertRaises(simctl.SimctlError) as caught:
            simctl.default_name("iPhone Air")
        self.assertIn("different simulators", str(caught.exception))

    def test_an_explicit_device_type_alone_names_its_own_device(self):
        self.assertEqual(simctl.default_name("iPhone Air"), "uiflow-iphoneair")

    def test_an_empty_device_type_is_still_rejected(self):
        with self.assertRaises(simctl.SimctlError):
            simctl.default_name("")

    def test_the_env_var_still_wins_when_no_device_type_is_given(self):
        self.env("UIFLOW_SIM_NAME", "pinned-name")
        self.assertEqual(simctl.default_name(), "pinned-name")

    def test_the_default_device_type_is_used_when_neither_is_given(self):
        self.assertEqual(simctl.default_name(), "uiflow-iphone17pro")


class SweepDeleteAgreementTests(ReapTestCase):
    def test_sweep_never_plans_a_device_delete_would_refuse(self):
        # They used to disagree: reapable excluded only default_udid while delete also excluded the
        # name-resolved device, so a sweep listed a spare, refused it, and could never come back
        # clean.
        planned = {entry["udid"] for entry in simctl.reapable()}
        for udid in planned:
            self.assertNotIn(udid, simctl.working_udids(simctl.list_devices()))

    def test_a_spare_is_still_reaped_when_a_udid_override_is_set(self):
        # UIFLOW_UDID is a supported override; it must not silently shrink what sweep can reap.
        self.env("UIFLOW_UDID", "SPARE-1")
        self.assertNotIn("SPARE-1", [d["udid"] for d in simctl.reapable()])
        self.assertIn("SPARE-1", simctl.working_udids(simctl.list_devices()))



class SimulatorFactsTests(unittest.TestCase):
    DEVICES = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
        {"udid": "AAAA-1111", "name": "uiflow-iphone17pro", "state": "Booted", "isAvailable": True}]}}
    RUNTIMES = {"runtimes": [{"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
                              "version": "26.5", "buildversion": "23F77",
                              "runtimeRoot": "/Volumes/iOS_23F77/RuntimeRoot"}]}

    def setUp(self):
        self.spawned = 0
        self.languages = '(\n    "en-US",\n    "ko-US"\n)\n'
        self.runtimes_fail = False
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.swap(simctl, "_run", self.fake_run)
        self.swap(simctl.paths, "device_root", lambda udid: os.path.join(self.root.name, udid))
        self.swap(simctl.session, "read_state", lambda udid: self.state)
        self.written = []
        self.swap(simctl.session, "write_state",
                  lambda udid, payload: self.written.append(payload))
        self.state = None

    def swap(self, module, name, value):
        saved = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, saved)

    def fake_run(self, args, check=True, capture=False):
        if args[:3] == ["simctl", "list", "runtimes"]:
            if self.runtimes_fail:
                raise simctl.SimctlError("boom")
            return json.dumps(self.RUNTIMES)
        if args[:3] == ["simctl", "list", "devices"]:
            return json.dumps(self.DEVICES)
        if args[:2] == ["simctl", "spawn"]:
            self.spawned += 1
            return self.languages
        if args[:2] == ["simctl", "get_app_container"]:
            return self.container
        return ""

    container = ""

    def write_globals(self, udid, languages):
        directory = os.path.join(self.root.name, udid, "Library", "Preferences")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, ".GlobalPreferences.plist"), "wb") as handle:
            plistlib.dump({"AppleLanguages": languages}, handle, fmt=plistlib.FMT_BINARY)

    def test_the_runtime_root_and_build_come_from_the_device_s_runtime_key(self):
        found = simctl.runtime_for("AAAA-1111")
        self.assertEqual(found["root"], "/Volumes/iOS_23F77/RuntimeRoot")
        self.assertEqual(found["build"], "23F77")

    def test_an_unknown_udid_has_no_runtime(self):
        self.assertIsNone(simctl.runtime_for("ZZZZ-9999"))

    def test_a_failing_simctl_is_no_runtime_not_an_exception(self):
        """An optional translation lookup must never abort a run."""
        self.runtimes_fail = True
        self.assertIsNone(simctl.runtime_for("AAAA-1111"))

    def test_session_state_answers_the_language_without_touching_the_device(self):
        self.state = {"udid": "AAAA-1111", "uiLanguage": "ko-US"}
        self.assertEqual(simctl.ui_language("AAAA-1111"), "ko-US")
        self.assertEqual(self.spawned, 0)

    def test_the_global_preferences_plist_answers_next(self):
        self.write_globals("AAAA-1111", ["ja"])
        self.assertEqual(simctl.ui_language("AAAA-1111"), "ja")
        self.assertEqual(self.spawned, 0)

    def test_a_device_with_no_plist_falls_back_to_defaults_read(self):
        """Booted 136160D4 reports ja from spawn while having no .GlobalPreferences.plist."""
        self.languages = "(\n    ja\n)\n"
        self.assertEqual(simctl.ui_language("AAAA-1111"), "ja")
        self.assertEqual(self.spawned, 1)

    def test_the_defaults_read_answer_is_written_back_to_the_live_session(self):
        """~221 ms, and every run re-paid it because nothing ever recorded the answer."""
        self.state = {"udid": "AAAA-1111", "channel": "/tmp/chan"}
        self.languages = "(\n    ja\n)\n"
        self.assertEqual(simctl.ui_language("AAAA-1111"), "ja")
        self.assertEqual(self.written, [{"udid": "AAAA-1111", "channel": "/tmp/chan",
                                         "uiLanguage": "ja"}])

    def test_a_simulator_with_no_session_is_given_no_state_file(self):
        self.languages = "(\n    ja\n)\n"
        self.assertEqual(simctl.ui_language("AAAA-1111"), "ja")
        self.assertEqual(self.written, [])

    def test_an_empty_defaults_read_is_no_language_not_a_crash(self):
        self.languages = ""
        self.assertIsNone(simctl.ui_language("AAAA-1111"))

    def test_no_udid_probes_nothing(self):
        self.assertIsNone(simctl.ui_language(None))
        self.assertIsNone(simctl.app_bundle_path(None, "io.dtails.Slyder"))
        self.assertIsNone(simctl.runtime_for(None))
        self.assertEqual(self.spawned, 0)

    def test_a_missing_app_container_is_none(self):
        self.container = "/nope/does/not/exist.app\n"
        self.assertIsNone(simctl.app_bundle_path("AAAA-1111", "io.dtails.Slyder"))

    def test_an_existing_app_container_is_returned_stripped(self):
        self.container = self.root.name + "\n"
        self.assertEqual(simctl.app_bundle_path("AAAA-1111", "io.dtails.Slyder"), self.root.name)


if __name__ == "__main__":
    unittest.main()
