import copy
import hashlib
import json
import math

MIN_SAMPLES = 3
MAX_SAMPLES = 20
FLOOR_MS = 400
CEILING_MS = 15000
DECAY = 0.6
MISS_CONFIDENCE = 4
ABSENT_FLOOR = 400

CHILD_KEYS = ("steps",)

# Ops whose `skipped` genuinely means the element was not on screen. `setSwitch` also skips,
# but only after resolving the row, so its authored timeout is a budget it really needs.
ABSENT_OPS = ("tapIfPresent",)


def fingerprint(step):
    payload = {key: value for key, value in (step or {}).items() if key not in ("timeoutMs", "name")}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:12]


def path_key(path):
    return ".".join(str(index) for index in path)


def entry_key(path, step):
    return "%s|%s" % (path_key(path), fingerprint(step))


def percentile(samples, fraction):
    ordered = sorted(samples)
    rank = max(1, int(math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def budget_ms(samples):
    if not samples:
        return FLOOR_MS
    return int(max(FLOOR_MS, min(CEILING_MS, percentile(samples, 0.95) * 3 + 250)))


def absent_budget(authored, misses):
    steps_down = max(1, misses - MISS_CONFIDENCE + 1)
    return int(min(authored, max(ABSENT_FLOOR, authored * DECAY ** steps_down)))


def budget_for(step, item):
    """None means leave the authored value alone."""
    hits = (item or {}).get("hits") or []
    if len(hits) >= MIN_SAMPLES:
        return budget_ms(hits)
    if hits:
        return None
    authored = (step or {}).get("timeoutMs")
    misses = (item or {}).get("misses") or 0
    if authored and misses >= MISS_CONFIDENCE:
        return absent_budget(authored, misses)
    return None


def table(record):
    return record.setdefault("timing", {})


def unpack(value):
    if isinstance(value, list):
        return {"hits": list(value)}
    return dict(value or {})


def entry(record, path, step):
    return unpack((record.get("timing") or {}).get(entry_key(path, step)))


def samples(record, path, step):
    return list(entry(record, path, step).get("hits") or [])


def walk(steps, prefix=()):
    for index, step in enumerate(steps or []):
        path = list(prefix) + [index]
        yield path, step
        for key in CHILD_KEYS:
            for inner in walk(step.get(key), path):
                yield inner


def apply(record):
    applied = copy.deepcopy(record)
    stored = record.get("timing") or {}
    for path, step in walk(applied.get("steps")):
        budget = budget_for(step, unpack(stored.get(entry_key(path, step))))
        if budget is not None:
            step["timeoutMs"] = budget
    return applied


def observe(steps, results, prefix=()):
    hits, absent = {}, set()
    _observe(steps, results, list(prefix), hits, absent)
    return hits, absent


def _observe(steps, results, prefix, hits, absent):
    for index, step, runs in _by_index(steps, results):
        path = list(prefix) + [index]
        key = entry_key(path, step)
        resolved = _resolved_ms(runs)
        if resolved is not None:
            hits[key] = resolved
        elif _absent_in_every_run(step, runs):
            absent.add(key)
        for child_key in CHILD_KEYS:
            if step.get(child_key):
                _observe(step[child_key], _children(runs), path, hits, absent)


def _by_index(steps, results):
    grouped = {}
    for child in results or []:
        index = child.get("index", -1)
        if index >= 0:
            grouped.setdefault(index, []).append(child)
    for index, step in enumerate(steps or []):
        runs = grouped.get(index)
        if runs:
            yield index, step, runs


def _children(runs):
    """Every cycle's children flattened, so a repeated step groups across cycles one level down."""
    flat = []
    for run in runs:
        flat.extend(run.get("children") or [])
    return flat


def _resolved_ms(runs):
    done = [run for run in runs if run.get("ok") and not run.get("skipped") and isinstance(run.get("ms"), int)]
    return done[-1]["ms"] if done else None


def _absent_in_every_run(step, runs):
    if (step or {}).get("op") not in ABSENT_OPS:
        return False
    if any(step.get(child_key) for child_key in CHILD_KEYS):
        return False
    return all(run.get("ok") and run.get("skipped") for run in runs)


def merge(record, fresh, absent=None):
    updated = copy.deepcopy(record)
    stored = table(updated)
    live = {entry_key(path, step) for path, step in walk(updated.get("steps"))}
    for key in list(stored):
        if key not in live:
            del stored[key]
        else:
            stored[key] = unpack(stored[key])
    for key, value in (fresh or {}).items():
        if key not in live:
            continue
        item = stored.setdefault(key, {})
        series = list(item.get("hits") or [])
        series.append(value)
        item["hits"] = series[-MAX_SAMPLES:]
    for key in absent or ():
        if key not in live:
            continue
        item = stored.setdefault(key, {})
        item["misses"] = (item.get("misses") or 0) + 1
    return updated


def learn(record, result):
    hits, absent = observe(record.get("steps"), (result or {}).get("children"))
    return merge(record, hits, absent=absent)


def collect_all(steps, result, origins, base=(), owners=None):
    collected, missing = {}, {}
    _walk_owned(steps, (result or {}).get("children"), origins, tuple(base), collected, missing, owners or {})
    return collected, missing


def collect(steps, result, origins, base=(), owners=None):
    return collect_all(steps, result, origins, base, owners)[0]


def _walk_owned(steps, results, origins, base, collected, missing, owners):
    for index, step, runs in _by_index(steps, results):
        here = base + (index,)
        owner = origins.get(here)
        if owner:
            ref, owner_path = owner
            key = entry_key(list(owner_path), owners.get(here, step))
            resolved = _resolved_ms(runs)
            if resolved is not None:
                collected.setdefault(ref, {})[key] = resolved
            elif _absent_in_every_run(step, runs):
                missing.setdefault(ref, set()).add(key)
        for child_key in CHILD_KEYS:
            if step.get(child_key):
                _walk_owned(step[child_key], _children(runs), origins, here, collected, missing, owners)
