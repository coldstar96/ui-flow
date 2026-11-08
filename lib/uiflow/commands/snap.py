import json

from uiflow import channel

NAME = "snap"
HELP = "print the accessibility tree, one JSON element per line"


def add_arguments(parser):
    parser.add_argument("--app", default=None, help="bundle id to snapshot (default: every registered app)")
    parser.add_argument("--udid", default=None)
    parser.add_argument("--type", default=None, dest="element_type", help="print only this element type")
    parser.add_argument("--timeout", type=float, default=30.0)


def run(args):
    elements = channel.snapshot(app=args.app, udid=args.udid, timeout=args.timeout)
    for element in elements:
        if args.element_type and element.get("type") != args.element_type:
            continue
        print(json.dumps(element, ensure_ascii=False))
    return 0
