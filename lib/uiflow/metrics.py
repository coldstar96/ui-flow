import json

from uiflow import channel, idb, paths

SOURCE_IDB = "idb"
SOURCE_DRIVER = "driver"

_CACHE = {}
_REASONS = {}


class MetricsUnavailable(Exception):
    pass


def fingerprint(width, height):
    """Smaller dimension first, so one device hashes the same whichever way round its source
    reports it: idb describes the hardware screen while the driver follows the interface
    orientation, and a rotation must not read as a different device."""
    low, high = sorted((int(round(float(width))), int(round(float(height)))))
    return "%dx%d" % (low, high)


def invalidate(udid=None):
    if udid is None:
        _CACHE.clear()
    else:
        _CACHE.pop(udid, None)


def _swallowed(source, error):
    _REASONS[source] = "%s: %s" % (type(error).__name__, error)


def probe_idb(udid):
    try:
        if not idb.alive(udid):
            return None
        reply = idb.send([{"op": "describe"}], udid, timeout=10.0)
        if not reply.get("ok"):
            return None
        result = (reply.get("results") or [{}])[0]
        return {"w": result.get("width"), "h": result.get("height"),
                "density": result.get("density")}
    except Exception as error:
        # A probe that cannot answer is not an error; its reason is kept for MetricsUnavailable.
        _swallowed(SOURCE_IDB, error)
        return None


def probe_driver(udid):
    try:
        if not channel.resolve_channel(udid):
            return None
        reply = channel.send([{"op": "screen"}], udid=udid, timeout=15.0)
        if not reply.get("ok"):
            return None
        payload = json.loads(((reply.get("results") or [{}])[0]).get("info") or "{}")
        return {"w": payload.get("w"), "h": payload.get("h"), "density": None}
    except Exception as error:
        # A probe that cannot answer is not an error; its reason is kept for MetricsUnavailable.
        _swallowed(SOURCE_DRIVER, error)
        return None


def _sized(found):
    if not found:
        return None
    try:
        width, height = float(found.get("w") or 0.0), float(found.get("h") or 0.0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    density = found.get("density")
    return {"w": width, "h": height, "density": float(density) if density else None,
            "fingerprint": fingerprint(width, height)}


def _device_name(udid):
    try:
        return (paths.describe(udid) or {}).get("deviceType") or None
    except Exception:
        # A listing failure must never cost the caller its metrics; the name is only ever cosmetic.
        return None


def screen(udid, refresh=False):
    if not udid:
        raise MetricsUnavailable("no simulator was resolved — nothing to measure its screen")
    if not refresh and udid in _CACHE:
        return dict(_CACHE[udid])
    _REASONS.clear()
    for source, probe in ((SOURCE_IDB, probe_idb), (SOURCE_DRIVER, probe_driver)):
        found = _sized(probe(udid))
        if found:
            found["source"] = source
            found["device"] = _device_name(udid)
            _CACHE[udid] = found
            return dict(found)
    reason = _REASONS.get(SOURCE_DRIVER) or _REASONS.get(SOURCE_IDB)
    raise MetricsUnavailable(
        "no live channel can report %s's screen size — start one with `uiflow up` (driver) "
        "or `uiflow idb up` (idb)%s"
        % (udid, "" if not reason else "; the last probe failed with %s" % reason)
    )
