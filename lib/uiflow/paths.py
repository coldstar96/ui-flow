import json
import os
import subprocess

from uiflow import device, lease

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME_DIR = os.environ.get("UIFLOW_HOME") or os.path.expanduser("~/.ui-flow")
DRIVER_DIR = os.path.join(SKILL_ROOT, "driver")
RECORDS_DIR = os.environ.get("UIFLOW_RECORDS_DIR") or os.path.join(HOME_DIR, "records")
STATE_DIR = os.environ.get("UIFLOW_STATE_DIR") or os.path.join(HOME_DIR, "state")
BUILD_DIR = os.environ.get("UIFLOW_BUILD_DIR") or os.path.join(HOME_DIR, "cache", "driver-build")
STRINGS_DIR = os.environ.get("UIFLOW_STRINGS_DIR") or os.path.join(HOME_DIR, "cache", "strings")

DRIVER_RUNNER_ID = "dev.uiflow.driver.tests.xctrunner"


def state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)
    return STATE_DIR


def sim_names():
    named = os.environ.get("UIFLOW_SIM_NAME")
    return (named,) if named else (device.structural_name(),)


def device_root(udid):
    return os.path.expanduser("~/Library/Developer/CoreSimulator/Devices/%s/data" % udid)


def containers_root(udid):
    return os.path.join(device_root(udid), "Containers", "Data", "Application")


def list_devices():
    out = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        return {"devices": {}}
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {"devices": {}}


def udid_from_listing(listing, name):
    booted = None
    other = None
    for devices in (listing or {}).get("devices", {}).values():
        for entry in devices:
            if entry.get("name") != name:
                continue
            if entry.get("isAvailable") is False:
                continue
            if str(entry.get("state", "")).lower() == "booted":
                booted = booted or entry.get("udid")
            else:
                other = other or entry.get("udid")
    return booted or other


def udid_for_name(name, listing=None):
    return udid_from_listing(listing if listing is not None else list_devices(), name)


def describe(udid, listing=None):
    if not udid:
        return None
    return device.describe(udid, list_devices() if listing is None else listing)


def sim_line(udid, sim=None):
    """`name · model · iOS · state · udid · held by <owner>` — the one description of a run."""
    info = describe(udid) if sim is None else sim
    return " · ".join(filter(None, [
        device.format_line(info), lease.describe(udid) if udid else "",
    ]))


def default_udid(listing=None):
    udid = os.environ.get("UIFLOW_UDID")
    if udid:
        return udid
    listing = list_devices() if listing is None else listing
    for name in sim_names():
        found = udid_for_name(name, listing)
        if found:
            return found
    return None
