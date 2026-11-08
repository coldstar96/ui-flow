import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import device

LISTING = json.loads("""
{
  "devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
      {"name": "uiflow-iphone17pro", "udid": "AAAA-1111", "state": "Booted", "isAvailable": true,
       "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro"},
      {"name": "bare", "udid": "BBBB-2222", "state": "Shutdown", "isAvailable": true}
    ],
    "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
      {"name": "old", "udid": "CCCC-3333", "state": "Shutdown", "isAvailable": true,
       "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-inch-M5-12GB"}
    ]
  }
}
""")


class SlugTests(unittest.TestCase):
    def test_iphone(self):
        self.assertEqual(device.slug("iPhone 17 Pro"), "iphone17pro")

    def test_inch_is_dropped_so_a_size_reads_as_a_number(self):
        self.assertEqual(device.slug("iPad Pro 13-inch (M5)"), "ipadpro13m5")

    def test_parentheses_and_spaces_go(self):
        self.assertEqual(device.slug("iPad (A16)"), "ipada16")

    def test_an_identifier_is_refused_rather_than_mis_slugged(self):
        # The identifier carries a RAM suffix the display name omits, so slugging it would name a
        # second simulator for a device type that already has one.
        with self.assertRaises(ValueError) as caught:
            device.slug("com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-inch-M5-12GB")
        self.assertIn("display name", str(caught.exception))

    def test_a_non_ascii_letter_does_not_collide_with_the_plain_name(self):
        # `ʀ` is stripped by the charset filter, so without a marker `iPhone Xʀ` becomes `iphonex`
        # and silently resolves to the iPhone X simulator.
        self.assertNotEqual(device.slug("iPhone Xʀ"), device.slug("iPhone X"))
        self.assertTrue(device.slug("iPhone Xʀ").startswith("iphonex"))

    def test_a_non_ascii_name_slugs_the_same_way_every_time(self):
        self.assertEqual(device.slug("iPhone Xʀ"), device.slug("iPhone Xʀ"))

    def test_empty_is_rejected(self):
        for bad in ("", "   ", "()", None):
            with self.assertRaises(ValueError):
                device.slug(bad)


class StructuralNameTests(unittest.TestCase):
    def test_default_device_type(self):
        self.assertEqual(device.structural_name(), "uiflow-iphone17pro")

    def test_explicit_device_type(self):
        self.assertEqual(device.structural_name("iPhone 17 Pro Max"), "uiflow-iphone17promax")

    def test_name_is_valid_for_simctl(self):
        # simctl.validate_name's charset — the generated name must never need quoting or escaping.
        self.assertRegex(device.structural_name("iPad Pro 13-inch (M5)"), r"^[A-Za-z0-9._-]+$")


class DescribeTests(unittest.TestCase):
    def test_full_description(self):
        info = device.describe("AAAA-1111", LISTING)
        self.assertEqual(info, {
            "udid": "AAAA-1111",
            "name": "uiflow-iphone17pro",
            "deviceType": "iPhone 17 Pro",
            "runtime": "iOS 26.5",
            "state": "Booted",
        })

    def test_runtime_comes_from_the_listing_key(self):
        self.assertEqual(device.describe("CCCC-3333", LISTING)["runtime"], "iOS 18.0")

    def test_device_type_identifier_is_prettified(self):
        self.assertEqual(
            device.describe("CCCC-3333", LISTING)["deviceType"], "iPad Pro 13 inch M5 12GB"
        )

    def test_missing_device_type_is_blank_not_an_error(self):
        self.assertEqual(device.describe("BBBB-2222", LISTING)["deviceType"], "")

    def test_unknown_udid_still_describes_the_udid(self):
        info = device.describe("NOPE", LISTING)
        self.assertEqual(info["udid"], "NOPE")
        self.assertEqual(info["name"], "")
        self.assertEqual(info["state"], "")

    def test_no_udid_is_none(self):
        self.assertIsNone(device.describe(None, LISTING))


class FormatLineTests(unittest.TestCase):
    def test_every_field_present(self):
        self.assertEqual(
            device.format_line(device.describe("AAAA-1111", LISTING)),
            "uiflow-iphone17pro · iPhone 17 Pro · iOS 26.5 · Booted · AAAA-1111",
        )

    def test_blank_fields_are_dropped_rather_than_shown_empty(self):
        self.assertEqual(
            device.format_line({"udid": "X", "name": "", "deviceType": "", "runtime": "", "state": ""}),
            "X",
        )

    def test_no_simulator_says_so(self):
        self.assertEqual(device.format_line(None), "no simulator resolved")


class FormatCompactTests(unittest.TestCase):
    def test_detail_nests_so_it_reads_inside_a_dot_separated_line(self):
        self.assertEqual(
            device.format_compact(device.describe("AAAA-1111", LISTING)),
            "uiflow-iphone17pro (iPhone 17 Pro, iOS 26.5, Booted, AAAA-1111)",
        )

    def test_a_lone_field_needs_no_parentheses(self):
        self.assertEqual(
            device.format_compact({"udid": "X", "name": "", "deviceType": "", "runtime": "", "state": ""}),
            "X",
        )

    def test_no_simulator_says_so(self):
        self.assertEqual(device.format_compact(None), "no simulator resolved")


if __name__ == "__main__":
    unittest.main()
