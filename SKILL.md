---
name: ui-flow
description: Drive, test or demo a UI flow on an iOS simulator app — "add the Korean keyboard", "turn on full access", "type this in my app", "walk through onboarding", "does the new key work?". Replays learned per-app flows in milliseconds from a warm driver session, falls back to reading the screen when the ask is new, and records what worked so the next run is instant. Works on ANY app: it needs no test code inside the app it drives.
---

# ui-flow

One long-lived XCUITest ("the driver") stays alive on a simulator and executes whole flows as a
single batched request. Learned flows replay with **no deliberation**; new ones are worked out by
reading the screen, then recorded.

The metric is wall clock **from the user's ask to the flow finishing** — which includes your own
reading and thinking. Optimise that, not gesture time.

Every tool call is a full-context turn, so: never hand-roll `tap`/`shot`/`sleep` shell helpers
(`t.sh`, `h.sh`); put every op for the current screen in ONE `uiflow cmd '[…]'` array, `find`
before driving, and `do` a learned flow instead of re-walking it. Look at a screenshot only at a
decision point, not after every tap.

## 1. Start the session before anything else

```bash
uiflow up &   # bin/uiflow in this skill's directory; symlink it onto PATH, e.g. ln -s ~/.claude/skills/ui-flow/bin/uiflow /usr/local/bin/uiflow
```

First action on any UI request, in the background. It is idempotent and returns in ~0.3 s when a
session is already alive, ~4 s cold — so that cost overlaps reading the ask instead of following it.

It prints the simulator it is on, and every later command repeats the same line:

```
sim uiflow-iphone17pro · iPhone 17 Pro · iOS 26.5 · Booted · B7E4… · mine
```

The device is named **structurally** — `uiflow-<device-slug>`, derived from the device type — so the
name is the same in every checkout and on every machine. The iOS version is deliberately NOT in the
name: baking it in strands an orphan device on every runtime bump, so it is reported instead.
`--device-type "iPhone Air"` resolves `uiflow-iphoneair`, a device of its own — pass the display
name from `simctl list devicetypes`, not the identifier.

Carrying a device from before this naming? Rename it rather than creating a second one:
`xcrun simctl rename uiflow uiflow-iphone17pro`.

**You own the simulator, not ui-flow.** ui-flow drives whatever device it is pointed at; it has no
lifecycle duties and will not clean up after you. So create the sim yourself with
`xcrun simctl create` (or your own simulator tooling), hand it over with `--udid` (or `UIFLOW_SIM_NAME`), and delete it when the run
ends — there are no long-lived simulators. A device ui-flow names for itself has no owner encoded in
its name, which is how the `uiflow-*` orphans accumulated at ~3 GB each.

## 2. Known flow → replay it. Do not think.

```bash
uiflow ls com.example.MyApp            # what this app already knows
uiflow do com.example.MyApp add-korean # replay, one batch
```

Do **not** open the record files, reason about their steps, or ask a clarifying question. `do`
prints a one-line plan summary before sending — read that, and stop only if a record you did not
author sets `launch` arguments or was auto-repaired since its last run.

`--pace 2` slows every timeout when asked to go slower. Never slow the default to make something
pass; fix the selector instead.

## 3. New flow → check for reuse, observe, act, record

```bash
uiflow find "keyboard settings"        # ALWAYS first — adopt what already exists
uiflow record mark
uiflow snap --app <bundle-id>          # one JSON element per line, with frames
uiflow cmd '[{"op":"tap","sel":{"type":"button","label":"General"}}]'
uiflow record save <bundle-id> <name> --verifies-why "…" --satisfied-when '[…]' \
  --use com.apple.Preferences/open-keyboard-settings
```

`find` before driving anything. Re-deriving a walk that already exists costs the whole flow's
wall clock and produces a second copy that rots independently. `record save` prints any exact
match it spots afterwards — but by then you have already paid for the taps.

Always supply `satisfiedWhen` and `verifiesWhy`. A record without a precondition can never be
skipped, and skipping is the property that makes the second run fast.

