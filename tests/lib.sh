# Shared harness for the ui-flow shell suites.
#
# zsh, not bash: this machine ships bash 3.2 only, which has no ${EPOCHREALTIME}. Forking a
# `python3`/`date` per timestamp costs ~30 ms inside a ~40 ms measurement (review finding 28), so
# timing comes from zsh/datetime and the reply poll from zsh/zselect — both builtins, no forks.

zmodload zsh/datetime
zmodload zsh/system
zmodload zsh/zselect

UF_ROOT="${0:A:h:h}"
UF_DRIVER_BUNDLE_ID="dev.uiflow.driver.tests"
UF_RUNNER_ID="${UF_DRIVER_BUNDLE_ID}.xctrunner"
# Derived, never hardcoded: the structural name is owned by lib/uiflow/device.py.
UF_SIM_NAME="${UIFLOW_SIM_NAME:-$(python3 -c "import sys; sys.path.insert(0, '$UF_ROOT/lib'); from uiflow import device; print(device.structural_name())")}"
# Derived, never hardcoded: the state dir is owned by lib/uiflow/paths.py and lives outside the skill.
UF_STATE="$(python3 -c "import sys; sys.path.insert(0, '$UF_ROOT/lib'); from uiflow import paths; print(paths.STATE_DIR)")"
UF_SUITE="${${ZSH_ARGZERO:-uiflow}:t:r}"

UF_PASS=0
UF_FAIL=0
UF_LOG=""
UF_XCTESTRUN=""
UF_RESULT_BUNDLE=""
DRIVER_PID=""
UF_UDID=""
CHANNEL=""
UF_SEQ=0
UF_LEASED=""
UF_LEASE_RENEWED=0
UF_REPLY=""
UF_LAST_MS=0
UF_STARTUP_MS=0

uf_now_ms() { print -r -- $(( ${EPOCHREALTIME} * 1000 )) }
uf_ms() { printf '%.0f' $(uf_now_ms) }

uf_say() { print -r -- "  $*" }

uf_ok() { UF_PASS=$((UF_PASS + 1)); print -r -- "PASS  $1" }
uf_bad() { UF_FAIL=$((UF_FAIL + 1)); print -r -- "FAIL  $1"; [[ -n "${2-}" ]] && print -r -- "      $2" }

uf_check() {
  # uf_check <description> <condition-result 0/1> [detail]
  if [[ "$2" == 0 ]]; then uf_ok "$1"; else uf_bad "$1" "${3-}"; fi
}

uf_expect_lt() {
  local desc="$1" actual="$2" limit="$3"
  [[ "$actual" == "null" || -z "$actual" ]] && { uf_bad "$desc" "no measurement"; return }
  if (( actual < limit )); then uf_ok "$desc ($actual < $limit)"; else uf_bad "$desc" "$actual >= $limit"; fi
}

uf_expect_file() {
  if [[ -f "$2" ]]; then uf_ok "$1"; else uf_bad "$1" "missing: $2"; fi
}

# ---------------------------------------------------------------- simulator

