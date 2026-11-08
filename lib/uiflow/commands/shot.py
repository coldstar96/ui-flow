from uiflow import channel, report

NAME = "shot"
HELP = "take a screenshot and print the PNG path"


def add_arguments(parser):
    parser.add_argument("--app", default=None)
    parser.add_argument("--udid", default=None)
    parser.add_argument("--timeout", type=float, default=60.0)


def run(args):
    step = {"op": "screenshot"}
    if args.app:
        step["app"] = args.app
    reply = channel.send([step], udid=args.udid, timeout=args.timeout)
    if not reply.get("ok"):
        print("\n".join(channel.format_reply(reply)))
        return 1
    print(report.screenshot_path((reply.get("results") or [{}])[0]) or "")
    return 0
