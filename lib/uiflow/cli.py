import argparse
import importlib
import sys

from uiflow import sources

# name -> module path. Nothing here is imported until the command is actually dispatched:
# importing every command (and transitively every lib) to run one is pure latency.
COMMANDS = {
    "up": "uiflow.commands.up",
    "down": "uiflow.commands.down",
    "status": "uiflow.commands.status",
    "cmd": "uiflow.commands.cmd",
    "snap": "uiflow.commands.snap",
    "shot": "uiflow.commands.shot",
    "sim": "uiflow.commands.sim",
    "lease": "uiflow.commands.lease",
    "idb": "uiflow.commands.idb",
    "do": "uiflow.commands.do",
    "ls": "uiflow.commands.ls",
    "record": "uiflow.commands.record",
    "find": "uiflow.commands.find",
    "factor": "uiflow.commands.factor",
    "index": "uiflow.commands.index",
}

USAGE = "usage: uiflow <command> [options]\n\ncommands:\n"


def _help(stream=None):
    stream = stream or sys.stdout
    stream.write(USAGE)
    for name in sorted(COMMANDS):
        try:
            module = importlib.import_module(COMMANDS[name])
            summary = getattr(module, "HELP", "")
        except Exception as error:
            summary = "(unavailable: %s)" % error
        stream.write("  %-9s %s\n" % (name, summary))
    stream.write("\nRun `uiflow <command> --help` for a command's own options.\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        _help()
        return 0
    name = argv[0]
    if name not in COMMANDS:
        sys.stderr.write("unknown command: %s\n\n" % name)
        _help(sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[name])
    parser = argparse.ArgumentParser(prog="uiflow %s" % name, description=getattr(module, "HELP", None))
    module.add_arguments(parser)
    args = parser.parse_args(argv[1:])
    try:
        default_app = getattr(args, "app", None)
        registered = list(sources.register_env(default_app=default_app))
        for path in getattr(args, "files", None) or []:
            registered.extend(sources.register(path, default_app))
    except (sources.SourceError, OSError) as error:
        sys.stderr.write("SourceError: %s\n" % error)
        return 1
    if registered:
        # deferred: a run with no caller sources must not pay for importing records
        from uiflow import records

        for ref in records.shadowed(list(dict.fromkeys(registered))):
            sys.stderr.write("shadowing builtin %s\n  with %s\n" % (ref, sources.path_for(ref)))
    try:
        return module.run(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        sys.stderr.write("%s: %s\n" % (type(error).__name__, error))
        return 1
