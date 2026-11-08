"""Which session owns a simulator.

ui-flow keeps one long-lived driver per simulator and several sessions may reach for the same
device. Reusing a *live* driver is the point of the warm session; killing one that another session
is mid-flight on is not. The lease is the bit of shared state that tells those two apart.

It lives outside every checkout because `state/` is derived from the checkout root, so two
worktrees driving one simulator would otherwise keep entirely separate books.
"""

import contextlib
import errno
import fcntl
import json
import os
import re
import time

from uiflow import registry

# Longer than any single batch: `channel.DEFAULT_TIMEOUT` is 180s, and a lease that expires while
# its own batch is still running is an invitation for another session to reap a healthy driver.
STALE_SECONDS = 600.0

CHECKOUT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ordered most to least specific. CLAUDE_CODE_BRIDGE_SESSION_ID identifies the CONVERSATION;
# CLAUDE_CODE_SESSION_ID is minted per subagent, so preferring it would make an agent and the
# subagents it fans work out to rivals that lock each other out of their own simulator.
SESSION_VARS = ("UIFLOW_OWNER", "MAESTRO_SESSION_ID", "CLAUDE_CODE_BRIDGE_SESSION_ID",
                "CLAUDE_CODE_SESSION_ID", "TERM_SESSION_ID")


class LeaseHeld(Exception):
    pass


def lock_dir(create=False):
    # Beside the registry, NOT in $TMPDIR: two sessions with different TMPDIRs (sudo, cron, another
    # launchd bootstrap) would keep separate books and the lease would silently protect nothing.
    directory = os.environ.get("UIFLOW_LOCK_DIR") or os.path.join(registry.home(), "locks")
    if create:
        os.makedirs(directory, exist_ok=True)
    return directory


def owner():
    """Who this process belongs to — stable across the many short CLI runs of one session.

    Never the pid: every `uiflow` command is a new process, so a pid-based owner would make a
    session fail to recognise its own lease and deadlock `down` against its own `up`. Never the
    checkout either: the skill ships as ONE shared directory, so every session would share an
    identity and the lease would protect nothing. The terminal, then the parent shell, are the
    coarsest things that still differ between two concurrent sessions.
    """
    for name in SESSION_VARS:
        named = os.environ.get(name)
        if named:
            return named
    try:
        return os.ttyname(0)
    except (OSError, ValueError):
        # Coarse, but STABLE. Nothing in the process tree survives: every `uiflow` command is a new
        # process and its pid, ppid, pgrp and session id all differ from the last one's — measured.
        # A checkout-wide identity only fails to tell apart two sessions sharing a checkout; a
        # process-derived one fails to recognise a session's OWN lease, which breaks down-then-up
        # outright. Set UIFLOW_OWNER when several sessions share one checkout.
        return CHECKOUT_ROOT


def stale_seconds():
    try:
        return float(os.environ.get("UIFLOW_LEASE_STALE") or STALE_SECONDS)
    except ValueError:
        return STALE_SECONDS


UDID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def validate_udid(udid):
    """The udid becomes a filename, so `--udid ../../x` would write outside the lock directory."""
    if not udid or not UDID_RE.match(str(udid)):
        raise LeaseHeld("bad simulator udid %r: expected letters, digits and dashes" % (udid,))
    return udid


def path(udid, create=False):
    validate_udid(udid)
    return os.path.join(lock_dir(create), "%s.json" % udid)


def lock_path(udid):
    validate_udid(udid)
    return os.path.join(lock_dir(create=True), "%s.lock" % udid)


@contextlib.contextmanager
def _guard(udid):
    """Read-decide-write is one step: without this two sessions both see a free lease and take it."""
    handle = os.open(lock_path(udid), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def read(udid):
    try:
        with open(path(udid)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write(udid, payload):
    target = path(udid, create=True)
    tmp = "%s.%s.tmp" % (target, os.getpid())
    with open(tmp, "w") as handle:
        json.dump(payload, handle)
    os.replace(tmp, target)
    return payload


def _fresh(held, now=None):
    if not held:
        return False
    return (now or time.time()) - float(held.get("renewedAt") or 0) <= stale_seconds()


def held_by_other(udid, now=None):
    """The live lease on this simulator when somebody else holds it, else None."""
    held = read(udid)
    if not held or held.get("owner") == owner():
        return None
    return held if _fresh(held, now) else None


def acquire(udid, steal=False):
    with _guard(udid):
        previous = read(udid)
        held = previous if previous and previous.get("owner") != owner() and _fresh(previous) else None
        if held and not steal:
            raise LeaseHeld(
                "simulator %s is in use by %s (last active %ds ago).\n"
                "Let that session finish, or take a device of your own:\n"
                "  uiflow sim udid --device-type \"iPhone Air\" --create\n"
                "  export UIFLOW_SIM_NAME=uiflow-iphoneair\n"
                "Pass --steal only to take this one over."
                % (udid, held.get("owner"), int(time.time() - float(held.get("renewedAt") or 0)))
            )
        now = time.time()
        mine = previous and previous.get("owner") == owner()
        return write(udid, {"udid": udid, "owner": owner(), "pid": os.getpid(),
                            "acquiredAt": previous.get("acquiredAt", now) if mine else now,
                            "renewedAt": now})


def renew(udid):
    """Mark this session as actively using the simulator.

    Claims an unheld device — active use is exactly the claim worth recording, and it is what keeps
    a device a session is working on out of `sweep`. Declines silently when somebody else holds it,
    so sharing a warm driver never steals the claim.
    """
    with _guard(udid):
        held = read(udid)
        if held and held.get("owner") != owner():
            return held
        return write(udid, dict(held or {"udid": udid, "owner": owner(), "pid": os.getpid(),
                                         "acquiredAt": time.time()}, renewedAt=time.time()))


def release(udid):
    with _guard(udid):
        held = read(udid)
        if not held or held.get("owner") != owner():
            return
        try:
            os.remove(path(udid))
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise


def describe(udid, now=None):
    held = read(udid)
    if not held or not _fresh(held, now):
        return ""
    if held.get("owner") == owner():
        return "mine"
    idle = int((now or time.time()) - float(held.get("renewedAt") or 0))
    return "held by %s %ds ago" % (held.get("owner"), idle)
