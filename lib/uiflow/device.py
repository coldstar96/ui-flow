import hashlib
import re

DEFAULT_DEVICE_TYPE = "iPhone 17 Pro"
NAME_PREFIX = "uiflow"

DEVICE_TYPE_PREFIX = "com.apple.CoreSimulator.SimDeviceType."
RUNTIME_PREFIX = "com.apple.CoreSimulator.SimRuntime."


def slug(device_type):
    """`iPad Pro 13-inch (M5)` -> `ipadpro13m5`. Takes the DISPLAY name from `simctl list devicetypes`.

    Not the identifier: `iPad-Pro-13-inch-M5-12GB` carries a RAM suffix the display name omits, so
    the two slug differently and a name built from one would not match a device found by the other
    — every iPad Pro, iPhone SE 1st-gen and iPhone Xʀ differ. Feeding an identifier here would
    silently create a duplicate simulator, so it is refused instead.
    """
    text = str(device_type or "")
    looks_like_identifier = "SimDeviceType" in text or ("-" in text and " " not in text)
    if looks_like_identifier:
        raise ValueError(
            "device type %r is an identifier; pass the display name instead, e.g. \"iPhone 17 Pro\" "
            "(see `xcrun simctl list devicetypes`)" % device_type
        )
    lowered = text.lower()
    slugged = re.sub(r"[^a-z0-9]", "", re.sub(r"inch", "", lowered))
    if not slugged:
        raise ValueError("device type %r has no name to slug" % (device_type,))
    # `iPhone Xʀ` would otherwise strip to `iphonex` and collide with `iPhone X`, silently
    # resolving one device type to the other's simulator.
    if any(char.isalpha() and not char.isascii() for char in lowered):
        slugged += hashlib.sha1(lowered.encode("utf-8")).hexdigest()[:4]
    return slugged


def structural_name(device_type=DEFAULT_DEVICE_TYPE):
    return "%s-%s" % (NAME_PREFIX, slug(device_type))


def _pretty(identifier, prefix):
    tail = str(identifier or "")
    if tail.startswith(prefix):
        tail = tail[len(prefix):]
    return tail.replace("-", " ").strip()


def _runtime(key):
    tail = _pretty(key, RUNTIME_PREFIX)
    family, _, version = tail.partition(" ")
    return ("%s %s" % (family, version.replace(" ", "."))).strip()


def describe(udid, listing):
    if not udid:
        return None
    for key, devices in (listing or {}).get("devices", {}).items():
        for entry in devices:
            if entry.get("udid") != udid:
                continue
            return {
                "udid": udid,
                "name": entry.get("name") or "",
                "deviceType": _pretty(entry.get("deviceTypeIdentifier"), DEVICE_TYPE_PREFIX),
                "runtime": _runtime(key),
                "state": entry.get("state") or "",
            }
    return {"udid": udid, "name": "", "deviceType": "", "runtime": "", "state": ""}


def _fields(info):
    return [field for field in (info.get("name"), info.get("deviceType"), info.get("runtime"),
                                info.get("state"), info.get("udid")) if field]


def format_line(info):
    if not info:
        return "no simulator resolved"
    return " · ".join(_fields(info))


def format_compact(info):
    """For places already using `·` between items — the model detail nests in parentheses."""
    if not info:
        return "no simulator resolved"
    fields = _fields(info)
    if len(fields) == 1:
        return fields[0]
    return "%s (%s)" % (fields[0], ", ".join(fields[1:]))
