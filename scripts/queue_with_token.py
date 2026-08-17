#!/usr/bin/env python3
"""Queue a TorBox -> Google Drive upload using a REAL minted access token.

Answers the last open question for architecture Option A: does TorBox accept a
Google access token issued by a client_id other than its own?

Reads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN and
TORBOX_API_KEY from .env.local. No secret is ever printed.

Usage:
    python scripts/queue_with_token.py <web_id> <file_id>
    python scripts/queue_with_token.py <web_id> zip
"""

import json
import sys
import urllib.parse
import urllib.request

from google_oauth import read_env  # reuse the BOM/CRLF-tolerant parser

TOKEN_URL = "https://oauth2.googleapis.com/token"
TORBOX = "https://api.torbox.app/v1/api/integration/googledrive"


def mint_access_token(env):
    body = urllib.parse.urlencode(
        {
            "client_id": env["GOOGLE_CLIENT_ID"],
            "client_secret": env["GOOGLE_CLIENT_SECRET"],
            "refresh_token": env["GOOGLE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }
    ).encode()
    with urllib.request.urlopen(
        urllib.request.Request(TOKEN_URL, data=body), timeout=60
    ) as r:
        tok = json.load(r)
    return tok["access_token"], tok.get("scope", "?"), tok.get("expires_in", "?")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    web_id = int(sys.argv[1])
    target = sys.argv[2]

    env = read_env()
    missing = [
        k
        for k in (
            "TORBOX_API_KEY",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN",
        )
        if not env.get(k)
    ]
    if missing:
        print("ERROR: missing from .env.local: %s" % ", ".join(missing))
        print("Run: python scripts/google_oauth.py")
        return 1

    try:
        access, scope, ttl = mint_access_token(env)
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
        print("ERROR: could not mint access token: %s %s" % (e, detail))
        return 1

    print("Minted a fresh access token (not displayed).")
    print("  scope: %s" % scope)
    print("  ttl:   %ss" % ttl)
    print("  length: %d chars" % len(access))

    payload = {"id": web_id, "type": "webdownload", "google_token": access}
    if target == "zip":
        payload["zip"] = True
    else:
        payload["file_id"] = int(target)

    print("\n### POST integration/googledrive with a REAL foreign-client token")
    req = urllib.request.Request(
        TORBOX,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer %s" % env["TORBOX_API_KEY"],
            "Content-Type": "application/json",
            # Cloudflare in front of api.torbox.app rejects python-urllib's
            # default UA with "error code: 1010" before the request ever
            # reaches TorBox. Any conventional client UA gets through.
            "User-Agent": "curl/8.4.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(json.dumps(json.load(r), indent=2))
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
        print("HTTP error: %s\n%s" % (e, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
