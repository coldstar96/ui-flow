import copy
import datetime
import os
import re

from uiflow import compose, index, journal, records, sources, timing

NAME_CLEAN = re.compile(r"[^a-z0-9]+")
MAX_NAME = 48


class NameTaken(ValueError):
    pass


def fragment_app(steps, fallback):
    apps = set()
    for step in steps or []:
        apps.add((step.get("sel") or {}).get("app") or step.get("app"))
    apps.discard(None)
    return apps.pop() if len(apps) == 1 else fallback


def suggest_name(steps):
    for step in steps or []:
        selector = step.get("sel") or {}
        label = selector.get("label") or selector.get("labelContains") or selector.get("id")
        if label:
            cleaned = NAME_CLEAN.sub("-", str(label).lower())[:MAX_NAME].strip("-")
            if cleaned:
                return cleaned
    return "fragment-%s" % index.run_hash([index.step_hash(step) for step in steps or []])[:8]


def proposals(app=None, min_run=index.MIN_RUN, payload=None):
    payload = payload if payload is not None else index.load()
    found = []
    claimed = {}
    for entry in index.duplicate_runs(payload, app=app, min_run=min_run):
        occurrences = []
        local = {}
        for occurrence in entry["occurrences"]:
            span = set(range(occurrence["start"], occurrence["start"] + entry["length"]))
            taken = claimed.setdefault(occurrence["ref"], set())
            mine = local.setdefault(occurrence["ref"], set())
            if taken & span or mine & span:
                continue
            mine |= span
            occurrences.append(occurrence)
        if len({occurrence["ref"] for occurrence in occurrences}) < 2:
            continue
        first = occurrences[0]
        owner = records.load_builtin_ref(first["ref"])
        steps = copy.deepcopy(owner["steps"][first["start"]:first["start"] + entry["length"]])
        if any(step.get("op") == compose.USE_OP for step in steps):
            continue
        for occurrence in occurrences:
            claimed[occurrence["ref"]].update(range(occurrence["start"], occurrence["start"] + entry["length"]))
        found.append({
            "name": suggest_name(steps),
            "app": fragment_app(steps, records.parse_ref(first["ref"])[0]),
            "length": entry["length"],
            "steps": steps,
            "occurrences": occurrences,
        })
    return found


def rekey_timing(table, start, length):
    caller, fragment = {}, {}
    end = start + length
    for key, value in (table or {}).items():
        path, _, digest = key.partition("|")
        head = path.split(".")
        try:
            first = int(head[0])
        except (ValueError, IndexError):
            continue
        rest = head[1:]
        entry = copy.deepcopy(value)
        if start <= first < end:
            moved = [str(first - start)] + rest
            fragment["%s|%s" % (".".join(moved), digest)] = entry
        elif first >= end:
            moved = [str(first - length + 1)] + rest
            caller["%s|%s" % (".".join(moved), digest)] = entry
        else:
            caller[key] = entry
    return caller, fragment


def merge_timing_entries(existing, incoming):
    sides = [timing.unpack(value) for value in (existing, incoming) if value is not None]
    hits = []
    for side in sides:
        hits.extend(side.get("hits") or [])
    merged = {"hits": hits[-timing.MAX_SAMPLES:]}
    # min, not sum: fewer misses is the larger, more cautious absent_budget.
    misses = min((side.get("misses") or 0 for side in sides), default=0)
    if misses:
        merged["misses"] = misses
    return merged


def name_taken(app, name):
    return os.path.exists(records.path_for(app, name)) or sources.is_external(
        records.qualify(app, name)
    )


def free_name(app, name, steps, taken=()):
    candidate = records.safe_name(name)
    if candidate not in taken and not name_taken(app, candidate):
        return candidate
    suffix = index.run_hash([index.step_hash(step) for step in steps])[:6]
    return records.safe_name("%s-%s" % (candidate, suffix))


def apply(proposal, name=None, taken=()):
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    app = proposal["app"]
    if name:
        chosen = records.safe_name(name)
        if name_taken(app, chosen):
            raise NameTaken("%s already exists; pick another --name" % records.qualify(app, chosen))
    else:
        chosen = free_name(app, proposal["name"], proposal["steps"], taken)
        if name_taken(app, chosen):
            raise NameTaken(
                "both %s and its disambiguated form are taken; pass --name"
                % records.qualify(app, proposal["name"])
            )
    ref = records.qualify(app, chosen)
    fragment = journal.to_record(
        app,
        chosen,
        copy.deepcopy(proposal["steps"]),
        description="Extracted by `uiflow factor` from %s"
                    % ", ".join(sorted({o["ref"] for o in proposal["occurrences"]})),
        history=["%s factored out of %d records" % (stamp, len(proposal["occurrences"]))],
        kind=records.FRAGMENT,
    )

    spans = {}
    for occurrence in proposal["occurrences"]:
        spans.setdefault(occurrence["ref"], []).append(occurrence["start"])
    rewritten = []
    for caller_ref, starts in spans.items():
        caller = copy.deepcopy(records.load_builtin_ref(caller_ref))
        table = caller.get("timing") or {}
        for start in sorted(starts, reverse=True):
            length = proposal["length"]
            caller["steps"] = (
                caller["steps"][:start]
                + [{"op": compose.USE_OP, "ref": ref}]
                + caller["steps"][start + length:]
            )
            table, moved = rekey_timing(table, start, length)
            for key, entry in moved.items():
                fragment["timing"][key] = merge_timing_entries(fragment["timing"].get(key), entry)
        caller["timing"] = table
        caller.setdefault("history", []).append(
            "%s factor extracted %d run(s) of %d steps into %s"
            % (stamp, len(starts), proposal["length"], ref)
        )
        rewritten.append(caller)

    path = records.save(fragment, external=False)
    rewrote = [records.save(caller, external=False) for caller in rewritten]
    records.invalidate()
    return {
        "fragment": path,
        "ref": ref,
        "rewrote": rewrote,
        "name": chosen,
        "warnings": ["no satisfiedWhen — %s can never be skipped, which is the single most "
                     "valuable property a fragment has. Re-save it with --satisfied-when." % ref],
    }
