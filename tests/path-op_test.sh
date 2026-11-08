#!/bin/zsh
# The `path`, `tapSeq` and `ops` sidecar ops, proven against Settings.
#
# Screenshots come from the sidecar's own `screenshot` op, not `uiflow shot`: this suite tests the
# idb channel, and routing the evidence through the XCUITest driver would make it depend on a
# driver build and a channel lease it otherwise has no use for.
set -u
source "${0:A:h}/lib.sh"

SETTINGS=com.apple.Preferences
UF_SHOTS="$UF_STATE/path-op"

path_op_cleanup() {
  local rc=$?
  [[ -n "$UF_UDID" ]] && "$UF_ROOT/bin/uiflow" idb down --udid "$UF_UDID" >/dev/null 2>&1
  uf_cleanup
  return $rc
}
trap 'path_op_cleanup' EXIT INT TERM

# Its own device type, so a run never contends with whoever holds the default iPhone 17 Pro.
UDID=${UIFLOW_UDID:-$("$UF_ROOT/bin/uiflow" sim udid --device-type "iPhone Air" --create 2>/dev/null)} || exit 1
[[ -n "$UDID" ]] || { print -u2 -- "could not resolve a simulator"; exit 1 }
uf_require_simulator "$UDID" || exit 1
uf_boot_sim "$UDID" || { print -u2 -- "could not boot $UDID"; exit 1 }

if ! "$UF_ROOT/bin/uiflow" lease acquire --udid "$UDID" >/dev/null 2>&1; then
  print -u2 -- "refusing: another ui-flow session holds $UDID —"
  "$UF_ROOT/bin/uiflow" lease status --udid "$UDID" >&2
  exit 1
fi
UF_LEASED="$UDID"
UF_LEASE_RENEWED=$EPOCHSECONDS
UF_UDID="$UDID"

"$UF_ROOT/bin/uiflow" idb up --udid "$UDID" >/dev/null || {
  uf_bad "idb pair comes up"; uf_summary path-op; exit 1
}
uf_ok "idb pair comes up"

mkdir -p "$UF_SHOTS"
rm -f "$UF_SHOTS"/*.png(N) "$UF_SHOTS"/*.gray(N)

idb_send() { UF_REPLY=$("$UF_ROOT/bin/uiflow" idb send "$1" --udid "$UDID" --json 2>&1) }
ok_of() { uf_jq "$UF_REPLY" '.ok' }

# ---------------------------------------------------------------- capability

idb_send '[{"op":"ops"}]'
uf_check "the sidecar answers ops" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
OPS=$(uf_jq "$UF_REPLY" '.results[0].ops // [] | join(" ")')
for wanted in path tapSeq ping; do
  uf_check "ops lists '$wanted'" $([[ " $OPS " == *" $wanted "* ]] && echo 0 || echo 1) "ops=$OPS"
done

# ---------------------------------------------------------------- the drag

idb_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"},{\"op\":\"sleep\",\"ms\":2500},{\"op\":\"describe\"}]"
uf_check "Settings launches" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
W=$(uf_jq "$UF_REPLY" '.results[2].width'); H=$(uf_jq "$UF_REPLY" '.results[2].height')
uf_say "screen ${W}x${H}"

shot() {
  idb_send "[{\"op\":\"screenshot\",\"path\":\"$UF_SHOTS/$1.png\"}]"
  [[ $(ok_of) == true ]] || { uf_bad "screenshot $1" "$UF_REPLY"; return 1 }
  return 0
}

# A baseline pair with nothing between them: without it a delta means "the two PNGs differ", which
# a status-bar clock alone can satisfy.
shot base-a || { uf_summary path-op; exit 1 }
idb_send '[{"op":"sleep","ms":900}]'
shot base-b || { uf_summary path-op; exit 1 }

POINTS=$(python3 - "$W" "$H" <<'PY'
import json, sys
width, height = float(sys.argv[1]), float(sys.argv[2])
x = width / 2
start, end, count = height * 0.78, height * 0.28, 40
points = []
for index in range(count):
    fraction = index / (count - 1)
    y = start + (end - start) * fraction
    points.append([round(x, 1), round(y, 1), 80 if index == 0 else 10])
print(json.dumps(points))
PY
)

idb_send "[{\"op\":\"path\",\"points\":$POINTS},{\"op\":\"sleep\",\"ms\":1200}]"
uf_check "the path op runs" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_check "the path op reports all 40 points" \
  $([[ $(uf_jq "$UF_REPLY" '.results[0].points') == 40 ]] && echo 0 || echo 1) "$UF_REPLY"
shot after || { uf_summary path-op; exit 1 }

gray() { ffmpeg -v error -y -i "$1" -vf scale=32:64 -pix_fmt gray -f rawvideo "$2" }
delta() {
  python3 -c 'import sys
a = open(sys.argv[1], "rb").read()
b = open(sys.argv[2], "rb").read()
n = min(len(a), len(b))
print("%.2f" % (sum(abs(a[i] - b[i]) for i in range(n)) / n if n else 0.0))' "$1" "$2"
}

for name in base-a base-b after; do gray "$UF_SHOTS/$name.png" "$UF_SHOTS/$name.gray"; done
BASELINE=$(delta "$UF_SHOTS/base-a.gray" "$UF_SHOTS/base-b.gray")
MOVED=$(delta "$UF_SHOTS/base-b.gray" "$UF_SHOTS/after.gray")
uf_say "mean per-cell delta: baseline=$BASELINE after-drag=$MOVED"
uf_check "an idle pair of frames barely differs" $(( BASELINE < 3 ? 0 : 1 )) "baseline=$BASELINE"
uf_check "the drag visibly moved the screen (mean per-cell delta > 3)" \
  $(( MOVED > 3 ? 0 : 1 )) "delta=$MOVED, baseline=$BASELINE"

# ---------------------------------------------------------------- tapSeq

TAPS=$(python3 - "$W" "$H" <<'PY'
import json, sys
width, height = float(sys.argv[1]), float(sys.argv[2])
print(json.dumps([[round(width / 2, 1), round(height * 0.02, 1), 171] for _ in range(20)]))
PY
)
idb_send "[{\"op\":\"tapSeq\",\"taps\":$TAPS,\"holdMs\":4}]"
uf_check "tapSeq runs" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
CPM=$(uf_jq "$UF_REPLY" '.results[0].cpm')
uf_say "tapSeq: $(uf_jq "$UF_REPLY" '.results[0] | @json')"
uf_check "tapSeq holds a 171 ms gap to within 10% of 350 CPM" \
  $([[ "$CPM" == <-> ]] && (( CPM > 315 && CPM < 385 )) && echo 0 || echo 1) "cpm=$CPM"

# ---------------------------------------------------------------- bad input

idb_send '[{"op":"path"}]'
uf_check "path without points is a badStep" \
  $([[ $(uf_jq "$UF_REPLY" '.error.reason') == badStep ]] && echo 0 || echo 1) "$UF_REPLY"
idb_send '[{"op":"tapSeq","taps":[[10]]}]'
uf_check "a tap missing its y is a badStep" \
  $([[ $(uf_jq "$UF_REPLY" '.error.reason') == badStep ]] && echo 0 || echo 1) "$UF_REPLY"

print -r -- ""
print -r -- "baseline=$BASELINE delta=$MOVED cpm=$CPM"
uf_summary path-op
