import json

from uiflow import index, sources

NAME = "find"
HELP = "search existing records before driving a new flow"


def add_arguments(parser):
    parser.add_argument("query", help="a name, alias, description word, or on-screen label")
    parser.add_argument("--app", default=None, help="restrict to one bundle id")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="as_json")
    sources.add_argument(parser)


def run(args):
    hits = index.find(args.query, app=args.app, limit=args.limit, payload=index.merged())
    for hit in hits:
        hit["source"] = sources.path_for(hit["ref"]) or "builtin"
    if args.as_json:
        print(json.dumps(hits, ensure_ascii=False))
        return 0 if hits else 1
    if not hits:
        print("no record matches %r — drive it, then `uiflow record save`" % args.query)
        return 1
    for hit in hits:
        origin = "builtin" if hit["source"] == "builtin" else "external %s" % hit["source"]
        print("%-48s %-8s %-16s %s" % (hit["ref"], hit["kind"], hit["why"], origin))
    return 0
