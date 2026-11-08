import errno
import os
import plistlib
import re
import sqlite3
import urllib.parse

from uiflow import paths, simctl

VALUE_CAP = 200
LPROJ = ".lproj"
ENGLISH_LPROJ = "en" + LPROJ
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8}){0,3}$")
TEXT_ENTRY = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;')
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\", "0": "\0"}
HEX = re.compile(r"^[0-9a-fA-F]{4}$")
SCRIPT_REGIONS = {"hans": ("CN",), "hant": ("TW", "HK")}


def normalize_lang(tag):
    """Every `.lproj` name worth trying for a language tag, most specific first.

    A tag that is not a language is refused rather than normalized: it reaches a filesystem path.
    """
    text = str(tag or "").strip()
    if not text or not LANG_RE.match(text):
        return []
    parts = re.split(r"[-_]", text)
    found = []
    for size in range(len(parts), 0, -1):
        for separator in ("-", "_"):
            candidate = separator.join(parts[:size])
            if candidate not in found:
                found.append(candidate)
    # A script subtag names a directory the runtime barely populates: iOS 26.5 ships
    # `zh-Hans.lproj` in 3 bundles and `zh_CN.lproj` in 2,763. Resolving the script spelling alone
    # builds a near-empty store, so the conventional region spellings are tried too.
    for region in SCRIPT_REGIONS.get(parts[1].lower(), ()) if len(parts) > 1 else ():
        for separator in ("_", "-"):
            candidate = separator.join([parts[0], region])
            if candidate not in found:
                found.append(candidate)
    # `zh-Hans-CN` must also reach `zh_CN.lproj`, which is no prefix of it.
    if len(parts) > 2:
        for separator in ("-", "_"):
            candidate = separator.join([parts[0], parts[-1]])
            if candidate not in found:
                found.append(candidate)
    return found


def is_english(tag):
    """The language subtag, never the tail: `en-Latn-US` normalizes to a list ending in `en_US`."""
    candidates = normalize_lang(tag)
    return bool(candidates) and re.split(r"[-_]", candidates[0])[0].lower() == "en"


def _unescape(text):
    out = []
    position = 0
    while position < len(text):
        char = text[position]
        if char != "\\" or position + 1 >= len(text):
            out.append(char)
            position += 1
            continue
        following = text[position + 1]
        if following in ("U", "u") and HEX.match(text[position + 2:position + 6] or ""):
            code = int(text[position + 2:position + 6], 16)
            position += 6
            if 0xD800 <= code <= 0xDBFF and text[position:position + 2].lower() == "\\u" \
                    and HEX.match(text[position + 2:position + 6] or ""):
                low = int(text[position + 2:position + 6], 16)
                if 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    position += 6
            # A surrogate that never paired cannot be encoded, and sqlite raises on the whole batch.
            out.append(chr(code) if not 0xD800 <= code <= 0xDFFF else "\ufffd")
            continue
        out.append(ESCAPES.get(following, following))
        position += 2
    return "".join(out)


def _parse_text(path):
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                body = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        found = TEXT_ENTRY.findall(body)
        if found:
            return {_unescape(key): _unescape(value) for key, value in found}
    return {}


def load_strings(path):
    """A `.strings` catalogue as a plain dict. Unreadable is empty, never an exception."""
    try:
        with open(path, "rb") as handle:
            payload = plistlib.load(handle)
    except Exception:
        payload = _parse_text(path)
    if not isinstance(payload, dict):
        return {}
    return {key: value for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)}


def localized_files(root, lang):
    wanted = {name + LPROJ for name in normalize_lang(lang)}
    if not wanted or not os.path.isdir(root):
        return []
    found = []
    for parent, directories, files in os.walk(root):
        if parent.endswith(LPROJ):
            directories[:] = []
            if os.path.basename(parent) in wanted:
                found.extend(os.path.join(parent, name)
                             for name in files if name.endswith(".strings"))
    return sorted(found)


def iter_pairs(roots, lang, cap=VALUE_CAP):
    """`(english, localized)` for every catalogue entry that has both, deduplicated."""
    seen = set()
    for root in roots or []:
        for localized in localized_files(root, lang):
            english = os.path.join(
                os.path.dirname(os.path.dirname(localized)), ENGLISH_LPROJ,
                os.path.basename(localized),
            )
            if not os.path.isfile(english):
                continue
            source = load_strings(english)
            if not source:
                continue
            target = load_strings(localized)
            for key, value in source.items():
                translated = target.get(key)
                if not translated or value == translated:
                    continue
                if len(value) > cap or len(translated) > cap:
                    continue
                pair = (value, translated)
                if pair not in seen:
                    seen.add(pair)
                    yield pair


SCHEMA_VERSION = "1"
SYSTEM_SUBDIRS = (
    os.path.join("System", "Library", "PreferenceBundles"),
    os.path.join("System", "Library", "PrivateFrameworks"),
    os.path.join("System", "Library", "Frameworks"),
    "Applications",
)
SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def system_roots(runtime_root):
    return [os.path.join(runtime_root, part) for part in SYSTEM_SUBDIRS]


