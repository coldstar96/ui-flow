import json
import sys

from uiflow import idb, paths

NAME = "idb"
HELP = "the fast actuation channel: taps and typing at HID rate, no XCUITest"
# Wall clock each tap costs beyond its hold and gap, measured on the held sidecar socket.
STREAM_OVERHEAD_MS = 6.0


def add_arguments(parser):
    sub = parser.add_subparsers(dest="action", required=True)

    for name, summary in (
        ("up", "start the idb companion and sidecar, or reuse the live pair"),
        ("down", "stop the sidecar and companion"),
        ("status", "report whether the sidecar is live"),
    ):
        node = sub.add_parser(name, help=summary)
        node.add_argument("--udid", default=None)

    send = sub.add_parser("send", help="send a JSON array of idb ops as one batch")
    send.add_argument("ops", help="a JSON array of ops, or - to read one from stdin")
    send.add_argument("--udid", default=None)
    send.add_argument("--json", action="store_true", dest="as_json")

    tap = sub.add_parser("tap", help="tap one screen point")
    tap.add_argument("x", type=float)
    tap.add_argument("y", type=float)
    tap.add_argument("--udid", default=None)

    type_keys = sub.add_parser("type", help="tap a run of keys from a keyMap at HID rate")
    type_keys.add_argument("app", help="bundle id owning the keyMap")
    type_keys.add_argument("text", help="the characters to tap")
    type_keys.add_argument("--hold", type=float, default=4.0, dest="hold_ms")
    pace = type_keys.add_mutually_exclusive_group()
    pace.add_argument("--gap", type=float, default=None, dest="gap_ms")
    pace.add_argument("--cpm", type=float, default=None, help="type at this measured rate, e.g. 1000")
    type_keys.add_argument("--udid", default=None)


def _udid(args):
    udid = getattr(args, "udid", None) or paths.default_udid()
    if not udid:
        raise idb.IdbError("no simulator found — pass --udid")
    return udid


def _report(reply):
    if not reply.get("ok"):
        error = reply.get("error") or {}
        sys.stderr.write(
            "idb %s failed: %s %s\n"
            % (error.get("op", "?"), error.get("reason", "?"), error.get("detail", ""))
        )
        return 1
    for result in reply.get("results") or []:
        extra = " ".join(
            "%s=%s" % (key, value)
            for key, value in sorted(result.items())
            if key not in ("op", "ms", "tree")
        )
        line = "ok   %-12s %6sms %s" % (result.get("op", "?"), result.get("ms", "?"), extra)
        print(line.rstrip())
    return 0


def run(args):
    udid = _udid(args)
    if args.action == "up":
        state = idb.up(udid)
        print("idb up %s port=%s sidecar=%s" % (udid, state.get("port"), state.get("sidecarPid")))
        return 0
    if args.action == "down":
        idb.down(udid)
        print("idb down %s" % udid)
        return 0
    if args.action == "status":
        state = idb.status(udid)
        print(
            "udid=%s live=%s port=%s sidecar=%s companion=%s"
            % (
                state.get("udid"),
                state.get("live"),
                state.get("port"),
                state.get("sidecarPid"),
                state.get("companionPid"),
            )
        )
        return 0 if state.get("live") else 1
    if args.action == "send":
        raw = sys.stdin.read() if args.ops == "-" else args.ops
        ops = json.loads(raw)
        if isinstance(ops, (dict, str)):
            ops = [ops]
        reply = idb.send(ops, udid)
        if args.as_json:
            print(json.dumps(reply, ensure_ascii=False))
            return 0 if reply.get("ok") else 1
        return _report(reply)
    if args.action == "tap":
        return _report(idb.send([{"op": "tap", "x": args.x, "y": args.y}], udid))
    if args.action == "type":
        from uiflow import channel, keymap, metrics, records

        try:
            screen = metrics.screen(udid)
        except metrics.MetricsUnavailable as error:
            raise idb.IdbError(str(error))
        key_map = records.key_map(args.app, screen["fingerprint"])
        if not key_map:
            # Learning needs a driver snapshot, and `idb type` is documented as the no-driver path.
            if not channel.resolve_channel(udid):
                raise idb.IdbError(
                    "no keyMap for %s on this device (%s) — learn one with "
                    "`uiflow record keymap %s`, with the keyboard on screen"
                    % (args.app, screen["fingerprint"], args.app)
                )
            try:
                key_map = keymap.ensure(args.app, udid, screen)
            except (keymap.CalibrationFailed, channel.ChannelError) as error:
                raise idb.IdbError(
                    "could not learn a keyMap for %s on this device (%s): %s"
                    % (args.app, screen["fingerprint"], error)
                )
        missing = sorted({ch for ch in args.text if ch not in key_map})
        if missing:
            raise idb.IdbError(
                "keyMap for %s has no entry for %s" % (args.app, ", ".join(map(repr, missing)))
            )
        gap_ms = args.gap_ms if args.gap_ms is not None else 4.0
        if args.cpm:
            gap_ms = max(0.0, 60000.0 / args.cpm - args.hold_ms - STREAM_OVERHEAD_MS)
        step = {"op": "tapBurst", "keys": list(args.text), "keyMap": key_map,
                "holdMs": args.hold_ms, "gapMs": gap_ms}
        return _report(idb.send([step], udid))
    raise ValueError("unknown idb action: %s" % args.action)
