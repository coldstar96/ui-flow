#!/bin/zsh
# Task 2 suite: selector resolution and the snapshot primitive, driven against Settings.
set -u
source "${0:A:h}/lib.sh"
trap 'uf_cleanup' EXIT INT TERM

SETTINGS=com.apple.Preferences

UDID=${UIFLOW_UDID:-$(uf_resolve_udid "$UF_SIM_NAME")} || exit 1
uf_require_simulator "$UDID" || exit 1
uf_boot_sim "$UDID" || { print -u2 -- "could not boot $UDID"; exit 1 }

uf_build || { uf_bad "driver builds"; uf_summary test_resolution; exit 1 }
uf_start "$UDID" 120 300 || { uf_bad "driver starts"; uf_summary test_resolution; exit 1 }

uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"}]" 60000 || { uf_bad "launch Settings"; uf_summary test_resolution; exit 1 }
uf_check "Settings launches" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- label-only resolution: no identifier anywhere in the selector
uf_send "[{\"op\":\"waitFor\",\"sel\":{\"type\":\"button\",\"label\":\"General\"},\"timeoutMs\":8000}]" 30000
uf_check "label-only selector resolves" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_say "$(uf_jq "$UF_REPLY" '.results[0].info')"

# --- any-of resolution: one selector, several candidate labels, only one of them real
uf_send '[{"op":"waitFor","sel":{"type":"button","labelAnyOf":["Zzzzz","General"]},"timeoutMs":8000}]' 30000
uf_check "labelAnyOf resolves when one alternative matches" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"

uf_send '[{"op":"waitFor","sel":{"type":"button","labelContainsAnyOf":["Zzzzz","Gener"]},"timeoutMs":8000}]' 30000
uf_check "labelContainsAnyOf resolves on a substring alternative" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"

uf_send '[{"op":"assert","sel":{"type":"button","labelAnyOf":["Zzzzz","Qqqqq"]},"present":false,"timeoutMs":2000}]' 30000
uf_check "labelAnyOf matches nothing when no alternative exists" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- assertValue must honour the alternation too, or a precondition passes vacuously. The
# Settings root list carries no switch elements at all, so the trap only fires on a screen that
# has real switches sitting at value "1" — General > Keyboard does.
uf_send '[{"op":"tap","sel":{"type":"button","label":"General"},"timeoutMs":4000},
          {"op":"tap","sel":{"type":"button","label":"Keyboard"},"timeoutMs":4000}]' 30000
uf_check "navigates to a screen with real switches" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"

uf_send '[{"op":"assertValue","sel":{"type":"switch","labelAnyOf":["Zzzzz","Qqqqq"]},"equals":"1","timeoutMs":2000}]' 30000
uf_check "assertValue does NOT resolve on an alternation naming nothing" $([[ $(uf_jq "$UF_REPLY" '.ok') == false ]] && echo 0 || echo 1) "$UF_REPLY"