uf_resolve_udid() {
  local name="$1" udid
  udid=$(xcrun simctl list devices --json \
    | /usr/bin/jq -r --arg n "$name" '
        [ .devices | to_entries[] | .value[] | select(.name == $n) ]
        | sort_by(.state == "Booted") | reverse | .[0].udid // empty')
  if [[ -z "$udid" ]]; then
    print -u2 -- "no simulator named '$name'"
    return 1
  fi
  print -r -- "$udid"
}

uf_require_simulator() {
  # Mirrors `make test`: a device destination is refused outright, never merely warned about.
  local udid="$1"
  if [[ ! -d "$HOME/Library/Developer/CoreSimulator/Devices/$udid" ]]; then
    print -u2 -- "refusing: $udid is not an iOS Simulator (ui-flow drives simulators only)"
    return 1
  fi
  return 0
}

uf_boot_sim() {
  local udid="$1" state
  state=$(xcrun simctl list devices --json | /usr/bin/jq -r --arg u "$udid" \
    '[ .devices | to_entries[] | .value[] | select(.udid == $u) ][0].state // "Unknown"')
  [[ "$state" == "Booted" ]] && return 0
  xcrun simctl boot "$udid" >/dev/null 2>&1
  local deadline=$(( EPOCHSECONDS + 120 ))
  while (( EPOCHSECONDS < deadline )); do
    xcrun simctl spawn "$udid" launchctl print system >/dev/null 2>&1 && return 0
    zselect -t 50
  done
  return 1
}

uf_show_software_keyboard() {
  # A simulator that has ever seen a hardware keyboard docks the software one OFF SCREEN: its keys
  # stay in the accessibility tree, present but never hittable, and no `Next keyboard` globe is
  # vended at all — which reads exactly like "this simulator has no stock keyboard".
  xcrun simctl spawn "$1" defaults write com.apple.keyboard.preferences \
    AutomaticMinimizationEnabled -bool false >/dev/null 2>&1
  return 0
}

uf_keyboard_cycle_bound() {
  # The globe rotation can never be longer than the installed-keyboard list, so probing that many
  # positions plus the one we start on visits every keyboard exactly once. Derived, never a
  # hardcoded count: adding a keyboard to the simulator must not turn the globe tests red.
  local plist="$HOME/Library/Developer/CoreSimulator/Devices/$1/data/Library/Preferences/.GlobalPreferences.plist"
  local n
  n=$(/usr/bin/plutil -extract AppleKeyboards json -o - "$plist" 2>/dev/null | /usr/bin/jq 'length' 2>/dev/null)
  [[ "$n" == <-> ]] && (( n > 0 )) || n=4
  print -r -- $(( n + 1 ))
}

# ---------------------------------------------------------------- channel

uf_channel_from_log() {
  # The driver prints its own channel as `UIFLOW_CHANNEL=<path>` before publishing `ready`, and that
  # is the only authoritative answer. Scanning the simulator's containers for the freshest `ready`
  # instead adopts whichever runner published last — which, when another agent session is driving
  # the same simulator, is somebody else's driver, and every send then times out.
  local line
  [[ -f "$UF_LOG" ]] || return 1
  line=$(grep -a -o 'UIFLOW_CHANNEL=.*' "$UF_LOG" 2>/dev/null | tail -1)
  [[ -n "$line" ]] || return 1
  print -r -- "${line#UIFLOW_CHANNEL=}"
}

uf_lock_channel() {
  # uf_lock_channel <fd-var-name> [timeout_s] — `zsystem flock` opens without O_CREAT, so the file
  # must exist first. Release with `exec {fd}>&-`.
  # NOT reentrant: fcntl locks are per-process but this takes a second fd, so calling uf_send from
  # inside a locked region self-deadlocks and reports it as "held by another session".
  local fdvar="$1" timeout_s="${2:-10}"
  [[ -n "$CHANNEL" ]] || return 0
  [[ -e "$CHANNEL/lock" ]] || : > "$CHANNEL/lock"
  zsystem flock -f "$fdvar" -t "$timeout_s" "$CHANNEL/lock"
}

uf_send() {
  # uf_send '<steps json array>' [timeout_ms] [pace]
  # Sets UF_REPLY and UF_LAST_MS. NOT a command substitution: a subshell would lose UF_SEQ, and
  # the driver ignores a seq it has already served.
  local steps="$1" timeout_ms="${2:-30000}" pace="${3:-null}"
  local lockfd=""
  uf_renew_lease
  {
    if [[ -n "$CHANNEL" ]]; then
      # The same flock `lib/uiflow/channel.py` takes, so shell and python queue instead of
      # clobbering the one channel a simulator has. `zsystem` is a builtin, so this costs no fork.
      if ! uf_lock_channel lockfd $(( timeout_ms / 1000 + 1 )); then
        print -u2 -- "could not lock $CHANNEL/lock within ${timeout_ms}ms (held by another session?)"
        return 1
      fi
      # Take the seq FROM the shared counter, not just publish to it. Publishing alone left this
      # counter process-local: a python send in between advanced the driver's lastSeq, and the
      # driver drops a seq it has already served WITHOUT a reply, so the next shell step silently
      # never ran and the suite blamed a timeout.
      local shared=0
      [[ -r "$CHANNEL/seq" ]] && shared=$(<"$CHANNEL/seq")
      [[ "$shared" == <-> ]] || shared=0
      (( shared > UF_SEQ )) && UF_SEQ=$shared
    fi
    UF_SEQ=$((UF_SEQ + 1))
    # Retire the last reply now: a caller that does `uf_send … || uf_bad` then asserts on UF_REPLY
    # would otherwise judge this failure against the PREVIOUS send's reply and pass.
    UF_REPLY=""
    UF_LAST_MS=0
    [[ -n "$CHANNEL" ]] && print -r -- "$UF_SEQ" > "$CHANNEL/seq"
    local req="{\"seq\":$UF_SEQ,\"pace\":$pace,\"steps\":$steps}"
    rm -f "$CHANNEL/reply.json"
    print -r -- "$req" > "$CHANNEL/cmd.tmp"
    mv -f "$CHANNEL/cmd.tmp" "$CHANNEL/cmd.json"
    local start deadline reply
    start=$(uf_now_ms)
    deadline=$(( start + timeout_ms ))
    while true; do
      if [[ -f "$CHANNEL/reply.json" ]]; then
        reply=$(<"$CHANNEL/reply.json") 2>/dev/null
        # The harness checks seq itself (finding 19) so bash and Python cannot diverge on semantics.
        if [[ "$reply" == *'"seq":'${UF_SEQ}[,\}]* ]]; then
          UF_LAST_MS=$(( $(uf_now_ms) - start ))
          UF_REPLY="$reply"
          return 0
        fi
      fi
      (( $(uf_now_ms) > deadline )) && {
        print -u2 -- "timed out after ${timeout_ms}ms waiting for seq $UF_SEQ"
        return 1
      }
      zselect -t 1
    done
  } always {
    # One release for every exit path. A `local` fd is NOT closed when the function returns, and a
    # leaked one is invisible: fcntl locks are per-process so the suite keeps working while every
    # python send blocks for its full timeout.
    [[ -n "$lockfd" ]] && exec {lockfd}>&-
  }
}

