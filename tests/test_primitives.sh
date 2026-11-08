#!/bin/zsh
# Task 3 suite: the primitive op table, driven against Settings.
set -u
source "${0:A:h}/lib.sh"
trap 'uf_cleanup' EXIT INT TERM

SETTINGS=com.apple.Preferences
CAPS='{"type":"switch","labelContains":"Enable Caps Lock"}'

UDID=${UIFLOW_UDID:-$(uf_resolve_udid "$UF_SIM_NAME")} || exit 1
uf_require_simulator "$UDID" || exit 1
uf_boot_sim "$UDID" || { print -u2 -- "could not boot $UDID"; exit 1 }
uf_show_software_keyboard "$UDID"

uf_build || { uf_bad "driver builds"; uf_summary test_primitives; exit 1 }
uf_start "$UDID" 180 600 || { uf_bad "driver starts"; uf_summary test_primitives; exit 1 }

ok_of() { uf_jq "$UF_REPLY" '.ok' }
reason_of() { uf_jq "$UF_REPLY" '.error.reason // ""' }

uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"}]" 60000
uf_check "launch" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- appState is a POSITIVE liveness check (C6): an absence precondition is vacuously true
# against an app that never launched.
uf_send "[{\"op\":\"appState\",\"app\":\"$SETTINGS\",\"state\":\"runningForeground\"}]" 30000
uf_check "appState confirms the app is foreground" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_send '[{"op":"appState","app":"io.dtails.NoSuchAppAnywhere","state":"runningForeground","timeoutMs":500}]' 30000
uf_check "appState fails for an app that is not running" $([[ $(ok_of) == false ]] && echo 0 || echo 1) "$UF_REPLY"

# --- label-only tap, then a bounded scrollTo
uf_send '[{"op":"tap","sel":{"type":"button","label":"General"}},
          {"op":"waitFor","sel":{"type":"navigationBar","id":"General"},"timeoutMs":8000}]' 60000
uf_check "label-only tap navigates" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"

uf_send '[{"op":"scrollTo","sel":{"type":"button","labelContains":"Language & Region"},"maxSwipes":8},
          {"op":"assert","sel":{"type":"button","labelContains":"Language & Region"},"hittable":true}]' 60000
uf_check "scrollTo brings a row below the fold into reach" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_say "scrollTo info: $(uf_jq "$UF_REPLY" '.results[0].info')"

