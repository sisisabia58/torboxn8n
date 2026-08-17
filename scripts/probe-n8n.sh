#!/usr/bin/env bash
# Probe the n8n instance's public REST API.
#
# Reads N8N_API_URL and N8N_API_KEY from .env.local. Neither is ever printed.
#
# Usage:
#   ./probe-n8n.sh ping                 # connectivity + workflow count
#   ./probe-n8n.sh list                 # list workflows (id + name)
#   ./probe-n8n.sh get <workflow_id>    # fetch one workflow as JSON
#   ./probe-n8n.sh nodetypes            # attempt to enumerate available node types

set -uo pipefail

HERE="$(dirname "$0")"
ENV_FILE="$HERE/../.env.local"

getenv() { # key
  [ -f "$ENV_FILE" ] || return 0
  tr -d '\r' < "$ENV_FILE" \
    | sed '1s/^\xef\xbb\xbf//' \
    | grep -m1 "^[[:space:]]*$1[[:space:]]*=" \
    | sed -e 's/^[^=]*=//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

URL="$(getenv N8N_API_URL)"
KEY="$(getenv N8N_API_KEY)"

if [ -z "$URL" ] || [ -z "$KEY" ]; then
  echo "ERROR: add these to .env.local:" >&2
  echo "  N8N_API_URL=https://your-n8n-host        (no trailing slash, no /api/v1)" >&2
  echo "  N8N_API_KEY=your-api-key                 (n8n: Settings -> API -> Create)" >&2
  exit 1
fi

URL="${URL%/}"

api() { # method path [body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -m 60 -X "$method" "$URL/api/v1$path" \
      -H "X-N8N-API-KEY: $KEY" -H "Content-Type: application/json" -d "$body"
  else
    curl -s -m 60 -X "$method" "$URL/api/v1$path" -H "X-N8N-API-KEY: $KEY"
  fi
}

case "${1:-}" in
  ping)
    echo "### GET /workflows?limit=1"
    api GET "/workflows?limit=1" | python -c '
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    print("Non-JSON response (first 300 chars):"); print(raw[:300]); sys.exit(1)
if isinstance(d, dict) and "data" in d:
    print("OK - API reachable and key accepted.")
    print("workflows visible: %d" % len(d["data"]))
    for w in d["data"][:3]:
        print("  id=%s  name=%s  active=%s" % (w.get("id"), w.get("name"), w.get("active")))
else:
    print("Unexpected response:"); print(json.dumps(d, indent=2)[:600])
'
    ;;

  list)
    echo "### GET /workflows"
    api GET "/workflows?limit=100" | python -c '
import sys, json
d = json.load(sys.stdin)
for w in d.get("data", []):
    print("id=%-8s active=%-5s %s" % (w.get("id"), w.get("active"), w.get("name")))
print("total: %d" % len(d.get("data", [])))
'
    ;;

  get)
    ID="${2:?usage: get <workflow_id>}"
    api GET "/workflows/$ID" | python -c 'import sys,json; print(json.dumps(json.load(sys.stdin), indent=2)[:8000])'
    ;;

  nodetypes)
    # The public API does not expose node types; the internal endpoint may,
    # depending on version and auth. Worth one attempt before giving up.
    echo "### GET /rest/node-types (internal, may require session auth)"
    curl -s -m 30 -o /dev/null -w "status: %{http_code}\n" "$URL/rest/node-types"
    echo "If this is not 200, node schemas must come from n8n-mcp's get_node instead."
    ;;

  *)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
