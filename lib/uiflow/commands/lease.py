import json

from uiflow import lease, paths

NAME = "lease"
HELP = "claim, release or inspect ownership of a simulator"


def add_arguments(parser):
    sub = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("acquire", "claim the simulator for this session"),
        ("release", "give up this session's claim"),
        ("renew", "keep this session's claim fresh during long work"),
        ("status", "print who holds the simulator"),
    ):
        action = sub.add_parser(name, help=help_text)
        action.add_argument("--udid", default=None)
        action.add_argument("--json", action="store_true", dest="as_json")
        if name == "acquire":
            action.add_argument("--steal", action="store_true",
                                help="take it from the session that holds it")


def run(args):
    udid = args.udid or paths.default_udid()
    if not udid:
        raise lease.LeaseHeld("no simulator resolved: name one with UIFLOW_SIM_NAME or pass --udid.")
    if args.action == "acquire":
        held = lease.acquire(udid, steal=args.steal)
    elif args.action == "renew":
        held = lease.renew(udid)
    elif args.action == "release":
        lease.release(udid)
        held = None
    else:
        held = lease.read(udid)
    if args.as_json:
        print(json.dumps({"udid": udid, "owner": lease.owner(), "held": held}))
    else:
        print("%s %s" % (udid, lease.describe(udid) or "unheld"))
    return 0 if args.action != "status" or lease.held_by_other(udid) is None else 1
