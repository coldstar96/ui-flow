import datetime
import json
import os
import sys

from uiflow import (compose, index, journal, lint, localize, metrics, paths, records, simctl,
                    sources, strings)

NAME = "record"
HELP = "mark the journal, and promote what was driven since into a record"


class RecordRefused(Exception):
    pass


def add_arguments(parser):
    sub = parser.add_subparsers(dest="action", required=True)

    mark = sub.add_parser("mark", help="mark the journal at the current byte offset")
    mark.add_argument("--udid", default=None)

    save = sub.add_parser("save", help="promote the steps journalled since the mark into a record")
    save.add_argument("app")
    save.add_argument("name")
    save.add_argument("--udid", default=None)
    save.add_argument("--description", default="")
    save.add_argument("--alias", action="append", default=[], dest="aliases")
    save.add_argument("--requires", action="append", default=[])
    save.add_argument("--satisfied-when", default=None, dest="satisfied_when", help="a JSON array of steps")
    save.add_argument("--verifies", default=None, help="a JSON array of steps")
    save.add_argument("--verifies-why", default="", dest="verifies_why")
    save.add_argument("--keep-text", action="store_true", dest="keep_text")
    save.add_argument("--canonicalize", action="store_true",
                      help="map observed labels back to English (implied when any look localized)")
    learn = sub.add_parser("keymap", help="learn label -> screen point for an app's custom keys")
    learn.add_argument("app")
    learn.add_argument("--udid", default=None)
    learn.add_argument("--type", default="staticText", dest="element_type")
    learn.add_argument("--probe", default=None, dest="probe_label")

    save.add_argument("--kind", default=records.FLOW, choices=list(records.KINDS))
    save.add_argument("--use", action="append", default=[], dest="use",
                      help="replace a matching run with a `use` of this record (repeatable)")
    save.add_argument("--to", default=None, dest="to_dir",
                      help="write under <dir>/<app>/<name>.json instead of the built-in root")
    sources.add_argument(save)


def matches(steps, app, payload=None, exclude=None, min_run=1):
    payload = payload if payload is not None else index.load()
    fingerprints = [index.step_hash(step) for step in steps or []]
    found = []
    for ref, entry in (payload.get("records") or {}).items():
        if ref == exclude:
            continue
        wanted = entry.get("fingerprints") or []
        if len(wanted) < min_run or len(wanted) > len(fingerprints):
            continue
        for start in range(0, len(fingerprints) - len(wanted) + 1):
            if fingerprints[start:start + len(wanted)] == wanted:
                found.append({"ref": ref, "start": start, "length": len(wanted)})
                break
    found.sort(key=lambda entry: (-entry["length"], entry["ref"]))
    return found


def reuse_report(steps, app, payload=None, exclude=None):
    return matches(steps, app, payload, exclude=exclude, min_run=index.MIN_RUN)


def adopt(steps, ref, app, payload=None):
    for entry in matches(steps, app, payload):
        if entry["ref"] != ref:
            continue
        target = records.load_ref(ref, app)
        if target.get("requires"):
            raise RecordRefused(
                "record %r declares requires %s; a record inlined by `use` may not carry "
                "prerequisites, so the caller could never replay"
                % (ref, ", ".join(str(name) for name in target["requires"]))
            )
        start, length = entry["start"], entry["length"]
        return list(steps[:start]) + [{"op": compose.USE_OP, "ref": ref}] + list(steps[start + length:])
    raise RecordRefused(
        "record %r does not appear as a contiguous run in these %d steps" % (ref, len(steps or []))
    )


def _has_alternation(selector):
    if not isinstance(selector, dict):
        return False
    if any(selector.get(target) for _, target in localize.LABEL_FIELDS):
        return True
    return _has_alternation(selector.get("descendant"))


def needs_canonicalizing(steps):
    """True when a selector carries an alternation the CLI synthesized.

    Foreign TEXT is deliberately not sniffed for: `Buscar`, `Ajustes` and `Allgemein` are all ASCII,
    so a character class can only ever detect a non-Latin script. What language the screen was in is
    a property of the simulator, and `run` reads it from there.
    """
    found = [False]

    def look(selector):
        if _has_alternation(selector):
            found[0] = True
        return selector

    localize.map_selectors(steps or [], look)
    return found[0]


def save_to(directory, record, app=None):
    app = app or record.get("app")
    target = os.path.join(
        os.path.abspath(os.path.expanduser(directory)), records.safe_name(app),
        records.safe_name(record["name"]) + ".json",
    )
    sources.adopt(records.qualify(app, record["name"]), target)
    return records.save(record, app)


