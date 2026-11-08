import json
import os
import re

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SEPARATOR = "/"
ENV_VAR = "UIFLOW_FILES"
RESERVED = ("index.json", "keymap.json")


class SourceError(ValueError):
    pass


_PATHS = {}
_ORDER = []
_CACHE = {}


def reset():
    _PATHS.clear()
    del _ORDER[:]
    _CACHE.clear()


def _valid(component):
    return (
        isinstance(component, str)
        and ".." not in component
        and bool(NAME_RE.fullmatch(component))
    )


def _read(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except OSError as error:
        raise SourceError("cannot read record file %s: %s" % (path, error))
    except ValueError as error:
        raise SourceError("record file %s is not valid JSON: %s" % (path, error))


def adopt(ref, path):
    app, _, name = ref.rpartition(SEPARATOR)
    if not _valid(app) or not _valid(name):
        raise SourceError("bad ref %r" % (ref,))
    if ref in _PATHS:
        _ORDER.remove(ref)
    _PATHS[ref] = os.path.abspath(os.path.expanduser(path))
    _ORDER.append(ref)
    _CACHE.pop(ref, None)
    return ref


def _register_file(path, default_app=None, force_app=None):
    payload = _read(path)
    if not isinstance(payload, dict):
        raise SourceError("record file %s must hold a JSON object" % path)
    app = force_app or payload.get("app") or default_app
    name = payload.get("name") or os.path.splitext(os.path.basename(path))[0]
    if not app:
        raise SourceError('record file %s has no "app" and no app was given' % path)
    if not _valid(app):
        raise SourceError("record file %s names a bad app %r" % (path, app))
    if not _valid(name):
        raise SourceError("record file %s names a bad record %r" % (path, name))
    return adopt(app + SEPARATOR + name, path)


def _register_dir(path):
    found = []
    for app in sorted(os.listdir(path)):
        directory = os.path.join(path, app)
        if app.startswith(".") or not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".json") or entry in RESERVED:
                continue
            found.append(_register_file(os.path.join(directory, entry), force_app=app))
    if not found:
        raise SourceError("no <app>/<name>.json records under %s" % path)
    return found


def register(path, default_app=None):
    resolved = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(resolved):
        return _register_dir(resolved)
    if os.path.isfile(resolved):
        return [_register_file(resolved, default_app)]
    raise SourceError("no such record file or directory: %s" % resolved)


def register_env(value=None, default_app=None):
    raw = os.environ.get(ENV_VAR) if value is None else value
    found = []
    for piece in (raw or "").split(os.pathsep):
        if piece.strip():
            found.extend(register(piece.strip(), default_app))
    return found


def add_argument(parser):
    parser.add_argument(
        "--file", action="append", default=[], dest="files", metavar="PATH",
        help="a record .json, or a directory of <app>/<name>.json (repeatable)",
    )


def refs():
    return list(_ORDER)


def apps():
    seen = []
    for ref in _ORDER:
        app = ref.rpartition(SEPARATOR)[0]
        if app not in seen:
            seen.append(app)
    return seen


def path_for(ref):
    return _PATHS.get(ref)


def is_external(ref):
    return ref in _PATHS


def forget_cache(ref=None):
    if ref is None:
        _CACHE.clear()
    else:
        _CACHE.pop(ref, None)


def record_for(ref):
    path = _PATHS.get(ref)
    if path is None:
        return None
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    cached = _CACHE.get(ref)
    if cached and cached[0] == stamp:
        return cached[1]
    payload = _read(path)
    app, _, name = ref.rpartition(SEPARATOR)
    # the ref is the identity: a payload that disagrees would make `save` compute a different
    # ref, miss its own registered path, and mint a stray built-in instead of writing back
    payload["app"] = app
    payload["name"] = name
    _CACHE[ref] = (stamp, payload)
    return payload


def records_for(app):
    prefix = app + SEPARATOR
    found = {}
    for ref in _ORDER:
        if not ref.startswith(prefix):
            continue
        payload = record_for(ref)
        if payload is not None:
            found[ref[len(prefix):]] = payload
    return found


def all_records():
    found = {}
    for ref in _ORDER:
        payload = record_for(ref)
        if payload is not None:
            found[ref] = payload
    return found


def refs_named(name):
    return [ref for ref in _ORDER if ref.rpartition(SEPARATOR)[2] == name]
