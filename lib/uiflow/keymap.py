"""Learn a label -> SCREEN point map for an app's custom-drawn keys, once per device.

XCUITest reports a custom keyboard's own elements in the KEYBOARD's coordinate space while
reporting the system keyboard's buttons and the host app's views in screen space, so the two cannot
be related by picking a shared anchor: there is no element that appears in both spaces. The offset
is therefore established the same way the skill insists everything else is — by driving the thing
and reading the result back.

The offset is a property of one screen, not of the app, so a learned map is stored against that
device's point fingerprint and `ensure` re-runs the probe on a device it has not seen before.
"""

import datetime

from uiflow import channel, idb, metrics, records

BACKSPACE_KEYCODE = 42
PROBE_ORDER = ("G", "H", "F", "J", "g", "h", "f", "j", "ㅎ", "ㅏ")
GRID_STEP = 20.0
CENTRE_TOLERANCE = 8.0


class CalibrationFailed(Exception):
    pass


def single_char_keys(snapshot, element_type="staticText"):
    keys = {}
    for element in snapshot:
        if element.get("type") != element_type:
            continue
        label = element.get("label")
        frame = element.get("frame")
        if not label or len(label) != 1 or not frame or len(frame) != 4:
            continue
        keys.setdefault(label, [float(frame[0]) + float(frame[2]) / 2.0,
                                float(frame[1]) + float(frame[3]) / 2.0])
    return keys


def text_field_point(snapshot):
    for element in snapshot:
        if element.get("type") == "textField" and element.get("frame"):
            x, y, width, height = (float(v) for v in element["frame"])
            return [x + width / 2.0, y + height / 2.0]
    return None


def key_field(keys):
    """The bounding box of the key CENTRES — the only reference the keyboard can be located from.

    A keyboard extension's elements are vended by the HOST process (driver review C7), so nothing in
    the snapshot identifies the keyboard itself and its own frame cannot be looked up.
    """
    if len(keys or {}) < 2:
        raise CalibrationFailed("need at least two keys to measure the key field, got %d"
                                % len(keys or {}))
    xs = [point[0] for point in keys.values()]
    ys = [point[1] for point in keys.values()]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    if width <= 0 or height <= 0:
        raise CalibrationFailed("the keys are collinear, so the key field has no area")
    return (min(xs), min(ys), width, height)


def derive_dx(field_centre_x, screen_width, tolerance=CENTRE_TOLERANCE):
    """A keyboard is horizontally centred, so the shift is whatever moves the field's centre onto
    the screen's centre. Never derived from the field's WIDTH: a key-centre box is narrower than the
    keyboard by half a key each side, which reads a correct full-width iPad keyboard as narrow."""
    offset = screen_width / 2.0 - field_centre_x
    return 0.0 if abs(offset) <= tolerance else round(offset, 2)


def candidate_offsets(local_y, screen_height, step=GRID_STEP, floor=None):
    """Zero first, because an app whose frames are already screen points must not be probed 20 times.
    Then bottom-upward, because a keyboard is anchored to the bottom of the screen.

    Candidates landing above `floor` are dropped: a keyboard occupies the lower part of the screen,
    so a higher point is not a missed key but a tap into the app's own UI, and one of those opened a
    dialog that broke the rest of the search.

    The search starts one step BELOW the measured bottom: screen_height comes from the snapshot's
    outermost frame, which under-reports whenever nothing on screen spans the full display, and a
    truncated search would miss a true offset sitting just past it. Probing off-screen types
    nothing, so the extra candidate costs one wasted probe and buys the whole range."""
    floor = screen_height / 2.0 if floor is None else floor
    limit = max(0.0, screen_height - local_y) + step
    offsets = []
    value = limit
    while value > 0:
        offsets.append(round(value, 2))
        value -= step
    if local_y >= floor:
        offsets.insert(0, 0.0)
    return [offset for offset in offsets if local_y + offset >= floor]


def interior_keys(keys):
    """Keys with other keys on another row, so an off-by-one-row miss still lands on a letter."""
    return [
        label for label, point in keys.items()
        if any(other != label and abs(keys[other][1] - point[1]) > 1 for other in keys)
    ]