uf_jq() { print -r -- "$1" | /usr/bin/jq -r "$2" }

# ---------------------------------------------------------------- driver

uf_build() {
  local out
  if [[ ! -x "$UF_ROOT/driver/build.sh" ]]; then
    print -u2 -- "driver/build.sh is missing or not executable"
    return 1
  fi
  out=$("$UF_ROOT/driver/build.sh" 2>&1) || { print -r -- "$out" | tail -30; return 1 }
  UF_XCTESTRUN=$(print -r -- "$out" | tail -1)
  [[ -f "$UF_XCTESTRUN" ]] || { print -u2 -- "build.sh printed no .xctestrun path: $UF_XCTESTRUN"; return 1 }
  return 0
}

uf_foreign_driver_pids() {
  # A simulator has exactly ONE runner container, so its channel — cmd.json, reply.json, ready —
  # is shared by every driver on it. Two drivers race for each request and neither run is
  # trustworthy, so say so instead of producing a screenful of unexplained timeouts.
  local udid="$1" pid described
  for pid in $(pgrep -x xcodebuild 2>/dev/null); do
    described=$(ps -o command= -p "$pid" 2>/dev/null)
    [[ "$described" == *UIFlowDriver* && "$described" == *"$udid"* ]] || continue
    print -r -- "$pid"
  done
}

