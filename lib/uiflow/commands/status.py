import json

from uiflow import lease, paths, session

NAME = "status"
HELP = "report the session's channel, pid and liveness"


def add_arguments(parser):
    parser.add_argument("--udid", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args):
    state = session.status(udid=args.udid)
    sim = paths.describe(state["udid"])
    if args.as_json:
        print(json.dumps(dict(state, sim=sim, heldBy=lease.read(state["udid"]) if state["udid"] else None)))
    else:
        print("sim %s" % paths.sim_line(state["udid"], sim))
        print("live=%s pid=%s channel=%s" % (state["live"], state.get("pid"), state.get("channel")))
    return 0 if state["live"] else 1
