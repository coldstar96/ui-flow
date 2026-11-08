import datetime
import json
import os
import plistlib
import re
import subprocess
import time

from uiflow import device, lease, paths, registry, session
from uiflow.paths import udid_from_listing

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_DEVICE_TYPE = device.DEFAULT_DEVICE_TYPE
BOOT_TIMEOUT = 180
# A lease goes stale between runs, so age is what separates an orphan from a paused session.
IDLE_HOURS = 24.0
BASE_KEYBOARDS = ["en_US@sw=QWERTY;hw=Automatic"]
EMOJI_KEYBOARD = "emoji@sw=Emoji"


class SimctlError(Exception):
    pass


def _run(args, check=True, capture=False):
    completed = subprocess.run(["xcrun"] + list(args), capture_output=True, text=True)
    if check and completed.returncode != 0:
        raise SimctlError("xcrun %s failed: %s" % (" ".join(args), (completed.stderr or "").strip()))
    return completed.stdout


def validate_name(name):
    if not name or not NAME_RE.match(name):
        raise SimctlError("bad simulator name %r: names must match %s" % (name, NAME_RE.pattern))
    return name


def list_devices():
    try:
        return json.loads(_run(["simctl", "list", "devices", "-j"], capture=True) or "{}")
    except ValueError:
        return {"devices": {}}


def default_name(device_type=None):
    """The structural name for a device type, unless UIFLOW_SIM_NAME pins one explicitly.

    A pinned name and an explicit device type that DISAGREE are refused rather than silently
    resolved either way: letting the flag win points `prepare` — which reinstalls and reboots — at a
    different simulator than the pinned one, and letting the variable win names an iPhone Air
    `uiflow-iphone17pro`. Agreeing is not a conflict: the documented escape hatch creates a device
    with --device-type and then exports its name, so the two match from that point on.
    """
    pinned = os.environ.get("UIFLOW_SIM_NAME")
    if device_type is None:
        return pinned if pinned else device.structural_name(DEFAULT_DEVICE_TYPE)
    try:
        wanted = device.structural_name(device_type)
    except ValueError as error:
        raise SimctlError(str(error))
    if pinned and pinned != wanted:
        raise SimctlError(
            "UIFLOW_SIM_NAME=%s and --device-type %r (%s) name different simulators. Unset the "
            "variable, or drop the flag and let the pinned name stand." % (pinned, device_type, wanted)
        )
    return wanted


def resolve(name=None, create=False, device_type=None, runtime=None):
    # The name tracks the device type, so asking for an iPhone Air must not resolve to (or create)
    # the default iPhone 17 Pro device sitting under `uiflow-iphone17pro`.
    name = validate_name(name or default_name(device_type))
    found = udid_from_listing(list_devices(), name)
    if found:
        return found
    if not create:
        raise SimctlError(
            "no simulator named %s. Pass --create to make one (a typo would otherwise silently "
            "create a second device beside the one you meant). Carrying an older ui-flow device "
            "under a different name? Rename it instead of creating a second one:\n"
            "  xcrun simctl rename <old-name> %s" % (name, name)
        )
    runtime = runtime or newest_ios_runtime()
    wanted = DEFAULT_DEVICE_TYPE if device_type is None else device_type
    created = _run(["simctl", "create", name, wanted, runtime], capture=True).strip()
    # Recorded the moment it exists, so a sweep can find it even if this run dies here.
    registry.record(created, name=name, device_type=wanted)
    return created


def newest_ios_runtime():
    listing = json.loads(_run(["simctl", "list", "runtimes", "-j"], capture=True) or "{}")
    runtimes = [r for r in listing.get("runtimes", []) if r.get("isAvailable") and "iOS" in r.get("name", "")]
    if not runtimes:
        raise SimctlError("no available iOS runtime")

    def key(runtime):
        return [int(part) for part in runtime.get("version", "0").split(".") if part.isdigit()]

    return sorted(runtimes, key=key)[-1]["identifier"]


