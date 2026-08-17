#!/usr/bin/env bash
# Probe TorBox behaviour for the Mega -> TorBox -> Google Drive workflow.
#
# Settles two unknowns:
#   1. Does TorBox expand a Mega FOLDER link into multiple files?
#   2. Does POST integration/googledrive work WITHOUT google_token once Drive
#      is connected in TorBox settings, and what shape is the job object?
#
# Your API key is read from the environment and never printed.
# Account email is redacted from output so it is safe to paste back.
#
# Usage:
#   export TORBOX_API_KEY='...'
#   ./probe-torbox.sh whoami
#   ./probe-torbox.sh submit 'https://mega.nz/folder/XXXX#YYYY'
#   ./probe-torbox.sh status <web_id>
#   ./probe-torbox.sh queue  <web_id>
#   ./probe-torbox.sh jobs

set -uo pipefail

API='https://api.torbox.app/v1/api'

# Load the key from .env.local if present, so it never has to be typed into a
# shared session. Parsed rather than sourced: tolerates a UTF-8 BOM and CRLF
# line endings (both common when the file is written from PowerShell), and
# cannot execute code from the file. The value is never echoed.
ENV_FILE="$(dirname "$0")/../.env.local"
KEY="${TORBOX_API_KEY:-}"

if [ -z "$KEY" ] && [ -f "$ENV_FILE" ]; then
  KEY=$(
    tr -d '\r' < "$ENV_FILE" \
      | sed '1s/^\xef\xbb\xbf//' \
      | grep -m1 '^[[:space:]]*TORBOX_API_KEY[[:space:]]*=' \
      | sed -e 's/^[^=]*=//' \
            -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
            -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
  )
fi

if [ -z "$KEY" ]; then
  echo "ERROR: no TORBOX_API_KEY found." >&2
  echo "Create .env.local in the project root containing:" >&2
  echo "  TORBOX_API_KEY=your-key-here" >&2
  exit 1
fi

# Pretty-print JSON and redact anything personal (email, auth id, the key itself).
show() {
  python -c '
import sys, json, re
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print(raw[:2000]); sys.exit()

REDACT = {"email", "auth_id", "customer", "user_id", "token", "google_token"}
def clean(o):
    if isinstance(o, dict):
        return {k: ("<redacted>" if k.lower() in REDACT else clean(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, str):
        return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", o)
    return o
print(json.dumps(clean(d), indent=2)[:6000])
'
}

req() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -m 60 -X "$method" "$API/$path" \
      -H "Authorization: Bearer $KEY" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -s -m 60 -X "$method" "$API/$path" -H "Authorization: Bearer $KEY"
  fi
}

case "${1:-}" in

  whoami)
    echo "### GET /user/me  (checking key + plan; web downloads need a paid plan)"
    req GET "user/me" | show
    ;;

  submit)
    LINK="${2:?usage: submit '<mega folder link>'}"
    echo "### POST /webdl/createwebdownload"
    echo "### Watch for: is the folder accepted, and does the response list MULTIPLE files?"
    curl -s -m 120 -X POST "$API/webdl/createwebdownload" \
      -H "Authorization: Bearer $KEY" \
      -F "link=$LINK" | show
    ;;

  status)
    ID="${2:?usage: status <web_id>}"
    echo "### GET /webdl/mylist?id=$ID&bypass_cache=true"
    echo "### Watch for: files[] length, download_state, download_finished, progress"
    req GET "webdl/mylist?id=$ID&bypass_cache=true" | show
    ;;

  queue)
    ID="${2:?usage: queue <web_id>}"
    echo "### Test 1 of 2 - POST /integration/googledrive WITHOUT google_token"
    echo "### If this succeeds, the stored connection works and n8n needs no Google auth."
    req POST "integration/googledrive" \
      "{\"id\": $ID, \"type\": \"webdownload\", \"zip\": true}" | show
    echo
    echo "### Test 2 of 2 - same call WITH an empty google_token"
    echo "### This is exactly what the community node sends when the field is left blank."
    req POST "integration/googledrive" \
      "{\"id\": $ID, \"type\": \"webdownload\", \"zip\": true, \"google_token\": \"\"}" | show
    ;;

  queuefile)
    ID="${2:?usage: queuefile <web_id> <file_id>}"
    FID="${3:?usage: queuefile <web_id> <file_id>}"
    echo "### POST /integration/googledrive - single file, file_id=$FID, no zip"
    echo "### Discriminator: if a tiny file uploads OK, auth works and zip is the problem."
    req POST "integration/googledrive" \
      "{\"id\": $ID, \"type\": \"webdownload\", \"file_id\": $FID, \"google_token\": \"\"}" | show
    ;;

  dl)
    ID="${2:?usage: dl <web_id> <file_id>}"
    FID="${3:?usage: dl <web_id> <file_id>}"
    echo "### GET /webdl/requestdl - direct CDN link for file_id=$FID"
    echo "### Validates Option B: can n8n stream the file itself?"
    url=$(curl -s -m 40 "$API/webdl/requestdl?token=$KEY&web_id=$ID&file_id=$FID" \
            -H "Authorization: Bearer $KEY" \
          | python -c 'import sys,json; print(json.load(sys.stdin).get("data") or "")')
    if [ -z "$url" ]; then
      echo "no url returned"; exit 1
    fi
    echo "got a direct URL (host only): $(printf '%s' "$url" | sed -E 's#(https?://[^/]+)/.*#\1/...#')"
    echo "--- HEAD on that URL ---"
    curl -s -m 40 -I "$url" | grep -iE "^HTTP|content-length|content-type|accept-ranges" | head -8
    ;;

  jobs)
    echo "### GET /integration/jobs"
    echo "### Watch for: a Drive file id, a progress field, and the status vocabulary"
    req GET "integration/jobs" | show
    ;;

  *)
    sed -n '2,18p' "$0"
    exit 1
    ;;
esac
