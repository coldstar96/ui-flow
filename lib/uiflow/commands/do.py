import copy
import datetime
import json
import re
import sys
import time

from uiflow import (channel, compose, lint, localize, metrics, paths, records, repair, report,
                    session, simctl, sources, strings, timing)

PROBE_TIMEOUT_MS = 500

NAME = "do"
HELP = "replay one or more learned records as a single batch"


def add_arguments(parser):
    parser.add_argument("app", help="bundle id the records belong to")
    parser.add_argument("names", nargs="+", help="record names or aliases")
    parser.add_argument("--udid", default=None)
    parser.add_argument("--pace", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=channel.DEFAULT_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--no-learn", action="store_true", dest="no_learn")
    parser.add_argument("--auto-repair", action="store_true", dest="auto_repair")
    parser.add_argument("--no-repair", action="store_true", dest="no_repair")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--no-report", action="store_true", dest="no_report")
    parser.add_argument("--report-dir", default=None, dest="report_dir")
    parser.add_argument("--no-localize", action="store_true", dest="no_localize")
    sources.add_argument(parser)


def count_ops(steps):
    total = 0
    for step in steps or []:
        total += 1
        for key in compose.STEP_KEYS:
            total += count_ops(step.get(key))
    return total


def launch_args(steps):
    found = []
    for step in steps or []:
        if step.get("op") == "launch" and step.get("args"):
            found.append("%s %s" % (step.get("app") or "?", " ".join(step["args"])))
        for key in compose.STEP_KEYS:
            found.extend(launch_args(step.get(key)))
    return found


def expanded_view(record, app):
    refs = [] if not app else [records.qualify(app, record["name"])]
    if not app:
        return record.get("steps"), refs, {}
    try:
        steps, origins, _owners = compose.expand(record, app)
    except (LookupError, ValueError):
        return record.get("steps"), refs, {}
    for ref, _owner_path in origins.values():
        if ref not in refs:
            refs.append(ref)
    return steps, refs, origins


def plan_summary(resolved, app=None):
    lines = []
    for record in resolved:
        steps, refs, _origins = expanded_view(record, record.get("app") or app)
        parts = ["%s: %d ops" % (record["name"], count_ops(steps))]
        if not record.get("satisfiedWhen"):
            parts.append("no satisfiedWhen (cannot be skipped)")
        repaired = (record.get("health") or {}).get("repairedAt")
        if repaired:
            parts.append("auto-repaired %s" % repaired)
        for launched in launch_args(steps):
            parts.append("launch args: %s" % launched)
        for ref in refs:
            external = sources.path_for(ref)
            if external:
                parts.append("from %s %s" % (ref, external))
        lines.append("plan " + " | ".join(parts))
    return lines


def first_match_index(reply):
    if not (reply or {}).get("ok"):
        return None
    results = reply.get("results") or [{}]
    info = results[0].get("info") or ""
    found = re.search(r"index=(-?\d+)", info) or re.search(r"(-?\d+)", info)
    if not found:
        return None
    index = int(found.group(1))
    return None if index < 0 else index


def ask(candidate):
    if not sys.stdin.isatty():
        return False
    answer = input(
        "repair on a label from app-supplied on-screen text: %s\napply it? [y/N] "
        % json.dumps(candidate["selector"], ensure_ascii=False)
    )
    return answer.strip().lower() in ("y", "yes")


def owner_at(origins, record_index, inner_path):
    expansion = (origins or {}).get(record_index) or {}
    return (expansion.get("map") or {}).get(tuple(inner_path))


def repair_once(resolved, reply, probe=None, save=None, auto_repair=False, confirm=None, echo=print,
                udid=None, positions=None, origins=None, oracle=None, english_only=False):
    error = (reply or {}).get("error") or {}
    if error.get("reason") not in repair.REPAIRABLE_REASONS:
        return None
    path = list(error.get("failedPath") or [])
    if len(path) < 2:
        return None
    owner = path[0] if positions is None else report.record_index(positions, path[0])
    if owner is None or owner >= len(resolved):
        return None
    caller = resolved[owner]
    inner = path[1:]
    target = owner_at(origins, owner, inner)
    if target:
        ref, owner_path = target
        try:
            record = records.load_ref(ref)
        except records.MissingRecord:
            return None
        local = list(owner_path)
    else:
        record, local = caller, list(inner)
    try:
        step = repair.step_at(record, local)
    except repair.RepairError:
        return None
    selector = step.get("sel")
    if not selector:
        return None
    sent = localize.expand(selector, oracle.translate)[0] if oracle else None
    found = repair.candidates(selector, repair.parse_snapshot(error.get("snapshot")), sent=sent)
    if not found:
        echo("no repair candidate for %s step %s" % (record["name"], ".".join(str(i) for i in local)))
        return None
    probe = probe or (lambda steps, **kwargs: channel.send(steps, udid=udid, timeout=30.0))
    probe_reply = probe([{
        "op": "firstMatch",
        "sels": [candidate["selector"] for candidate in found],
        "timeoutMs": PROBE_TIMEOUT_MS,
    }])
    index = first_match_index(probe_reply)
    if index is None or index >= len(found):
        echo("none of the %d repair candidates resolved" % len(found))
        return None
    winner = found[index]
    canonical = repair.canonical_selector(winner["selector"], oracle, required=english_only)
    if canonical is None:
        echo("repair not persisted: no unambiguous English original for %s"
             % json.dumps(winner["selector"], ensure_ascii=False))
        return None
    winner = dict(winner, selector=canonical)
    unknown = unrecognised_labels(canonical, oracle) if english_only else []
    if unknown:
        echo("repair persisted verbatim: the %s catalogue knows neither %s as a translation nor as "
             "an English original, so it cannot be confirmed English"
             % (oracle.lang, ", ".join(json.dumps(text, ensure_ascii=False) for text in unknown)))
    if winner["basis"] != "id" and not auto_repair:
        if not (confirm or ask)(winner):
            echo("repair declined — the winning candidate is label-based, not id-based")
            return None
    patched = repair.patch(
        record, local, winner["selector"], note="basis=%s score=%s" % (winner["basis"], winner["score"])
    )
    (save or records.save)(patched)
    for position, existing in enumerate(resolved):
        if existing.get("name") == patched["name"] and existing.get("app") == patched.get("app"):
            resolved[position] = patched
    echo("repaired %s step %s -> %s" % (
        record["name"], ".".join(str(i) for i in local), json.dumps(winner["selector"], ensure_ascii=False)
    ))
    return patched


def reset_misses(record):
    """A too-tight budget fails downstream of the step that caused it, so the whole record resets."""
    updated = copy.deepcopy(record)
    for item in timing.table(updated).values():
        if isinstance(item, dict) and item.get("misses"):
            item["misses"] = 0
    return updated


def apply_learning(resolved, reply, save=None, positions=None, origins=None, loader=None, repaired=False):
    save = save or records.save
    loader = loader or records.load_ref
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    results = reply.get("results") or []
    known = {records.qualify(record.get("app"), record["name"]): record for record in resolved}
    collected = {}
    missing = {}
    outcomes = {}
    failed = _failed_owner(reply, origins, positions)
    for index, record in enumerate(resolved):
        if positions is not None and index >= len(positions):
            break
        slot = index if positions is None else positions[index]
        if slot >= len(results):
            break
        result = results[slot]
        ref = records.qualify(record.get("app"), record["name"])
        outcomes[ref] = bool(result.get("ok"))
        expansion = (origins or {}).get(index)
        if expansion is None:
            hits, absent = timing.observe(record.get("steps"), result.get("children"))
            collected.setdefault(ref, {}).update(hits)
            missing.setdefault(ref, set()).update(absent)
            continue
        owned = expansion["map"]
        samples, absent_keys = timing.collect_all(
            expansion["steps"], result, owned, owners=expansion["owners"]
        )
        for owner_ref, owned_samples in samples.items():
            collected.setdefault(owner_ref, {}).update(owned_samples)
        for owner_ref, keys in absent_keys.items():
            missing.setdefault(owner_ref, set()).update(keys)
        # a named culprit exonerates every other owner that ran inside the same failing record
        verdict = True if failed else bool(result.get("ok"))
        for owner_ref, _owner_path in owned.values():
            outcomes.setdefault(owner_ref, verdict)
    order = list(collected)
    for ref in outcomes:
        if ref not in order:
            order.append(ref)
    timed_out = ((reply or {}).get("error") or {}).get("reason") in repair.REPAIRABLE_REASONS
    saved = []
    for ref in order:
        record = known.get(ref)
        if record is None:
            try:
                record = loader(ref)
            except (records.MissingRecord, records.BadName):
                continue
        green = ref != failed and outcomes.get(ref, True)
        # a run that died proves nothing about what was on screen, so only a green one counts absence
        updated = timing.merge(record, collected.get(ref) or {}, absent=missing.get(ref) if green else None)
        if not green and timed_out and not repaired:
            updated = reset_misses(updated)
        health = updated.setdefault("health", {"runs": 0, "failures": 0, "lastOk": None, "repairedAt": None})
        health["runs"] = (health.get("runs") or 0) + 1
        if not green:
            health["failures"] = (health.get("failures") or 0) + 1
        else:
            health["lastOk"] = now
        save(updated)
        saved.append(updated)
    return saved


def _failed_owner(reply, origins, positions=None):
    error = (reply or {}).get("error") or {}
    path = list(error.get("failedPath") or [])
    if len(path) < 2 or not origins:
        return None
    owner = path[0] if positions is None else report.record_index(positions, path[0])
    if owner is None or owner not in origins:
        return None
    target = owner_at(origins, owner, path[1:])
    return target[0] if target else None


def unrecognised_labels(selector, oracle):
    """Label text the catalogue cannot place in either direction — persisted, but never silently."""
    if not oracle or not isinstance(selector, dict):
        return []
    found = []
    for key in ("label", "labelContains"):
        text = selector.get(key)
        if isinstance(text, str) and text and not oracle.knows_localized(text) \
                and not oracle.knows_english(text):
            found.append(text)
    inner = selector.get("descendant")
    if isinstance(inner, dict):
        found.extend(unrecognised_labels(inner, oracle))
    return found


def localize_batch(steps, oracle):
    """Localization runs LAST: `build_batch` fingerprints each step to find its learned timing, so
    translating first would fork a timing table per language."""
    if not oracle:
        return steps, [], 0
    return localize.localize(steps, oracle.translate)


ANY_OF_FEATURE = "labelAnyOf"


def resolve_oracle(args, udid, listing, lang=None, echo=None):
    """The oracle to send this batch through, or None — English-only replay is the safe degrade.

    A driver built before `labelAnyOf` parses the key and IGNORES it, and `expand` deletes the
    `label` it replaced: the resulting query carries zero predicates, so a tap lands on the first
    element on screen and an assert passes without checking anything.
    """
    echo = echo or (lambda line: sys.stderr.write(line + "\n"))
    if args.no_localize:
        return None
    try:
        found = strings.oracle(udid, app=args.app, lang=lang, listing=listing)
    except Exception as error:
        echo("no translation oracle: %s" % error)
        return None
    if found and not session.driver_supports(udid, ANY_OF_FEATURE):
        echo("no localization: the live driver does not understand %s — rebuild it with "
             "driver/build.sh. Replaying in English." % ANY_OF_FEATURE)
        found.close()
        return None
    return found


def build_batch(resolved, capture):
    """The batch to send, where each record's own result lands, and who owns each expanded step."""
    groups = []
    origins = {}
    for position, record in enumerate(resolved):
        group, owned, owners = compose.expanded_group(record, record.get("app"), prepare=timing.apply)
        groups.append(group)
        origins[position] = {"map": owned, "owners": owners, "steps": group.get("steps")}
    if not capture:
        return groups, list(range(len(groups))), {}, origins
    steps, positions, captions = report.plan(groups, [record["name"] for record in resolved])
    return steps, positions, captions, origins


def failure_shot(udid=None, send=None):
    """One extra round-trip for the screen the batch died on — the batch itself aborted."""
    try:
        reply = (send or channel.send)([{"op": "screenshot"}], udid=udid, timeout=30.0)
    except Exception:
        return []
    if not reply.get("ok"):
        return []
    path = report.screenshot_path((reply.get("results") or [{}])[0])
    # keep: a failure that leaves the screen untouched has bytes identical to the shot before
    # it, and dedupe would delete the one image a reader of a failed run needs.
    return [{"caption": "FAILURE", "path": path, "keep": True}] if path else []


def write_report(args, resolved, reply, positions, wall, shots, echo=print, started_at=None,
                 sim=None, locale=None):
    directory = args.report_dir or report.default_dir(args.app)
    try:
        path = report.write(
            report.build_context(
                app=args.app,
                names=[record["name"] for record in resolved],
                resolved=resolved,
                reply=reply,
                positions=positions,
                wall_ms=wall,
                shots=shots,
                sim=sim,
                started_at=started_at,
                locale=locale,
            ),
            directory,
        )
        if not args.report_dir:
            report.prune(report.runs_root())
    except Exception as error:
        sys.stderr.write("report failed: %s\n" % error)
        return None
    echo("report %s" % path)
    return path


def stored_fingerprint(value):
    """A fingerprint stored before the smaller-first rule reads `874x402`, and a raw string compare
    would report a device that has not changed as changed."""
    try:
        width, height = str(value).split("x")
        return metrics.fingerprint(width, height)
    except (TypeError, ValueError):
        return None


def owning_refs(paths_found, origins, own_ref):
    """The lint paths index the EXPANDED steps, which is not a position in any file the user can
    edit, so the warning names the records that hold the taps instead."""
    found = []
    for key in paths_found:
        path = tuple(int(part) if part.lstrip("-").isdigit() else part for part in key.split("."))
        ref = own_ref
        while path:
            origin = origins.get(path)
            if origin:
                ref = origin[0]
                break
            path = path[:-1]
        if ref and ref not in found:
            found.append(ref)
    return found


def shape_warnings(records_to_run, udid, app=None):
    """Only a bare tapAt actually depends on the device's shape, so a selector-only record is silent.

    Scans the EXPANDED steps: a bare tapAt reached through a `use` ref is exactly the composed case
    the warning exists for.
    """
    candidates = []
    for record in records_to_run or []:
        was = stored_fingerprint(((record or {}).get("recordedOn") or {}).get("fingerprint"))
        if not was:
            continue
        own_app = record.get("app") or app
        steps, _refs, origins = expanded_view(record, own_app)
        paths_found = lint.shape_dependent(
            steps or [],
            satisfied_when=record.get("satisfiedWhen") or [],
            verifies=record.get("verifies") or [],
        )
        if paths_found:
            own_ref = records.qualify(own_app, record["name"]) if own_app else record["name"]
            candidates.append((record, was, paths_found, owning_refs(paths_found, origins, own_ref)))
    if not candidates:
        return []
    try:
        live = metrics.screen(udid)
    except metrics.MetricsUnavailable:
        return []
    lines = []
    for record, was, paths_found, refs in candidates:
        if live["fingerprint"] == was:
            continue
        lines.append(
            "WARNING: %s was recorded on %s (%s) and this device is %s — %d window-relative tapAt "
            "step(s) with no sel in %s aim at a different control here. Re-record, or give them a "
            "sel."
            % (record.get("name") or "this record",
               (record.get("recordedOn") or {}).get("device") or "another device",
               was, live["fingerprint"], len(paths_found), ", ".join(refs))
        )
    return lines


def run(args):
    resolved = records.resolve(args.app, args.names)
    capture = not args.no_report and not args.dry_run
    steps, positions, captions, origins = build_batch(resolved, capture)
    # A dry run sends no batch, so it needs no device listing and no simulator.
    if args.dry_run:
        for line in plan_summary(resolved, args.app):
            print(line)
        print(json.dumps(steps, ensure_ascii=False))
        return 0
    # One device listing for the whole run, threaded through: resolving, describing and the report
    # each used to fetch their own.
    listing = paths.list_devices()
    udid = args.udid or paths.default_udid(listing)
    sim = paths.describe(udid, listing)
    lang = simctl.ui_language(udid)
    # What language the SCREEN is in decides whether a repair may be persisted; whether an oracle
    # was built (or was asked for at all) decides only whether the batch is translated.
    english_only = bool(lang) and not strings.is_english(lang)
    oracle = resolve_oracle(args, udid, listing, lang=lang)
    try:
        steps, missing, labels = localize_batch(steps, oracle)
        print("sim %s" % paths.sim_line(udid, sim))
        for line in shape_warnings(resolved, udid, args.app):
            sys.stderr.write(line + "\n")
        if oracle:
            print("locale %s%s" % (oracle.lang, "" if not missing else
                  " · %d of %d labels untranslated: %s" % (len(missing), labels, ", ".join(missing[:6]))))
        for line in plan_summary(resolved, args.app):
            print(line)
        started = time.time()
        reply = channel.send(
            steps, udid=udid, pace=args.pace, default_app=args.app, timeout=args.timeout
        )
        wall = int((time.time() - started) * 1000)
        repaired = False
        if not reply.get("ok") and not args.no_repair:
            print("\n".join(channel.format_reply(reply)))
            if repair_once(resolved, reply, auto_repair=args.auto_repair, udid=udid,
                           positions=positions, origins=origins, oracle=oracle,
                           english_only=english_only):
                repaired = True
                steps, positions, captions, origins = build_batch(resolved, capture)
                steps, missing, labels = localize_batch(steps, oracle)
                started = time.time()
                reply = channel.send(
                    steps, udid=udid, pace=args.pace, default_app=args.app, timeout=args.timeout
                )
                wall += int((time.time() - started) * 1000)
        if not args.no_learn:
            try:
                apply_learning(resolved, reply, positions=positions, origins=origins, repaired=repaired)
            except Exception as error:
                sys.stderr.write("learning not saved: %s\n" % error)
        report_path = None
        if capture:
            shots = report.shots_from(reply, captions)
            if not reply.get("ok"):
                shots.extend(failure_shot(udid=udid))
            locale = None if not oracle else {
                "lang": oracle.lang, "untranslated": missing, "labels": labels}
            report_path = write_report(args, resolved, reply, positions, wall, shots,
                                       started_at=report.now_iso(started), sim=sim, locale=locale)
        if args.as_json:
            print(json.dumps(dict(reply, wallClockMs=wall, reportPath=report_path), ensure_ascii=False))
        else:
            print("\n".join(channel.format_reply(reply)))
            print("wallClockMs %d" % wall)
        return 0 if reply.get("ok") else 1
    finally:
        if oracle:
            oracle.close()
