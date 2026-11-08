#!/bin/zsh
# Task 4 suite: group / repeatUntil / firstMatch.
set -u
source "${0:A:h}/lib.sh"
trap 'uf_cleanup' EXIT INT TERM

SETTINGS=com.apple.Preferences
GENERAL='{"type":"button","label":"General"}'
MISSING='{"type":"button","label":"NoSuchRowExists"}'

UDID=${UIFLOW_UDID:-$(uf_resolve_udid "$UF_SIM_NAME")} || exit 1
uf_require_simulator "$UDID" || exit 1
uf_boot_sim "$UDID" || { print -u2 -- "could not boot $UDID"; exit 1 }
uf_show_software_keyboard "$UDID"

uf_build || { uf_bad "driver builds"; uf_summary test_groups; exit 1 }
uf_start "$UDID" 180 600 || { uf_bad "driver starts"; uf_summary test_groups; exit 1 }

ok_of() { uf_jq "$UF_REPLY" '.ok' }

uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"}]" 60000
uf_check "launch" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"

# --- a satisfied group is skipped, and the check costs one query per condition, not a poll
# Sent twice: the first pays for the first accessibility query against a freshly launched app,
# the second is what a warm session actually costs per skipped group.
GROUP='{"op":"group","name":"settings-open",
        "satisfiedWhen":[{"op":"appState","app":"'$SETTINGS'","state":"runningForeground"},
                         {"op":"assert","sel":'$GENERAL'}],
        "steps":[{"op":"tap","sel":'$GENERAL'}]}'