uf_start() {
  # uf_start <udid> [idle_seconds] [max_seconds]; sets DRIVER_PID, CHANNEL, UF_STARTUP_MS
  local udid="$1" idle="${2:-300}" max="${3:-900}"
  local foreign waited=0
  while foreign=$(uf_foreign_driver_pids "$udid"); [[ -n "$foreign" ]]; do
    if (( waited >= 90 )); then
      print -u2 -- "refusing: another ui-flow driver still owns $udid (pids: ${foreign//$'\n'/ })."
      print -u2 -- "One simulator has one channel directory, so they would corrupt each other."
      print -u2 -- "Stop it with \`./bin/uiflow down\`, or point UIFLOW_SIM_NAME at another simulator."
      return 1
    fi
    (( waited == 0 )) && print -u2 -- "waiting for another ui-flow driver on $udid to finish..."
    zselect -t 200
    waited=$((waited + 2))
  done
  UF_RESULT_BUNDLE="$UF_STATE/result-$UF_SUITE-$EPOCHSECONDS.xcresult"
  # Keyed on the simulator too: uf_channel_from_log reads this file, so two runs of the same
  # suite on different simulators sharing one log would each adopt the other's channel.
  UF_LOG="$UF_STATE/driver-$UF_SUITE-$udid.log"
  local start=$(uf_now_ms)
  mkdir -p "$UF_STATE"
  # Claim the simulator in the same book `uiflow up` reads. The pid scan above only makes the shell
  # refuse a Python driver; without this the reverse is not true and `uiflow up` would clear this
  # suite's runner as abandoned the moment its heartbeat looked stale.
  if ! "$UF_ROOT/bin/uiflow" lease acquire --udid "$udid" >/dev/null 2>&1; then
    print -u2 -- "refusing: another ui-flow session holds $udid —"
    "$UF_ROOT/bin/uiflow" lease status --udid "$udid" >&2
    return 1
  fi
  UF_LEASED="$udid"
  UF_LEASE_RENEWED=$EPOCHSECONDS
  # Only now do we own a runner on this simulator: uf_cleanup terminates whatever UF_UDID names, so
  # setting it before the guard above would have reaped the very session we just declined to disturb.
  UF_UDID="$udid"
  # Truncate before launching: uf_channel_from_log must never read the previous run's channel out of
  # a log the background job has not redirected yet.
  : >"$UF_LOG"
  TEST_RUNNER_UIFLOW_IDLE_SECONDS="$idle" \
  TEST_RUNNER_UIFLOW_MAX_SECONDS="$max" \
  xcodebuild test-without-building \
    -xctestrun "$UF_XCTESTRUN" \
    -destination "id=$udid" \
    -resultBundlePath "$UF_RESULT_BUNDLE" \
    >"$UF_LOG" 2>&1 &
  DRIVER_PID=$!
  local deadline=$(( $(uf_now_ms) + 120000 )) found
  while true; do
    if found=$(uf_channel_from_log) && [[ -f "$found/ready" ]]; then
      CHANNEL="$found"
      UF_STARTUP_MS=$(printf '%.0f' $(( $(uf_now_ms) - start )))
      return 0
    fi
    if ! kill -0 "$DRIVER_PID" 2>/dev/null; then
      print -u2 -- "driver exited before publishing a channel; tail of $UF_LOG:"
      tail -25 "$UF_LOG" >&2
      return 1
    fi
    (( $(uf_now_ms) > deadline )) && { print -u2 -- "driver never published a fresh ready file"; tail -25 "$UF_LOG" >&2; return 1 }
    zselect -t 5
  done
}

uf_quit_and_wait() {
  # Graceful stop; returns the driver's exit status.
  [[ -n "$DRIVER_PID" ]] || return 0
  [[ -n "$CHANNEL" ]] && uf_send '[{"op":"quit"}]' 5000
  local deadline=$(( EPOCHSECONDS + 60 ))
  while (( EPOCHSECONDS < deadline )); do
    if ! kill -0 "$DRIVER_PID" 2>/dev/null; then
      wait "$DRIVER_PID"; local rc=$?
      DRIVER_PID=""
      return $rc
    fi
    zselect -t 10
  done
  print -u2 -- "driver ignored quit"
  return 1
}

uf_kill_driver() {
  # Last resort. A leaked detached xcodebuild blocks every other session's build for hours (C14),
  # so this runs from the EXIT trap on EVERY path, including a failed assertion or a Ctrl-C.
  [[ -n "$DRIVER_PID" ]] || return 0
  kill -0 "$DRIVER_PID" 2>/dev/null || { DRIVER_PID=""; return 0 }
  local kid
  for kid in $(pgrep -P "$DRIVER_PID" 2>/dev/null); do kill -TERM "$kid" 2>/dev/null; done
  kill -TERM "$DRIVER_PID" 2>/dev/null
  local deadline=$(( EPOCHSECONDS + 15 ))
  while (( EPOCHSECONDS < deadline )); do
    kill -0 "$DRIVER_PID" 2>/dev/null || { DRIVER_PID=""; return 0 }
    zselect -t 20
  done
  for kid in $(pgrep -P "$DRIVER_PID" 2>/dev/null); do kill -KILL "$kid" 2>/dev/null; done
  kill -KILL "$DRIVER_PID" 2>/dev/null
  DRIVER_PID=""
}

uf_terminate_runner() {
  # The runner app lives inside the simulator, not as a child of `xcodebuild`, so killing the pid
  # tree leaves it attached to the channel. An orphan like that is invisible to
  # uf_foreign_driver_pids and silently shares the next run's cmd.json.
  [[ -n "$UF_UDID" ]] || return 0
  xcrun simctl terminate "$UF_UDID" "$UF_RUNNER_ID" >/dev/null 2>&1
  return 0
}

uf_renew_lease() {
  # A driver may live 900s while a lease goes stale at 600s, so acquiring once at start is not
  # enough: another session's `uiflow up` would find the claim expired and clear this runner.
  # Throttled, because each renew is a process spawn.
  [[ -n "$UF_LEASED" ]] || return 0
  (( EPOCHSECONDS - UF_LEASE_RENEWED < 60 )) && return 0
  "$UF_ROOT/bin/uiflow" lease renew --udid "$UF_LEASED" >/dev/null 2>&1
  UF_LEASE_RENEWED=$EPOCHSECONDS
  return 0
}

uf_release_lease() {
  [[ -n "$UF_LEASED" ]] || return 0
  "$UF_ROOT/bin/uiflow" lease release --udid "$UF_LEASED" >/dev/null 2>&1
  UF_LEASED=""
  return 0
}

uf_cleanup() {
  local rc=$?
  uf_kill_driver
  uf_terminate_runner
  uf_release_lease
  # The channel outlives the driver — the container's `tmp` is migrated across reinstalls. An
  # unanswered request left there (the `quit` above, or whatever timed out) is read by the NEXT
  # run's driver as its first command, which ends that session in milliseconds.
  # Under the lock, like every other writer: a concurrent python send must not lose its reply here.
  if [[ -n "$CHANNEL" ]]; then
    local cleanfd=""
    uf_lock_channel cleanfd 5
    rm -f "$CHANNEL/cmd.json" "$CHANNEL/reply.json" "$CHANNEL/ready" "$CHANNEL/heartbeat"
    [[ -n "$cleanfd" ]] && exec {cleanfd}>&-
  fi
  [[ -n "$UF_RESULT_BUNDLE" ]] && rm -rf "$UF_RESULT_BUNDLE"
  return $rc
}

uf_summary() {
  print -r -- ""
  print -r -- "$1: $UF_PASS passed, $UF_FAIL failed"
  (( UF_FAIL == 0 ))
}
