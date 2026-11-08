import base64
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile

from uiflow import device

RUNS_DIRNAME = "uiflow-runs"
REPORT_NAME = "report.html"
SIDECAR_NAME = "run.json"
KEEP_RUNS = 20
THUMB_WIDTH = 460
SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]|\.{2,}")

STATUS_OK = "ok"
STATUS_SKIP = "skip"
STATUS_FAIL = "FAIL"
STATUS_NOT_RUN = "not run"


def now_iso(when=None):
    moment = (
        datetime.datetime.fromtimestamp(when, datetime.timezone.utc)
        if when is not None
        else datetime.datetime.now(datetime.timezone.utc)
    )
    return moment.isoformat(timespec="seconds")


def plan(groups, names):
    """Interleave a screenshot before the run and after every group.

    Returns (steps, positions, captions): `positions[i]` is the batch index of record `i`,
    and `captions` maps each screenshot's batch index to its caption.
    """
    steps = []
    positions = []
    captions = {}
    for index, group in enumerate(groups):
        captions[len(steps)] = "before" if index == 0 else "after %s" % names[index - 1]
        steps.append({"op": "screenshot"})
        positions.append(len(steps))
        steps.append(group)
    captions[len(steps)] = "after %s" % names[-1] if names else "before"
    steps.append({"op": "screenshot"})
    return steps, positions, captions


def record_index(positions, batch_index):
    try:
        return positions.index(batch_index)
    except ValueError:
        return None


def screenshot_path(result):
    info = ((result or {}).get("info") or "").strip()
    if not info:
        return None
    for token in info.split():
        if token.startswith("path="):
            return token[5:]
    return info


def shots_from(reply, captions):
    results = (reply or {}).get("results") or []
    shots = []
    last_index = None
    for index, result in enumerate(results):
        caption = captions.get(index)
        if caption is None or not result.get("ok"):
            continue
        path = screenshot_path(result)
        if not path:
            continue
        # A skipped record ran nothing, so its shot cannot differ — and a blinking caret
        # makes that too unreliable to catch by comparing bytes. Folding onto the last shot is
        # only sound while that shot is the screenshot immediately before this record.
        previous = results[index - 1] if index else None
        adjacent = last_index == index - 2
        if shots and adjacent and previous is not None and changed_nothing(previous):
            shots[-1].setdefault("also", []).append(caption)
            last_index = index
            continue
        shots.append({"caption": caption, "path": path})
        last_index = index
    return shots


def changed_nothing(result):
    """Skipped outright, or every step inside it skipped — `tapIfPresent` and `setSwitch` no-op."""
    if (result or {}).get("skipped"):
        return True
    children = (result or {}).get("children")
    return bool(children) and all(changed_nothing(child) for child in children)


