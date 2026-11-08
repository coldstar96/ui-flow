import json

from uiflow import lease, paths, session

NAME = "up"
HELP = "start the warm driver session, or reuse the live one"


def add_arguments(parser):
    parser.add_argument("--udid", default=None, help="simulator UDID (default: resolved by name)")
    parser.add_argument("--timeout", type=float, default=session.READY_TIMEOUT_SECONDS)
    parser.add_argument("--steal", action="store_true",
                        help="take a simulator another ui-flow session is holding")
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args):
    state = session.up(udid=args.udid, timeout=args.timeout, steal=args.steal)
    sim = paths.describe(state["udid"])
    if args.as_json:
        print(json.dumps(dict(state, sim=sim, heldBy=lease.read(state["udid"]))))
    else:
        print("sim %s" % paths.sim_line(state["udid"], sim))
        print("%s %s" % ("reused" if state["reused"] else "started", state["channel"]))
    return 0
