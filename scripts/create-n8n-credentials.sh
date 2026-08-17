#!/usr/bin/env bash
# Create the two TorBox-related n8n credentials from values in .env.local.
#
# Only credentials whose secrets already live in .env.local are created here.
# Telegram and the Google OAuth2 credentials require a browser consent flow and
# must be created in the n8n UI.
#
# No secret is ever printed.
#
# Usage: ./create-n8n-credentials.sh

set -uo pipefail

HERE="$(dirname "$0")"
ENV_FILE="$HERE/../.env.local"

getenv() {
  [ -f "$ENV_FILE" ] || return 0
  tr -d '\r' < "$ENV_FILE" \
    | sed '1s/^\xef\xbb\xbf//' \
    | grep -m1 "^[[:space:]]*$1[[:space:]]*=" \
    | sed -e 's/^[^=]*=//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

URL="$(getenv N8N_API_URL)"; URL="${URL%/}"
KEY="$(getenv N8N_API_KEY)"
TB="$(getenv TORBOX_API_KEY)"

for v in URL KEY TB; do
  eval "val=\$$v"
  [ -n "$val" ] || { echo "ERROR: missing value for $v in .env.local" >&2; exit 1; }
done

create() { # name type dataJson
  python - "$URL" "$KEY" "$1" "$2" "$3" <<'PY'
import json, sys, urllib.request
url, apikey, name, ctype, data = sys.argv[1:6]
body = json.dumps({"name": name, "type": ctype, "data": json.loads(data)}).encode()
req = urllib.request.Request(
    url + "/api/v1/credentials", data=body,
    headers={"X-N8N-API-KEY": apikey, "Content-Type": "application/json",
             "User-Agent": "curl/8.4.0"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    print("created: %s  id=%s  type=%s" % (name, out.get("id"), ctype))
except Exception as e:
    detail = ""
    if hasattr(e, "read"):
        try: detail = e.read().decode("utf-8", "replace")[:300]
        except Exception: pass
    print("FAILED: %s -> %s %s" % (name, e, detail))
PY
}

# Only the header-auth credential is created. The community node's torBoxApi
# credential type no longer exists on the instance (Railway wiped the package),
# and the workflow no longer needs it -- all TorBox calls go through plain
# HTTP Request nodes using this Bearer header.
create "TorBox Bearer" "httpHeaderAuth" "$(python -c 'import json,sys; print(json.dumps({"name": "Authorization", "value": "Bearer " + sys.argv[1]}))' "$TB")"
