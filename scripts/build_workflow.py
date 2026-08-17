#!/usr/bin/env python3
"""Build and deploy the Mega -> TorBox -> Google Drive workflow via the n8n API.

Idempotent: creates the workflow if absent, updates it in place if present.
Credential IDs are resolved by NAME from the live instance, so no IDs are
hard-coded and nothing secret lives in this file.

All node parameter shapes come from docs/node-schemas.md, which was read from
n8n's published source. Do not "fix" a parameter name from memory.

Usage:
    python scripts/build_workflow.py --check     # resolve credentials only
    python scripts/build_workflow.py --deploy    # create or update
"""

import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "..", ".env.local")

WORKFLOW_NAME = "Mega -> TorBox -> Drive"
UA = "n8n-torbox-workflow/1.0"
TORBOX_API = "https://api.torbox.app/v1/api"

# Credentials required, by n8n credential type -> credential NAME on the instance.
REQUIRED_CREDS = {
    "torBoxApi": "TorBox API",
    "httpHeaderAuth": "TorBox Bearer",
    "telegramApi": "Telegram",
    "googleDriveOAuth2Api": "Google Drive",
    "googleSheetsOAuth2Api": "Google Sheets",
}


def read_env():
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


ENV = read_env()
BASE = (ENV.get("N8N_API_URL") or "").rstrip("/")
APIKEY = ENV.get("N8N_API_KEY") or ""


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + "/api/v1" + path,
        data=data,
        method=method,
        headers={
            "X-N8N-API-KEY": APIKEY,
            "Content-Type": "application/json",
            "User-Agent": "curl/8.4.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
        raise SystemExit("API %s %s failed: %s\n%s" % (method, path, e, detail))


def resolve_credentials():
    """Map credential type -> {id, name}, failing loudly on anything missing."""
    existing = api("GET", "/credentials")
    items = existing.get("data", existing) if isinstance(existing, dict) else existing
    by_name = {c["name"]: c for c in items}

    resolved, missing = {}, []
    for ctype, cname in REQUIRED_CREDS.items():
        c = by_name.get(cname)
        if c and c.get("type") == ctype:
            resolved[ctype] = {"id": str(c["id"]), "name": cname}
        else:
            missing.append("%s (type %s)" % (cname, ctype))
    return resolved, missing


def node(name, ntype, tv, pos, params, creds=None, extra=None):
    n = {
        "parameters": params,
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": ntype,
        "typeVersion": tv,
        "position": pos,
    }
    if creds:
        n["credentials"] = creds
    if extra:
        n.update(extra)
    return n


def build(creds):
    """Tasks 2-4: Telegram intake, link validation, download creation, poll loop."""
    tg = {"telegramApi": creds["telegramApi"]}
    chat_id = "={{ $('Telegram Trigger').item.json.message.chat.id }}"

    nodes = [
        node("Telegram Trigger", "n8n-nodes-base.telegramTrigger", 1.5, [-220, 300],
             {"updates": ["message"], "additionalFields": {}}, tg),

        node("Is Mega Link", "n8n-nodes-base.if", 2.2, [0, 300], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": "mega-regex",
                    "leftValue": "={{ $json.message.text }}",
                    "rightValue": "https?://mega\\.nz/(folder|file)/\\S+",
                    "operator": {"type": "string", "operation": "regex"},
                }],
            },
            "looseTypeValidation": False,
            "options": {},
        }),

        node("Reject", "n8n-nodes-base.telegram", 1.2, [220, 460], {
            "resource": "message", "operation": "sendMessage",
            "chatId": chat_id,
            "text": "Send me a Mega.nz folder or file link, e.g. "
                    "https://mega.nz/folder/XXXX#YYYY",
            "additionalFields": {"appendAttribution": False},
        }, tg),

        node("Ack", "n8n-nodes-base.telegram", 1.2, [220, 160], {
            "resource": "message", "operation": "sendMessage",
            "chatId": chat_id,
            "text": "Queued. Submitting to TorBox...",
            "additionalFields": {"appendAttribution": False},
        }, tg),

        node("Create Download", "n8n-nodes-torbox.torBox", 1, [440, 160], {
            "resource": "webdl",
            "operation": "createWebDownload",
            "link": "={{ $('Telegram Trigger').item.json.message.text"
                    ".match(/https?:\\/\\/mega\\.nz\\/\\S+/)[0] }}",
            "options": {},
        }, {"torBoxApi": creds["torBoxApi"]},
           {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}),

        node("Check Download", "n8n-nodes-torbox.torBox", 1, [660, 160], {
            "resource": "webdl",
            "operation": "getWebDownloadList",
            "options": {
                "id": "={{ $('Create Download').item.json.data.webdownload_id }}",
                "bypass_cache": True,
            },
        }, {"torBoxApi": creds["torBoxApi"]}),

        node("Download Done?", "n8n-nodes-base.if", 2.2, [880, 160], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": "dl-finished",
                    "leftValue": "={{ $json.data.download_finished }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
            },
            "looseTypeValidation": False,
            "options": {},
        }),

        node("Progress: Download", "n8n-nodes-base.telegram", 1.2, [880, 380], {
            "resource": "message", "operation": "editMessageText",
            "messageType": "message",
            "chatId": chat_id,
            "messageId": "={{ $('Ack').item.json.result.message_id }}",
            "text": "={{ $('Check Download').item.json.data.name }}\n\n"
                    "Downloading from Mega: "
                    "{{ ($('Check Download').item.json.data.progress * 100).toFixed(1) }}%\n"
                    "{{ $runIndex < 1 ? 'measuring speed...' : "
                    "($('Check Download').item.json.data.download_speed / 1048576).toFixed(1) "
                    "+ ' MB/s' }}",
            "additionalFields": {},
        }, tg),

        node("Wait 30s", "n8n-nodes-base.wait", 1.1, [660, 380],
             {"resume": "timeInterval", "amount": 30, "unit": "seconds"},
             None, {"webhookId": "a1b2c3d4-0000-4000-8000-000000000001"}),
    ]

    connections = {
        "Telegram Trigger": {"main": [[{"node": "Is Mega Link", "type": "main", "index": 0}]]},
        "Is Mega Link": {"main": [
            [{"node": "Ack", "type": "main", "index": 0}],       # true
            [{"node": "Reject", "type": "main", "index": 0}],    # false
        ]},
        "Ack": {"main": [[{"node": "Create Download", "type": "main", "index": 0}]]},
        "Create Download": {"main": [[{"node": "Check Download", "type": "main", "index": 0}]]},
        "Check Download": {"main": [[{"node": "Download Done?", "type": "main", "index": 0}]]},
        "Download Done?": {"main": [
            [],                                                   # true -> Task 5 (later)
            [{"node": "Progress: Download", "type": "main", "index": 0}],
        ]},
        "Progress: Download": {"main": [[{"node": "Wait 30s", "type": "main", "index": 0}]]},
        "Wait 30s": {"main": [[{"node": "Check Download", "type": "main", "index": 0}]]},
    }

    return {
        "name": WORKFLOW_NAME,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
    }


def find_workflow():
    res = api("GET", "/workflows?limit=250")
    for w in res.get("data", []):
        if w.get("name") == WORKFLOW_NAME:
            return w
    return None


def main():
    if not BASE or not APIKEY:
        raise SystemExit("Missing N8N_API_URL / N8N_API_KEY in .env.local")

    creds, missing = resolve_credentials()
    if missing:
        print("Missing credentials on the n8n instance:")
        for m in missing:
            print("  - %s" % m)
        print("\nCreate them in the n8n UI with these exact names, then re-run.")
        return 1
    print("All %d credentials resolved." % len(creds))

    if "--check" in sys.argv:
        return 0
    if "--deploy" not in sys.argv:
        print("Pass --deploy to create/update the workflow.")
        return 0

    wf = build(creds)
    existing = find_workflow()
    if existing:
        out = api("PUT", "/workflows/%s" % existing["id"], wf)
        print("updated workflow id=%s (%d nodes)" % (out.get("id"), len(wf["nodes"])))
    else:
        out = api("POST", "/workflows", wf)
        print("created workflow id=%s (%d nodes)" % (out.get("id"), len(wf["nodes"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
