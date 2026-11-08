# ui-flow

A [Claude Code](https://claude.com/claude-code) skill (and standalone CLI) for driving, testing and
demoing UI flows on an iOS simulator. One long-lived XCUITest driver stays alive on a simulator and
runs whole flows as a single batched request; learned flows replay in milliseconds, new ones are
worked out from the screen and recorded. It works on any app and needs no test code inside it.

## Requirements

- macOS with Xcode and the iOS Simulator
- Python 3
- [`fb-idb`](https://fbidb.io) (optional; `uiflow idb up` installs it on first use, for fast typing/tapping)

## Install

```bash
git clone https://github.com/coldstar96/ui-flow ~/.claude/skills/ui-flow
ln -s ~/.claude/skills/ui-flow/bin/uiflow /usr/local/bin/uiflow   # or anywhere on PATH
uiflow up
```

`SKILL.md` is the full guide (Claude loads it automatically as a skill); it covers the command
reference, flow records, replay, idb actuation and multi-session leases.

State lives under `~/.ui-flow` (override with `UIFLOW_HOME`) and `~/Library/Application Support/uiflow`.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT
