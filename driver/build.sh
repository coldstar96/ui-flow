#!/bin/sh
# Regenerates UIFlowDriver.xcodeproj and builds it for testing. Prints the .xctestrun path last.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DEST=${UIFLOW_BUILD_DEST:-generic/platform=iOS Simulator}

case "$DEST" in
  *"iOS Simulator"*) ;;
  *)
    printf 'refusing: ui-flow drives simulators only, and "%s" is not a simulator destination\n' "$DEST" >&2
    exit 1
    ;;
esac

ruby "$HERE/gen-project.rb" >/dev/null

BUILD=$(python3 -c "import sys; sys.path.insert(0, '$HERE/../lib'); from uiflow import paths; print(paths.BUILD_DIR)")
mkdir -p "$BUILD"

LOG="$BUILD.log"
if ! xcodebuild build-for-testing \
  -project "$HERE/UIFlowDriver.xcodeproj" \
  -scheme UIFlowDriver \
  -destination "$DEST" \
  -derivedDataPath "$BUILD" \
  -quiet >"$LOG" 2>&1; then
  printf 'build failed; see %s\n' "$LOG" >&2
  tail -40 "$LOG" >&2
  exit 1
fi

XCTESTRUN=$(ls -t "$BUILD"/Build/Products/*.xctestrun 2>/dev/null | head -1)
if [ -z "$XCTESTRUN" ]; then
  printf 'build produced no .xctestrun under %s/Build/Products\n' "$BUILD" >&2
  exit 1
fi
printf '%s\n' "$XCTESTRUN"
