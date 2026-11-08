import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import registry


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-registry-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = os.environ.get("UIFLOW_HOME")
        os.environ["UIFLOW_HOME"] = self.dir
        self.addCleanup(lambda: os.environ.__setitem__("UIFLOW_HOME", saved) if saved is not None
                        else os.environ.pop("UIFLOW_HOME", None))


class RecordTests(RegistryTestCase):
    def test_a_created_device_is_recorded(self):
        registry.record("AAAA", name="uiflow-iphone17pro", device_type="iPhone 17 Pro")
        entry = registry.read().get("AAAA")
        self.assertEqual(entry["name"], "uiflow-iphone17pro")
        self.assertEqual(entry["deviceType"], "iPhone 17 Pro")
        self.assertTrue(entry["createdAt"])

    def test_recording_survives_a_reload(self):
        registry.record("AAAA", name="uiflow-iphone17pro", device_type="iPhone 17 Pro")
        registry.record("BBBB", name="uiflow-iphoneair", device_type="iPhone Air")
        self.assertEqual(sorted(registry.read()), ["AAAA", "BBBB"])

    def test_forget_removes_one_without_touching_the_others(self):
        registry.record("AAAA", name="a", device_type="t")
        registry.record("BBBB", name="b", device_type="t")
        registry.forget("AAAA")
        self.assertEqual(sorted(registry.read()), ["BBBB"])

    def test_forgetting_an_unknown_device_is_not_an_error(self):
        registry.forget("NOPE")

    def test_a_corrupt_registry_reads_as_empty_rather_than_crashing(self):
        with open(registry.path(create=True), "w") as handle:
            handle.write("{ not json")
        self.assertEqual(registry.read(), {})

    def test_reading_does_not_create_the_home_directory(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.assertEqual(registry.read(), {})
        self.assertFalse(os.path.exists(self.dir))


class OwnershipTests(RegistryTestCase):
    def test_a_recorded_device_is_ours(self):
        registry.record("AAAA", name="whatever-name", device_type="t")
        self.assertTrue(registry.is_ours("AAAA", "whatever-name"))

    def test_the_name_prefix_is_the_backstop_when_the_registry_is_lost(self):
        self.assertTrue(registry.is_ours("UNKNOWN", "uiflow-iphone17pro"))

    def test_a_foreign_device_is_not_ours(self):
        self.assertFalse(registry.is_ours("UNKNOWN", "slyder-test"))

    def test_registered_and_unregistered_are_distinguishable(self):
        registry.record("AAAA", name="uiflow-iphone17pro", device_type="t")
        self.assertTrue(registry.is_recorded("AAAA"))
        self.assertFalse(registry.is_recorded("BBBB"))


if __name__ == "__main__":
    unittest.main()
