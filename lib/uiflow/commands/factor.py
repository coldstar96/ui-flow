import json

from uiflow import factor, index

NAME = "factor"
HELP = "extract step runs duplicated across records into reusable fragments"


def add_arguments(parser):
    parser.add_argument("app", nargs="?", default=None)
    parser.add_argument("--min-run", type=int, default=index.MIN_RUN, dest="min_run",
                        help="minimum shared run length; values below %d are clamped to it, because "
                             "the index only enumerates runs of %d or more"
                             % (index.MIN_RUN, index.MIN_RUN))
    parser.add_argument("--name", default=None, help="name for the extracted fragment (single proposal only)")
    parser.add_argument("--apply", action="store_true", dest="do_apply")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args):
    min_run = max(args.min_run, index.MIN_RUN)
    if min_run != args.min_run:
        print("--min-run %d clamped to %d (the index enumerates no shorter run)"
              % (args.min_run, min_run))
    args.min_run = min_run
    found = factor.proposals(app=args.app, min_run=min_run)
    if args.as_json:
        print(json.dumps(found, ensure_ascii=False))
    else:
        for proposal in found:
            print("%s/%s: %d steps shared by %s" % (
                proposal["app"], proposal["name"], proposal["length"],
                ", ".join(occurrence["ref"] for occurrence in proposal["occurrences"]),
            ))
    if not found:
        print("no run of %d or more steps is shared by two records" % args.min_run)
        return 0
    if not args.do_apply:
        print("dry run — pass --apply to extract")
        return 0
    if args.name and len(found) > 1:
        print("--name needs exactly one proposal; %d were found" % len(found))
        return 2
    taken = set()
    name = args.name
    while found:
        proposal = found[0]
        try:
            outcome = factor.apply(proposal, name=name, taken=taken)
        except factor.NameTaken as error:
            print("skipped: %s" % error)
            return 2
        name = None
        taken.add(outcome["name"])
        print("extracted %s, rewrote %d records" % (outcome["ref"], len(outcome["rewrote"])))
        for warning in outcome.get("warnings") or []:
            print("  WARNING: %s" % warning)
        if not outcome["rewrote"]:
            break
        found = factor.proposals(app=args.app, min_run=args.min_run)
    return 0