def pick_probe(keys):
    """A probe must be surrounded by other letters. An off-by-one-row miss then lands on another
    letter instead of shift, delete or the globe — a wrong character is recoverable, leaving the
    keyboard is not."""
    for label in PROBE_ORDER:
        if label in keys:
            return label
    interior = interior_keys(keys)
    return sorted(interior)[len(interior) // 2] if interior else (sorted(keys)[0] if keys else None)


class Prober:
    def __init__(self, udid, field_point):
        self.udid = udid
        self.field_point = field_point

    def _send(self, ops):
        reply = idb.send(ops, self.udid)
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise CalibrationFailed("idb %s: %s" % (error.get("reason"), error.get("detail")))
        return reply.get("results") or []

    def read(self):
        result = self._send([{"op": "value", "x": self.field_point[0], "y": self.field_point[1]}])
        return result[0].get("value") or ""

    def clear(self):
        """Hardware backspace, not a tap: the delete key's screen point is exactly what has not been
        calibrated yet when the field needs clearing."""
        count = len(self.read())
        if count <= 0:
            return
        self._send([{"op": "key", "code": BACKSPACE_KEYCODE} for _ in range(count)])

    def restore(self, text):
        """HID text entry, so it needs no keymap and works before the keyboard is calibrated."""
        if text:
            self._send([{"op": "text", "text": text}])

    def trim_to(self, text):
        """Backspace the probe's own characters off. idb's HID `text` op silently drops every
        non-ASCII character, so re-typing the field would delete a Korean or accented value."""
        current = self.read()
        if not current.startswith(text):
            self.clear()
            _restore(self, text)
            return
        extra = len(current) - len(text)
        if extra > 0:
            self._send([{"op": "key", "code": BACKSPACE_KEYCODE} for _ in range(extra)])

    def probe(self, point):
        before = self.read()
        self._send([{"op": "tapBurst", "points": [list(point)], "holdMs": 30, "gapMs": 30},
                    {"op": "sleep", "ms": 250}])
        after = self.read()
        return after[len(before):] if after.startswith(before) else after


def _restore(prober, text):
    if not text:
        return
    try:
        prober.restore(text)
    except Exception:
        # Putting the text back is best-effort: failing must not fail an otherwise good learn.
        pass


def _trim_to(prober, text):
    try:
        prober.trim_to(text)
    except Exception:
        # Cleanup is best-effort: failing must not fail an otherwise good learn.
        pass


def calibrate(prober, keys, screen_height, screen_width=None, field_centre_x=None,
              probe_label=None, step=GRID_STEP, floor=None):
    """Returns the offset that makes the probe key type itself, as {"dx", "dy", "probe"}.

    dx is derived from the field's centre and then confirmed by the same probe that confirms dy, so
    a keyboard that does not sit where its own frames claim still calibrates.
    """
    label = probe_label or pick_probe(keys)
    if not label:
        raise CalibrationFailed("no single-character keys in the snapshot to calibrate against")
    if label not in keys:
        raise CalibrationFailed(
            "probe key %r is not on screen; snapshot has %s"
            % (label, ", ".join(sorted(keys)) or "no single-character keys")
        )
    local_x, local_y = keys[label]
    candidates_dx = [0.0]
    if screen_width and field_centre_x is not None:
        derived = derive_dx(field_centre_x, screen_width)
        if derived != 0.0:
            candidates_dx = [derived, 0.0]
    offsets = candidate_offsets(local_y, screen_height, step=step, floor=floor)
    zero_first = bool(offsets) and offsets[0] == 0.0
    if zero_first and prober.probe([local_x, local_y]).lower() == label.lower():
        return {"dx": 0.0, "dy": 0.0, "probe": label}
    # dx OUTER: inner, dx=0 wins at the first offset still inside the probe's own half-key hit area.
    for dx in candidates_dx:
        for offset in offsets:
            if zero_first and dx == 0.0 and offset == 0.0:
                continue
            if prober.probe([local_x + dx, local_y + offset]).lower() == label.lower():
                return {"dx": dx, "dy": offset, "probe": label}
    raise CalibrationFailed(
        "no offset made key %r type itself — is the keyboard on screen and a field focused?" % label
    )


def learn(app, udid, element_type="staticText", probe_label=None, snapshot=None, screen=None):
    snapshot = snapshot if snapshot is not None else channel.snapshot(app=app, udid=udid)
    keys = single_char_keys(snapshot, element_type=element_type)
    if not keys:
        raise CalibrationFailed(
            "no single-character %s elements on screen — bring the keyboard up first" % element_type
        )
    field = text_field_point(snapshot)
    if not field:
        raise CalibrationFailed("no text field on screen to read typed characters back from")
    screen = screen if screen is not None else metrics.screen(udid)
    width, height = float(screen["w"]), float(screen["h"])
    prober = Prober(udid, field)
    before = prober.read()
    try:
        box = key_field(keys)
    except CalibrationFailed:
        # A single-row or collinear key set carries no dx information, but still calibrates for dy.
        box = None
    try:
        found = calibrate(prober, keys, height, screen_width=width,
                          field_centre_x=None if box is None else box[0] + box[2] / 2.0,
                          probe_label=probe_label)
    finally:
        _trim_to(prober, before)
    screen_keys = {label: [point[0] + found["dx"], point[1] + found["dy"]]
                   for label, point in keys.items()}
    calibration = {
        "dx": round(found["dx"], 2),
        "dy": round(found["dy"], 2),
        "probe": found["probe"],
        "screenW": round(width, 2),
        "screenH": round(height, 2),
        "learnedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if box is not None:
        calibration["field"] = [round(box[0] + found["dx"], 2), round(box[1] + found["dy"], 2),
                                round(box[2], 2), round(box[3], 2)]
    path = records.save_key_map(app, screen_keys, screen["fingerprint"], calibration=calibration)
    return {"path": path, "offset": found["dy"], "dx": found["dx"], "probe": found["probe"],
            "keys": len(screen_keys), "fingerprint": screen["fingerprint"]}


def ensure(app, udid, screen, element_type="staticText", snapshot=None):
    fingerprint = screen["fingerprint"]
    found = records.key_map(app, fingerprint)
    if found:
        return found
    if records.key_map_is_legacy(app):
        legacy = records.key_map_only(app)
        snapshot = snapshot if snapshot is not None else channel.snapshot(app=app, udid=udid)
        if legacy and _validates(udid, legacy, snapshot):
            records.save_key_map(app, legacy, fingerprint, calibration={"adopted": "legacy"})
            return legacy
    learn(app, udid, element_type=element_type, snapshot=snapshot, screen=screen)
    return records.key_map(app, fingerprint)


def _probe_labels(keys):
    """Spread across the field, because one key typing itself says nothing about the other end.

    Never an edge key: validation runs exactly when a miss is likely, and a miss on the outermost
    column hits the globe and leaves the keyboard, which the next `learn` cannot recover from.
    """
    if not keys:
        return []
    by_x = sorted(interior_keys(keys) or list(keys), key=lambda label: keys[label][0])
    candidates = by_x[1:-1] or by_x
    xs = [point[0] for point in keys.values()]
    low, span = min(xs), max(xs) - min(xs)
    chosen = []
    for fraction in (0.25, 0.5, 0.75):
        target = low + span * fraction
        nearest = min(candidates, key=lambda label: abs(keys[label][0] - target))
        if nearest not in chosen:
            chosen.append(nearest)
    return chosen


def _validates(udid, keys, snapshot):
    """A legacy map records no device, so whether it fits the live one can only be found by typing."""
    labels = _probe_labels(keys)
    field = text_field_point(snapshot)
    if not labels or not field:
        return False
    try:
        prober = Prober(udid, field)
        before = prober.read()
        try:
            for label in labels:
                # One miss already proves it stale, and every extra probe can leave the keyboard.
                if prober.probe(keys[label]).lower() != label.lower():
                    return False
        finally:
            _trim_to(prober, before)
    except Exception:
        # Any failure means "cannot confirm", and the answer to that is always to relearn.
        return False
    return True
