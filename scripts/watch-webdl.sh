#!/usr/bin/env bash
# Poll a TorBox web download and emit one compact progress line per check.
# Exits when the download finishes, errors, or the max poll count is reached.
#
# Usage: ./watch-webdl.sh <web_id> [interval_seconds] [max_polls]

set -uo pipefail

ID="${1:?usage: watch-webdl.sh <web_id> [interval] [max_polls]}"
INTERVAL="${2:-60}"
MAX="${3:-30}"
HERE="$(dirname "$0")"

for i in $(seq 1 "$MAX"); do
  raw=$(bash "$HERE/probe-torbox.sh" status "$ID" 2>/dev/null | grep -v '^###') || true

  verdict=$(printf '%s' "$raw" | python -c '
import sys, json
try:
    d = json.load(sys.stdin)["data"]
except Exception:
    print("POLL: no parseable response (transient?)"); sys.exit()

state = d.get("download_state")
pct   = (d.get("progress") or 0) * 100
spd   = (d.get("download_speed") or 0) / 1048576.0
size  = (d.get("size") or 0) / 1073741824.0
eta   = d.get("eta") or 0
files = len(d.get("files") or [])
err   = d.get("error")
done  = d.get("download_finished")

print("POLL: state=%s progress=%.2f%% speed=%.2fMB/s eta=%.1fh size=%.2fGB files=%d"
      % (state, pct, spd, eta / 3600.0, size, files))

if err:
    print("TERMINAL: error=%s" % err)
elif done:
    print("TERMINAL: download_finished=true files=%d" % files)
' 2>/dev/null) || verdict="POLL: request failed (transient?)"

  printf '%s\n' "$verdict"

  case "$verdict" in
    *TERMINAL:*) exit 0 ;;
  esac

  sleep "$INTERVAL"
done

echo "TERMINAL: gave up after $MAX polls - still running"
