import glob
import json
import os
import plistlib
import signal
import subprocess
import time

from uiflow import lease, paths

HEARTBEAT_STALE_SECONDS = 20.0
HEARTBEAT_RETRIES = 3
HEARTBEAT_RETRY_DELAY = 0.15
READY_TIMEOUT_SECONDS = float(os.environ.get("UIFLOW_READY_TIMEOUT", "180"))
IDLE_SECONDS = int(os.environ.get("UIFLOW_IDLE_SECONDS", "1800"))
MAX_SECONDS = int(os.environ.get("UIFLOW_MAX_SECONDS", "7200"))

METADATA_PLIST = ".com.apple.mobile_container_manager.metadata.plist"


class SessionError(Exception):
    pass


def _list_devices():
    return paths.list_devices()


def assert_simulator(udid):
    if not udid:
        raise SessionError("no simulator resolved: name a simulator with UIFLOW_SIM_NAME or pass --udid.")
    for devices in (_list_devices() or {}).get("devices", {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return udid
    raise SessionError(
        "refusing %s: uiflow runs on a simulator only.\n"
        "That UDID is not in `simctl list devices`, so it is a physical device or does not exist. "
        "Four physical devices are attached to this machine and none of them may be driven from here." % udid
    )


def _container_identifier(container):
    try:
        with open(os.path.join(container, METADATA_PLIST), "rb") as handle:
            return plistlib.load(handle).get("MCMMetadataIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def find_channel(udid=None, newer_than=0.0, root=None):
    root = root or paths.containers_root(udid)
    best = None
    best_at = None
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        if _container_identifier(entry.path) != paths.DRIVER_RUNNER_ID:
            continue
        ready = os.path.join(entry.path, "tmp", "ready")
        try:
            stamp = os.stat(ready).st_mtime
        except OSError:
            continue
        if newer_than and stamp <= newer_than:
            continue
        if best_at is None or stamp > best_at:
            best, best_at = os.path.join(entry.path, "tmp"), stamp
    return best


def channel_is_live(channel, now=None, sleep=time.sleep, stale_after=None):
    if not channel:
        return False
    beat = os.path.join(channel, "heartbeat")
    for attempt in range(HEARTBEAT_RETRIES):
        try:
            stamp = os.stat(beat).st_mtime
        except OSError:
            # A momentarily-absent heartbeat is a write window, not a dead session (C10).
            sleep(HEARTBEAT_RETRY_DELAY)
            continue
        limit = HEARTBEAT_STALE_SECONDS if stale_after is None else stale_after
        return (now or time.time()) - stamp <= limit
    return False


FEATURES_PREFIX = "features="
PROBE_TIMEOUT_SECONDS = 10.0


def probe_features(channel_dir):
    """What the LIVE driver understands, from its own `ping` reply.

    The build is cached outside the checkout, so the driver answering may predate any op this
    checkout knows about. A driver that answered but advertised nothing supports nothing; a driver
    that could not be asked at all returns None, which is not an answer worth remembering — the
    caller degrades either way, but only the first is cached.
    """
    from uiflow import channel as channel_client

    try:
        reply = channel_client.send_to(channel_dir, [{"op": "ping"}], timeout=PROBE_TIMEOUT_SECONDS)
    except Exception:
        return None
    if not (reply or {}).get("ok"):
        return None
    info = ((reply.get("results") or [{}])[0] or {}).get("info") or ""
    for part in info.split():
        if part.startswith(FEATURES_PREFIX):
            return [name for name in part[len(FEATURES_PREFIX):].split(",") if name]
    return []


def driver_supports(udid, feature):
    state = read_state(udid) or {}
    return feature in (state.get("features") or [])


def state_path(udid):
    return os.path.join(paths.STATE_DIR, "session-%s.json" % udid)


def write_state(udid, payload):
    os.makedirs(paths.STATE_DIR, exist_ok=True)
    path = state_path(udid)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def read_state(udid):
    try:
        with open(state_path(udid)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def clear_state(udid):
    try:
        os.remove(state_path(udid))
    except OSError:
        pass


def xctestrun_path():
    found = sorted(
        glob.glob(os.path.join(paths.BUILD_DIR, "Build", "Products", "*.xctestrun")),
        key=os.path.getmtime,
    )
    if not found:
        raise SessionError(
            "no .xctestrun under %s — build the driver once with driver/build.sh." % paths.BUILD_DIR
        )
    return found[-1]


def _launch_driver(udid, xctestrun, log_path):
    env = dict(os.environ)
    env["TEST_RUNNER_UIFLOW_IDLE_SECONDS"] = str(IDLE_SECONDS)
    env["TEST_RUNNER_UIFLOW_MAX_SECONDS"] = str(MAX_SECONDS)
    command = [
        "xcodebuild",
        "test-without-building",
        "-xctestrun",
        xctestrun,
        "-destination",
        "platform=iOS Simulator,id=%s" % udid,
    ]
    log = open(log_path, "ab", buffering=0)
    process = subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    return process.pid


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _clear_stale_runner(udid):
    """Drop a runner left over from a previous session before starting a new one.

    XCTest's tear-down runs for the better part of a minute after the driver is told to quit, so a
    `down` immediately followed by an `up` would otherwise launch a second runner against a
    simulator the dying one still owns — which hangs, reproducibly.
    """
    for pid in _driver_xcodebuild_pids(udid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    subprocess.run(
        ["xcrun", "simctl", "terminate", udid, paths.DRIVER_RUNNER_ID],
        capture_output=True, check=False,
    )


def _driver_xcodebuild_pids(udid=None):
    """Driver `xcodebuild` processes, restricted to one simulator when a udid is given.

    Every ui-flow driver on the machine carries "UIFlowDriver" and "xctestrun" in its command line,
    whatever simulator or checkout it belongs to. Reaping on those two substrings alone SIGTERMs
    every other session's driver, on every simulator — the destination is what makes a runner ours.
    """
    found = subprocess.run(["pgrep", "-x", "xcodebuild"], capture_output=True, text=True, check=False)
    pids = []
    for line in found.stdout.split():
        described = subprocess.run(
            ["ps", "-o", "command=", "-p", line], capture_output=True, text=True, check=False
        ).stdout
        if "UIFlowDriver" not in described or "xctestrun" not in described:
            continue
        if udid and udid not in described:
            continue
        pids.append(int(line))
    return pids


def up(udid=None, timeout=READY_TIMEOUT_SECONDS, steal=False):
    from uiflow import simctl

    udid = assert_simulator(udid or paths.default_udid())
    cached = read_state(udid) or {}
    channel = cached.get("channel")
    # Sharing a live driver is the warm session working as intended, so reuse never needs the
    # lease. Only the paths below here destroy somebody else's work.
    if channel and channel_is_live(channel):
        lease.renew(udid)
        state = dict(cached, udid=udid, channel=channel)
        if state.get("features") is None:
            found = probe_features(channel)
            if found is not None:
                state["features"] = found
                write_state(udid, state)
        return dict(state, reused=True)
    channel = find_channel(udid)
    if channel and channel_is_live(channel):
        lease.renew(udid)
        state = {"udid": udid, "channel": channel, "pid": cached.get("pid"),
                 "uiLanguage": cached.get("uiLanguage"), "features": probe_features(channel)}
        write_state(udid, state)
        return dict(state, reused=True)

    lease.acquire(udid, steal=steal)
    try:
        # Only clear a runner that is genuinely abandoned. A driver busy inside a long batch can
        # look stale for a moment, and killing it there would destroy the session we are about to
        # use.
        if not channel or not channel_is_live(channel, stale_after=60.0):
            _clear_stale_runner(udid)
        xctestrun = xctestrun_path()
        log_path = os.path.join(paths.state_dir(), "driver-%s.log" % udid)
        launched_at = time.time()
        pid = _launch_driver(udid, xctestrun, log_path)
        deadline = launched_at + timeout
        while time.time() < deadline:
            channel = find_channel(udid, newer_than=launched_at)
            if channel and channel_is_live(channel):
                state = {"udid": udid, "channel": channel, "pid": pid, "startedAt": launched_at,
                         "uiLanguage": simctl._language_from_plist(udid)
                                       or simctl._language_from_defaults(udid),
                         "features": probe_features(channel)}
                write_state(udid, state)
                lease.renew(udid)
                return dict(state, reused=False)
            if not _pid_alive(pid):
                raise SessionError("the driver exited before publishing a channel — see %s" % log_path)
            time.sleep(0.2)
        raise SessionError(
            "the driver never published a ready channel within %ss — see %s" % (timeout, log_path)
        )
    except BaseException:
        # An unbuilt driver and a driver that never came up are the two commonest failures here.
        # Keeping the lease after either locks every other session out of a simulator that has
        # nothing running on it.
        lease.release(udid)
        raise


def down(udid=None, wait=6.0, steal=False):
    udid = udid or paths.default_udid()
    if not udid:
        return {"udid": None, "stopped": False, "detail": "no simulator resolved"}
    state = read_state(udid) or {}
    channel = state.get("channel") or find_channel(udid)
    # `up` may hand us a driver another session started, so stopping is as destructive as clearing
    # a stale runner and needs the same permission. Liveness — not lease freshness — is what makes
    # this unsafe: the driver idles for 1800s but a lease goes stale at 600s, so a healthy driver
    # spends 20 minutes looking abandoned.
    foreign = lease.read(udid)
    mine = foreign and foreign.get("owner") == lease.owner()
    if not mine and not steal:
        # A live driver we did not start and do not hold a lease on is still somebody's: `up`
        # releases its lease when the ready poll times out, yet the detached xcodebuild can publish
        # its channel moments later, leaving a healthy driver with no claim on it.
        started_here = bool(state.get("pid")) or bool(state.get("channel"))
        if foreign and (lease.held_by_other(udid) or (channel and channel_is_live(channel))):
            raise lease.LeaseHeld(
                "simulator %s is in use by %s (last active %ds ago).\n"
                "Taking it down would kill that session's driver. Pass --steal to do it anyway."
                % (udid, foreign.get("owner"),
                   int(time.time() - float(foreign.get("renewedAt") or 0)))
            )
        if not foreign and channel and channel_is_live(channel) and not started_here:
            raise lease.LeaseHeld(
                "a live driver is on %s and this session neither started it nor holds its lease.\n"
                "Pass --steal to take it down anyway." % udid
            )
    if steal and foreign and not mine:
        # Otherwise the lease we stole past outlives the driver and the next `up` needs --steal too.
        lease.acquire(udid, steal=True)
    pid = state.get("pid")
    quit_sent = False
    if channel and channel_is_live(channel):
        try:
            from uiflow import channel as channel_client

            channel_client.send_to(channel, [{"op": "quit"}], timeout=3.0)
            quit_sent = True
        except Exception:
            quit_sent = False
    deadline = time.time() + wait
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    killed = False
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed = True
        except OSError:
            pass
        deadline = time.time() + wait
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)
    alive = _pid_alive(pid)
    # `channel_is_live` reads heartbeat AGE, so a just-stopped session still looks live for the
    # whole staleness window and the next `up` would "reuse" a dying runner. Retire the files.
    if not alive and channel:
        for name in ("heartbeat", "ready"):
            try:
                os.remove(os.path.join(channel, name))
            except OSError:
                pass
    clear_state(udid)
    lease.release(udid)
    if alive:
        raise SessionError("driver pid %s is still running after SIGTERM" % pid)
    return {"udid": udid, "stopped": True, "quitSent": quit_sent, "signalled": killed, "pid": pid}


def status(udid=None):
    udid = udid or paths.default_udid()
    if not udid:
        return {"udid": None, "live": False, "detail": "no simulator resolved"}
    state = read_state(udid) or {}
    channel = state.get("channel") or find_channel(udid)
    return {
        "udid": udid,
        "channel": channel,
        "pid": state.get("pid"),
        "pidAlive": _pid_alive(state.get("pid")),
        "live": bool(channel and channel_is_live(channel)),
    }
