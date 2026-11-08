import json
import os
import re

from uiflow import paths, sources

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_CACHE = {}


class BadName(ValueError):
    pass


class MissingRecord(LookupError):
    pass


class CycleError(ValueError):
    pass


def _safe(component):
    if not isinstance(component, str) or not NAME_RE.fullmatch(component) or ".." in component:
        raise BadName("bad record name %r: names must match %s" % (component, NAME_RE.pattern))
    return component


safe_name = _safe

REF_SEPARATOR = "/"
FLOW = "flow"
FRAGMENT = "fragment"
KINDS = (FLOW, FRAGMENT)


def parse_ref(ref, default_app=None):
    if not isinstance(ref, str) or not ref:
        raise BadName("bad ref %r" % (ref,))
    if REF_SEPARATOR in ref:
        app, _, name = ref.rpartition(REF_SEPARATOR)
    else:
        app, name = default_app, ref
    if not app:
        raise BadName("ref %r names no app and no default app was given" % (ref,))
    return _safe(app), _safe(name)


def qualify(app, name):
    return "%s%s%s" % (_safe(app), REF_SEPARATOR, _safe(name))


def load_ref(ref, default_app=None):
    app, name = parse_ref(ref, default_app)
    return load(app, name)


def kind_of(record):
    kind = (record or {}).get("kind") or FLOW
    if kind not in KINDS:
        raise BadName("unknown kind %r: expected one of %s" % (kind, ", ".join(KINDS)))
    return kind


def canonical_ref(ref, default_app=None, builtins_only=False):
    record = (load_builtin_ref if builtins_only else load_ref)(ref, default_app)
    return qualify(record.get("app") or parse_ref(ref, default_app)[0], record["name"])


def dir_for(app):
    return os.path.join(paths.RECORDS_DIR, _safe(app))


def path_for(app, name):
    return os.path.join(dir_for(app), _safe(name) + ".json")


def key_map_path(app):
    return os.path.join(dir_for(app), "keymap.json")


def key_map_document(app):
    try:
        with open(key_map_path(app)) as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def key_map_is_legacy(app):
    document = key_map_document(app)
    return isinstance(document.get("keys"), dict) and not document.get("devices")


def key_map_fingerprints(app):
    devices = key_map_document(app).get("devices")
    return tuple(sorted(devices)) if isinstance(devices, dict) else ()


def _points(raw):
    if not isinstance(raw, dict):
        return {}
    return {label: [float(at[0]), float(at[1])]
            for label, at in raw.items() if isinstance(at, (list, tuple)) and len(at) == 2}


def key_map(app, fingerprint):
    """Label -> SCREEN point for one device's custom-drawn keys.

    Per device because a keyboard reports its frames in the KEYBOARD's coordinate space and the
    offset onto the screen is probed: the same keyboard sits somewhere else on another screen, so a
    map probed on one simulator is only ever right for that one.
    """
    devices = key_map_document(app).get("devices")
    if not isinstance(devices, dict):
        return {}
    entry = devices.get(fingerprint)
    return _points(entry.get("keys")) if isinstance(entry, dict) else {}


def key_map_only(app):
    """The map when there is exactly one, for the legacy adoption path and for diagnostics."""
    document = key_map_document(app)
    devices = document.get("devices")
    if not isinstance(devices, dict) or not devices:
        return _points(document.get("keys"))
    if len(devices) != 1:
        return {}
    entry = next(iter(devices.values()))
    return _points(entry.get("keys")) if isinstance(entry, dict) else {}


def save_key_map(app, keys, fingerprint, calibration=None):
    payload = dict(key_map_document(app))
    payload.pop("keys", None)
    payload["app"] = app
    devices = payload.get("devices")
    payload["devices"] = dict(devices) if isinstance(devices, dict) else {}
    entry = dict(calibration or {})
    entry["keys"] = {k: [round(float(v[0]), 2), round(float(v[1]), 2)] for k, v in keys.items()}
    payload["devices"][fingerprint] = entry
    os.makedirs(dir_for(app), exist_ok=True)
    tmp = "%s.%s.tmp" % (key_map_path(app), os.getpid())
    with open(tmp, "w") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, key_map_path(app))
    return key_map_path(app)


def invalidate(app=None):
    if app is None:
        _CACHE.clear()
    else:
        _CACHE.pop(app, None)


def builtins_for(app):
    directory = dir_for(app)
    try:
        stamp = os.stat(directory).st_mtime_ns
    except OSError:
        stamp = None
    cached = _CACHE.get(app)
    if cached and cached[0] == stamp:
        return cached[1]
    loaded = {}
    if stamp is not None:
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, entry)) as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            payload.setdefault("app", app)
            payload.setdefault("name", entry[:-5])
            loaded[payload["name"]] = payload
    _CACHE[app] = (stamp, loaded)
    return loaded


def all_for(app):
    builtins = builtins_for(app)
    external = sources.records_for(app)
    if not external:
        return builtins
    merged = dict(builtins)
    merged.update(external)
    return merged


def _lookup(table, app, name):
    if name in table:
        return table[name]
    aliased = _by_alias(table, name)
    if aliased:
        return aliased
    raise MissingRecord(_missing_message(app, name, table))


def load(app, name):
    return _lookup(all_for(app), app, name)


def load_builtin(app, name):
    return _lookup(builtins_for(app), app, name)


def load_builtin_ref(ref, default_app=None):
    app, name = parse_ref(ref, default_app)
    return load_builtin(app, name)


def _missing_message(app, name, known):
    elsewhere = [ref for ref in sources.refs_named(name) if not ref.startswith(app + REF_SEPARATOR)]
    message = "no record %r for %s. Known: %s" % (name, app, ", ".join(sorted(known)) or "<none>")
    if elsewhere:
        message += ". Registered under another app: %s" % ", ".join(elsewhere)
    return message


def shadowed(refs):
    found = []
    for ref in refs or ():
        app, _, name = ref.rpartition(REF_SEPARATOR)
        if name in builtins_for(app):
            found.append(ref)
    return found


def save(record, app=None, external=True):
    app = app or record.get("app")
    ref = qualify(app, record["name"])
    path = (sources.path_for(ref) if external else None) or path_for(app, record["name"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)
    invalidate(app)
    sources.forget_cache(ref)
    return path


def _by_alias(table, alias):
    wanted = (alias or "").strip().lower()
    for record in table.values():
        if record["name"].lower() == wanted:
            return record
        for candidate in record.get("aliases") or []:
            if str(candidate).strip().lower() == wanted:
                return record
    return None


def find_by_alias(app, alias):
    return _by_alias(all_for(app), alias)


def resolve(app, names):
    ordered = []
    seen = set()
    visiting = []

    def walk(name):
        record = load(app, name)
        actual = record["name"]
        if actual in seen:
            return
        if actual in visiting:
            raise CycleError("record cycle: %s" % " -> ".join(visiting + [actual]))
        visiting.append(actual)
        for requirement in record.get("requires") or []:
            walk(requirement)
        visiting.pop()
        seen.add(actual)
        ordered.append(record)

    for name in names:
        walk(name)
    return ordered


def to_group(record):
    group = {"op": "group", "name": record["name"], "steps": list(record.get("steps") or [])}
    for key in ("satisfiedWhen", "verifies", "reassert"):
        value = record.get(key)
        if value:
            group[key] = list(value)
    return group