`record save` lints the final steps, `satisfiedWhen` and `verifies` — descending into a group's
`steps`/`verifies`/`reassert` and a `repeatUntil`'s `until` — and warns, never refuses, since you
already paid for the drive. Findings cover two different things nothing else ever notices: steps
that cost wall clock and can never fail, and steps that are silently wrong on another device. Four
rules:

- **`redundant-swipe-before-scrollTo`** — one or more selector-less vertical `swipe`s immediately
  before a `scrollTo` in the same app. Same direction as the scrollTo: delete them, it swipes that
  way itself and stops the moment its target resolves. Opposite: delete them *and* put
  `"direction": "<theirs>"` on the scrollTo, which otherwise swipes away from where they leave the
  screen.
- **`repeated-swipe`** — 2+ consecutive identical selector-less vertical swipes not followed by a
  `scrollTo`, where the step immediately after the run names a selector in the same app. Replace
  with one `scrollTo` toward that selector. With no such step there is nothing to scroll to, so
  nothing is flagged.

Both swipe rules carry the same caveat, because both delete swipes: **replay the flow afterwards.**
`scrollTo` aborts after two swipes that leave the match count unchanged, so a target that is
genuinely absent from the accessibility tree until several swipes in is never reached, and deleting
a long run can turn a passing record into a failing one.
- **`bare-sleep`** — a wall-clock wait is paid in full every run and is too short the one time it
  matters; in a `reassert` it is paid again after every focus-moving step of the group.
- **`device-shaped-tapAt`** — a `tapAt` with no `sel`. Its `dx`/`dy` offset is taken inside the app
  window, so at a different screen shape that same fraction lands on a different control. Give it a
  `sel` so the offset resolves against a real element instead. A `tapAt` that carries a `sel` is
  never flagged — it is already device-independent (§4).

Horizontal (`left`/`right`) runs are never flagged: `scrollTo` is binary in the driver — `"down"`
swipes down and anything else swipes up — so "make it a scrollTo" would silently turn a carousel
gesture into a vertical one. A `swipe` carrying its own `sel` is never flagged either: it is executed
against that element, while `scrollTo` scopes to the app's first table/collection/scroll view. There
is no rule for an unchecked swipe: judging one needs whole-record execution-order reasoning across
group `verifies` and parent lists, and every draft of it fired on correct records instead.

Three blind swipes to the top of Settings cost 3.2s of a 15.6s flow and never once failed, so no
health counter or timing budget could ever notice them — only `scrollTo {"direction":"down"}`,
which stops the moment the row resolves, deletes that cost.

## 4. Unlabeled UI → tap by frame

Custom-drawn UI (a keyboard, a canvas, a game) often exposes elements with **no identifier and no
label of their own**, but always with a `frame`. Two moves, in order:

1. **A labeled child.** The label frequently sits on a `staticText` child while the tappable parent
   is anonymous. Point `tapAt` at the CHILD and let it tap the child's own centre:

   ```json
   {"op":"tapAt","sel":{"type":"staticText","label":"H","index":0},"dx":0.5,"dy":0.5}
   ```

   `tapAt` resolves its selector without requiring hittability and taps the resolved element's
   coordinate, so this hits the anonymous parent. Crucially it **re-resolves on every tap**, which a
   frame captured from an earlier snapshot does not — a keyboard that animates or a view that
   scrolls will have moved by the time the tap lands.
2. **A screenshot.** `uiflow shot`, read the glyphs off the image, and store the label→SCREEN-point
   map with `uiflow record keymap` (§4b) — `~/.ui-flow/records/<app>/keymap.json`, not the record's own unused
   `keyMap` field. Replay is then pure coordinate taps with no recognition cost. The file holds one
   entry per device, keyed by a `"{w}x{h}"` point fingerprint; a fingerprint hit costs exactly what
   it costs today, and a miss learns the new device automatically whenever a driver is up.

This is why no app needs test code. Never add a test-only launch argument to make a target
addressable.

## 5. Typing and tapping fast → idb, not XCUITest

XCUITest charges ~578 ms for every tap however it is targeted — four accessibility-snapshot
fetches, an interrupting-element check, event synthesis and two quiescence waits, none of them
declinable. That caps a custom keyboard near **104 CPM**. idb injects into IndigoHID instead and
costs **2 ms** a tap.