# The driver's `launch` is a cold relaunch (XCUITest terminates any running instance first), so it
# resets Settings to root without an explicit terminate. Assert a ROOT-ONLY element rather than
# `.ok`: `launch` cannot report WHERE it landed, and the General row's label is no witness — the
# Keyboard pane's back button is {"type":"button","id":"BackButton","label":"General"}, so a
# label-based check passes on both screens and every later case would run on the wrong one.
uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"},
          {\"op\":\"assert\",\"sel\":{\"type\":\"button\",\"id\":\"com.apple.settings.general\"},\"present\":true,\"timeoutMs\":6000},
          {\"op\":\"assert\",\"sel\":{\"type\":\"button\",\"id\":\"BackButton\"},\"present\":false,\"timeoutMs\":1000}]" 60000
uf_check "relaunch lands on the Settings ROOT, not a restored sub-pane" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- one-shot matching: a satisfied precondition costs one query, not a poll
# Two identical one-shot asserts: the first pays for the accessibility server's first query
# against a freshly launched app, the second is the steady-state cost a precondition pays.
uf_send '[{"op":"assert","sel":{"type":"button","label":"General"},"timeoutMs":0},
          {"op":"assert","sel":{"type":"button","label":"General"},"timeoutMs":0}]' 30000
FIRST_MATCH_MS=$(uf_jq "$UF_REPLY" '.results[0].ms')
MATCH_MS=$(uf_jq "$UF_REPLY" '.results[1].ms')
uf_say "one-shot assert: first=${FIRST_MATCH_MS}ms steady=${MATCH_MS}ms"
uf_check "one-shot assert succeeds" $([[ $(uf_jq "$UF_REPLY" '.ok') == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_expect_lt "matches returns under 200ms" "$MATCH_MS" 200

# --- iterate to the first HITTABLE match (C4), never a fixed index 0
# Two swipes put the first rows above the fold: they stay in the tree and stop being hittable,
# which is precisely the shape `KeyGrid.hittableKey` exists for.
uf_send '[{"op":"swipe","direction":"up"},{"op":"swipe","direction":"up"}]' 30000
uf_send '[{"op":"waitFor","sel":{"type":"button"},"hittable":false,"timeoutMs":4000},
          {"op":"waitFor","sel":{"type":"button"},"hittable":true,"timeoutMs":4000}]' 30000
loose=$(uf_jq "$UF_REPLY" '.results[0].info')
strict=$(uf_jq "$UF_REPLY" '.results[1].info')
uf_say "loose=$loose strict=$strict"
loose_picked=${loose#*picked=}; loose_picked=${loose_picked%% *}
strict_picked=${strict#*picked=}; strict_picked=${strict_picked%% *}
strict_count=${strict#*matches=}; strict_count=${strict_count%% *}
uf_check "the unfiltered match set has more than one candidate" \
  $([[ ${strict_count:-0} -gt 1 ]] && echo 0 || echo 1) "$strict"
uf_check "requiring hittability picks a later match than index 0" \
  $([[ ${strict_picked:-0} -gt ${loose_picked:-0} ]] && echo 0 || echo 1) "loose=$loose strict=$strict"

# --- a missing element resolves to notFound, with a snapshot and a failedPath
uf_send '[{"op":"assert","sel":{"type":"button","label":"NoSuchRowExists"},"timeoutMs":300}]' 30000
uf_check "a missing element reports notFound" \
  $([[ $(uf_jq "$UF_REPLY" '.error.reason') == notFound ]] && echo 0 || echo 1) "$UF_REPLY"
uf_check "the failure carries a failedPath" \
  $([[ $(uf_jq "$UF_REPLY" '.error.failedPath | length') -ge 1 ]] && echo 0 || echo 1) "$UF_REPLY"
uf_check "the failure carries snapshot lines" \
  $([[ $(uf_jq "$UF_REPLY" '.error.snapshot | length') -gt 0 ]] && echo 0 || echo 1) "$UF_REPLY"

# --- snapshot: one app.snapshot() per app, one JSON object per line, under 400ms
uf_send '[{"op":"snapshot"}]' 30000
SNAP_MS=$(uf_jq "$UF_REPLY" '.results[0].ms')
uf_jq "$UF_REPLY" '.results[0].info' > "$UF_STATE/snapshot-lines.txt"
LINES=$(wc -l < "$UF_STATE/snapshot-lines.txt" | tr -d ' ')
uf_check "snapshot returns rows" $([[ ${LINES:-0} -gt 10 ]] && echo 0 || echo 1) "$LINES lines"
uf_expect_lt "snapshot under 400ms" "$SNAP_MS" 400
uf_check "every snapshot line parses as JSON" \
  $(/usr/bin/jq -e . "$UF_STATE/snapshot-lines.txt" >/dev/null 2>&1 && echo 0 || echo 1) \
  "$(head -3 "$UF_STATE/snapshot-lines.txt")"
uf_check "every snapshot line carries a frame" \
  $(/usr/bin/jq -se 'all(.[]; (.frame | length) == 4)' "$UF_STATE/snapshot-lines.txt" >/dev/null 2>&1 && echo 0 || echo 1) \
  "$(head -3 "$UF_STATE/snapshot-lines.txt")"
uf_check "snapshot covers the app under test" \
  $(/usr/bin/jq -sre --arg a "$SETTINGS" 'any(.[]; .app == $a)' "$UF_STATE/snapshot-lines.txt" >/dev/null 2>&1 && echo 0 || echo 1)
# Redaction is by LENGTH, not by the absence of an identifier: a short unidentified label is a
# control glyph or UI copy and is the only handle an app with no identifiers offers, while long
# unidentified prose is the app's own content and stays withheld.
uf_check "unidentified staticText prose is withheld" \
  $(/usr/bin/jq -se 'all(.[]; select(.type == "staticText" and (.id == "" or .id == null) and (.label != null)) | (.label | length) <= 40)' \
     "$UF_STATE/snapshot-lines.txt" >/dev/null 2>&1 && echo 0 || echo 1) \
  "a long unidentified staticText label was emitted verbatim"
uf_check "withheld labels still report their length" \
  $(/usr/bin/jq -se 'all(.[]; select(.type == "staticText" and (.id == "" or .id == null) and (has("label") | not)) | has("labelLen"))' \
     "$UF_STATE/snapshot-lines.txt" >/dev/null 2>&1 && echo 0 || echo 1)

uf_quit_and_wait && uf_ok "driver quits green" || uf_bad "driver quits green"

print -r -- ""
print -r -- "snapshotMs=$SNAP_MS matchMs=$MATCH_MS snapshotLines=$LINES"
uf_summary test_resolution
