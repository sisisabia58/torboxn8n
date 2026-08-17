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

# Destination for the Drive fix-up (TorBox ignores its own folder-ID setting
# and uploads land orphaned, so the workflow moves them here itself).
DRIVE_FOLDER_ID = "1ERCHFMwp1jPVgFQPM4VPN4mlObA2pzwI"

# Attempt log. Tab name and header row were read from the live spreadsheet,
# not assumed: tab "Sheet1" (gid=0), columns timestamp, telegram_user,
# source_link, folder_name, size_bytes, file_count, outcome, stage, error,
# duration_sec.
SHEET_ID = "15gN93Ly5x1XBXWrTjPsk6kMGtjF1IgVuf_jpIvBZez0"
SHEET_TAB = "Sheet1"

# Above this many files, upload one zip instead of one job per file, to stay
# clear of TorBox's 300 requests/min limit.
#
# Batching submits ~200 requests/min, so 150 files is roughly 45s of queueing --
# comfortably under the ceiling. Set low (30) at first, which would have turned
# a real 72-file folder into a single 3 GB zip and made its videos unstreamable.
# Zip is for genuinely huge folders, not ordinary ones.
FILE_COUNT_THRESHOLD = 150

# Credentials required, by n8n credential type -> credential NAME on the instance.
# torBoxApi is intentionally absent: the workflow no longer uses the community
# node, so it needs no community credential type either.
REQUIRED_CREDS = {
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

        # Deliberately NOT the n8n-nodes-torbox community node: Railway wipes
        # ~/.n8n/nodes on every deploy, which silently prevents the workflow
        # from activating ("Unrecognized node type") and leaves the Telegram
        # webhook unregistered. Plain HTTP Request depends on n8n core only.
        node("Create Download", "n8n-nodes-base.httpRequest", 4.2, [440, 160], {
            "method": "POST",
            "url": TORBOX_API + "/webdl/createwebdownload",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "specifyHeaders": "keypair",
            "headerParameters": {"parameters": [{"name": "User-Agent", "value": UA}]},
            "sendBody": True,
            "contentType": "form-urlencoded",
            "bodyParameters": {"parameters": [
                {"name": "link",
                 "value": "={{ $('Telegram Trigger').item.json.message.text"
                          ".match(/https?:\\/\\/mega\\.nz\\/\\S+/)[0] }}"},
            ]},
            "options": {},
        }, {"httpHeaderAuth": creds["httpHeaderAuth"]},
           {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}),

        # Query string is built into the URL rather than via queryParameters so
        # this depends on no parameter names beyond method/url.
        node("Check Download", "n8n-nodes-base.httpRequest", 4.2, [660, 160], {
            "method": "GET",
            "url": "={{ '" + TORBOX_API + "/webdl/mylist?bypass_cache=true&id=' + "
                   "$('Create Download').item.json.data.webdownload_id }}",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "specifyHeaders": "keypair",
            "headerParameters": {"parameters": [{"name": "User-Agent", "value": UA}]},
            "options": {},
        }, {"httpHeaderAuth": creds["httpHeaderAuth"]}),

        node("Download Done?", "n8n-nodes-base.if", 2.2, [880, 160], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "strict", "version": 2},
                # Both conditions are required. TorBox flips download_finished
                # BEFORE it populates files[], so gating on the flag alone lets
                # the run continue with an empty file list: Expand Files then
                # emits nothing, the upload never happens, and the execution
                # still reports success. Observed on execution 11, where the
                # final poll saw finished=true files=0 for a download that
                # actually contained 72 files.
                "conditions": [
                    {
                        "id": "dl-finished",
                        "leftValue": "={{ $json.data.download_finished }}",
                        "rightValue": "",
                        "operator": {"type": "boolean", "operation": "true",
                                     "singleValue": True},
                    },
                    {
                        "id": "files-populated",
                        "leftValue": "={{ ($json.data.files || []).length }}",
                        "rightValue": 0,
                        "operator": {"type": "number", "operation": "gt"},
                    },
                ],
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
                    "+ ' MB/s' }}\n"
                    # Telegram rejects an edit whose text is byte-identical to
                    # the current message ("message is not modified", HTTP 400).
                    # Two consecutive polls can easily render the same string,
                    # so a changing timestamp keeps every edit distinct.
                    "updated {{ $now.toFormat('HH:mm:ss') }}",
            "additionalFields": {},
        }, tg, {"onError": "continueRegularOutput"}),

        node("Wait 30s", "n8n-nodes-base.wait", 1.1, [660, 380],
             {"resume": "timeInterval", "amount": 30, "unit": "seconds"},
             None, {"webhookId": "a1b2c3d4-0000-4000-8000-000000000001"}),

        # --- Task 5: mint a real Google access token -------------------------
        # TorBox requires a genuine token in the request body; an empty value
        # passes schema validation and then fails the job asynchronously.
        # n8n community edition has no Variables feature, so these come from
        # the host environment via $env.
        node("Mint Token", "n8n-nodes-base.httpRequest", 4.2, [1100, 160], {
            "method": "POST",
            "url": "https://oauth2.googleapis.com/token",
            "sendBody": True,
            "contentType": "form-urlencoded",
            "bodyParameters": {"parameters": [
                {"name": "client_id", "value": "={{ $env.GOOGLE_CLIENT_ID }}"},
                {"name": "client_secret", "value": "={{ $env.GOOGLE_CLIENT_SECRET }}"},
                {"name": "refresh_token", "value": "={{ $env.GOOGLE_REFRESH_TOKEN }}"},
                {"name": "grant_type", "value": "refresh_token"},
            ]},
            "options": {},
        }),

        # --- Task 6: fan-out decision and upload queueing --------------------
        node("Many Files?", "n8n-nodes-base.if", 2.2, [1320, 160], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": "file-count",
                    "leftValue": "={{ $('Check Download').item.json.data.files.length }}",
                    "rightValue": FILE_COUNT_THRESHOLD,
                    "operator": {"type": "number", "operation": "gt"},
                }],
            },
            "looseTypeValidation": False,
            "options": {},
        }),

        node("Queue Zip", "n8n-nodes-base.httpRequest", 4.2, [1540, 40], {
            "method": "POST",
            "url": TORBOX_API + "/integration/googledrive",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "specifyHeaders": "keypair",
            "headerParameters": {"parameters": [{"name": "User-Agent", "value": UA}]},
            "sendBody": True,
            "contentType": "json",
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({"
                        " id: $('Create Download').item.json.data.webdownload_id,"
                        " type: 'webdownload', zip: true,"
                        " google_token: $('Mint Token').item.json.access_token }) }}",
            "options": {},
        }, {"httpHeaderAuth": creds["httpHeaderAuth"]}),

        node("Expand Files", "n8n-nodes-base.code", 2, [1540, 280], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "// One item per file so each gets its own upload job.\n"
                "// The list lives on a nested property of another node's output,\n"
                "// which Edit Fields cannot expand.\n"
                "const files = $('Check Download').first().json.data.files;\n"
                "return files.map(f => ({ json: {\n"
                "  file_id: f.id,\n"
                "  file_name: f.name || f.short_name,\n"
                "} }));",
        }),

        node("Batch Files", "n8n-nodes-base.splitInBatches", 3, [1760, 280],
             {"batchSize": 10, "options": {"reset": False}}),

        node("Queue File", "n8n-nodes-base.httpRequest", 4.2, [1980, 400], {
            "method": "POST",
            "url": TORBOX_API + "/integration/googledrive",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "specifyHeaders": "keypair",
            "headerParameters": {"parameters": [{"name": "User-Agent", "value": UA}]},
            "sendBody": True,
            "contentType": "json",
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({"
                        " id: $('Create Download').item.json.data.webdownload_id,"
                        " type: 'webdownload', file_id: $json.file_id,"
                        " google_token: $('Mint Token').item.json.access_token }) }}",
            "options": {},
        }, {"httpHeaderAuth": creds["httpHeaderAuth"]}),

        # 10 per batch with a 3s pause is ~200 req/min, leaving headroom under
        # the 300/min ceiling for the polling calls running alongside.
        node("Throttle", "n8n-nodes-base.wait", 1.1, [1980, 560],
             {"resume": "timeInterval", "amount": 3, "unit": "seconds"},
             None, {"webhookId": "a1b2c3d4-0000-4000-8000-000000000002"}),

        # --- Task 7: poll jobs, then correct Drive placement -----------------
        # One call returns every job for the hash, so polling cost is constant
        # whether the folder held 4 files or 400.
        node("Check Jobs", "n8n-nodes-base.httpRequest", 4.2, [2200, 160], {
            "method": "GET",
            "url": "={{ '" + TORBOX_API + "/integration/jobs/' + "
                   "$('Create Download').item.json.data.hash }}",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "specifyHeaders": "keypair",
            "headerParameters": {"parameters": [{"name": "User-Agent", "value": UA}]},
            "options": {},
        }, {"httpHeaderAuth": creds["httpHeaderAuth"]}),

        node("All Jobs Done?", "n8n-nodes-base.if", 2.2, [2420, 160], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": "jobs-terminal",
                    "leftValue": "={{ $json.data.every(j => j.status === 'completed'"
                                 " || j.status === 'failed') }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
            },
            "looseTypeValidation": False,
            "options": {},
        }),

        node("Progress: Upload", "n8n-nodes-base.telegram", 1.2, [2420, 400], {
            "resource": "message", "operation": "editMessageText",
            "messageType": "message",
            "chatId": chat_id,
            "messageId": "={{ $('Ack').item.json.result.message_id }}",
            "text": "={{ $('Check Download').item.json.data.name }}\n\n"
                    "Uploading to Drive: "
                    "{{ $json.data.filter(j => j.status === 'completed').length }}"
                    "/{{ $json.data.length }} files\n"
                    "{{ Math.round($json.data.reduce((a, j) => a + (j.progress || 0), 0)"
                    " / $json.data.length * 100) }}%\n"
                    "updated {{ $now.toFormat('HH:mm:ss') }}",
            "additionalFields": {},
        # onError keeps a cosmetic edit failure from aborting a transfer that is
        # otherwise succeeding. Execution 12 uploaded all 72 files correctly and
        # then died here, reporting error, because two polls rendered the same
        # text and Telegram refused the duplicate edit.
        }, tg, {"onError": "continueRegularOutput"}),

        node("Wait Jobs 20s", "n8n-nodes-base.wait", 1.1, [2200, 400],
             {"resume": "timeInterval", "amount": 20, "unit": "seconds"},
             None, {"webhookId": "a1b2c3d4-0000-4000-8000-000000000003"}),

        node("Completed Jobs", "n8n-nodes-base.code", 2, [2640, 160], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "// Emit one item per successful upload so the Drive fix-up runs\n"
                "// per file. Failed jobs are handled on the error path.\n"
                "const jobs = $input.first().json.data;\n"
                "return jobs\n"
                "  .filter(j => j.status === 'completed')\n"
                "  .map(j => ({ json: {\n"
                "    job_id: j.id,\n"
                "    file_name: j.file_name,\n"
                "    final_name: String(j.file_name || '').split('/').pop(),\n"
                "  } }));",
        }),

        # TorBox uploads arrive orphaned, with the source path flattened into
        # the filename. The next three nodes find, rename, and file them.
        node("Find In Drive", "n8n-nodes-base.googleDrive", 3, [2860, 160], {
            "authentication": "oAuth2",
            "resource": "fileFolder",
            "operation": "search",
            "searchMethod": "query",
            "queryString": "={{ \"name = '\" + $json.file_name.replace(/'/g, \"\\\\'\")"
                           " + \"' and trashed = false\" }}",
            "returnAll": False,
            "limit": 5,
            "filter": {"whatToSearch": "files", "includeTrashed": False},
            "options": {},
        }, {"googleDriveOAuth2Api": creds["googleDriveOAuth2Api"]}),

        # file:update renames only -- it cannot move. Moving is a separate
        # operation; see docs/node-schemas.md.
        node("Rename File", "n8n-nodes-base.googleDrive", 3, [3080, 160], {
            "authentication": "oAuth2",
            "resource": "file",
            "operation": "update",
            "fileId": {"__rl": True, "mode": "id", "value": "={{ $json.id }}"},
            "newUpdatedFileName": "={{ $('Completed Jobs').item.json.final_name }}",
            "options": {},
        }, {"googleDriveOAuth2Api": creds["googleDriveOAuth2Api"]}),

        node("Move To Folder", "n8n-nodes-base.googleDrive", 3, [3300, 160], {
            "authentication": "oAuth2",
            "resource": "file",
            "operation": "move",
            "fileId": {"__rl": True, "mode": "id", "value": "={{ $json.id }}"},
            "folderId": {"__rl": True, "mode": "id", "value": DRIVE_FOLDER_ID},
        }, {"googleDriveOAuth2Api": creds["googleDriveOAuth2Api"]}),

        # --- Task 8: report and log ------------------------------------------
        # Move To Folder emits one item per file, so without this fan-in the
        # final message and the log row would fire once per file.
        node("Summarize", "n8n-nodes-base.code", 2, [3520, 160], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "// Collapse the per-file branch into a single summary item.\n"
                "const moved = $input.all().length;\n"
                "const dl = $('Check Download').first().json.data;\n"
                "const startedMs = $('Telegram Trigger').first().json.message.date * 1000;\n"
                "return [{ json: {\n"
                "  moved,\n"
                "  folder_name: dl.name,\n"
                "  size_bytes: dl.size,\n"
                "  file_count: (dl.files || []).length,\n"
                "  duration_sec: Math.round((Date.now() - startedMs) / 1000),\n"
                "} }];",
        }),

        node("Done", "n8n-nodes-base.telegram", 1.2, [3740, 160], {
            "resource": "message", "operation": "editMessageText",
            "messageType": "message",
            "chatId": chat_id,
            "messageId": "={{ $('Ack').item.json.result.message_id }}",
            "text": "={{ '\\u2705 ' + $json.folder_name }}\n\n"
                    "{{ $json.moved }}/{{ $json.file_count }} files"
                    " · {{ ($json.size_bytes / 1073741824).toFixed(2) }} GB\n"
                    "Uploaded to Google Drive in {{ $json.duration_sec }}s",
            "additionalFields": {},
        }, tg, {"onError": "continueRegularOutput"}),

        node("Log Success", "n8n-nodes-base.googleSheets", 4.5, [3960, 160], {
            "authentication": "oAuth2",
            "resource": "sheet",
            "operation": "append",
            "documentId": {"__rl": True, "mode": "id", "value": SHEET_ID},
            "sheetName": {"__rl": True, "mode": "name", "value": SHEET_TAB},
            "columns": {
                "mappingMode": "defineBelow",
                "value": {
                    "timestamp": "={{ $now.toISO() }}",
                    "telegram_user":
                        "={{ $('Telegram Trigger').first().json.message.from.username }}",
                    "source_link":
                        "={{ $('Telegram Trigger').first().json.message.text }}",
                    "folder_name": "={{ $('Summarize').item.json.folder_name }}",
                    "size_bytes": "={{ $('Summarize').item.json.size_bytes }}",
                    "file_count": "={{ $('Summarize').item.json.file_count }}",
                    "outcome": "success",
                    "stage": "complete",
                    "error": "",
                    "duration_sec": "={{ $('Summarize').item.json.duration_sec }}",
                },
            },
            "options": {},
        }, {"googleSheetsOAuth2Api": creds["googleSheetsOAuth2Api"]},
           {"onError": "continueRegularOutput"}),
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
            [{"node": "Mint Token", "type": "main", "index": 0}],  # true
            [{"node": "Progress: Download", "type": "main", "index": 0}],
        ]},
        "Progress: Download": {"main": [[{"node": "Wait 30s", "type": "main", "index": 0}]]},
        "Wait 30s": {"main": [[{"node": "Check Download", "type": "main", "index": 0}]]},

        "Mint Token": {"main": [[{"node": "Many Files?", "type": "main", "index": 0}]]},
        "Many Files?": {"main": [
            [{"node": "Queue Zip", "type": "main", "index": 0}],     # true  -> zip
            [{"node": "Expand Files", "type": "main", "index": 0}],  # false -> per file
        ]},
        "Queue Zip": {"main": [[{"node": "Check Jobs", "type": "main", "index": 0}]]},
        "Expand Files": {"main": [[{"node": "Batch Files", "type": "main", "index": 0}]]},
        # splitInBatches outputs are ordered [done, loop]
        "Batch Files": {"main": [
            [{"node": "Check Jobs", "type": "main", "index": 0}],
            [{"node": "Queue File", "type": "main", "index": 0}],
        ]},
        "Queue File": {"main": [[{"node": "Throttle", "type": "main", "index": 0}]]},
        "Throttle": {"main": [[{"node": "Batch Files", "type": "main", "index": 0}]]},

        "Check Jobs": {"main": [[{"node": "All Jobs Done?", "type": "main", "index": 0}]]},
        "All Jobs Done?": {"main": [
            [{"node": "Completed Jobs", "type": "main", "index": 0}],   # true
            [{"node": "Progress: Upload", "type": "main", "index": 0}],  # false
        ]},
        "Progress: Upload": {"main": [[{"node": "Wait Jobs 20s", "type": "main", "index": 0}]]},
        "Wait Jobs 20s": {"main": [[{"node": "Check Jobs", "type": "main", "index": 0}]]},

        "Completed Jobs": {"main": [[{"node": "Find In Drive", "type": "main", "index": 0}]]},
        "Find In Drive": {"main": [[{"node": "Rename File", "type": "main", "index": 0}]]},
        "Rename File": {"main": [[{"node": "Move To Folder", "type": "main", "index": 0}]]},
        "Move To Folder": {"main": [[{"node": "Summarize", "type": "main", "index": 0}]]},
        "Summarize": {"main": [[{"node": "Done", "type": "main", "index": 0}]]},
        "Done": {"main": [[{"node": "Log Success", "type": "main", "index": 0}]]},
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