def is_booted(udid):
    listing = _run(["simctl", "list", "devices"], check=False, capture=True) or ""
    for line in listing.splitlines():
        if udid in line and "Booted" in line:
            return True
    return False


def _system_domain_answers(udid):
    completed = subprocess.run(
        ["xcrun", "simctl", "spawn", udid, "launchctl", "print", "system"], capture_output=True, text=True
    )
    return completed.returncode == 0


def boot(udid):
    _run(["simctl", "boot", udid], check=False)


def shutdown(udid):
    _run(["simctl", "shutdown", udid], check=False)


def wait_booted(udid, timeout=BOOT_TIMEOUT, poll=2.0, sleep=time.sleep):
    # Never `simctl bootstatus -b`: it has hung 5+ minutes on an already-up device. `Booted` also
    # flips well before the system is usable, so it is followed by a bounded readiness probe.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_booted(udid):
            while time.time() < deadline:
                if _system_domain_answers(udid):
                    return True
                sleep(poll)
            raise SimctlError("device %s reached Booted but the system domain never answered" % udid)
        sleep(poll)
    raise SimctlError("device %s never reached Booted within %ss" % (udid, timeout))


def install(udid, app_path):
    app_path = os.path.abspath(os.path.expanduser(app_path))
    if not app_path.endswith(".app") or not os.path.isdir(app_path):
        raise SimctlError("app_path must be an absolute path to an existing .app: %s" % app_path)
    boot(udid)
    wait_booted(udid)
    _run(["simctl", "install", udid, app_path])
    return app_path


def _preferences_dir(udid):
    directory = os.path.join(paths.device_root(udid), "Library", "Preferences")
    os.makedirs(directory, exist_ok=True)
    return directory


def _load_plist(path):
    try:
        with open(path, "rb") as handle:
            return plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def _dump_plist(path, payload):
    with open(path, "wb") as handle:
        plistlib.dump(payload, handle, fmt=plistlib.FMT_BINARY)


def write_prefs(udid, keyboard_ids=(), locale=None):
    directory = _preferences_dir(udid)
    global_path = os.path.join(directory, ".GlobalPreferences.plist")
    springboard_path = os.path.join(directory, "com.apple.springboard.plist")
    globals_plist = _load_plist(global_path)
    if keyboard_ids:
        globals_plist["AppleKeyboards"] = BASE_KEYBOARDS + list(keyboard_ids) + [EMOJI_KEYBOARD]
    if locale:
        globals_plist["AppleLanguages"] = [locale]
        globals_plist["AppleLocale"] = locale
    _dump_plist(global_path, globals_plist)
    springboard = _load_plist(springboard_path)
    # An erase wipes SBAutoLockTime; the display then sleeps, taps still report success and
    # screenshots come back black.
    springboard["SBAutoLockTime"] = 0.0
    _dump_plist(springboard_path, springboard)
    keyboard_path = os.path.join(directory, "com.apple.keyboard.preferences.plist")
    keyboard = _load_plist(keyboard_path)
    # A device that has ever seen a hardware keyboard docks the software one OFF SCREEN: its keys
    # stay in the accessibility tree but are never hittable and no globe is vended at all.
    keyboard["AutomaticMinimizationEnabled"] = False
    _dump_plist(keyboard_path, keyboard)


APPLE_LANGUAGES = re.compile(r'"([^"]+)"|([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*)')


def list_runtimes():
    try:
        return json.loads(_run(["simctl", "list", "runtimes", "-j"], check=False) or "{}")
    except (ValueError, SimctlError):
        return {"runtimes": []}


def runtime_for(udid, listing=None, runtimes=None):
    """The runtime a device runs: identifier, build version and on-disk RuntimeRoot."""
    if not udid:
        return None
    listing = list_devices() if listing is None else listing
    identifier = None
    for key, devices in (listing or {}).get("devices", {}).items():
        if any(entry.get("udid") == udid for entry in devices):
            identifier = key
            break
    if not identifier:
        return None
    runtimes = list_runtimes() if runtimes is None else runtimes
    for entry in (runtimes or {}).get("runtimes", []):
        if entry.get("identifier") == identifier and entry.get("runtimeRoot"):
            return {"identifier": identifier, "build": entry.get("buildversion") or "",
                    "root": entry["runtimeRoot"]}
    return None


