"""Which simulators ui-flow made, so a sweep can delete them and nothing is orphaned.

Two independent signals, because either alone fails:

- The registry records what `simctl create` actually made. Precise, but a lost file would hide a
  device from cleanup forever — which is the orphan it is meant to prevent. It therefore lives in
  the user's home, NOT in $TMPDIR (cleared on reboot) and NOT in a checkout's `state/` (one per
  worktree).
- The `uiflow-` name prefix is ui-flow's namespace. It survives anything, so it is the backstop
  when the registry is gone.
"""

import contextlib
import fcntl
import json
import os
import time

NAME_PREFIX = "uiflow-"


def home(create=False):
    directory = os.environ.get("UIFLOW_HOME") or os.path.expanduser(
        "~/Library/Application Support/uiflow"
    )
    if create:
        os.makedirs(directory, exist_ok=True)
    return directory


def path(create=False):
    return os.path.join(home(create), "created.json")


def read():
    try:
        with open(path()) as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write(entries):
    target = path(create=True)
    tmp = "%s.%s.tmp" % (target, os.getpid())
    with open(tmp, "w") as handle:
        json.dump(entries, handle, indent=1, sort_keys=True)
    os.replace(tmp, target)


@contextlib.contextmanager
def _guard():
    """Read-modify-write on one shared file: without this, two concurrent records lose an entry."""
    handle = os.open(os.path.join(home(create=True), "created.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def record(udid, name, device_type, created_at=None):
    with _guard():
        entries = read()
        entries[udid] = {"name": name, "deviceType": device_type,
                         "createdAt": created_at or time.time()}
        _write(entries)
        return entries[udid]


def forget(udid):
    with _guard():
        entries = read()
        if entries.pop(udid, None) is not None:
            _write(entries)


def is_recorded(udid):
    return udid in read()


def is_ours(udid, name):
    return is_recorded(udid) or str(name or "").startswith(NAME_PREFIX)
