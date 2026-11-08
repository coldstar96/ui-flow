import json

from uiflow import idb, session

NAME = "down"
HELP = "quit the driver session and the idb pair, escalating to SIGTERM"


def add_arguments(parser):
    parser.add_argument("--udid", default=None)
    parser.add_argument("--keep-idb", action="store_true", dest="keep_idb")
    parser.add_argument("--steal", action="store_true",
                        help="stop a driver another ui-flow session is holding")
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args):
    # The lease guard lives in session.down and raises before this point, so a refusal also spares
    # the other session's companion and sidecar below.
    state = session.down(udid=args.udid, steal=args.steal)
    udid = state.get("udid") or args.udid
    # A companion and a sidecar outlive the driver, so a teardown that skips them leaks two
    # processes per simulator every session.
    if udid and not args.keep_idb:
        try:
            state["idb"] = idb.down(udid)
        except idb.IdbError as error:
            state["idb"] = {"error": str(error)}
    print(json.dumps(state) if args.as_json else "down %s" % (udid or ""))
    return 0