def _language_from_plist(udid):
    path = os.path.join(paths.device_root(udid), "Library", "Preferences", ".GlobalPreferences.plist")
    languages = _load_plist(path).get("AppleLanguages")
    if isinstance(languages, list) and languages and isinstance(languages[0], str):
        return languages[0]
    return None


def _language_from_defaults(udid):
    out = _run(["simctl", "spawn", udid, "defaults", "read", "-g", "AppleLanguages"], check=False)
    for quoted, bare in APPLE_LANGUAGES.findall(out or ""):
        return quoted or bare
    return None


def ui_language(udid):
    """The simulator's UI language, cheapest source first — session state, plist, then `defaults`.

    `defaults read` costs ~250 ms and needs a booted device, but it is the only source for a device
    whose `.GlobalPreferences.plist` was never written.
    """
    if not udid:
        return None
    state = session.read_state(udid) or {}
    if state.get("uiLanguage"):
        return state["uiLanguage"]
    found = _language_from_plist(udid) or _language_from_defaults(udid)
    # Written back so the ~221 ms `defaults read` is paid once per session, not once per run. Read
    # again first: probing took long enough for an `up` in another session to have replaced it.
    fresh = session.read_state(udid) if found else None
    if fresh:
        session.write_state(udid, dict(fresh, uiLanguage=found))
    return found


def app_bundle_path(udid, bundle_id):
    if not udid or not bundle_id:
        return None
    out = _run(["simctl", "get_app_container", udid, bundle_id, "app"], check=False)
    path = (out or "").strip()
    return path if path and os.path.isdir(path) else None


def prepare(udid, app_path=None, keyboard_ids=(), locale=None):
    # iOS resolves AppleKeyboards at BOOT, so the order is install -> shutdown -> prefs -> boot.
    if app_path:
        install(udid, app_path)
    shutdown(udid)
    write_prefs(udid, keyboard_ids=keyboard_ids, locale=locale)
    boot(udid)
    wait_booted(udid)
    return udid


def _entries(listing=None):
    for devices in (listing or list_devices()).get("devices", {}).values():
        for entry in devices:
            yield entry


def _idb_down(udid):
    """The companion and sidecar outlive the driver, so a teardown that skips them leaves two
    processes attached to a device that is about to stop existing."""
    try:
        from uiflow import idb

        idb.down(udid)
    except Exception:
        pass


def _busy(udid):
    """Why this device must not be touched, or None."""
    held = lease.held_by_other(udid)
    if held:
        return "held by %s" % held.get("owner")
    channel = session.find_channel(udid)
    if channel and session.channel_is_live(channel):
        return "a driver is live on it"
    return None


