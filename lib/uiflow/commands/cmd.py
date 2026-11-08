import json
import sys

from uiflow import channel

NAME = "cmd"
HELP = "send a JSON array of steps to the driver as one batch"


def add_arguments(parser):
    parser.add_argument("steps", help='a JSON array of steps, or - to read one from stdin')
    parser.add_argument("--udid", default=None)
    parser.add_argument("--app", default=None, dest="default_app", help="default bundle id for steps with no app")
    parser.add_argument("--pace", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=channel.DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args):
    raw = sys.stdin.read() if args.steps == "-" else args.steps
    steps = json.loads(raw)
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, list):
        raise ValueError("steps must be a JSON array")
    reply = channel.send(
        steps, udid=args.udid, pace=args.pace, default_app=args.default_app, timeout=args.timeout
    )
    if args.as_json:
        print(json.dumps(reply, ensure_ascii=False))
    else:
        print("\n".join(channel.format_reply(reply)))
        print("elapsed %sms" % reply.get("elapsedMs"))
    return 0 if reply.get("ok") else 1
