import hashlib
import json
import os

from uiflow import compose, paths, records, sources, timing

VERSION = 1
MIN_RUN = 3
INDEX_NAME = "index.json"


def index_path():
    return os.path.join(paths.RECORDS_DIR, INDEX_NAME)


def step_hash(step):
    return timing.fingerprint(step)


def run_hash(fingerprints):
    joined = "|".join(fingerprints)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _apps():
    try:
        entries = sorted(os.listdir(paths.RECORDS_DIR))
    except OSError:
        return []
    return [
        entry for entry in entries
        if not entry.startswith(".")
        and entry != INDEX_NAME
        and os.path.isdir(os.path.join(paths.RECORDS_DIR, entry))
    ]


def _entries(app):
    """(name, [mtime_ns, size]) per record file, without parsing any JSON."""
    try:
        scanned = list(os.scandir(os.path.join(paths.RECORDS_DIR, app)))
    except OSError:
        return []
    found = []
    for entry in scanned:
        if not entry.name.endswith(".json") or entry.name == INDEX_NAME:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        found.append((entry.name[:-5], [stat.st_mtime_ns, stat.st_size]))
    return sorted(found)


def _selector_keys(step):
    selector = step.get("sel") or {}
    if not selector:
        return []
    return ["%s|%s|%s|%s|%s" % (
        selector.get("app") or step.get("app") or "",
        selector.get("type") or "",
        selector.get("id") or selector.get("idPrefix") or "",
        selector.get("label") or "",
        selector.get("labelContains") or "",
    )]


def _current_stamps():
    return {
        "%s%s%s" % (app, records.REF_SEPARATOR, name): stamp
        for app in _apps()
        for name, stamp in _entries(app)
    }


def uses_of(record, app, builtins_only=False):
    found = []
    for raw in compose.refs_of(record):
        try:
            edge = records.canonical_ref(raw, app, builtins_only=builtins_only)
        except (records.MissingRecord, records.BadName):
            edge = records.REF_SEPARATOR.join(records.parse_ref(raw, app))
        if edge not in found:
            found.append(edge)
    return found


def link_used_by(entries):
    for ref, entry in entries.items():
        for used in entry["uses"]:
            target = entries.get(used)
            if target is not None and ref not in target["usedBy"]:
                target["usedBy"].append(ref)
    return entries


def _index_steps(payload, ref, record):
    top = record.get("steps") or []
    fingerprints = []
    for position, step in enumerate(top):
        digest = step_hash(step)
        fingerprints.append(digest)
        payload["steps"].setdefault(digest, []).append([ref, [position]])
        for key in _selector_keys(step):
            bucket = payload["selectors"].setdefault(key, [])
            if ref not in bucket:
                bucket.append(ref)
    for length in range(MIN_RUN, len(fingerprints) + 1):
        for start in range(0, len(fingerprints) - length + 1):
            digest = run_hash(fingerprints[start:start + length])
            payload["runs"].setdefault(digest, []).append([ref, start, length])


def _entry(record, app, builtins_only=False):
    return {
        "kind": records.kind_of(record),
        "aliases": list(record.get("aliases") or []),
        "description": record.get("description") or "",
        "stepCount": len(record.get("steps") or []),
        "hasSatisfiedWhen": bool(record.get("satisfiedWhen")),
        "fingerprints": [step_hash(step) for step in record.get("steps") or []],
        "uses": uses_of(record, app, builtins_only),
        "usedBy": [],
        "health": dict(record.get("health") or {}),
    }


def _empty(stamps=None):
    return {
        "version": VERSION,
        "stamps": stamps if stamps is not None else {},
        "records": {},
        "steps": {},
        "runs": {},
        "selectors": {},
        "skipped": [],
    }


def _add(payload, ref, record, app, builtins_only=False):
    try:
        records.safe_name(app)
        records.safe_name(record.get("name") or ref.rpartition(records.REF_SEPARATOR)[2])
        entry = _entry(record, app, builtins_only)
    except (records.BadName, ValueError):
        payload["skipped"].append(ref)
        return
    payload["records"][ref] = entry
    _index_steps(payload, ref, record)


def build():
    payload = _empty(_current_stamps())
    for app in _apps():
        for name, record in sorted(records.builtins_for(app).items()):
            _add(payload, "%s%s%s" % (app, records.REF_SEPARATOR, name), record, app,
                 builtins_only=True)
    link_used_by(payload["records"])
    return payload


