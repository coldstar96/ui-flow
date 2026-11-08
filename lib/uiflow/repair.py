import copy
import datetime
import difflib
import json

from uiflow import localize, timing

FUZZY_THRESHOLD = 0.6
TYPE_MISMATCH_PENALTY = 0.85
REPAIRABLE_REASONS = ("notFound", "stillPresent", "timedOut")
CARRIED_KEYS = ("app", "type", "descendant")


class RepairError(ValueError):
    pass


def parse_snapshot(lines):
    if isinstance(lines, str):
        lines = lines.splitlines()
    parsed = []
    for line in lines or []:
        if isinstance(line, dict):
            parsed.append(line)
            continue
        try:
            payload = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            parsed.append(payload)
    return parsed


def _ratio(left, right):
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, str(left), str(right)).ratio()


def _propose(selector, element):
    proposed = {key: selector[key] for key in CARRIED_KEYS if selector.get(key) is not None}
    if element.get("app"):
        proposed["app"] = element["app"]
    if element.get("type"):
        proposed["type"] = element["type"]
    if element.get("id"):
        proposed["id"] = element["id"]
        return proposed, "id"
    label = element.get("label")
    if not label:
        return None, None
    # A truncated label can never match exactly, so it may only ever become labelContains (C12).
    proposed["labelContains" if element.get("truncated") else "label"] = label
    return proposed, "label"


def candidates(selector, elements, limit=5, sent=None):
    selector = selector or {}
    scoring = sent or selector
    scored = []
    for element in elements or []:
        if scoring.get("app") and element.get("app") and element["app"] != scoring["app"]:
            continue
        score = 0.0
        if scoring.get("id") and element.get("id"):
            score = max(score, 1.0 if element["id"] == scoring["id"] else _ratio(scoring["id"], element["id"]))
        if scoring.get("idPrefix") and element.get("id"):
            score = max(score, 0.9 if str(element["id"]).startswith(scoring["idPrefix"]) else 0.0)
        wanted_labels = [value for value in (
            [scoring.get("label"), scoring.get("labelContains")]
            + list(scoring.get("labelAnyOf") or [])
            + list(scoring.get("labelContainsAnyOf") or [])
        ) if value]
        if wanted_labels and element.get("label"):
            for wanted in wanted_labels:
                if element["label"] == wanted or wanted in element["label"]:
                    score = max(score, 1.0)
                else:
                    score = max(score, _ratio(wanted, element["label"]))
        if scoring.get("valueEquals") and element.get("value") == scoring["valueEquals"]:
            score = max(score, 0.8)
        if scoring.get("type") and element.get("type") and element["type"] != scoring["type"]:
            score *= TYPE_MISMATCH_PENALTY
        if score <= FUZZY_THRESHOLD:
            continue
        proposed, basis = _propose(selector, element)
        if not proposed or proposed == selector:
            continue
        scored.append({"selector": proposed, "score": round(score, 4), "basis": basis})
    scored.sort(key=lambda candidate: (-candidate["score"], candidate["basis"] != "id"))
    deduped = []
    for candidate in scored:
        if candidate["selector"] not in [existing["selector"] for existing in deduped]:
            deduped.append(candidate)
    return deduped[:limit]


def canonical_selector(selector, oracle, required=False):
    """A proposed selector with its labels mapped back to English. None means: do not persist it.

    A repair is read off whatever screen failed, so on a localized simulator it proposes localized
    text; writing that into a record would pin the record to one language. `required` says the
    screen was localized — having no oracle is then a reason to refuse, not to wave it through.
    """
    if not oracle:
        return None if required else selector
    mapped, ambiguous = localize.canonicalize(selector, oracle)
    return None if ambiguous else mapped


def step_at(record, path):
    if not path:
        raise RepairError("empty repair path")
    steps = record.get("steps") or []
    step = None
    for depth, index in enumerate(path):
        if not isinstance(index, int) or index < 0 or index >= len(steps):
            raise RepairError("no step at path %s in record %r" % (path, record.get("name")))
        step = steps[index]
        if depth < len(path) - 1:
            steps = step.get("steps") or []
    return step


def patch(record, path, selector, note=None):
    patched = copy.deepcopy(record)
    step = step_at(patched, path)
    previous = step.get("sel")
    step["sel"] = selector
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    health = patched.setdefault("health", {"runs": 0, "failures": 0, "lastOk": None, "repairedAt": None})
    health["repairedAt"] = stamp
    patched.setdefault("history", []).append(
        "%s repaired step %s: %s -> %s%s"
        % (
            stamp,
            timing.path_key(path),
            json.dumps(previous, ensure_ascii=False, sort_keys=True),
            json.dumps(selector, ensure_ascii=False, sort_keys=True),
            " (%s)" % note if note else "",
        )
    )
    prefix = timing.path_key(path) + "|"
    table = patched.get("timing") or {}
    for key in [existing for existing in table if existing.startswith(prefix)]:
        del table[key]
    return patched