def build_record(app, name, steps, description="", aliases=(), requires=(), satisfied_when=(),
                 verifies=(), verifies_why="", keep_text=False, warn=None,
                 kind=records.FLOW, use=(), payload=None, oracle=None, lang=None,
                 recorded_on=None):
    warn = warn or (lambda line: sys.stderr.write(line + "\n"))
    lang = lang or (oracle.lang if oracle else None)
    if not steps:
        raise RecordRefused(
            "no steps journalled since the mark — `uiflow record mark`, drive the flow, then save."
        )
    for ref in use or ():
        steps = adopt(steps, ref, app, payload)
    history = ()
    if oracle:
        steps, ambiguous = localize.canonicalize_steps(steps, oracle)
        satisfied_when, ambiguous_when = localize.canonicalize_steps(satisfied_when or (), oracle)
        verifies, ambiguous_verifies = localize.canonicalize_steps(verifies or (), oracle)
        for label, candidates in sorted(set(ambiguous + ambiguous_when + ambiguous_verifies)):
            warn("WARNING: observed label %r has %d English originals (%s) — refusing to guess "
                 "which. Replace it with an id, or pick one by hand; this record is pinned to %s "
                 "until you do." % (label, len(candidates), ", ".join(candidates), lang))
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        history = ["%s learned from a journalled session" % stamp,
                   "%s normalized observed labels from %s to English" % (stamp, lang)]
    elif lang and not strings.is_english(lang):
        warn("WARNING: the simulator's UI language is %s and no translation oracle could be built, "
             "so the observed labels are saved exactly as they were seen — this record is pinned "
             "to %s." % (lang, lang))
    if kind == records.FRAGMENT:
        verifies_why = verifies_why or "fragment: a reusable step sequence, not a claim"
    if not verifies_why:
        raise RecordRefused(
            "--verifies-why is required: it is what forces you to notice when the assertion is vacuous."
        )
    literal = journal.literal_text_steps(list(steps) + list(satisfied_when or []) + list(verifies or []))
    if literal and not keep_text:
        raise RecordRefused(
            "steps %s carry literal typed text. Pass --keep-text only if that text is not a secret."
            % ", ".join(str(index) for index in literal)
        )
    if not satisfied_when:
        warn(
            "WARNING: no satisfiedWhen — this record can never be skipped, which is the single most "
            "valuable property a record has. Add one with --satisfied-when."
        )
    if not verifies:
        warn("WARNING: no verifies — nothing checks that this record did what it claims.")
    for finding in lint.check(steps, satisfied_when=satisfied_when, verifies=verifies):
        warn("WARNING: %s" % finding.message)
    return journal.to_record(
        app,
        records.safe_name(name),
        steps,
        description=description,
        aliases=aliases,
        requires=requires,
        satisfied_when=satisfied_when,
        verifies=verifies,
        verifies_why=verifies_why,
        kind=kind,
        history=history,
        recorded_on=recorded_on,
    )


def _steps_argument(raw):
    if not raw:
        return []
    parsed = json.loads(raw)
    return [parsed] if isinstance(parsed, dict) else list(parsed)


def run(args):
    if args.action == "mark":
        marked = journal.mark(udid=args.udid)
        print("marked %s at byte %d" % (marked["channel"], marked["offset"]))
        return 0
    if args.action == "keymap":
        from uiflow import keymap
        from uiflow.commands import idb as idb_command

        udid = idb_command._udid(args)
        try:
            learned = keymap.learn(
                args.app, udid, element_type=args.element_type, probe_label=args.probe_label
            )
        except (metrics.MetricsUnavailable, keymap.CalibrationFailed) as error:
            raise RecordRefused("could not learn a keyMap for %s: %s" % (args.app, error))
        print(
            "learned %d keys for %s on %s (dx=%s, dy=%s, probed %r)"
            % (learned["keys"], args.app, learned["fingerprint"], learned["dx"],
               learned["offset"], learned["probe"])
        )
        print(learned["path"])
        return 0
    if journal.mark_is_stale(udid=args.udid):
        raise RecordRefused(
            "the journal mark belongs to another channel (the driver restarted). "
            "Run `uiflow record mark`, drive the flow again, then save."
        )
    steps = journal.to_steps(journal.since_mark(udid=args.udid))
    satisfied_when = _steps_argument(args.satisfied_when)
    verifies = _steps_argument(args.verifies)
    # merged, not load(): `use` crosses the built-in/caller boundary, and this payload is only
    # ever read here — nothing writes it back to index.json.
    payload = index.merged()
    listing = paths.list_devices()
    udid = args.udid or paths.default_udid(listing)
    recorded_on = None
    if udid:
        try:
            seen = metrics.screen(udid)
            recorded_on = {"device": seen.get("device"), "w": seen["w"], "h": seen["h"],
                           "fingerprint": seen["fingerprint"]}
        except metrics.MetricsUnavailable:
            # Provenance is best-effort: a record with none simply never warns about a device change.
            recorded_on = None
    lang = simctl.ui_language(udid)
    oracle = None
    if (lang and not strings.is_english(lang)) or args.canonicalize \
            or needs_canonicalizing(list(steps) + satisfied_when + verifies):
        try:
            oracle = strings.oracle(udid, app=args.app, lang=lang, listing=listing)
        except Exception as error:
            sys.stderr.write("no translation oracle: %s\n" % error)
    try:
        record = build_record(
            app=args.app,
            name=args.name,
            steps=steps,
            description=args.description,
            aliases=args.aliases,
            requires=args.requires,
            satisfied_when=satisfied_when,
            verifies=verifies,
            verifies_why=args.verifies_why,
            keep_text=args.keep_text,
            kind=args.kind,
            use=args.use,
            payload=payload,
            oracle=oracle,
            lang=lang,
            recorded_on=recorded_on,
        )
    finally:
        if oracle:
            oracle.close()
    reusable = reuse_report(
        record["steps"], args.app, payload=payload,
        exclude=records.qualify(args.app, record["name"])
    )
    path = save_to(args.to_dir, record) if args.to_dir else records.save(record, external=False)
    print("saved %s (%d steps)" % (path, len(record["steps"])))
    for entry in reusable:
        print("reuse: steps %d-%d are an exact match for %s"
              % (entry["start"], entry["start"] + entry["length"] - 1, entry["ref"]))
        print("       re-save with --use %s" % entry["ref"])
    return 0