uf_send "[$GROUP,$GROUP]" 60000
FIRST_GROUP_MS=$(uf_jq "$UF_REPLY" '.results[0].ms')
GROUP_MS=$(uf_jq "$UF_REPLY" '.results[1].ms')
uf_say "satisfied group: first=${FIRST_GROUP_MS}ms steady=${GROUP_MS}ms"
uf_check "a satisfied group is skipped" \
  $([[ $(ok_of) == true && $(uf_jq "$UF_REPLY" '.results[1].skipped') == true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_expect_lt "a satisfied group costs under 400ms" "$GROUP_MS" 400

# --- an unsatisfied group runs its steps, then its verifies, and reports every inner result
uf_send "[{\"op\":\"group\",\"name\":\"open-general\",
           \"satisfiedWhen\":[{\"op\":\"assert\",\"sel\":$MISSING}],
           \"steps\":[{\"op\":\"tap\",\"sel\":$GENERAL},
                     {\"op\":\"waitFor\",\"sel\":{\"type\":\"navigationBar\",\"id\":\"General\"},\"timeoutMs\":8000}],
           \"verifies\":[{\"op\":\"assert\",\"sel\":{\"type\":\"navigationBar\",\"id\":\"General\"}}]}]" 60000
uf_check "an unsatisfied group runs" \
  $([[ $(ok_of) == true && $(uf_jq "$UF_REPLY" '.results[0].skipped') != true ]] && echo 0 || echo 1) "$UF_REPLY"
uf_check "the group reports every inner step in children (steps then verifies)" \
  $([[ $(uf_jq "$UF_REPLY" '.results[0].children | length') == 3 ]] && echo 0 || echo 1) \
  "$(uf_jq "$UF_REPLY" '.results[0].children')"
uf_check "every inner child carries its own ms and op" \
  $(/usr/bin/jq -e -n --argjson r "$UF_REPLY" \
      '$r.results[0].children | all(.[]; has("ms") and has("op") and .ok == true)' >/dev/null 2>&1 && echo 0 || echo 1) \
  "$(uf_jq "$UF_REPLY" '.results[0].children')"

# --- a failing group still reports its children, and names the inner index that failed
uf_send "[{\"op\":\"group\",\"name\":\"will-fail\",
           \"satisfiedWhen\":[{\"op\":\"assert\",\"sel\":$MISSING}],
           \"steps\":[{\"op\":\"ping\"},{\"op\":\"tap\",\"sel\":$MISSING,\"timeoutMs\":400}]}]" 60000
uf_check "a failing group reports notFound" \
  $([[ $(uf_jq "$UF_REPLY" '.error.reason') == notFound ]] && echo 0 || echo 1) "$UF_REPLY"
uf_check "failedPath walks group -> inner step" \
  $([[ $(uf_jq "$UF_REPLY" '.error.failedPath | join(",")') == "0,1" ]] && echo 0 || echo 1) \
  "$(uf_jq "$UF_REPLY" '.error.failedPath')"
uf_check "a failing group still reports its children" \
  $([[ $(uf_jq "$UF_REPLY" '.results[0].children | length') == 2 ]] && echo 0 || echo 1) \
  "$(uf_jq "$UF_REPLY" '.results[0].children')"
uf_check "the failing child is marked not ok" \
  $([[ $(uf_jq "$UF_REPLY" '.results[0].children[1].ok') == false ]] && echo 0 || echo 1) \
  "$(uf_jq "$UF_REPLY" '.results[0].children')"

# --- repeatUntil: stops the moment `until` holds
uf_send "[{\"op\":\"repeatUntil\",\"steps\":[{\"op\":\"sleep\",\"ms\":400}],
           \"until\":[{\"op\":\"assert\",\"sel\":{\"type\":\"navigationBar\",\"id\":\"General\"}}],\"maxCycles\":6}]" 60000
CYCLE_MS=$(uf_jq "$UF_REPLY" '.results[0].ms')
uf_check "repeatUntil returns immediately when until already holds" \
  $([[ $(ok_of) == true && $(uf_jq "$UF_REPLY" '.results[0].info') == *cycles=0* ]] && echo 0 || echo 1) "$UF_REPLY"
uf_expect_lt "an already-satisfied repeatUntil costs under 400ms" "$CYCLE_MS" 400

# --- repeatUntil: the globe cycle it exists for — one tap advances to whichever keyboard is next
KB_CYCLES=$(uf_keyboard_cycle_bound "$UDID")
uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"},
          {\"op\":\"tap\",\"sel\":{\"type\":\"searchField\"},\"timeoutMs\":8000}]" 90000
# `space` is the one key every layout vends, so this separates "no keyboard at all" from "not a
# letter keyboard" — without it the step-away check below passes vacuously against a bare screen.
uf_send '[{"op":"assert","sel":{"type":"key","id":"space"},"hittable":true,"timeoutMs":6000}]' 30000
uf_check "the software keyboard is on screen" $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
# Step off the letter keyboard first, so the repeatUntil below has real work to do.
for cycle in {1..$KB_CYCLES}; do
  uf_send '[{"op":"assert","sel":{"type":"key","labelContains":"q"},"hittable":true,"timeoutMs":500}]' 30000
  [[ $(ok_of) == true ]] || break
  uf_send '[{"op":"tapIfPresent","sel":{"type":"button","label":"Next keyboard"},"timeoutMs":3000},
            {"op":"sleep","ms":800}]' 30000
done
uf_send '[{"op":"assert","sel":{"type":"key","labelContains":"q"},"hittable":true,"timeoutMs":500}]' 30000
uf_check "the letter keyboard can be stepped away from" $([[ $(ok_of) == false ]] && echo 0 || echo 1) "$UF_REPLY"
uf_send "[{\"op\":\"repeatUntil\",
           \"steps\":[{\"op\":\"tapIfPresent\",\"sel\":{\"type\":\"button\",\"label\":\"Next keyboard\"},\"timeoutMs\":3000},
                     {\"op\":\"sleep\",\"ms\":800}],
           \"until\":[{\"op\":\"assert\",\"sel\":{\"type\":\"key\",\"labelContains\":\"q\"},\"hittable\":true}],
           \"maxCycles\":$KB_CYCLES}]" 120000
uf_check "repeatUntil cycles the globe until a letter keyboard is up" \
  $([[ $(ok_of) == true ]] && echo 0 || echo 1) "$UF_REPLY"
GLOBE_INFO=$(uf_jq "$UF_REPLY" '.results[0].info')
uf_say "globe: $GLOBE_INFO"
GLOBE_CYCLES=${GLOBE_INFO#*cycles=}; GLOBE_CYCLES=${GLOBE_CYCLES%% *}
uf_check "repeatUntil actually cycled" $([[ ${GLOBE_CYCLES:-0} -ge 1 ]] && echo 0 || echo 1) "$GLOBE_INFO"
uf_check "repeatUntil reports its inner results in children" \
  $([[ $(uf_jq "$UF_REPLY" '.results[0].children | length') -ge 2 ]] && echo 0 || echo 1) "$UF_REPLY"

# --- repeatUntil gives up at maxCycles
uf_send "[{\"op\":\"repeatUntil\",\"steps\":[{\"op\":\"ping\"}],
           \"until\":[{\"op\":\"assert\",\"sel\":$MISSING}],\"maxCycles\":2}]" 60000
uf_check "repeatUntil gives up at maxCycles" \
  $([[ $(uf_jq "$UF_REPLY" '.error.reason') == timedOut ]] && echo 0 || echo 1) "$UF_REPLY"
uf_check "the give-up names the cycle count" \
  $([[ $(uf_jq "$UF_REPLY" '.error.detail') == *cycles=2* ]] && echo 0 || echo 1) "$(uf_jq "$UF_REPLY" '.error.detail')"

# --- firstMatch probes several selectors in ONE batch
uf_send "[{\"op\":\"launch\",\"app\":\"$SETTINGS\"},
          {\"op\":\"firstMatch\",\"sels\":[$MISSING,{\"type\":\"button\",\"label\":\"NopeEither\"},$GENERAL],\"timeoutMs\":500}]" 60000
uf_check "firstMatch reports the index of the first selector that resolves" \
  $([[ $(ok_of) == true && $(uf_jq "$UF_REPLY" '.results[1].info') == *index=2* ]] && echo 0 || echo 1) \
  "$(uf_jq "$UF_REPLY" '.results[1].info')"
uf_send "[{\"op\":\"firstMatch\",\"sels\":[$MISSING],\"timeoutMs\":500}]" 60000
uf_check "firstMatch reports index=-1 when nothing resolves" \
  $([[ $(ok_of) == true && $(uf_jq "$UF_REPLY" '.results[0].info') == *index=-1* ]] && echo 0 || echo 1) \
  "$(uf_jq "$UF_REPLY" '.results[0].info')"

uf_quit_and_wait && uf_ok "driver quits green" || uf_bad "driver quits green"

print -r -- ""
print -r -- "satisfiedGroupMs=$GROUP_MS satisfiedRepeatUntilMs=$CYCLE_MS"
uf_summary test_groups
