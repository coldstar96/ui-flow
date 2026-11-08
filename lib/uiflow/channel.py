import contextlib
import errno
import fcntl
import json
import os
import time

from uiflow import lease, paths, session

DEFAULT_TIMEOUT = 180.0
POLL_SECONDS = 0.003
LOCK_POLL_SECONDS = 0.05
RENEW_SECONDS = 30.0


class ChannelError(Exception):
    pass


class ChannelTimeout(ChannelError):
    pass


def seq_path(directory):
    # Beside cmd.json, not in the checkout's state dir: every checkout keeps its own state, so
    # per-checkout counters let two sessions hand one driver the same seq — and then each can read
    # the other's reply as its own.
    return os.path.join(directory, "seq")


def lock_path(directory):
    return os.path.join(directory, "lock")


@contextlib.contextmanager
def held(directory, timeout=DEFAULT_TIMEOUT):
    """Serialise batches on one driver: a second session queues instead of clobbering cmd.json."""
    handle = os.open(lock_path(directory), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.time() >= deadline:
                    raise ChannelTimeout(
                        "another ui-flow session held %s for %ss" % (directory, timeout)
                    )
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def served_seq(directory):
    """The last seq this driver answered, from its own reply file."""
    reply = _read_reply(os.path.join(directory, "reply.json"))
    try:
        return int((reply or {}).get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def next_seq(directory):
    path = seq_path(directory)
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = os.read(handle, 64).decode("utf-8", "replace").strip()
        try:
            value = int(raw) + 1
        except ValueError:
            # The driver drops any seq it has already served (Channel.swift: `seq > lastSeq`), so a
            # counter starting fresh against a driver already mid-life is ignored into a timeout.
            value = served_seq(directory) + 1
        os.lseek(handle, 0, os.SEEK_SET)
        os.ftruncate(handle, 0)
        os.write(handle, str(value).encode())
        return value
    finally:
        os.close(handle)


def write_atomic(path, payload):
    tmp = "%s.%s.tmp" % (path, os.getpid())
    with open(tmp, "w") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_reply(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def send_to(directory, steps, seq=None, pace=None, default_app=None, timeout=DEFAULT_TIMEOUT,
            udid=None):
    if not directory or not os.path.isdir(directory):
        raise ChannelError("no channel directory: %s" % directory)
    reply_path = os.path.join(directory, "reply.json")
    # ONE deadline across waiting for the lock and waiting for the reply: a deadline per stage let a
    # 180s call block for 360s while its caller believed 180s was the ceiling.
    deadline = time.time() + timeout
    with held(directory, timeout=max(0.0, deadline - time.time())):
        seq = next_seq(directory) if seq is None else seq
        request = {"seq": seq, "steps": list(steps)}
        if pace:
            request["pace"] = pace
        if default_app:
            request["defaultApp"] = default_app
        write_atomic(os.path.join(directory, "cmd.json"), json.dumps(request, ensure_ascii=False))
        renew_at = time.time() + RENEW_SECONDS
        while time.time() < deadline:
            reply = _read_reply(reply_path)
            if reply is not None and reply.get("seq") == seq:
                return reply
            # A batch can run longer than the lease is fresh for; without this the session driving
            # the device looks abandoned to everybody else halfway through its own work.
            if udid and time.time() >= renew_at:
                lease.renew(udid)
                renew_at = time.time() + RENEW_SECONDS
            time.sleep(POLL_SECONDS)
    raise ChannelTimeout("no reply for seq %s within %ss on %s" % (seq, timeout, directory))


def resolve_channel(udid=None):
    udid = udid or paths.default_udid()
    if not udid:
        raise ChannelError("no simulator resolved: name one with UIFLOW_SIM_NAME or pass --udid.")
    state = session.read_state(udid) or {}
    cached = state.get("channel")
    if cached and session.channel_is_live(cached):
        return cached
    found = session.find_channel(udid)
    if found and session.channel_is_live(found):
        if found != cached:
            session.write_state(udid, dict(state, udid=udid, channel=found))
        return found
    return None


def send(steps, udid=None, pace=None, default_app=None, timeout=DEFAULT_TIMEOUT, resolve=None):
    # Renew the device we actually resolved, not the argument: `cmd`, `snap` and `shot` are
    # normally called without --udid, and would otherwise never refresh the lease they rely on.
    udid = udid or paths.default_udid()
    directory = (resolve or resolve_channel)(udid)
    if not directory:
        raise ChannelError("no live driver session — run `uiflow up` first.")
    if udid:
        lease.renew(udid)
    return send_to(directory, steps, pace=pace, default_app=default_app, timeout=timeout, udid=udid)


def _result_lines(result):
    lines = result.get("lines")
    if isinstance(lines, list):
        return lines
    info = result.get("info") or ""
    return [line for line in info.splitlines() if line.strip()]


def parse_lines(lines):
    elements = []
    for line in lines:
        try:
            elements.append(json.loads(line))
        except ValueError:
            continue
    return elements


def snapshot_from(directory, app=None, timeout=30.0):
    step = {"op": "snapshot"}
    if app:
        step["app"] = app
    reply = send_to(directory, [step], timeout=timeout)
    if not reply.get("ok"):
        raise ChannelError("snapshot failed: %s" % json.dumps(reply.get("error") or {}))
    results = reply.get("results") or [{}]
    return parse_lines(_result_lines(results[0]))


def snapshot(app=None, udid=None, timeout=30.0):
    udid = udid or paths.default_udid()
    directory = resolve_channel(udid)
    if not directory:
        raise ChannelError("no live driver session — run `uiflow up` first.")
    if udid:
        lease.renew(udid)
    return snapshot_from(directory, app=app, timeout=timeout)


def failure_snapshot(reply):
    error = (reply or {}).get("error") or {}
    return parse_lines(error.get("snapshot") or [])


def format_reply(reply, indent=0, results=None):
    lines = []
    for result in results if results is not None else (reply.get("results") or []):
        mark = "skip" if result.get("skipped") else ("ok" if result.get("ok") else "FAIL")
        label = result.get("name") or result.get("op")
        lines.append("%s%-4s %-14s %5sms%s" % (
            "  " * indent, mark, label, result.get("ms", "?"),
            "  " + result["info"].splitlines()[0] if result.get("info") and len(result.get("info", "")) < 120 else "",
        ))
        if result.get("children"):
            lines.extend(format_reply(reply, indent + 1, result["children"]))
    if indent == 0 and not reply.get("ok"):
        error = reply.get("error") or {}
        lines.append("error: %s — %s (path %s)" % (
            error.get("reason", "?"), error.get("detail", ""), ".".join(str(i) for i in error.get("failedPath") or []),
        ))
        snapshot = error.get("snapshot") or []
        if snapshot:
            lines.append("snapshot: %d elements in the reply (do not re-snap)" % len(snapshot))
    return lines
