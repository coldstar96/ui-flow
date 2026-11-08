import os

from uiflow import index, paths, records, sources

NAME = "ls"
HELP = "list the apps with records, or one app's records"


def add_arguments(parser):
    parser.add_argument("app", nargs="?", default=None)
    parser.add_argument("--all", action="store_true", dest="show_all", help="include fragments")
    sources.add_argument(parser)


def _wiring(app, known):
    """uses/usedBy per qualified ref, global so a cross-app fragment shows its callers."""
    entries = (index.merged().get("records") or {})
    if entries:
        return entries
    return index.link_used_by({
        records.qualify(app, name): {"uses": index.uses_of(record, app), "usedBy": []}
        for name, record in known.items()
    })


def run(args):
    if not args.app:
        try:
            found = [entry for entry in os.listdir(paths.RECORDS_DIR) if not entry.startswith(".")]
        except OSError:
            found = []
        apps = [entry for entry in found if os.path.isdir(os.path.join(paths.RECORDS_DIR, entry))]
        for app in sources.apps():
            if app not in apps:
                apps.append(app)
        for app in sorted(apps):
            print("%-32s %d records" % (app, len(records.all_for(app))))
        return 0
    known = records.all_for(args.app)
    wiring = _wiring(args.app, known)
    for name, record in sorted(known.items()):
        try:
            kind = records.kind_of(record)
        except records.BadName:
            print("%-24s %-8s (unreadable kind — skipped)" % (name, "?"))
            continue
        if kind == records.FRAGMENT and not args.show_all:
            continue
        health = record.get("health") or {}
        aliases = ", ".join(record.get("aliases") or [])
        print("%-24s %-8s runs=%-4s fails=%-4s %s" % (
            name, kind, health.get("runs", 0), health.get("failures", 0), aliases))
        if record.get("description"):
            print("    %s" % record["description"])
        external = sources.path_for(records.qualify(args.app, name))
        if external:
            print("    external %s" % external)
        entry = wiring.get(records.qualify(args.app, name)) or {}
        if entry.get("uses"):
            print("    uses: %s" % ", ".join(entry["uses"]))
        if entry.get("usedBy"):
            print("    used-by: %s" % ", ".join(sorted(entry["usedBy"])))
    return 0
