import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import records

APP = "com.example.Demo"
IPHONE = "402x874"
IPAD = "1024x1366"


class KeyMapStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-keymap-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)

    def write_legacy(self):
        os.makedirs(records.dir_for(APP), exist_ok=True)
        with open(records.key_map_path(APP), "w") as handle:
            json.dump({"app": APP, "keys": {"G": [166.0, 742.3]}, "origin": {"dy": 568.0}}, handle)

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(records.key_map(APP, IPHONE), {})
        self.assertEqual(records.key_map_fingerprints(APP), ())
        self.assertFalse(records.key_map_is_legacy(APP))

    def test_saving_writes_a_per_device_document(self):
        records.save_key_map(APP, {"G": [166.0, 742.3]}, IPHONE,
                             calibration={"dx": 0.0, "dy": 568.0, "probe": "G"})
        document = records.key_map_document(APP)
        self.assertEqual(list(document["devices"]), [IPHONE])
        self.assertEqual(document["devices"][IPHONE]["dy"], 568.0)
        self.assertFalse(records.key_map_is_legacy(APP))

    def test_a_fingerprint_hit_returns_that_devices_points(self):
        records.save_key_map(APP, {"G": [166.0, 742.3]}, IPHONE)
        self.assertEqual(records.key_map(APP, IPHONE), {"G": [166.0, 742.3]})

    def test_a_fingerprint_miss_returns_empty_not_another_devices_points(self):
        records.save_key_map(APP, {"G": [166.0, 742.3]}, IPHONE)
        self.assertEqual(records.key_map(APP, IPAD), {})

    def test_a_second_device_does_not_disturb_the_first(self):
        records.save_key_map(APP, {"G": [166.0, 742.3]}, IPHONE)
        records.save_key_map(APP, {"G": [455.2, 1183.6]}, IPAD)
        self.assertEqual(records.key_map(APP, IPHONE), {"G": [166.0, 742.3]})
        self.assertEqual(records.key_map(APP, IPAD), {"G": [455.2, 1183.6]})
        self.assertEqual(records.key_map_fingerprints(APP), (IPAD, IPHONE))

    def test_relearning_one_device_replaces_only_its_entry(self):
        records.save_key_map(APP, {"G": [1.0, 2.0]}, IPHONE)
        records.save_key_map(APP, {"G": [3.0, 4.0]}, IPAD)
        records.save_key_map(APP, {"G": [9.0, 9.0]}, IPHONE)
        self.assertEqual(records.key_map(APP, IPHONE), {"G": [9.0, 9.0]})
        self.assertEqual(records.key_map(APP, IPAD), {"G": [3.0, 4.0]})

    def test_key_map_only_returns_the_sole_device(self):
        records.save_key_map(APP, {"G": [166.0, 742.3]}, IPHONE)
        self.assertEqual(records.key_map_only(APP), {"G": [166.0, 742.3]})

    def test_key_map_only_refuses_to_guess_between_devices(self):
        records.save_key_map(APP, {"G": [1.0, 2.0]}, IPHONE)
        records.save_key_map(APP, {"G": [3.0, 4.0]}, IPAD)
        self.assertEqual(records.key_map_only(APP), {})

    def test_a_legacy_document_is_reported_as_legacy_and_readable(self):
        self.write_legacy()
        self.assertTrue(records.key_map_is_legacy(APP))
        self.assertEqual(records.key_map_only(APP), {"G": [166.0, 742.3]})

    def test_a_legacy_document_misses_every_fingerprint(self):
        self.write_legacy()
        self.assertEqual(records.key_map(APP, IPHONE), {})

    def test_saving_over_a_legacy_document_migrates_it(self):
        self.write_legacy()
        records.save_key_map(APP, {"G": [166.0, 742.3]}, IPHONE)
        self.assertFalse(records.key_map_is_legacy(APP))
        self.assertEqual(records.key_map(APP, IPHONE), {"G": [166.0, 742.3]})
        self.assertNotIn("keys", records.key_map_document(APP))

    def test_an_unknown_top_level_key_survives_a_save(self):
        self.write_legacy()
        records.save_key_map(APP, {"G": [1.0, 2.0]}, IPHONE)
        self.assertEqual(records.key_map_document(APP)["origin"]["dy"], 568.0)

    def test_a_malformed_document_reads_as_empty(self):
        os.makedirs(records.dir_for(APP), exist_ok=True)
        with open(records.key_map_path(APP), "w") as handle:
            handle.write("{not json")
        self.assertEqual(records.key_map(APP, IPHONE), {})
        self.assertEqual(records.key_map_only(APP), {})

    def test_a_device_entry_that_is_not_an_object_reads_as_empty(self):
        records.save_key_map(APP, {"G": [1.0, 2.0]}, IPHONE)
        document = records.key_map_document(APP)
        document["devices"][IPHONE] = [["G", 1.0, 2.0]]
        with open(records.key_map_path(APP), "w") as handle:
            json.dump(document, handle)
        self.assertEqual(records.key_map(APP, IPHONE), {})
        self.assertEqual(records.key_map_only(APP), {})

    def test_a_malformed_point_is_dropped_not_raised(self):
        records.save_key_map(APP, {"G": [1.0, 2.0]}, IPHONE)
        document = records.key_map_document(APP)
        document["devices"][IPHONE]["keys"]["H"] = [5.0]
        with open(records.key_map_path(APP), "w") as handle:
            json.dump(document, handle)
        self.assertEqual(records.key_map(APP, IPHONE), {"G": [1.0, 2.0]})


if __name__ == "__main__":
    unittest.main()
