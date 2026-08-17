#!/usr/bin/env bash
# Poll a TorBox integration job and emit one compact line per check.
# Exits on any terminal status (completed / failed / cancelled / error).
#
# Usage: ./watch-job.sh <job_id> [interval_seconds] [max_polls]

set -uo pipefail

JOB="${1:?usage: watch-job.sh <job_id> [interval] [max_polls]}"
INTERVAL="${2:-30}"
MAX="${3:-40}"
HERE="$(dirname "$0")"

for i in $(seq 1 "$MAX"); do
  raw=$(bash "$HERE/probe-torbox.sh" jobs 2>/dev/null | grep -v '^###') || true

  verdict=$(printf '%s' "$raw" | JOB="$JOB" python -c '
import sys, json, os
want = int(os.environ["JOB"])
try:
    jobs = json.load(sys.stdin)["data"]
except Exception:
    print("POLL: no parseable response (transient?)"); sys.exit()

j = next((x for x in jobs if x.get("id") == want), None)
if j is None:
    print("POLL: job %d not present yet" % want); sys.exit()

print("POLL: status=%s progress=%s file_name=%s detail=%s"
      % (j.get("status"), j.get("progress"), j.get("file_name"), (j.get("detail") or "")[:90]))

if j.get("status") in ("completed", "success", "done", "failed", "cancelled", "error"):
    print("TERMINAL: status=%s download_url=%s" % (j.get("status"), bool(j.get("download_url"))))
' 2>/dev/null) || verdict="POLL: request failed (transient?)"

  printf '%s\n' "$verdict"

  case "$verdict" in
    *TERMINAL:*) exit 0 ;;
  esac

  sleep "$INTERVAL"
done

echo "TERMINAL: gave up after $MAX polls - still running"
