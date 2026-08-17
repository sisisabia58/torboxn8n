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

SHEET_COLUMNS = ("timestamp", "telegram_user", "source_link", "folder_name",
                 "size_bytes", "file_count", "outcome", "stage", "error",
                 "duration_sec")

# Poll ceilings. Without these a loop runs until n8n's execution timeout, which
# looks identical to "still working" from the outside.
MAX_DOWNLOAD_POLLS = 120   # x30s = 1 hour
MAX_JOB_POLLS = 180        # x20s = 1 hour


def sheet_schema():
    """The resourceMapper requires `schema` alongside `value`."""
    return [{"id": c, "displayName": c, "type": "string", "required": False,
             "defaultMatch": False, "display": True, "canBeUsedToMatch": True}
            for c in SHEET_COLUMNS]

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


DRIVE_API = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


def drive_http(name, pos, method, url_expr, body_expr=None):
    """A Drive REST call authorised with the token already minted this run.

    Using the raw API rather than the Google Drive node because the folder
    tree needs find-or-create semantics the node does not offer, and because
    one PATCH can set name and parent together -- the node splits those into
    two operations (three API calls per file).
    """
    params = {
        "method": method,
        "url": url_expr,
        "sendHeaders": True,
        "specifyHeaders": "keypair",
        "headerParameters": {"parameters": [
            {"name": "Authorization",
             "value": "=Bearer {{ $('Mint Token').first().json.access_token }}"},
            {"name": "User-Agent", "value": UA},
        ]},
        "options": {},
    }
    if body_expr is not None:
        params.update({
            "sendBody": True,
            "contentType": "json",
            "specifyBody": "json",
            "jsonBody": body_expr,
        })
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, params,
                None, {"alwaysOutputData": True})


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
           {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000,
            "onError": "continueErrorOutput"}),

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
        }, None, {"onError": "continueErrorOutput"}),

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

        # GET /integration/jobs/{hash} returns every job ever created for that
        # hash, including those from previous runs of the same link. Without
        # this filter a second run sees 144 jobs for a 72-file folder, doubles
        # the Drive fix-up work, and reports inflated counts. Filtering by
        # created_at also covers the zip branch, which produces a single job.
        node("This Run's Jobs", "n8n-nodes-base.code", 2, [2310, 160], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "const startedMs = $('Telegram Trigger').first().json.message.date * 1000;\n"
                "const all = $input.first().json.data || [];\n"
                "const mine = all.filter(j => {\n"
                "  const t = Date.parse(j.created_at);\n"
                "  return Number.isFinite(t) && t >= startedMs;\n"
                "});\n"
                "// Fall back to the full list rather than emitting nothing if\n"
                "// timestamps are unparseable -- doing extra work beats doing none.\n"
                "return [{ json: { data: mine.length ? mine : all } }];",
        }),

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

        # Fills the silent window between "uploads finished" and "files filed".
        # For a 72-file folder the Drive fix-up is ~216 API calls, during which
        # the chat would otherwise still show the original "Queued" message --
        # especially when a cached download skips the download progress entirely.
        # Placed before Completed Jobs so it runs once, not once per file.
        node("Progress: Filing", "n8n-nodes-base.telegram", 1.2, [2640, 20], {
            "resource": "message", "operation": "editMessageText",
            "messageType": "message",
            "chatId": chat_id,
            "messageId": "={{ $('Ack').item.json.result.message_id }}",
            "text": "={{ $('Check Download').first().json.data.name }}\n\n"
                    "Uploaded. Filing "
                    "{{ $json.data.filter(j => j.status === 'completed').length }}"
                    " files into Drive...\n"
                    "updated {{ $now.toFormat('HH:mm:ss') }}",
            "additionalFields": {},
        }, tg, {"onError": "continueRegularOutput"}),

        # TorBox flattens the source path into the filename, e.g.
        #   "Course/02-FUNDAMENTALS/01-FBT.mp4"
        # Plan Tree recovers the structure from those names.
        node("Plan Tree", "n8n-nodes-base.code", 2, [2860, 20], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "// Recover a two-level tree (root/section/file) from the\n"
                "// flattened names TorBox produces. Anything deeper than two\n"
                "// levels collapses into its level-2 section, by design.\n"
                "//\n"
                "// Read the job list from its node by name, NOT from $input:\n"
                "// the immediately upstream node is a Telegram edit, whose\n"
                "// output is the Telegram API response, not the jobs payload.\n"
                "const src = $(\"This Run's Jobs\").first().json.data;\n"
                "if (!Array.isArray(src)) {\n"
                "  throw new Error('Expected a jobs array from This Run\\u2019s Jobs, got '\n"
                "    + JSON.stringify(src).slice(0, 120));\n"
                "}\n"
                "const jobs = src.filter(j => j.status === 'completed');\n"
                "const failed = src.filter(j => j.status === 'failed');\n"
                "if (!jobs.length) {\n"
                "  throw new Error('All ' + src.length + ' uploads failed. First reason: '\n"
                "    + ((failed[0] || {}).detail || 'none given'));\n"
                "}\n"
                "\n"
                "const files = jobs.map(j => {\n"
                "  const parts = String(j.file_name || '').split('/');\n"
                "  const final_name = parts.pop();\n"
                "  const root = parts.length ? parts[0] : '';\n"
                "  const section = parts.length > 1 ? parts[1] : '';\n"
                "  return { job_id: j.id, file_name: j.file_name, final_name, root, section };\n"
                "});\n"
                "\n"
                "// Fall back to the download's own name rather than a generic\n"
                "// label, so a flat Mega folder still gets a meaningful root.\n"
                "const root = (files.find(f => f.root) || {}).root\n"
                "  || $('Check Download').first().json.data.name\n"
                "  || 'TorBox Transfer';\n"
                "const sections = [...new Set(files.map(f => f.section).filter(Boolean))];\n"
                "return [{ json: { root, sections, files,\n"
                "  file_count: files.length,\n"
                "  failed_count: failed.length,\n"
                "  failed_reason: (failed[0] || {}).detail || '' } }];",
        }, None, {"onError": "continueErrorOutput"}),

        drive_http("Find Root", [3080, 20], "GET",
                   "={{ 'https://www.googleapis.com/drive/v3/files?q=' + "
                   "encodeURIComponent(\"name='\" + $json.root.replace(/'/g, \"\\\\'\") + "
                   "\"' and '" + DRIVE_FOLDER_ID + "' in parents and mimeType='" +
                   FOLDER_MIME + "' and trashed=false\") + '&fields=files(id,name)' }}"),

        node("Root Missing?", "n8n-nodes-base.if", 2.2, [3300, 20], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": "no-root",
                    "leftValue": "={{ ($json.files || []).length }}",
                    "rightValue": 0,
                    "operator": {"type": "number", "operation": "equals"},
                }],
            },
            "looseTypeValidation": True,
            "options": {},
        }),

        drive_http("Create Root", [3520, -100], "POST",
                   "=" + DRIVE_API + "?fields=id,name",
                   "={{ JSON.stringify({ name: $('Plan Tree').first().json.root,"
                   " mimeType: '" + FOLDER_MIME + "',"
                   " parents: ['" + DRIVE_FOLDER_ID + "'] }) }}"),

        # Reached from either branch, so it normalises both response shapes:
        # create returns {id}, search returns {files:[{id}]}.
        node("Root Ready", "n8n-nodes-base.code", 2, [3740, 20], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "const j = $input.first().json;\n"
                "const rootId = j.id || ((j.files || [])[0] || {}).id;\n"
                "if (!rootId) throw new Error('Could not resolve the root folder id');\n"
                "return [{ json: { rootId } }];",
        }),

        drive_http("List Sections", [3960, 20], "GET",
                   "={{ 'https://www.googleapis.com/drive/v3/files?q=' + "
                   "encodeURIComponent(\"'\" + $json.rootId + \"' in parents and mimeType='" +
                   FOLDER_MIME + "' and trashed=false\") + "
                   "'&pageSize=200&fields=files(id,name)' }}"),

        node("Plan Sections", "n8n-nodes-base.code", 2, [4180, 20], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "// Always emit exactly one item. A node that emits zero items\n"
                "// stops the branch, which would strand the run when every\n"
                "// section already exists.\n"
                "const existing = ($input.first().json.files || []);\n"
                "const have = new Set(existing.map(f => f.name));\n"
                "const needed = $('Plan Tree').first().json.sections;\n"
                "const missing = needed.filter(s => !have.has(s));\n"
                "return [{ json: {\n"
                "  rootId: $('Root Ready').first().json.rootId,\n"
                "  missing,\n"
                "  missing_count: missing.length,\n"
                "} }];",
        }),

        node("Any Missing?", "n8n-nodes-base.if", 2.2, [4400, 20], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": "has-missing",
                    "leftValue": "={{ $json.missing_count }}",
                    "rightValue": 0,
                    "operator": {"type": "number", "operation": "gt"},
                }],
            },
            "looseTypeValidation": True,
            "options": {},
        }),

        node("Split Sections", "n8n-nodes-base.code", 2, [4620, -100], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "const j = $input.first().json;\n"
                "return j.missing.map(name => ({ json: { name, rootId: j.rootId } }));",
        }),

        node("Section Loop", "n8n-nodes-base.splitInBatches", 3, [4840, -100],
             {"batchSize": 1, "options": {"reset": False}}),

        drive_http("Create Section", [5060, -20], "POST",
                   "=" + DRIVE_API + "?fields=id,name",
                   "={{ JSON.stringify({ name: $json.name,"
                   " mimeType: '" + FOLDER_MIME + "',"
                   " parents: [$json.rootId] }) }}"),

        # Re-listed after creation so the map covers pre-existing and new alike.
        drive_http("List Sections Final", [5280, 20], "GET",
                   "={{ 'https://www.googleapis.com/drive/v3/files?q=' + "
                   "encodeURIComponent(\"'\" + $('Root Ready').first().json.rootId + "
                   "\"' in parents and mimeType='" + FOLDER_MIME + "' and trashed=false\") + "
                   "'&pageSize=200&fields=files(id,name)' }}"),

        node("Build Map", "n8n-nodes-base.code", 2, [5500, 20], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "// One item per file, carrying the folder it belongs in.\n"
                "const map = {};\n"
                "for (const f of ($input.first().json.files || [])) map[f.name] = f.id;\n"
                "const rootId = $('Root Ready').first().json.rootId;\n"
                "return $('Plan Tree').first().json.files.map(f => ({ json: {\n"
                "  file_name: f.file_name,\n"
                "  final_name: f.final_name,\n"
                "  target: f.section ? (map[f.section] || rootId) : rootId,\n"
                "} }));",
        }),

        # Newest first: re-running a link leaves older uploads with the same
        # flattened name, and an unordered search would pick one arbitrarily.
        drive_http("Find File", [5720, 20], "GET",
                   "={{ 'https://www.googleapis.com/drive/v3/files?q=' + "
                   "encodeURIComponent(\"name='\" + $json.file_name.replace(/'/g, \"\\\\'\") + "
                   "\"' and trashed=false\") + "
                   "'&orderBy=createdTime desc&pageSize=5&fields=files(id,name,parents)' }}"),

        # Rename and reparent in a single PATCH -- the Google Drive node splits
        # these into two operations costing three API calls per file.
        #
        # removeParents is required, not optional: TorBox now honours its own
        # folder-ID setting and uploads land INSIDE the destination folder
        # rather than orphaned. Drive enforces a single parent, so adding one
        # without removing the current parent does not move the file.
        drive_http("File Into Place", [5940, 20], "PATCH",
                   "={{ 'https://www.googleapis.com/drive/v3/files/' + "
                   "(($json.files || [])[0] || {}).id + '?addParents=' + "
                   "$('Build Map').item.json.target + '&removeParents=' + "
                   "encodeURIComponent(((($json.files || [])[0] || {}).parents || []).join(',')) + "
                   "'&fields=id,name,parents' }}",
                   "={{ JSON.stringify({ name: $('Build Map').item.json.final_name }) }}"),

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
                "  failed_count: $('Plan Tree').first().json.failed_count || 0,\n"
                "  failed_reason: $('Plan Tree').first().json.failed_reason || '',\n"
                "} }];",
        }),

        # Uses .first() rather than .item: Summarize collapses 72 items into
        # one, which severs the paired-item chain that .item relies on.
        # With .item this fails with "Paired item data ... is unavailable".
        node("Done", "n8n-nodes-base.telegram", 1.2, [3740, 160], {
            "resource": "message", "operation": "editMessageText",
            "messageType": "message",
            "chatId": "={{ $('Telegram Trigger').first().json.message.chat.id }}",
            "messageId": "={{ $('Ack').first().json.result.message_id }}",
            "text": "={{ '\\u2705 ' + $json.folder_name }}\n\n"
                    "{{ $json.moved }}/{{ $json.file_count }} files"
                    " · {{ ($json.size_bytes / 1073741824).toFixed(2) }} GB\n"
                    "Uploaded to Google Drive in {{ $json.duration_sec }}s"
                    # Partial failures were previously invisible: jobs that
                    # failed were filtered out and the run reported success
                    # with a smaller file count and no explanation.
                    "{{ $json.failed_count ? '\\n\\n\\u26a0\\ufe0f ' + $json.failed_count"
                    " + ' upload(s) failed: ' + $json.failed_reason : '' }}",
            "additionalFields": {},
        }, tg, {"onError": "continueRegularOutput"}),

        node("Log Success", "n8n-nodes-base.googleSheets", 4.5, [3960, 160], {
            "authentication": "oAuth2",
            "resource": "sheet",
            "operation": "append",
            "documentId": {"__rl": True, "mode": "id", "value": SHEET_ID},
            "sheetName": {"__rl": True, "mode": "name", "value": SHEET_TAB},
            # The resourceMapper requires `schema` alongside `value`; without
            # it the node fails with "`columns.schema` is required when
            # `columns.mappingMode` is `defineBelow`".
            "columns": {
                "mappingMode": "defineBelow",
                "matchingColumns": [],
                "schema": sheet_schema(),
                "value": {
                    "timestamp": "={{ $now.toISO() }}",
                    "telegram_user":
                        "={{ $('Telegram Trigger').first().json.message.from.username }}",
                    "source_link":
                        "={{ $('Telegram Trigger').first().json.message.text }}",
                    "folder_name": "={{ $('Summarize').first().json.folder_name }}",
                    "size_bytes": "={{ $('Summarize').first().json.size_bytes }}",
                    "file_count": "={{ $('Summarize').first().json.file_count }}",
                    "outcome": "success",
                    "stage": "complete",
                    "error": "",
                    "duration_sec": "={{ $('Summarize').first().json.duration_sec }}",
                },
            },
            "options": {},
        }, {"googleSheetsOAuth2Api": creds["googleSheetsOAuth2Api"]},
           {"onError": "continueRegularOutput"}),

        # --- Task 9: bounded loops and a single failure path -----------------
        # Both poll loops were previously unbounded. A download whose files[]
        # never populates, or a job that never reaches a terminal status, would
        # spin until n8n's execution timeout -- indistinguishable from progress.
        node("Download Timeout?", "n8n-nodes-base.if", 2.2, [440, 380], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": "dl-cap",
                    "leftValue": "={{ $runIndex }}",
                    "rightValue": MAX_DOWNLOAD_POLLS,
                    "operator": {"type": "number", "operation": "gte"},
                }],
            },
            "looseTypeValidation": True,
            "options": {},
        }),

        node("Jobs Timeout?", "n8n-nodes-base.if", 2.2, [1980, 620], {
            "conditions": {
                "combinator": "and",
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": "job-cap",
                    "leftValue": "={{ $runIndex }}",
                    "rightValue": MAX_JOB_POLLS,
                    "operator": {"type": "number", "operation": "gte"},
                }],
            },
            "looseTypeValidation": True,
            "options": {},
        }),

        # Every failure route converges here so there is one place that decides
        # what the user is told and what gets logged.
        node("Classify Failure", "n8n-nodes-base.code", 2, [3740, 620], {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode":
                "// Inputs arrive from node error outputs and from timeout\n"
                "// branches, so the shape varies. Normalise to {stage, reason}.\n"
                "const j = $input.first().json || {};\n"
                "const raw = JSON.stringify(j);\n"
                "\n"
                "let stage = 'unknown';\n"
                "let reason = j.error?.message || j.message || j.detail\n"
                "  || 'No detail provided.';\n"
                "\n"
                "// Cloudflare answers unfamiliar clients with an unstructured\n"
                "// 1010 body. It reads like an auth failure and is not one.\n"
                "if (raw.includes('error code: 1010')) {\n"
                "  stage = 'cloudflare';\n"
                "  reason = 'Blocked by Cloudflare (1010). The User-Agent header was "
                "rejected \\u2014 this is NOT an API key problem.';\n"
                "} else if (raw.includes('download_finished') || j.timeout === 'download') {\n"
                "  stage = 'download';\n"
                "} else if (raw.includes('access_token') || raw.includes('invalid_grant')) {\n"
                "  stage = 'token';\n"
                "} else if (raw.includes('integration/googledrive') || raw.includes('job')) {\n"
                "  stage = 'upload';\n"
                "}\n"
                "\n"
                "return [{ json: { stage, reason: String(reason).slice(0, 300) } }];",
        }),

        node("Report Failure", "n8n-nodes-base.telegram", 1.2, [3960, 620], {
            "resource": "message", "operation": "editMessageText",
            "messageType": "message",
            "chatId": "={{ $('Telegram Trigger').first().json.message.chat.id }}",
            "messageId": "={{ $('Ack').first().json.result.message_id }}",
            "text": "={{ '\\u274c Transfer failed' }}\n\n"
                    "Stage: {{ $json.stage }}\n"
                    "{{ $json.reason }}\n"
                    "updated {{ $now.toFormat('HH:mm:ss') }}",
            "additionalFields": {},
        }, tg, {"onError": "continueRegularOutput"}),

        node("Log Failure", "n8n-nodes-base.googleSheets", 4.5, [4180, 620], {
            "authentication": "oAuth2",
            "resource": "sheet",
            "operation": "append",
            "documentId": {"__rl": True, "mode": "id", "value": SHEET_ID},
            "sheetName": {"__rl": True, "mode": "name", "value": SHEET_TAB},
            "columns": {
                "mappingMode": "defineBelow",
                "matchingColumns": [],
                "schema": sheet_schema(),
                "value": {
                    "timestamp": "={{ $now.toISO() }}",
                    "telegram_user":
                        "={{ $('Telegram Trigger').first().json.message.from.username }}",
                    "source_link":
                        "={{ $('Telegram Trigger').first().json.message.text }}",
                    "folder_name": "",
                    "size_bytes": "",
                    "file_count": "",
                    "outcome": "failure",
                    "stage": "={{ $('Classify Failure').first().json.stage }}",
                    "error": "={{ $('Classify Failure').first().json.reason }}",
                    "duration_sec":
                        "={{ Math.round(($now.toMillis() - "
                        "$('Telegram Trigger').first().json.message.date * 1000) / 1000) }}",
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
        "Create Download": {"main": [
            [{"node": "Check Download", "type": "main", "index": 0}],
            [{"node": "Classify Failure", "type": "main", "index": 0}],
        ]},
        "Check Download": {"main": [[{"node": "Download Done?", "type": "main", "index": 0}]]},
        "Download Done?": {"main": [
            [{"node": "Mint Token", "type": "main", "index": 0}],  # true
            [{"node": "Progress: Download", "type": "main", "index": 0}],
        ]},
        "Progress: Download": {"main": [[{"node": "Wait 30s", "type": "main", "index": 0}]]},
        "Wait 30s": {"main": [[{"node": "Download Timeout?", "type": "main", "index": 0}]]},
        "Download Timeout?": {"main": [
            [{"node": "Classify Failure", "type": "main", "index": 0}],  # capped out
            [{"node": "Check Download", "type": "main", "index": 0}],    # keep polling
        ]},

        "Mint Token": {"main": [
            [{"node": "Many Files?", "type": "main", "index": 0}],
            [{"node": "Classify Failure", "type": "main", "index": 0}],
        ]},
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

        "Check Jobs": {"main": [[{"node": "This Run's Jobs", "type": "main", "index": 0}]]},
        "This Run's Jobs": {"main": [[{"node": "All Jobs Done?", "type": "main", "index": 0}]]},
        "All Jobs Done?": {"main": [
            [{"node": "Progress: Filing", "type": "main", "index": 0}],  # true
            [{"node": "Progress: Upload", "type": "main", "index": 0}],  # false
        ]},
        "Progress: Filing": {"main": [[{"node": "Plan Tree", "type": "main", "index": 0}]]},
        "Progress: Upload": {"main": [[{"node": "Wait Jobs 20s", "type": "main", "index": 0}]]},
        "Wait Jobs 20s": {"main": [[{"node": "Jobs Timeout?", "type": "main", "index": 0}]]},
        "Jobs Timeout?": {"main": [
            [{"node": "Classify Failure", "type": "main", "index": 0}],
            [{"node": "Check Jobs", "type": "main", "index": 0}],
        ]},

        # Folder tree: find-or-create the root, then the sections beneath it.
        "Plan Tree": {"main": [
            [{"node": "Find Root", "type": "main", "index": 0}],
            [{"node": "Classify Failure", "type": "main", "index": 0}],
        ]},
        "Find Root": {"main": [[{"node": "Root Missing?", "type": "main", "index": 0}]]},
        "Root Missing?": {"main": [
            [{"node": "Create Root", "type": "main", "index": 0}],  # true  -> create
            [{"node": "Root Ready", "type": "main", "index": 0}],   # false -> reuse
        ]},
        "Create Root": {"main": [[{"node": "Root Ready", "type": "main", "index": 0}]]},
        "Root Ready": {"main": [[{"node": "List Sections", "type": "main", "index": 0}]]},
        "List Sections": {"main": [[{"node": "Plan Sections", "type": "main", "index": 0}]]},
        "Plan Sections": {"main": [[{"node": "Any Missing?", "type": "main", "index": 0}]]},
        "Any Missing?": {"main": [
            [{"node": "Split Sections", "type": "main", "index": 0}],       # true
            [{"node": "List Sections Final", "type": "main", "index": 0}],  # false
        ]},
        "Split Sections": {"main": [[{"node": "Section Loop", "type": "main", "index": 0}]]},
        # splitInBatches outputs are ordered [done, loop]
        "Section Loop": {"main": [
            [{"node": "List Sections Final", "type": "main", "index": 0}],
            [{"node": "Create Section", "type": "main", "index": 0}],
        ]},
        "Create Section": {"main": [[{"node": "Section Loop", "type": "main", "index": 0}]]},

        # Per-file placement: locate the upload, then rename+reparent in one call.
        "List Sections Final": {"main": [[{"node": "Build Map", "type": "main", "index": 0}]]},
        "Build Map": {"main": [[{"node": "Find File", "type": "main", "index": 0}]]},
        "Find File": {"main": [[{"node": "File Into Place", "type": "main", "index": 0}]]},
        "File Into Place": {"main": [[{"node": "Summarize", "type": "main", "index": 0}]]},
        "Summarize": {"main": [[{"node": "Done", "type": "main", "index": 0}]]},
        "Done": {"main": [[{"node": "Log Success", "type": "main", "index": 0}]]},
        "Classify Failure": {"main": [[{"node": "Report Failure", "type": "main", "index": 0}]]},
        "Report Failure": {"main": [[{"node": "Log Failure", "type": "main", "index": 0}]]},
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