# iOS 26 titles this pane "Keyboards" and opens it scrolled to the top with every switch below the
# fold — and an off-screen row is not in the tree at all, so no waitFor budget can ever reach one.
uf_send "[{\"op\":\"scrollTo\",\"sel\":{\"type\":\"button\",\"label\":\"Keyboard\"},\"maxSwipes\":8},
          {\"op\":\"tap\",\"sel\":{\"type\":\"button\",\"label\":\"Keyboard\"}},
          {\"op\":\"waitFor\",\"sel\":{\"type\":\"button\",\"id\":\"KEYBOARDS\"},\"timeoutMs\":8000},
          {\"op\":\"scrollTo\",\"sel\":$CAPS,\"maxSwipes\":10}]" 60000
uf_check "reaches the Keyboard pane" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- setSwitch resolves the DESCENDANT switch (C5), and assertValue can read it back
uf_send "[{\"op\":\"setSwitch\",\"sel\":$CAPS,\"on\":false},
          {\"op\":\"assertValue\",\"sel\":$CAPS,\"equals\":\"0\",\"timeoutMs\":4000}]" 60000
uf_check "setSwitch off, assertValue reads 0" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"

uf_send "[{\"op\":\"setSwitch\",\"sel\":$CAPS,\"on\":true},
          {\"op\":\"assertValue\",\"sel\":$CAPS,\"equals\":\"1\",\"timeoutMs\":4000}]" 60000
uf_check "setSwitch on, assertValue reads 1" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"

uf_send "[{\"op\":\"setSwitch\",\"sel\":$CAPS,\"on\":true}]" 60000
uf_check "setSwitch is a no-op when already in the wanted state" \
  $([[ $(ok_of) == true && $(uf_jq "$UF_REPLY" '.results[0].skipped') == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- tapAt: the row centre hits the label, the right edge hits the control (C5)
uf_send "[{\"op\":\"tapAt\",\"sel\":$CAPS,\"dx\":0.95,\"dy\":0.5},
          {\"op\":\"assertValue\",\"sel\":$CAPS,\"equals\":\"0\",\"timeoutMs\":4000}]" 60000
uf_check "tapAt a normalized offset toggles the row" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_send "[{\"op\":\"setSwitch\",\"sel\":$CAPS,\"on\":true}]" 60000

# --- tapIfPresent skips instead of failing
uf_send '[{"op":"tapIfPresent","sel":{"type":"button","label":"NoSuchRowExists"},"timeoutMs":400}]' 30000
uf_check "tapIfPresent reports skipped" \
  $([[ $(ok_of) == true && $(uf_jq "$UF_REPLY" '.results[0].skipped') == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- a failure names the failing index and carries a JSON snapshot
uf_send '[{"op":"ping"},{"op":"tap","sel":{"type":"button","label":"NoSuchRowExists"},"timeoutMs":400}]' 30000
uf_check "a failing step reports notFound" $([[ $(reason_of) == notFound ]] && echo 0 || echo 1) "$UF_REPLY"
uf_check "failedPath names the failing index" \
  $([[ $(uf_jq "$UF_REPLY" '.error.failedPath | @csv') == '1' ]] && echo 0 || echo 1) "$(uf_jq "$UF_REPLY" '.error.failedPath')"
uf_jq "$UF_REPLY" '.error.snapshot[]' > "$UF_STATE/failure-snapshot.txt"
uf_check "the failure snapshot is JSON lines" \
  $(/usr/bin/jq -e . "$UF_STATE/failure-snapshot.txt" >/dev/null 2>&1 && echo 0 || echo 1) \
  "$(head -2 "$UF_STATE/failure-snapshot.txt")"
uf_check "the steps before the failure still report" \
  $([[ $(uf_jq "$UF_REPLY" '.results | length') == 2 ]] && echo 0 || echo 1) "$UF_REPLY"

# --- type: one typeText at HID rate, into a resolved field
uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"},
          {\"op\":\"tap\",\"sel\":{\"type\":\"searchField\"},\"timeoutMs\":8000},
          {\"op\":\"type\",\"text\":\"the quick brown fox jumps over the lazy dog\",\"sel\":{\"type\":\"searchField\"}}]" 90000
uf_check "type into a resolved field" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
CPM_INFO=$(uf_jq "$UF_REPLY" '.results[2].info')
CPM=${CPM_INFO#*cpm=}; CPM=${CPM%% *}
uf_check "typing beats 300 CPM" $([[ ${CPM:-0} -gt 300 ]] && echo 0 || echo 1) "info=$CPM_INFO"

# --- a label containing a quote survives the snapshot round trip (C12)
uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"},
          {\"op\":\"tap\",\"sel\":{\"type\":\"searchField\"},\"timeoutMs\":8000},
          {\"op\":\"type\",\"text\":\"a\\\"b\",\"sel\":{\"type\":\"searchField\"}},
          {\"op\":\"snapshot\",\"app\":\"$SETTINGS\"}]" 90000
uf_jq "$UF_REPLY" '.results[3].info' > "$UF_STATE/quote-snapshot.txt"
uf_check "quoted text round-trips through the snapshot" \
  $(/usr/bin/jq -sre 'any(.[]; (.value // "") | test("a\"b"))' "$UF_STATE/quote-snapshot.txt" >/dev/null 2>&1 && echo 0 || echo 1) \
  "$(grep -c . "$UF_STATE/quote-snapshot.txt") lines"

# --- typeKeys taps type-scoped keys with no fixed inter-key sleep.
# The keyboard that comes up is whichever the simulator used last, so the globe is cycled until a
# LETTER key is in front, the same bounded re-resolve the kit does. `space` is the liveness probe:
# it is the one key every layout vends, so a missing keyboard is reported as itself rather than as
# a globe that never reached a letter.
KB_CYCLES=$(uf_keyboard_cycle_bound "$UDID")
uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"},
          {\"op\":\"tap\",\"sel\":{\"type\":\"searchField\"},\"timeoutMs\":8000}]" 90000
uf_send '[{"op":"assert","sel":{"type":"key","id":"space"},"hittable":true,"timeoutMs":6000}]' 30000
uf_check "the software keyboard is on screen" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
reached=1
for cycle in {1..$KB_CYCLES}; do
  uf_send '[{"op":"assert","sel":{"type":"key","labelContains":"q"},"hittable":true,"timeoutMs":500}]' 30000
  [[ $(ok_of) == true ]] && { reached=0; break }
  uf_send '[{"op":"tapIfPresent","sel":{"type":"button","label":"Next keyboard"},"timeoutMs":3000},
            {"op":"sleep","ms":900}]' 30000
done
uf_check "a stock keyboard can be reached with the globe" $reached "cycled $KB_CYCLES times without reaching a letter keyboard"

uf_send '[{"op":"typeKeys","ids":["q","w","e"],"sel":{"type":"key"}},
          {"op":"snapshot"}]' 90000
uf_check "typeKeys taps stock keyboard keys" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_say "typeKeys info: $(uf_jq "$UF_REPLY" '.results[0].info')"
uf_jq "$UF_REPLY" '.results[1].info' > "$UF_STATE/typekeys-snapshot.txt"
TYPED=$(/usr/bin/jq -sr '[.[] | select(.type == "searchField") | .value // ""][0] // ""' "$UF_STATE/typekeys-snapshot.txt")
uf_check "typeKeys produced qwe (auto-shift makes the comparison case-insensitive)" \
  $([[ ${TYPED:l} == "qwe" ]] && echo 0 || echo 1) "value=$TYPED"

# --- screenshot falls back to the screen, never to a bare XCUIApplication() (C13)
uf_send '[{"op":"screenshot"}]' 60000
SHOT=$(uf_jq "$UF_REPLY" '.results[0].info')
SHOT=${SHOT#*path=}
uf_check "screenshot writes a PNG to the channel" \
  $([[ -f "$SHOT" && $(wc -c < "$SHOT") -gt 1000 ]] && echo 0 || echo 1) "$SHOT"

uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"}]" 60000
uf_quit_and_wait && uf_ok "driver quits green" || uf_bad "driver quits green"

print -r -- ""
print -r -- "cpm=$CPM"
uf_summary test_primitives
