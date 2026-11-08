from uiflow import index

NAME = "index"
HELP = "inspect or rebuild the record index"


def add_arguments(parser):
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--stats", action="store_true")


def run(args):
    payload = index.load(rebuild=args.rebuild)
    if args.rebuild:
        print("rebuilt %s" % index.index_path())
    if args.stats or not args.rebuild:
        for table in ("records", "steps", "runs", "selectors"):
            print("%-12s %d" % (table, len(payload.get(table) or {})))
    return 0