def _idle_hours(entry, created):
    """Hours since the device was last used, by its own boot stamp or when uiflow made it."""
    stamps = []
    booted = entry.get("lastBootedAt")
    if booted:
        try:
            stamps.append(datetime.datetime.strptime(booted, "%Y-%m-%dT%H:%M:%SZ")
                          .replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            pass
    if created:
        stamps.append(created)
    if not stamps:
        # Never booted and unknown to the registry — but the device directory itself was created
        # when the device was. Without this a device made seconds ago reads as infinitely idle and
        # any --older-than sweeps it away.
        try:
            stamps.append(os.path.getmtime(os.path.dirname(paths.device_root(entry.get("udid")))))
        except OSError:
            return float("inf")
    return (time.time() - max(stamps)) / 3600.0


def working_udids(listing):
    """The devices ui-flow hands you. `reapable` and `delete` must agree on this exactly, or sweep
    plans a device delete then refuses, and the run can never come back clean."""
    return {paths.default_udid(listing), udid_from_listing(listing, default_name())}


def reapable(listing=None, older_than_hours=IDLE_HOURS):
    """ui-flow's own spare devices: not the working one, not busy, not somebody else's, not recent.

    The age guard matters because a lease goes stale between runs — without it a sweep would reap
    the device another session is merely paused on.
    """
    listing = listing or list_devices()
    working = working_udids(listing)
    recorded = registry.read()
    found = []
    for entry in _entries(listing):
        udid, name = entry.get("udid"), entry.get("name")
        if not udid or udid in working:
            continue
        if not registry.is_ours(udid, name):
            continue
        if _busy(udid):
            continue
        # `lastBootedAt` is stamped at boot and never advances, so a device someone has had open in
        # Simulator.app for days reads as days idle. Being booted at all is the only signal that
        # somebody may still be looking at it.
        if str(entry.get("state", "")).lower() == "booted":
            continue
        idle = _idle_hours(entry, (recorded.get(udid) or {}).get("createdAt"))
        if idle < older_than_hours:
            continue
        found.append({"udid": udid, "name": name, "recorded": udid in recorded, "idleHours": idle})
    return found


def delete(name=None, udid=None, force=False, confirm=None, listing=None):
    listing = listing or list_devices()
    if not udid:
        udid = udid_from_listing(listing, validate_name(name or default_name()))
    entry = next((e for e in _entries(listing) if e.get("udid") == udid), None)
    if not entry:
        raise SimctlError("no simulator %s" % udid)
    label = entry.get("name")
    if udid in working_udids(listing) and not (force and label and confirm == label):
        raise SimctlError(
            "refusing to delete %s (%s): it is the device ui-flow resolves for you, not a spare. "
            "`sweep` never touches it. Pass --force and --confirm %s if you really mean it."
            % (label, udid, label)
        )
    if not registry.is_ours(udid, label):
        if not force:
            raise SimctlError(
                "refusing to delete %s (%s): uiflow did not create it and it is outside the `%s` "
                "namespace. Pass --force and repeat the name with --confirm to delete it anyway."
                % (label, udid, registry.NAME_PREFIX)
            )
        if not label or confirm != label:
            raise SimctlError("--force needs --confirm %s to delete %s" % (label, udid))
    busy = _busy(udid)
    if busy:
        raise SimctlError("refusing to delete %s (%s): %s" % (label, udid, busy))
    # Deleting out from under a live xcodebuild is data loss, so the session goes down first.
    session.down(udid=udid)
    _idb_down(udid)
    shutdown(udid)
    # Taking it down took seconds; another session may have claimed it in the meantime.
    busy = _busy(udid)
    if busy:
        raise SimctlError("refusing to delete %s (%s): %s" % (label, udid, busy))
    _run(["simctl", "delete", udid])
    registry.forget(udid)
    return {"udid": udid, "name": label}


def sweep(yes=False, listing=None, older_than_hours=IDLE_HOURS):
    """Every entry reports its own outcome: one device claimed since the plan was made must not
    abandon the rest, nor hide the ones already deleted."""
    listing = listing or list_devices()
    planned = reapable(listing, older_than_hours=older_than_hours)
    if not yes:
        return planned
    for entry in planned:
        try:
            delete(udid=entry["udid"], listing=listing)
            entry["deleted"] = True
        except (SimctlError, session.SessionError, lease.LeaseHeld) as error:
            entry["deleted"] = False
            entry["skipped"] = str(error).splitlines()[0]
    return planned


def erase(name=None, udid=None, force=False, confirm=None):
    name = validate_name(name or default_name())
    if name not in paths.sim_names():
        if not force:
            raise SimctlError(
                "refusing to erase %s: `uiflow sim erase` owns %s only. Pass --force and repeat the "
                "name with --confirm to erase another session's simulator." % (name, ", ".join(paths.sim_names()))
            )
        if confirm != name:
            raise SimctlError("--force needs --confirm %s to erase %s" % (name, name))
    udid = udid or resolve(name)
    # Erasing a device with a live attached xcodebuild is data loss, so the session goes down first
    # and stays down.
    session.down(udid=udid)
    _idb_down(udid)
    shutdown(udid)
    _run(["simctl", "erase", udid])
    return udid