```bash
uiflow idb up                          # companion + sidecar; first run installs fb-idb
uiflow down                            # stops the driver AND the idb pair (--keep-idb to keep)
uiflow record keymap <bundle-id>       # learn label -> SCREEN point, keyboard on screen
uiflow idb type <bundle-id> "hello"    # one HID stream, ~6,000 CPM
uiflow idb type <bundle-id> "hello" --cpm 1000   # human-watchable pace, measured
```

Measured on a custom keyboard's own keys, 70 characters, every character read back off the field:
4 ms hold/gap = **6,030 CPM** exact (x5 runs); 8 ms = 3,133; 1 ms = 15,105; only at 0 ms does a
character drop. **4 ms is the operating point** — 20x the fastest human and still 4x slower than
the rate anything breaks at.

Three ops exist on the sidecar because the driver has no equivalent, and neither does idb's own API:

- **`path {points: [[x, y, holdMs], ...]}`** — a drag as an explicit point list. A repeated DOWN at a
  new point is how IndigoHID expresses a move; the final UP ends the drag. `swipe` is a single
  `HIDSwipe` event and **cannot hold** at either end, which every slide gesture needs.
- **`tapSeq {taps: [[x, y, gapMs], ...], holdMs}`** — taps each on their own gap, so a stream can carry
  human rhythm. `tapBurst` is one uniform gap and can only ever be a speed run.
- **`ops {}`** — the op names this sidecar serves. Capability probing should not require deliberately
  triggering an error.

Both actuating ops report their own wall clock: `tapSeq` returns `{taps, ms, cpm}`. **The human
typing target is 1000 CPM.** Every tap costs its gap plus ~10 ms (the 4 ms hold and ~6 ms of
streaming), so hit a rate with gap = 60000/cpm − 10: 60 taps at a 50 ms gap measure **~1000 CPM**,
and a 66.7 ms gap only ~810. `idb type --cpm N` does that subtraction for you.

`<state>/idb-<udid>.json` carries the **`socket`** a host-side runner connects to. Read it from there;
never rebuild the path, because `socket_path()` falls back out of the state dir entirely when the
name would breach the ~104-byte AF_UNIX limit.

