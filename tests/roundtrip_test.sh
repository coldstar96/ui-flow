#!/bin/zsh
# Task 1 suite: the driver builds, publishes a channel, answers a ping, journals, and quits green.
set -u
source "${0:A:h}/lib.sh"
trap 'uf_cleanup' EXIT INT TERM

UDID=${UIFLOW_UDID:-$(uf_resolve_udid "$UF_SIM_NAME")} || exit 1
uf_require_simulator "$UDID" || exit 1
uf_boot_sim "$UDID" || { print -u2 -- "could not boot $UDID"; exit 1 }
print -r -- "simulator: $UF_SIM_NAME ($UDID)"

uf_build || { uf_bad "driver builds"; uf_summary roundtrip_test; exit 1 }
uf_ok "driver builds -> $UF_XCTESTRUN"

uf_start "$UDID" 120 300 || { uf_bad "driver starts and publishes a channel"; uf_summary roundtrip_test; exit 1 }
uf_ok "driver published channel $CHANNEL"
uf_expect_lt "startupMs under 20000" "$UF_STARTUP_MS" 20000

uf_send '[{"op":"ping"}]' 20000 || { uf_bad "ping round trip"; uf_summary roundtrip_test; exit 1 }
reply="$UF_REPLY"
first_ms=$(printf '%.0f' "$UF_LAST_MS")
uf_check "ping replies ok" $([[ $(uf_jq "$reply" '.ok') == true ]] && echo 0 || echo 1) "$reply"
uf_check "ping result op is ping" $([[ $(uf_jq "$reply" '.results[0].op') == ping ]] && echo 0 || echo 1) "$reply"

# Warm round trip: the first send pays for the driver's first file-system touch.
total=0
for i in 1 2 3 4 5; do
  uf_send '[{"op":"ping"}]' 20000 || break
  total=$(( total + UF_LAST_MS ))
done
PING_MS=$(printf '%.0f' $(( total / 5.0 )))
uf_expect_lt "pingRoundTripMs under 200" "$PING_MS" 200

# A request the driver cannot decode must be answered, never swallowed (C15).
# Under the channel lock like every other write: one simulator has one channel, and a concurrent
# python send would otherwise have its cmd.json overwritten or its reply deleted mid-poll.
rawfd=""
if ! uf_lock_channel rawfd 10; then
  # Falling through would write cmd.json unlocked — exactly the clobbering the lock exists to
  # prevent, and a concurrent python send would lose its reply mid-poll with no diagnostic.
  uf_bad "could not lock the channel for the badRequest probe (skipped)"
else
  {
    rm -f "$CHANNEL/reply.json"
    print -r -- '{"seq":99,"steps":[{"op":]}' > "$CHANNEL/cmd.tmp"
    mv -f "$CHANNEL/cmd.tmp" "$CHANNEL/cmd.json"
    bad=""
    deadline=$(( EPOCHSECONDS + 10 ))
    while (( EPOCHSECONDS < deadline )); do
      [[ -f "$CHANNEL/reply.json" ]] && { bad=$(<"$CHANNEL/reply.json"); break }
      zselect -t 5
    done
  } always {
    [[ -n "$rawfd" ]] && exec {rawfd}>&-
  }
  uf_check "undecodable request is answered badRequest" \
    $([[ $(uf_jq "$bad" '.error.reason') == badRequest ]] && echo 0 || echo 1) "$bad"
fi
UF_SEQ=99

uf_send '[{"op":"sleep","ms":50}]' 20000 || uf_bad "sleep op"
reply="$UF_REPLY"
uf_check "driver still serves after a bad request" \
  $([[ $(uf_jq "$reply" '.ok') == true ]] && echo 0 || echo 1) "$reply"

uf_expect_file "heartbeat exists" "$CHANNEL/heartbeat"
uf_expect_file "journal.ndjson exists" "$CHANNEL/journal.ndjson"
uf_check "journal lines parse as JSON" \
  $(/usr/bin/jq -e . "$CHANNEL/journal.ndjson" >/dev/null 2>&1 && echo 0 || echo 1) \
  "$(tail -3 "$CHANNEL/journal.ndjson")"

if uf_quit_and_wait; then uf_ok "quit exits the driver green"; else uf_bad "quit exits the driver green" "$(tail -5 "$UF_LOG")"; fi

print -r -- ""
print -r -- "startupMs=$UF_STARTUP_MS pingRoundTripMs=$PING_MS firstPingMs=$first_ms"
uf_summary roundtrip_test