def build_from(entries):
    payload = _empty()
    for ref, record in sorted((entries or {}).items()):
        _add(payload, ref, record, ref.rpartition(records.REF_SEPARATOR)[0])
    link_used_by(payload["records"])
    return payload


def _ref_of(occurrence):
    return occurrence[0] if isinstance(occurrence, list) else occurrence


def merge_payloads(base, overlay):
    # empty stamps so a merged payload can never compare fresh if one is ever written
    payload = _empty()
    payload["skipped"] = list(base.get("skipped") or []) + list(overlay.get("skipped") or [])
    shadowed = set(overlay.get("records") or {})
    for ref, entry in (base.get("records") or {}).items():
        if ref not in shadowed:
            payload["records"][ref] = dict(entry, usedBy=[])
    for ref, entry in (overlay.get("records") or {}).items():
        payload["records"][ref] = dict(entry, usedBy=[])
    for key in ("steps", "runs", "selectors"):
        for position, source in enumerate((base, overlay)):
            for digest, occurrences in (source.get(key) or {}).items():
                bucket = payload[key].setdefault(digest, [])
                for occurrence in occurrences:
                    # a shadowed ref keeps only the overlay's occurrences, or `find` scores a
                    # ref on selector labels belonging to the built-in it no longer resolves to
                    if position == 0 and _ref_of(occurrence) in shadowed:
                        continue
                    if occurrence not in bucket:
                        bucket.append(occurrence)
    link_used_by(payload["records"])
    return payload


def merged(payload=None):
    base = payload if payload is not None else load()
    external = sources.all_records()
    if not external:
        return base
    return merge_payloads(base, build_from(external))


def _write(payload):
    path = index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load(rebuild=False):
    if not rebuild:
        try:
            with open(index_path()) as handle:
                payload = json.load(handle)
            if payload.get("version") == VERSION and payload.get("stamps") == _current_stamps():
                return payload
        except (OSError, ValueError, TypeError):
            pass
    payload = build()
    try:
        _write(payload)
    except OSError:
        pass
    return payload


def duplicate_runs(payload=None, app=None, min_run=MIN_RUN):
    payload = payload if payload is not None else load()
    found = []
    for digest, occurrences in payload.get("runs", {}).items():
        kept = [
            {"ref": ref, "start": start}
            for ref, start, _ in occurrences
            if app is None or ref.startswith(app + records.REF_SEPARATOR)
        ]
        if len({entry["ref"] for entry in kept}) < 2:
            continue
        length = occurrences[0][2]
        if length < min_run:
            continue
        found.append({"hash": digest, "length": length, "occurrences": kept})
    found.sort(key=lambda entry: (-entry["length"], -len(entry["occurrences"]), entry["hash"]))
    return found


def _covers(tokens, text):
    return bool(text) and all(token in text for token in tokens)


def find(query, app=None, limit=20, payload=None):
    payload = payload if payload is not None else load()
    wanted = (query or "").strip().lower()
    if not wanted:
        return []
    # A phrase like "keyboard settings" must match a record spelled "open-keyboard-settings",
    # so every token has to hit somewhere rather than the whole query hitting one field.
    tokens = wanted.split()
    labels = {}
    for key, refs in payload.get("selectors", {}).items():
        for ref in refs:
            labels.setdefault(ref, []).append(key)
    hits = []
    for ref, entry in payload.get("records", {}).items():
        if app and not ref.startswith(app + records.REF_SEPARATOR):
            continue
        name = ref.rpartition(records.REF_SEPARATOR)[2].lower()
        aliases = [str(alias).lower() for alias in entry.get("aliases") or []]
        description = (entry.get("description") or "").lower()
        selectors = " ".join(labels.get(ref, [])).lower()
        score, why = 0.0, None
        if wanted == name:
            score, why = 1.0, "name"
        elif _covers(tokens, name):
            score, why = 0.9, "name"
        for alias in aliases:
            if _covers(tokens, alias) and score < 0.85:
                score, why = 0.85, "alias %s" % alias
        if score < 0.7 and _covers(tokens, description):
            score, why = 0.7, "description"
        if score < 0.6 and _covers(tokens, " ".join([name] + aliases + [description])):
            score, why = 0.6, "name+description"
        if score < 0.5 and _covers(tokens, selectors):
            score, why = 0.5, "selector"
        if not why:
            continue
        hits.append({"ref": ref, "kind": entry.get("kind"), "why": why, "score": score})
    hits.sort(key=lambda hit: (-hit["score"], hit["ref"]))
    return hits[:limit]