- **Resolve on XCUITest, actuate on idb.** idb's accessibility tree cannot see custom-drawn keys
  (24 elements against XCUITest's 321 on the same screen) and shapes ordinary UI differently, so
  selectors stay on the driver. Once a flow is learned, replay is coordinates and needs no driver.
- **`tap`, `tapBurst` and `swipe` also accept `nx`/`ny`** (`fromNx`/`fromNy`/`toNx`/`toNy` for
  `swipe`) alongside absolute `x`/`y` — a fraction of the live screen instead of a point. The
  screen is resolved lazily, only when a step actually carries one of these, so an absolute tap
  still costs the sidecar's ~2 ms. Supplying both forms for one axis is refused.
- **A keyMap holds SCREEN points.** A custom keyboard reports frames in the KEYBOARD's coordinate
  space, and no element appears in both spaces to calibrate against, so `record keymap` taps a key
  and reads back what it typed. Never store raw snapshot frames — they land hundreds of points off.
  Points are stored **per device** — the same keyboard sits somewhere else on a different screen —
  keyed by the `"{w}x{h}"` fingerprint of the device it was learned on. Calibration solves for a
  `(dx, dy)` shift so a keyboard that does not sit where its own frames claim still works.
- **`tapAt` with a `sel` is device-independent and is the form to prefer** — it offsets inside the
  *resolved element's* frame (§4), which moves with the element on any screen shape. A bare `tapAt`
  (no `sel`) offsets inside the app **window** instead, so the same fraction lands on a different
  control at another aspect ratio. `uiflow record save`'s lint now flags a bare `tapAt`
  (`device-shaped-tapAt`, §3), and `do` warns at replay when a record with one runs on a device
  other than the one it was recorded on.
- **idb's tap does not wait for the app.** That is why it is fast; the settle is yours to supply,
  with an assert rather than a blind sleep.
- **Never shell out to the `idb` CLI** — ~2,030 ms per invocation, worse than XCUITest. The speed
  lives entirely in the sidecar's held connection.
- Simulator only. `adjust` (picker wheels) has no idb equivalent and stays on XCUITest.
- **`uiflow idb type` still needs no driver on the hit path.** A fingerprint hit reads cached
  points with no snapshot at all. Only a fingerprint *miss* needs a driver, to learn the new
  device's map; with no driver live it fails naming `uiflow record keymap` rather than guessing.
- **A keymap is NOT invalidated when the app's own keyboard layout changes.** A fingerprint hit
  returns cached points without ever looking at the screen again — that is what makes it free — so
  a layout change on an already-learned device is invisible to it. This is not a regression (the
  single map of before had the same property), but re-run `uiflow record keymap` after changing a
  keyboard's layout.
- **Rotation is NOT distinguished by the fingerprint.** The two dimensions are ordered
  smaller-first, so `402x874` and `874x402` are one key — deliberately, because idb reports the
  hardware screen while the driver reports the current interface orientation, and the same device
  must not hash two ways depending on which channel is live. The cost is that a map learned in
  portrait is served in landscape: re-run `uiflow record keymap` after rotating, the same as after
  a keyboard-layout change.
- **`uiflow idb type`'s auto-learn borrows the focused field.** On a fingerprint miss it reads the
  field's current text, probes (which types stray characters into it), then BACKSPACES exactly its
  own characters back off. Nothing is re-typed, so Korean, accented and emoji contents survive
  whatever the script — the field ends as it started, though the app sees the intermediate edits.
  The one exception is an autocorrect replacement landing mid-probe: the value no longer starts
  with what was read, so the trim cannot tell the probe's characters from the app's and falls back
  to clearing and re-typing, which is best-effort and ASCII-only (idb's HID `text` op silently
  drops every non-ASCII character).

## 6. Composition — one flow, many records

`{"op": "use", "ref": "<name>"}` inlines another record's steps at that exact position. A bare
`ref` resolves in the same app; `<bundle-id>/<name>` reaches another app's records.

**Fragments live under the app whose UI they drive.** The Settings walk is
`com.apple.Preferences/open-keyboard-settings` even though only keyboard-related flows call it, because
those are the screens its steps touch. Save one with `--kind fragment`, which drops the
`--verifies-why` requirement.

An inlined fragment expands into a nested `group`, so **it is skipped on its own
`satisfiedWhen`** — replaying a flow whose Settings walk is already done costs one probe instead
of seven taps. Give every fragment a `satisfiedWhen` for that reason.

Learned timings and auto-repairs land on the **fragment**, not the caller. A fragment therefore
converges on a timing budget faster than any single flow could, and one selector fix repairs
every caller at once.

`use` is only valid inside `steps` — never in `satisfiedWhen`, `verifies`, `reassert` or `until`.
It takes no parameters: a variant is a second fragment.

`uiflow factor` finds runs of three or more steps shared by two or more records, extracts them
into a fragment, rewrites each caller to a `use`, and migrates the learned timings across. It is
a dry run until `--apply`.

## 6b. A caller's own records — `--file`

A caller that wants to keep its flows in its own repo passes them in. They behave exactly like
built-in records: they compose, they are skipped on their `satisfiedWhen`, and everything learned
about them is written back **into the caller's own file**, so the converged timings can be
committed alongside the flow.

```bash
uiflow do com.example.MyApp onboarding --file ~/proj/flows
uiflow do com.example.MyApp --file ~/proj/flows/one-off.json one-off
export UIFLOW_FILES=~/proj/flows:~/shared/flows   # same grammar, set once
```

- **A directory means `<dir>/<app>/<name>.json`** — the same shape as the built-in root. A loose
  file is reached by naming it directly, and takes its `app`/`name` from inside the JSON, falling
  back to the command's bundle id and the filename.
- **External records shadow built-ins of the same ref**, and a later `--file` beats an earlier one.
  `--file` is applied after `UIFLOW_FILES`, so the flag always wins. **A shadow is never silent** —
  each one prints `shadowing builtin <ref>` with the file that replaced it, so a caller record
  standing in for a flow you trusted is something you see rather than something you discover.
- **`use` crosses the boundary in both directions.** A caller record may `use` a built-in
  fragment; a built-in may `use` a caller's record while that source is registered. A ref that
  resolves to neither reports the apps it searched.
- **A bad `--file` is a hard error**, never a silent skip — otherwise a caller's flow would fall
  through to a stale built-in of the same name.
- **`uiflow record save <app> <name> --to <dir>`** writes a newly learned flow to
  `<dir>/<app>/<name>.json` instead of the built-in root. That is the export half of the round trip.
- `ls` and `find` show external records, marked with their source path. `index` and `factor` stay
  built-ins-only: the on-disk index must never hold a ref to a file that vanishes with the next
  invocation.

## 7. Failure → the reply already carries the snapshot

A failed step returns `error.snapshot` with the screen at that instant. Read it; do not re-`snap`.
`do` attempts one repair itself and prints what it changed. If it reports none, fall into §3 from
the last good state and re-save the record.

## 8. One record, any language

Records are written in **English**. On a simulator whose UI is not English, `do` translates every
`label` and `labelContains` into that language using the catalogues the OS already ships — the
runtime's `*.lproj/*.strings` plus the app bundle's own — and sends
`labelAnyOf: ["General", "일반"]`. A Korean run replays the same English record: translation happens
*after* the timing lookup, so learned timings are shared across every language a record ever runs
under.

- **Prefer an identifier to a label.** `id` never localizes and needs no index at all. Settings
  exposes them (`com.apple.settings.general`); use one wherever it exists. It is also the only
  reliable answer for a SHORT label: translation is per-string, not per-sense, so `OK` collects
  every catalogue row that spells it — `보통`, `알았어`, `적정`, `확인` — and the alternation then
  matches rows the record never meant. Long labels do not have this problem.
- **A missing translation degrades, it does not fail.** English stays in the alternation, and the
  run report names the labels it could not translate.
- **Recording on a localized simulator is fine.** `record save` maps observed labels back to
  English before writing the record and says so in `history`. It decides from the SIMULATOR's UI
  language, not from what the characters look like — `Buscar` and `Allgemein` are ASCII —
  so `--canonicalize` is only needed to force the pass on an English simulator.
- **Nothing is ever guessed back into a record.** Only a string the catalogue knows as a localized
  value is mapped, and a string with several English originals (확인 has 14) is left alone with a
  warning naming every candidate. Records are unversioned user data: a wrong English label
  round-trips invisibly under `ko` and only surfaces in English.
- **The driver must say it understands `labelAnyOf` before anything is translated.** The build is
  cached in `~/.ui-flow/cache/driver-build`, outside this checkout, so a driver from before this
  feature parses the key and IGNORES it — with `label` gone the query holds no predicates at all
  and a tap lands on the first element on screen. `ping` answers `features=…`, `up` records it, and
  `do` replays in English (saying so) when the live driver does not advertise it. Rebuild with
  `driver/build.sh`.
- **The per-language records already in the repo stay.** The seven `grant-full-access-<lang>.json`
  copies are kept deliberately — a marketing capture pipeline references them by name, an owner
  outside this repo — even though a new flow only ever needs the one English record.
- The pair store is a sqlite file under `$UIFLOW_HOME/cache/strings/`, built once per runtime build
  and language — **8.2 s**, ~21 MB / **112,159 rows** for zh-Hans, ~113,000 for ko — and queried per
  label, never loaded whole: holding the index as a dict measured 250 ms to load and 163 MB resident
  just to translate the 6 labels a real record contains. Warm, open plus 6 label lookups costs
  **0.40 ms**; an English run resolves no oracle at all and costs **0.64 ms**. `--no-localize` turns
  the pass off.

## 9. Every `do` run leaves a report — hand over its path

`do` screenshots the screen before the run and after each record, then writes a self-contained
`report.html`: verdict, a per-record table, the storyboard of screens, and on failure the reason
plus the screen it died on. The images are embedded, so the file outlives the driver session that
produced it.

This is how the user reviews a run they did not watch, so **always give them the path**, alone on
its own line and nothing else on that line:

```
/var/folders/.../uiflow-runs/20260902-104233-148384-com.example.MyApp/report.html
```

`--report-dir <dir>` puts it somewhere durable — use it when the user names a location, or when the
run belongs in a session's documents. `--no-report` skips capture altogether; reach for it only
when asked for the fastest possible replay. Reports land under `$TMPDIR/uiflow-runs`, newest 20 kept.

## 10. Another session may be on that simulator — do not take it

Several sessions share one machine and share one `~/.ui-flow/state`, which records only that a
driver is up on a udid — never which session is using it. A **lease** in `~/Library/Application Support/uiflow/locks/<udid>.json` is the shared book.

- **Sharing a live driver is fine and fast** — `up` reuses it, which is the warm session working.
- **`up` refuses to kill a driver another session holds**, and names the holder. Do not reach for
  `--steal`; take a device of your own with `--device-type <model>`, or wait.
- **Batches serialise per driver.** Every writer — the CLI and the zsh suites alike — takes the
  same lock beside `cmd.json` and draws its request number from the same counter, so two runs queue
  instead of clobbering each other. The driver drops a number it has already served *without
  replying*, so a private counter looks exactly like a hang.

`UIFLOW_OWNER` names this session in that book, falling back to `MAESTRO_SESSION_ID`,
`CLAUDE_CODE_BRIDGE_SESSION_ID` (the conversation — NOT `CLAUDE_CODE_SESSION_ID`, which is minted
per subagent and would make an agent rival its own subagents), then the terminal. **Set it in cron,
CI or any non-interactive run**: with none of those present every session shares one identity and
the lease protects nothing.

Any client can take part, not just the Python CLI: `uiflow lease acquire｜release｜renew｜status`
(`status` exits non-zero when somebody else holds it). The shell suites in `tests/` use exactly
that, so a suite and a `uiflow up` cannot reap each other.

## 11. Delete the simulators ui-flow made

`--create` makes a device; nothing else ever removes one, so they accumulate at ~3 GB each.

```bash
uiflow sim sweep              # list what is reapable, delete nothing
uiflow sim sweep --yes        # delete them
uiflow sim delete --name uiflow-iphoneair
```

`sweep` reaps only devices ui-flow created (recorded in
`~/Library/Application Support/uiflow/created.json`, with the `uiflow-` name prefix as the backstop
when that file is lost). It never touches the device you are working on, one another session holds,
one with a live driver, or one used in the last 24 h — `--older-than <hours>` moves that line. A
paused session's device looks identical to an orphan except by age, which is why the guard exists.

## 12. Never ask the user what the screen can answer

Snapshot instead. Reserve questions for genuinely destructive choices.

## Commands

| | |
| --- | --- |
| `uiflow up` / `down` / `status` | warm session lifecycle (`up --steal` takes a held simulator) |
| `uiflow lease acquire｜release｜renew｜status` | claim a simulator so no other session reaps it |
| `uiflow do <app> <name>…` | replay records + write the run report (`--pace`, `--dry-run`, `--auto-repair`, `--json`, `--no-report`, `--report-dir`, `--file`) |
| `uiflow ls [app] [--all] [--file]` | what is recorded; fragments hidden unless `--all` |
| `uiflow find <query> [--app] [--file]` | what already exists — run before recording anything new |
| `--file <path>` / `UIFLOW_FILES` | a caller's own record JSON: a file, or a dir of `<app>/<name>.json` (§6b) |
| `uiflow cmd '<json array>'` | raw batch (`--app` sets the default bundle id) |
| `uiflow snap [--app] [--type]` | accessibility tree, one JSON element per line |
| `uiflow shot` | screenshot, prints the PNG path |
| `uiflow record mark` / `record save <app> <name> [--kind flow｜fragment] [--use <ref>] [--to <dir>]` | learn a flow, or adopt an existing fragment into it |
| `uiflow factor [app] [--min-run N] [--name X] [--apply]` | extract runs shared by 2+ records into a fragment |
| `uiflow index [--rebuild|--stats]` | the lookup cache behind `find` and `factor` |
| `uiflow sim udid｜prepare｜install｜erase` | simulator (`--device-type` picks the model); `prepare` is install → shutdown → prefs → boot |
| `uiflow sim sweep｜delete` | reap the simulators ui-flow created (`--yes` to delete, `--older-than`) |
| `uiflow idb up｜down｜status｜send｜tap｜type` | fast actuation over idb (§4b) |
| `uiflow record keymap <app>` | learn label → screen point for custom-drawn keys, for the device it runs against |

## Ops

`launch {app,args?}` · `activate` · `terminate` · `appState {app,state}` · `tap` · `tapIfPresent` ·
`doubleTap` · `longPress {durationMs}` · `tapAt {sel?,dx,dy}` · `swipe {direction}` ·
`scrollTo {maxSwipes?}` · `setSwitch {on}` · `adjust {value}` (picker wheel) · `type {text}` ·
`typeKeys {ids}` · `waitFor` / `assert {present?,hittable?}` · `assertValue {equals}` · `snapshot` ·
`screenshot` · `screen` (Springboard's frame in points, for `uiflow idb type`'s device fingerprint) ·
`group {name,satisfiedWhen,steps,verifies,reassert}` · `use {ref}` ·
`repeatUntil {steps,until,maxCycles}` ·
`firstMatch {sels}` · `ping` · `quit` · `sleep {ms}`

**Selector:** `{app,type,id,idPrefix,label,labelContains,labelAnyOf,labelContainsAnyOf,valueEquals,
index,descendant}`. `type` is one of
`button cell staticText switch textField secureTextField key image pickerWheel any`.
Always give a concrete `type` — `any` walks the whole hierarchy and is the most expensive query.
`labelAnyOf` and `labelContainsAnyOf` are the localization pass's own output — `do` adds them when
it translates a `label`/`labelContains` (§8) — never author them by hand; a record holds the
singular fields only.
`labelContains` matches case-insensitively (`CONTAINS[c]`); `label` is exact and case-sensitive.

**Record:** `app, name, kind, description, aliases, requires, satisfiedWhen, steps, verifies,
verifiesWhy, keyMap, provenance, recordedOn, history, timing, health` at
`~/.ui-flow/records/<bundle-id>/<name>.json`.
`recordedOn` is `{"device","w","h","fingerprint"}`, the device the flow was learned on — omitted
entirely when unknown. `do` reads it to warn when a shape-dependent record replays somewhere else
(§3).
`kind` is `flow` (default, omitted from the file) or `fragment`. A `timing` entry is
`{"hits": [ms, ...], "misses": n}` — read it with `timing.unpack`, never by indexing the raw value,
because a pre-migration record still holds a bare list.

**Storage.** Everything writable lives under `UIFLOW_HOME` (default `~/.ui-flow`), never in the skill
directory, so the skill can be replaced wholesale without losing learned flows:
`records/` (the flows — the only irreplaceable part), `state/` (driver sessions and logs),
`cache/driver-build/` (the built driver), `cache/strings/` (the localization pair stores —
rebuildable, pruned to the newest few). `UIFLOW_RECORDS_DIR`, `UIFLOW_STATE_DIR`, `UIFLOW_BUILD_DIR`
and `UIFLOW_STRINGS_DIR` override one apiece — that is how a run is sandboxed off the real records.
Run reports go to the system temp dir; simulator leases to `~/Library/Application Support/uiflow/locks/`.

## Gotchas that cost real time

- **Verify typing by reading the field's `value`, never by the taps succeeding.** A key tap reports
  `ok` whether or not anything was inserted.
- **`type` bypasses the on-screen keyboard.** `typeText` goes through the hardware-keyboard path
  (~1500 CPM), so it does not exercise a custom keyboard at all. To actually test one, tap its keys.
  Through the driver that costs ~578 ms a tap and no argument to `tapAt` makes it cheaper — which is
  what §4b's idb channel exists to escape.
- **A query against a backgrounded app returns empty, not an error** — identical to "the element
  was never there". Check `appState` before concluding something is missing.
- **Absence is vacuously true for an app that never launched**, so open every `satisfiedWhen` with a
  liveness check or a fresh install will skip the setup it needs.
- **The container UUID changes every run.** Never cache a channel path across sessions.
- **The driver drops any seq it has already served**, so a request counter must never restart
  against a live driver — it lives beside `cmd.json` and is seeded from the last reply.
- **Simulator `/tmp` is not host-visible** — the sandbox redirects it.
- **A batch can outlive a stale-looking heartbeat.** Don't kill a session that is mid-flight.
- **The report shows the final attempt.** When `do` auto-repairs and re-runs, the report describes
  the re-run — the failure that triggered the repair is in the terminal output, not the page.
- Element types differ per app: iOS 26 Settings exposes `buttons`, not `cells`.
- App-specific paths live in `~/.ui-flow/records/<app>/notes.md`, never here.
- **A `use` inside a probe list is refused, not ignored.** `satisfiedWhen`, `verifies`, `reassert`
  and `until` are one-shot checks; only `steps` may hold a `use`.
- **An accessibility identifier can itself be localized.** The Settings Full Access switch is
  SwiftUI with no explicit `id`, so under `ko` the pane vends `id="전체 접근 허용"`. Contrast
  `BackButton`, an Apple-vended id that stays stable while its own label localizes to `키보드` —
  prefer an id (§8), but check it under a non-English UI before trusting it.
- **A script subtag names a directory the runtime barely populates.** iOS 26.5 ships `zh-Hans.lproj`
  in 3 bundles against `zh_CN.lproj` in 2,763 (`zh-Hant.lproj` 1, `zh_TW.lproj` 2,766).
  `normalize_lang` maps `Hans → zh_CN` and `Hant → zh_TW`/`zh_HK` for this reason; resolving the
  script spelling alone built a 12-row store and the flow failed at `setSwitch`.
- **A `labelContains` fragment is not a catalogue string.** `Keyboards,` appears in no `.strings`
  file; the trailing punctuation is stripped before the lookup. An identifier avoids the question.
- **A localized record is a bug, not a variant.** Non-English text in a selector means the language
  it was derived on is baked in — canonicalize it rather than saving a second copy.
- **`AppleLanguages` is not always on disk.** A booted device can report `ja` while having no
  `.GlobalPreferences.plist` at all, so the language is read from session state, then the plist,
  then `defaults read` — never inferred from the device name.
- **A `--file` record is written to, not just read from.** Learned timings, health counters and any
  auto-repaired selector land in the caller's own file. That is what makes the second run fast — but
  it means ui-flow modifies a file in someone else's repo, so say so when you introduce it.
- **Nothing locks a record file.** `do` writes `health` and `timing` back after every run, and the
  records root is not a git repo, so two sessions replaying the SAME record silently lose one
  side's stats — no error, and no way to tell afterwards. The simulator lease covers the device,
  not the file. `--file` extends that race into the caller's own repo, so a caller that runs flows
  from CI and locally at once should expect the same loss.
- **An off-screen row is not in the tree at all**, so no `waitFor`/`assert` budget can reach one —
  `scrollTo` it into view first. iOS 26 Settings' Keyboard pane opens at the top with every switch
  two pages down, which is why a record targeting a row below the fold must scroll before it asserts.
- **A simulator that has seen a hardware keyboard docks the software one OFF SCREEN.**
  `com.apple.keyboard.preferences` carries `HardwareKeyboardLastSeen=true` and
  `AutomaticMinimizationEnabled=true`; the keys stay in the accessibility tree but are never hittable
  and no `Next keyboard` globe is vended at all — which reads exactly like "this simulator has no
  stock keyboard". Fix it live, no reboot:
  `uiflow sim prepare` already writes this durably — `simctl.write_prefs` merges
  `AutomaticMinimizationEnabled=False` into `com.apple.keyboard.preferences.plist` while the device
  is shut down, which is why it is a plist write and not a `defaults` call. For a sim that is
  ALREADY BOOTED, the live fix is the other half of that ordering:
  `xcrun simctl spawn <udid> defaults write com.apple.keyboard.preferences AutomaticMinimizationEnabled -bool false`
- **Selector matching is case-insensitive on labels**: `labelContains` is `CONTAINS[c]`, and
  `typeKeys` matches a key id against identifier (case-SENSITIVE) or label (`==[c]`). `label`
  (exact) is the case-SENSITIVE one.