def _digest(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.md5(handle.read()).hexdigest()
    except (OSError, TypeError, ValueError):
        return None


def dedupe(shots):
    """A skipped record changes nothing, so its shot repeats the previous screen — fold those."""
    kept = []
    previous = None
    for shot in shots or []:
        digest = _digest(shot.get("path"))
        if kept and digest is not None and digest == previous and not shot.get("keep"):
            folded = kept[-1].setdefault("also", [])
            folded.append(shot.get("caption"))
            folded.extend(shot.get("also") or [])
            continue
        entry = dict(shot)
        if entry.get("also"):
            entry["also"] = list(entry["also"])
        kept.append(entry)
        previous = digest
    return kept


def _status(result):
    if result is None:
        return STATUS_NOT_RUN
    if result.get("skipped"):
        return STATUS_SKIP
    return STATUS_OK if result.get("ok") else STATUS_FAIL


def build_context(app, names, resolved, reply, positions, wall_ms, shots, sim=None,
                  started_at=None, locale=None):
    results = (reply or {}).get("results") or []
    entries = []
    for index, record in enumerate(resolved):
        batch_index = positions[index] if index < len(positions) else None
        result = results[batch_index] if batch_index is not None and batch_index < len(results) else None
        entries.append({
            "name": record.get("name") or "?",
            "status": _status(result),
            "ms": (result or {}).get("ms"),
            "description": record.get("description") or "",
            "verifiesWhy": record.get("verifiesWhy") or "",
        })
    error = None
    raw = (reply or {}).get("error")
    if raw:
        path = list(raw.get("failedPath") or [])
        owner = record_index(positions, path[0]) if path else None
        error = {
            "reason": raw.get("reason") or "?",
            "detail": raw.get("detail") or "",
            "path": ".".join(str(part) for part in path),
            "record": resolved[owner].get("name") if owner is not None and owner < len(resolved) else None,
            "snapshot": list(raw.get("snapshot") or []),
        }
    return {
        "app": app,
        "names": list(names),
        "ok": bool((reply or {}).get("ok")),
        "wallMs": wall_ms,
        "sim": sim,
        "startedAt": started_at or now_iso(),
        "records": entries,
        "shots": list(shots or []),
        "error": error,
        "locale": dict(locale) if locale else None,
    }


STYLE = """
:root { color-scheme: light dark; --bg:#fbfbfa; --fg:#1a1a19; --dim:#6b6b68; --line:#e3e3e0;
        --card:#fff; --ok:#1a7f4b; --fail:#c0392b; --skip:#8a6d1f; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161615; --fg:#ececea; --dim:#9a9a96; --line:#2e2e2c; --card:#1e1e1d;
          --ok:#4ec27f; --fail:#ff7a6b; --skip:#d8b34a; }
}
* { box-sizing: border-box; }
body { margin:0; padding:32px 24px 64px; background:var(--bg); color:var(--fg);
       font:14px/1.55 ui-sans-serif,-apple-system,'SF Pro Text',Segoe UI,sans-serif; }
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size:19px; margin:0 0 4px; font-weight:650; letter-spacing:-.01em; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
     margin:36px 0 12px; font-weight:600; }
.sub { color:var(--dim); font-size:13px; margin:0; }
.verdict { display:inline-block; padding:3px 10px; border-radius:5px; font-weight:700;
           font-size:12px; letter-spacing:.06em; vertical-align:3px; margin-right:10px; }
.verdict.pass { background:var(--ok); color:var(--bg); }
.verdict.fail { background:var(--fail); color:var(--bg); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; font-weight:600; color:var(--dim); font-size:11px; text-transform:uppercase;
     letter-spacing:.06em; padding:0 12px 6px 0; border-bottom:1px solid var(--line); }
td { padding:9px 12px 9px 0; border-bottom:1px solid var(--line); vertical-align:top; }
td.num { text-align:right; font-variant-numeric:tabular-nums; color:var(--dim); white-space:nowrap; }
.s { font-weight:650; font-size:12px; }
.s.ok { color:var(--ok); } .s.fail { color:var(--fail); } .s.skip { color:var(--skip); }
.s.none { color:var(--dim); }
.why { color:var(--dim); }
.shots { display:flex; gap:18px; overflow-x:auto; padding-bottom:8px; }
figure { margin:0; flex:0 0 auto; width:240px; }
figure a { display:block; }
figure img { width:100%; border:1px solid var(--line); border-radius:8px; display:block;
             background:var(--card); }
figcaption { font-size:12px; color:var(--dim); margin-top:7px; }
.also { display:block; opacity:.7; font-style:italic; }
figure.miss { width:240px; height:340px; border:1px dashed var(--line); border-radius:8px;
              display:flex; align-items:center; justify-content:center; color:var(--dim);
              font-size:12px; text-align:center; padding:12px; }
.err { background:var(--card); border:1px solid var(--fail); border-left-width:3px;
       border-radius:8px; padding:14px 16px; }
.err dl { display:grid; grid-template-columns:auto 1fr; gap:4px 16px; margin:0; font-size:13px; }
.err dt { color:var(--dim); }
.err dd { margin:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
          word-break:break-word; }
details { margin-top:12px; font-size:12px; }
summary { cursor:pointer; color:var(--dim); }
pre { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px;
      overflow-x:auto; font-size:11px; line-height:1.5; margin:8px 0 0;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
footer { color:var(--dim); font-size:12px; margin-top:40px; }
"""


def _escape(value):
    return html.escape(str(value if value is not None else ""))


def _seconds(ms):
    return "%.1f s" % (ms / 1000.0) if isinstance(ms, (int, float)) else "?"


def _millis(ms):
    return "%d ms" % ms if isinstance(ms, (int, float)) else "—"


def _downscaled(path, width=THUMB_WIDTH):
    """A 3x simulator screenshot inlines to megabytes; at storyboard size that detail is waste."""
    handle, scaled = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    try:
        done = subprocess.run(
            ["sips", "-Z", str(width), "-s", "format", "png", path, "--out", scaled],
            capture_output=True,
        )
        if done.returncode == 0:
            with open(scaled, "rb") as source:
                return source.read()
    except OSError:
        pass
    finally:
        os.unlink(scaled)
    return None


def _image(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    scaled = _downscaled(path)
    # `sips -Z` enlarges an image already smaller than the target, so take whichever is smaller.
    if scaled and len(scaled) < len(raw):
        raw = scaled
    return base64.b64encode(raw).decode("ascii")


def _figure(shot):
    caption = _escape(shot.get("caption"))
    also = ""
    if shot.get("also"):
        folded = ", ".join(entry for entry in shot["also"] if entry)
        also = '<span class="also">unchanged %s</span>' % _escape(folded)
    encoded = _image(shot.get("path"))
    if not encoded:
        return '<figure class="miss">screenshot unavailable<br>%s%s</figure>' % (caption, also)
    image = '<img alt="%s" src="data:image/png;base64,%s">' % (caption, encoded)
    if shot.get("file"):
        image = '<a href="%s" title="full size">%s</a>' % (_escape(shot["file"]), image)
    return "<figure>%s<figcaption>%s%s</figcaption></figure>" % (image, caption, also)


def _rows(records):
    rows = []
    for entry in records:
        note = entry.get("verifiesWhy") or entry.get("description") or ""
        status = entry["status"]
        css = {STATUS_OK: "ok", STATUS_FAIL: "fail", STATUS_SKIP: "skip"}.get(status, "none")
        rows.append(
            '<tr><td><strong>%s</strong></td><td class="s %s">%s</td><td class="num">%s</td>'
            '<td class="why">%s</td></tr>'
            % (_escape(entry["name"]), css, _escape(status), _millis(entry.get("ms")), _escape(note))
        )
    return "\n".join(rows)


def _error_block(error):
    if not error:
        return ""
    rows = [("reason", error["reason"]), ("detail", error["detail"])]
    if error.get("record"):
        rows.append(("record", error["record"]))
    if error.get("path"):
        rows.append(("step path", error["path"]))
    items = "".join(
        "<dt>%s</dt><dd>%s</dd>" % (_escape(label), _escape(value)) for label, value in rows
    )
    snapshot = ""
    if error.get("snapshot"):
        snapshot = (
            "<details><summary>screen at the moment of failure (%d elements)</summary>"
            "<pre>%s</pre></details>"
            % (len(error["snapshot"]), _escape("\n".join(str(line) for line in error["snapshot"])))
        )
    return '<h2>Failure</h2><div class="err"><dl>%s</dl>%s</div>' % (items, snapshot)


def _locale_line(locale):
    if not locale:
        return ""
    missing = list(locale.get("untranslated") or [])
    lang = _escape(locale.get("lang") or "?")
    if not missing:
        return "locale %s" % lang
    return "locale %s · %d of %d labels untranslated (%s)" % (
        lang, len(missing), locale.get("labels") or 0,
        _escape(", ".join(missing[:6]) + (", …" if len(missing) > 6 else "")))


def render(context):
    verdict = "PASS" if context["ok"] else "FAIL"
    shots = "".join(_figure(shot) for shot in context.get("shots") or [])
    storyboard = '<h2>Screens</h2><div class="shots">%s</div>' % shots if shots else ""
    meta = " · ".join(filter(None, [
        _escape(context["app"]),
        _seconds(context.get("wallMs")),
        _escape(context.get("startedAt")),
        ("sim %s" % _escape(device.format_compact(context["sim"]))) if context.get("sim") else "",
        _locale_line(context.get("locale")),
    ]))
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ui-flow · %(names)s</title>
<style>%(style)s</style></head>
<body><main>
<h1><span class="verdict %(css)s">%(verdict)s</span>%(names)s</h1>
<p class="sub">%(meta)s</p>
<h2>Records</h2>
<table><thead><tr><th>record</th><th>status</th><th>time</th><th>what it verifies</th></tr></thead>
<tbody>%(rows)s</tbody></table>
%(error)s
%(storyboard)s
<footer>Generated by the ui-flow skill. Screenshots are embedded, so this file stands alone; click one for the full-size PNG beside it.</footer>
</main></body></html>
""" % {
        "names": _escape(", ".join(context.get("names") or []) or context["app"]),
        "style": STYLE,
        "css": "pass" if context["ok"] else "fail",
        "verdict": verdict,
        "meta": meta,
        "rows": _rows(context.get("records") or []),
        "error": _error_block(context.get("error")),
        "storyboard": storyboard,
    }


def runs_root(root=None):
    return root or os.environ.get("UIFLOW_REPORT_ROOT") or os.path.join(
        tempfile.gettempdir(), RUNS_DIRNAME
    )


def _component(value):
    cleaned = SAFE_COMPONENT.sub("_", str(value or "run")).strip(".")
    return cleaned or "run"


def default_dir(app, root=None):
    # Replays finish in milliseconds, so a whole-second stamp collides on back-to-back runs.
    now = datetime.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    return os.path.join(runs_root(root), "%s-%s" % (stamp, _component(app)))


def _collect(shots, directory):
    """Full-resolution copies beside the report: the inlined thumbnail is only a storyboard."""
    saved = []
    for index, shot in enumerate(shots or [], 1):
        entry = dict(shot)
        name = "%02d-%s.png" % (index, _component((shot.get("caption") or "shot").replace(" ", "-")))
        try:
            shutil.copyfile(shot["path"], os.path.join(directory, name))
            entry["file"] = name
        except (OSError, KeyError):
            pass
        saved.append(entry)
    return saved


def write(context, directory):
    os.makedirs(directory, exist_ok=True)
    context = dict(context, shots=_collect(dedupe(context.get("shots")), directory))
    path = os.path.join(os.path.abspath(directory), REPORT_NAME)
    with open(path, "w") as handle:
        handle.write(render(context))
    with open(os.path.join(os.path.abspath(directory), SIDECAR_NAME), "w") as handle:
        json.dump(context, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def prune(root, keep=KEEP_RUNS):
    try:
        entries = [
            os.path.join(root, name) for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
        ]
    except OSError:
        return
    entries.sort(key=_mtime, reverse=True)
    for stale in entries[keep:]:
        shutil.rmtree(stale, ignore_errors=True)
