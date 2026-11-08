import copy

from uiflow import records

USE_OP = "use"
MAX_DEPTH = 8
PROBE_KEYS = ("satisfiedWhen", "verifies", "reassert", "until")
STEP_KEYS = ("steps",) + PROBE_KEYS


class DepthError(ValueError):
    pass


class MisplacedUseError(ValueError):
    pass


class UnsatisfiableRequiresError(ValueError):
    pass


def refs_of(record):
    found = []

    def walk(steps):
        for step in steps or []:
            if step.get("op") == USE_OP:
                ref = step.get("ref")
                if ref and ref not in found:
                    found.append(ref)
            walk(step.get("steps"))

    walk((record or {}).get("steps"))
    return found


def _reject_probe_uses(record):
    def walk(steps):
        for step in steps or []:
            for key in PROBE_KEYS:
                for probe in step.get(key) or []:
                    if probe.get("op") == USE_OP:
                        raise MisplacedUseError(
                            "record %r has a `use` inside %s; `use` is only valid inside `steps`"
                            % (record.get("name"), key)
                        )
            walk(step.get("steps"))

    for key in PROBE_KEYS:
        for probe in record.get(key) or []:
            if probe.get("op") == USE_OP:
                raise MisplacedUseError(
                    "record %r has a `use` inside %s; `use` is only valid inside `steps`"
                    % (record.get("name"), key)
                )
    walk(record.get("steps"))


def expand(record, app=None, prepare=None):
    origins = {}
    owners = {}
    steps = _expand_record(record, app, prepare, origins, owners, base=(), stack=[], depth=0)
    return steps, origins, owners


def expanded_group(record, app=None, prepare=None):
    steps, origins, owners = expand(record, app, prepare)
    group = records.to_group(dict(record, steps=steps))
    return group, origins, owners


def _expand_record(record, app, prepare, origins, owners, base, stack, depth):
    if depth > MAX_DEPTH:
        raise DepthError("`use` nesting deeper than %d: %s" % (MAX_DEPTH, " -> ".join(stack)))
    prepared = (prepare or (lambda payload: payload))(record)
    _reject_probe_uses(prepared)
    if prepared.get("requires") and depth > 0:
        raise UnsatisfiableRequiresError(
            "record %r is inlined by `use` but declares requires %s; only a top-level record may "
            "carry prerequisites" % (prepared.get("name"), prepared["requires"])
        )
    app = prepared.get("app") or app
    ref = records.qualify(app, prepared["name"])
    if ref in stack:
        raise records.CycleError("record cycle: %s" % " -> ".join(stack + [ref]))
    return _expand_steps(
        prepared.get("steps"), record.get("steps"), ref, app, prepare, origins, owners,
        base, (), stack + [ref], depth
    )


def _expand_steps(steps, raw_steps, ref, app, prepare, origins, owners, base, owner_base,
                  stack, depth):
    expanded = []
    raw_steps = raw_steps or []
    for index, step in enumerate(steps or []):
        here = base + (len(expanded),)
        owner_here = owner_base + (index,)
        # the on-disk step: a prepared one carries injected child budgets, so its fingerprint
        # no longer matches the key `timing.merge` computes from the record.
        raw = raw_steps[index] if index < len(raw_steps) else step
        origins[here] = (ref, owner_here)
        owners[here] = raw
        if step.get("op") == USE_OP:
            fragment = records.load_ref(step.get("ref"), app)
            inner = _expand_record(fragment, app, prepare, origins, owners, here, stack, depth + 1)
            group = records.to_group(dict(fragment, steps=inner))
            group["name"] = "use:%s" % records.qualify(fragment.get("app") or app, fragment["name"])
            expanded.append(group)
            continue
        copied = copy.deepcopy(step)
        if copied.get("steps"):
            copied["steps"] = _expand_steps(
                step["steps"], raw.get("steps"), ref, app, prepare, origins, owners,
                here, owner_here, stack, depth
            )
        expanded.append(copied)
    return expanded
