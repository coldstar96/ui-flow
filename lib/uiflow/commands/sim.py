import sys

from uiflow import paths, simctl

NAME = "sim"
HELP = "resolve, prepare, install to or erase the ui-flow simulator"


def add_arguments(parser):
    sub = parser.add_subparsers(dest="action", required=True)

    udid = sub.add_parser("udid", help="print the UDID of a simulator resolved by name")
    udid.add_argument("--name", default=None)
    udid.add_argument("--device-type", default=None, dest="device_type")
    udid.add_argument("--create", action="store_true")

    prepare = sub.add_parser("prepare", help="install -> shutdown -> write prefs -> boot")
    prepare.add_argument("--name", default=None)
    prepare.add_argument("--device-type", default=None, dest="device_type")
    prepare.add_argument("--app", default=None, dest="app_path", help="absolute path to a built .app")
    prepare.add_argument("--keyboard", action="append", default=[], dest="keyboard_ids")
    prepare.add_argument("--locale", default=None)
    prepare.add_argument("--create", action="store_true")

    install = sub.add_parser("install", help="install a .app onto the simulator")
    install.add_argument("app_path")
    install.add_argument("--name", default=None)

    erase = sub.add_parser("erase", help="erase the ui-flow simulator (takes the session down first)")
    erase.add_argument("--name", default=None)
    erase.add_argument("--force", action="store_true")
    erase.add_argument("--confirm", default=None, help="repeat the name to erase a simulator uiflow does not own")

    delete = sub.add_parser("delete", help="delete a simulator ui-flow created")
    delete.add_argument("--name", default=None)
    delete.add_argument("--udid", default=None)
    delete.add_argument("--force", action="store_true")
    delete.add_argument("--confirm", default=None, help="repeat the name to delete a simulator uiflow did not create")

    sweep = sub.add_parser("sweep", help="delete ui-flow simulators nothing is using")
    sweep.add_argument("--yes", action="store_true", help="actually delete (without this, it only lists)")
    sweep.add_argument("--older-than", type=float, default=simctl.IDLE_HOURS, dest="older_than",
                       help="hours a device must have been unused (default 24)")


def _idle(hours):
    return "unknown" if hours == float("inf") else "%.0fh" % hours


def run(args):
    name = getattr(args, "name", None)
    device_type = getattr(args, "device_type", None)
    if args.action == "udid":
        udid = simctl.resolve(name, create=args.create, device_type=device_type)
        # stdout stays a bare UDID for `$(uiflow sim udid)`; the description goes to stderr.
        sys.stderr.write("sim %s\n" % paths.sim_line(udid))
        print(udid)
        return 0
    if args.action == "install":
        udid = simctl.resolve(name)
        simctl.install(udid, args.app_path)
        print("installed %s on %s" % (args.app_path, paths.sim_line(udid)))
        return 0
    if args.action == "prepare":
        udid = simctl.resolve(name, create=args.create, device_type=device_type)
        simctl.prepare(udid, app_path=args.app_path, keyboard_ids=args.keyboard_ids, locale=args.locale)
        print("prepared %s" % paths.sim_line(udid))
        return 0
    if args.action == "delete":
        gone = simctl.delete(name=name, udid=args.udid, force=args.force, confirm=args.confirm)
        print("deleted %s (%s)" % (gone["name"], gone["udid"]))
        return 0
    if args.action == "sweep":
        planned = simctl.sweep(yes=args.yes, older_than_hours=args.older_than)
        if not planned:
            print("nothing to sweep")
            return 0
        for entry in planned:
            if entry.get("skipped"):
                verb = "SKIPPED"
            elif args.yes:
                verb = "deleted"
            else:
                verb = "would delete"
            print("%s %s (%s) idle %s%s%s" % (
                verb, entry["name"], entry["udid"], _idle(entry["idleHours"]),
                "" if entry["recorded"] else "  [not in the registry, matched by name]",
                "  — %s" % entry["skipped"] if entry.get("skipped") else "",
            ))
        if not args.yes:
            print("\n%d simulator(s) — re-run with --yes to delete them" % len(planned))
        return 1 if any(entry.get("skipped") for entry in planned) else 0
    if args.action == "erase":
        udid = simctl.erase(name=name, force=args.force, confirm=args.confirm)
        print("erased %s" % paths.sim_line(udid))
        return 0
    raise ValueError("unknown sim action: %s" % args.action)
