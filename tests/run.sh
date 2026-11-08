#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
failed=0
for f in tests/test_*.py; do
  if ! out=$(python3 "$f" 2>&1); then
    printf 'FAIL %s\n%s\n' "$f" "$out"
    failed=1
  else
    printf 'ok   %-24s %s\n' "$(basename "$f")" "$(printf '%s' "$out" | grep -oE '^Ran [0-9]+ tests' | head -1)"
  fi
done
if [ "${1:-}" = "--python-only" ]; then
  exit "$failed"
fi
for f in tests/*_test.sh tests/test_*.sh; do
  [ -x "$f" ] || continue
  if ! out=$("$f" 2>&1); then
    printf 'FAIL %s\n%s\n' "$f" "$(printf '%s' "$out" | tail -20)"
    failed=1
  else
    printf 'ok   %s\n' "$(basename "$f")"
  fi
done
exit "$failed"