def store_path(build, lang, tag=None):
    parts = [SAFE_COMPONENT.sub("_", str(part)) for part in (build or "unknown", lang, tag) if part]
    return os.path.join(paths.STRINGS_DIR, "-".join(parts) + ".db")


def build_store(path, roots, lang):
    """Build the pair store beside `path`, then move it into place. A half-built file is never read."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.remove(tmp)
    except OSError:
        pass
    try:
        connection = sqlite3.connect(tmp)
        try:
            connection.execute("CREATE TABLE pair (en TEXT NOT NULL, loc TEXT NOT NULL, "
                               "PRIMARY KEY (en, loc)) WITHOUT ROWID")
            connection.execute("CREATE INDEX pair_loc ON pair (loc)")
            connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.executemany("INSERT OR IGNORE INTO pair (en, loc) VALUES (?, ?)",
                                   iter_pairs(roots, lang))
            connection.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                                   [("schema", SCHEMA_VERSION), ("lang", lang)])
            connection.commit()
        finally:
            connection.close()
        os.replace(tmp, path)
    except BaseException:
        # A ~21 MB tmp per failed build, re-paid every run, is how the cache directory grows without
        # anything ever being readable.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def _usable(path):
    try:
        connection = sqlite3.connect(
            "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(path)), uri=True)
        schema = connection.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.Error:
        return None
    if not schema or schema[0] != SCHEMA_VERSION:
        connection.close()
        return None
    return connection


_unbuildable = set()


def open_store(path, roots, lang):
    connection = _usable(path) if os.path.isfile(path) else None
    if connection:
        # `prune` orders by mtime, so without this an in-use store ages out and rebuilds every run.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return connection
    directory = os.path.dirname(os.path.abspath(path))
    if directory in _unbuildable:
        return None
    build_store(path, roots, lang)
    connection = _usable(path)
    if connection is None:
        # A store unreadable the instant it was written is an environment fault, not a stale cache:
        # rebuilding it again costs seconds and cannot succeed.
        _unbuildable.add(directory)
    return connection


TMP_SUFFIX = re.compile(r"\.(\d+)\.tmp$")


def _abandoned(name):
    found = TMP_SUFFIX.search(name)
    if not found:
        return False
    try:
        os.kill(int(found.group(1)), 0)
    except OverflowError:
        return True
    except OSError as error:
        return error.errno != errno.EPERM
    return False


def _age(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def prune(keep=6, protect=()):
    """The stores are rebuildable; nothing else reaps them, and one language is megabytes."""
    protected = {os.path.abspath(path) for path in protect}
    try:
        names = os.listdir(paths.STRINGS_DIR)
    except OSError:
        return
    found = []
    for name in names:
        path = os.path.join(paths.STRINGS_DIR, name)
        if _abandoned(name):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if name.endswith(".db") and os.path.abspath(path) not in protected:
            found.append(path)
    found.sort(key=_age, reverse=True)
    for path in found[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass


class Oracle:
    """Sources in priority order — the app's own catalogue outranks the system's."""

    def __init__(self, lang, connections):
        self.lang = lang
        self.connections = list(connections)

    def _column(self, sql, value):
        found = []
        for connection in self.connections:
            try:
                rows = connection.execute(sql, (value,)).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                if row[0] not in found:
                    found.append(row[0])
        return found

    def translate(self, label):
        return self._column("SELECT loc FROM pair WHERE en = ? ORDER BY loc", label)

    def originals(self, label):
        return self._column("SELECT en FROM pair WHERE loc = ? ORDER BY en", label)

    def canonical(self, label):
        """The one English original, or None when the localized string has several.

        Alphabetical order is not evidence: on a real store 확인 has 14 originals and `[0]` is
        `Acknowledge`, not `OK`. A record is unversioned user data, so a guess is unrecoverable.
        """
        found = self.originals(label)
        return found[0] if len(found) == 1 else None

    def knows_english(self, label):
        return bool(self._column("SELECT en FROM pair WHERE en = ? LIMIT 1", label))

    def knows_localized(self, label):
        return bool(self._column("SELECT loc FROM pair WHERE loc = ? LIMIT 1", label))

    def close(self):
        for connection in self.connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self.connections = []


def oracle(udid, app=None, lang=None, listing=None):
    """The translation oracle for a simulator's UI language, or None when there is nothing to do."""
    if not udid:
        return None
    lang = lang or simctl.ui_language(udid)
    if not normalize_lang(lang) or is_english(lang):
        return None
    runtime = simctl.runtime_for(udid, listing=listing)
    if not runtime:
        return None
    opened = []
    live = []
    system = store_path(runtime["build"], lang)
    if app:
        bundle = simctl.app_bundle_path(udid, app)
        if bundle:
            tag = "%s-%s" % (app, os.path.basename(os.path.dirname(bundle)))
            path = store_path(runtime["build"], lang, tag)
            connection = open_store(path, [bundle], lang)
            if connection:
                opened.append(connection)
                live.append(path)
    connection = open_store(system, system_roots(runtime["root"]), lang)
    if connection:
        opened.append(connection)
        live.append(system)
    if not opened:
        return None
    # Every store this call holds open, not just the system one: reaping the app store seconds
    # after opening it rebuilds it on the next run, and the next, forever.
    prune(protect=live)
    return Oracle(lang, opened)
