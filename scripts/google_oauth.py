#!/usr/bin/env python3
"""One-time Google OAuth helper.

Runs the installed-app (loopback) flow locally and appends GOOGLE_REFRESH_TOKEN
to .env.local. Tokens are never printed - only a success/failure line.

Prereqs in .env.local:
    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...

Usage:
    python scripts/google_oauth.py
"""

import http.server
import json
import os
import re
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "..", ".env.local")

# Full Drive scope: the upload must be able to write into a folder TorBox did
# not create. drive.file would restrict us to app-created files.
SCOPE = "https://www.googleapis.com/auth/drive"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def read_env():
    """Parse .env.local, tolerating a UTF-8 BOM and CRLF line endings."""
    env = {}
    if not os.path.exists(ENV_FILE):
        return env
    with open(ENV_FILE, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    for line in raw.decode("utf-8", "replace").replace("\r\n", "\n").split("\n"):
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def append_env(key, value):
    """Append (or replace) a key in .env.local without disturbing the rest."""
    existing = ""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "rb") as fh:
            raw = fh.read()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        existing = raw.decode("utf-8", "replace").replace("\r\n", "\n")
        existing = "\n".join(
            l for l in existing.split("\n") if not re.match(r"\s*%s\s*=" % key, l)
        ).rstrip("\n")
    with open(ENV_FILE, "w", encoding="utf-8", newline="\n") as fh:
        if existing:
            fh.write(existing + "\n")
        fh.write("%s=%s\n" % (key, value))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    env = read_env()
    cid = env.get("GOOGLE_CLIENT_ID")
    csec = env.get("GOOGLE_CLIENT_SECRET")

    if not cid or not csec:
        print("ERROR: add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env.local first.")
        print("Create them at console.cloud.google.com -> Credentials -> OAuth client ID")
        print("Application type must be 'Desktop app'.")
        return 1

    port = free_port()
    redirect = "http://localhost:%d" % port
    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            holder.update({k: v[0] for k, v in params.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in params
            self.wfile.write(
                (
                    "<html><body style='font-family:system-ui;padding:40px'>"
                    "<h2>%s</h2><p>You can close this tab and return to the terminal.</p>"
                    "</body></html>"
                    % ("Authorized - token exchange in progress." if ok else "Authorization failed.")
                ).encode()
            )

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    auth = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",  # force a refresh_token even on re-auth
        }
    )

    print("Opening your browser to authorize Drive access...")
    print("If it does not open, paste this into a browser:\n")
    print(auth + "\n")
    try:
        webbrowser.open(auth)
    except Exception:
        pass

    print("Waiting for the redirect (up to 180s)...")
    server.timeout = 180
    for _ in range(180):
        if holder:
            break
        threading.Event().wait(1)

    if "code" not in holder:
        print("ERROR: no authorization code received (timed out or denied).")
        return 1

    body = urllib.parse.urlencode(
        {
            "code": holder["code"],
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        }
    ).encode()

    try:
        with urllib.request.urlopen(
            urllib.request.Request(TOKEN_URL, data=body), timeout=60
        ) as r:
            tok = json.load(r)
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
        print("ERROR: token exchange failed: %s %s" % (e, detail))
        return 1

    if "refresh_token" not in tok:
        print("ERROR: no refresh_token returned. Revoke prior access and retry.")
        return 1

    append_env("GOOGLE_REFRESH_TOKEN", tok["refresh_token"])
    print("OK: refresh token saved to .env.local (value not displayed).")
    print("    granted scope: %s" % tok.get("scope", "?"))
    print("    access token TTL: %ss" % tok.get("expires_in", "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
