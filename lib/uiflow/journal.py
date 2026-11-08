import datetime
import json
import os

from uiflow import channel as channel_client, paths

JOURNAL_NAME = "journal.ndjson"
MARK_NAME = "journal-mark.json"
SCAFFOLDING = ("ping", "quit", "snapshot", "screenshot", "sleep", "screen")
META_KEYS = ("ts", "seq", "ms", "ok", "index", "info", "skipped", "children", "elapsedMs")
SECURE_TYPES = ("secureTextField",)


def journal_path(channel):
    return os.path.join(channel, JOURNAL_NAME)


def mark_path():
    return os.path.join(paths.state_dir(), MARK_NAME)


def read(channel, offset=0):
    path = journal_path(channel)
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    if offset > size:
        offset = 0
    entries = []
    consumed = offset
    with open(path, "rb") as handle:
        handle.seek(offset)
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            consumed += len(raw)
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
    return entries, consumed


def write_mark(channel, offset):
    payload = {"channel": channel, "offset": offset}
    channel_client.write_atomic(mark_path(), json.dumps(payload))
    return payload


def read_mark():
    try:
        with open(mark_path()) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def resolve_channel(channel=None, udid=None):
    return channel or channel_client.resolve_channel(udid)


def mark(channel=None, udid=None):
    channel = resolve_channel(channel, udid)
    try:
        offset = os.path.getsize(journal_path(channel))
    except OSError:
        offset = 0
    return write_mark(channel, offset)


def mark_is_stale(channel=None, udid=None):
    channel = resolve_channel(channel, udid)
    stored = read_mark()
    return not stored or stored.get("channel") != channel


def since_mark(channel=None, udid=None):
    channel = resolve_channel(channel, udid)
    stored = read_mark() or {}
    # The journal lives in a per-session container recreated on every restart, so a mark from
    # another channel is meaningless: start from the beginning rather than return nothing (C16).
    offset = stored.get("offset", 0) if stored.get("channel") == channel else 0
    entries, _ = read(channel, offset)
    return entries


def redact(step):
    selector = step.get("sel") or {}
    if step.get("op") == "type" and selector.get("type") in SECURE_TYPES and "text" in step:
        redacted = dict(step)
        redacted["textLen"] = len(redacted.pop("text") or "")
        return redacted
    return step


def to_steps(entries):
    steps = []
    for entry in entries or []:
        if entry.get("op") in SCAFFOLDING or entry.get("ok") is False:
            continue
        # The driver journals the executed Step nested under "step", alongside its own meta keys.
        # Taking the envelope instead would produce a record whose steps each wrap another step.
        step = entry.get("step")
        if not isinstance(step, dict):
            step = {key: value for key, value in entry.items() if key not in META_KEYS}
        step = {key: value for key, value in step.items() if key not in META_KEYS and value is not None}
        if not step.get("op"):
            continue
        steps.append(redact(step))
    return steps


def literal_text_steps(steps):
    return [index for index, step in enumerate(steps or []) if isinstance(step.get("text"), str)]


def to_record(
    app,
    name,
    steps,
    description="",
    aliases=(),
    requires=(),
    satisfied_when=(),
    verifies=(),
    verifies_why="",
    key_map=None,
    provenance=None,
    recorded_on=None,
    history=(),
    kind=None,
):
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    payload = {
        "app": app,
        "name": name,
        "description": description,
        "aliases": list(aliases),
        "requires": list(requires),
        "satisfiedWhen": [redact(step) for step in satisfied_when or []],
        "steps": [redact(step) for step in steps or []],
        "verifies": [redact(step) for step in verifies or []],
        "verifiesWhy": verifies_why,
        "keyMap": dict(key_map or {}),
        "provenance": dict(provenance or {"verified": stamp}),
        "history": list(history) or ["%s learned from a journalled session" % stamp],
        "timing": {},
        "health": {"runs": 0, "failures": 0, "lastOk": None, "repairedAt": None},
    }
    kept = {key: (recorded_on or {}).get(key) for key in ("device", "w", "h", "fingerprint")}
    kept = {key: value for key, value in kept.items() if value is not None}
    if kept:
        payload["recordedOn"] = kept
    if kind and kind != "flow":
        payload["kind"] = kind
    return payload
