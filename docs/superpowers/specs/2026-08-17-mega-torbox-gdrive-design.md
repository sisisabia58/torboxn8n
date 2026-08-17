# Mega.nz → TorBox → Google Drive (n8n workflow)

**Date:** 2026-08-17
**Status:** Approved design, not yet implemented

## Goal

Send a Mega.nz folder link to a Telegram bot; the folder is fetched by TorBox and
uploaded to a designated Google Drive folder, with live progress reported back in
Telegram and every attempt logged to a spreadsheet.

n8n orchestrates. It never carries the file payload.

## Evidence base

Every claim below was verified against the live TorBox API on 2026-08-17 using
`scripts/probe-torbox.sh`. Nothing here is inferred from documentation, which is
sparse and in places wrong (TorBox publishes no OpenAPI spec, and web search
claims Mega support was removed — it has not been).

| Question | Verified answer |
| --- | --- |
| Is Mega supported? | Yes. `status: true`, 100 links/day, 200 GB per link, 20 TB/day |
| Does a folder link work? | Yes. Expands into `files[]`, each with a `file_id` |
| Download telemetry | `progress` (0–1 float), `download_speed`, `eta`, `download_state`, `download_finished` |
| Is early `eta` reliable? | **No.** First reading showed 258 B/s / ~180 days, then settled at 24.5 MB/s |
| Empty `google_token`? | Passes schema validation, then **the job fails** at upload |
| Foreign-client token? | **Accepted.** TorBox does not require a token from its own OAuth client |
| Is the Folder ID honoured? | **No.** Files arrive orphaned (`parents` absent) |
| Filename handling | **Path flattened into the name**, `/` characters included |
| Job object | `status` (`completed`/`failed`), `progress`, `detail`, `file_name`, `hash` |
| Queue-before-download-finished | Returns success, then **fails** |
| Rate limit | 300 requests/min per API key |
| Direct CDN link | Works, correct `Content-Length`, `accept-ranges: bytes` |
| Cloudflare | Rejects unfamiliar user-agents: `403`, `error code: 1010` in the **body** |

### Three rules these findings impose

1. **Gate on `download_finished: true` before queuing an upload.** Queuing at 31%
   returned `"success": true` and then failed the job.
2. **The queue response proves nothing.** `"success": true` accompanied three
   separate failures. Only a job's terminal `status` is authoritative.
3. **Set an explicit `User-Agent` on every TorBox request.** Cloudflare's `1010`
   arrives as a 403 with an unstructured body and reads like an auth failure.

## Architecture

**Chosen: server-side relay.** TorBox downloads from Mega and uploads to Drive
directly. n8n only issues API calls and polls. The payload never touches the
n8n host.

Two post-upload metadata operations correct TorBox's placement behaviour.

### Rejected alternatives

- **Stream through n8n** (`requestdl` → HTTP download → Drive upload). Fully
  viable and verified — the CDN link supports byte ranges. Rejected because it
  moves 2× the file size through the host and needs matching scratch disk. It
  needs the same Google Cloud OAuth client as the chosen design, so it saves no
  setup. **Revisit if TorBox's uploader becomes unreliable**, since it depends on
  nothing but the CDN.
- **rclone via Execute Command.** Streams well, but moves configuration out of
  the workflow onto the host and is commonly unavailable in Docker installs.

### Why not TorBox's stored Drive connection

TorBox stores a Drive connection for its web UI, but the API requires
`google_token` on every call and does not fall back to the stored credential —
an empty value queues and then fails. n8n must therefore mint a real access
token per run from its own OAuth client.

## Workflow

### 1. Intake

- **Telegram Trigger** on incoming message.
- **IF** the text matches `https?://mega\.nz/(folder|file)/\S+`. Non-matching
  input gets a usage reply and stops.
- **Telegram sendMessage** acknowledging receipt. **Retain `message_id`** — all
  later updates edit this one message rather than posting new ones.

### 2. Download (gated)

- **TorBox → Web Download → Create** with the link. Returns `webdownload_id` and
  `hash`.
- **Wait + poll loop** on `webdl/mylist?id=…&bypass_cache=true` until
  `download_finished` is true.
  - Poll every 30s.
  - Each iteration edits the Telegram message with `progress`, speed, and ETA.
  - **Suppress speed and ETA on the first poll** — they are meaningless until the
    transfer ramps.
  - Treat a non-null `error` field as terminal.

### 3. Fan-out decision

- **IF `files.length > 30`** → zip path: one upload with `zip: true`.
- **Else** → per-file path: one upload per entry in `files[]`.

The threshold exists to stay under the 300 req/min limit. A 1000-file folder
would need 1000 queue calls (≈3.5 min of submission at the ceiling) and risks
TorBox-side Drive throttling; one zip avoids both.

### 4. Upload

- **HTTP Request** → `https://oauth2.googleapis.com/token`, `grant_type=refresh_token`,
  yielding a fresh access token (≈3600s TTL).
- **HTTP Request** → `POST https://api.torbox.app/v1/api/integration/googledrive`
  - Body: `{ id, type: "webdownload", google_token, file_id | zip: true }`
  - Headers: `Authorization: Bearer <torbox key>`, **explicit `User-Agent`**
  - Per-file path batches with a delay holding total throughput under 300/min.

A plain HTTP Request node is used rather than the community node's *Queue Google
Drive* operation, because the token must be injected per run from an expression.

### 5. Confirm (gated)

- **Wait + poll** `GET integration/jobs/{hash}` — returns **every** job for the
  hash in a single call, so polling cost is constant regardless of file count.
- Aggregate: complete when no job remains non-terminal.
- Any job with `status: "failed"` routes to the error path carrying its `detail`.

### 6. Drive fix-up

Required because TorBox orphans uploads and flattens paths into filenames.

This applies to **both** branches. A zip upload arrives orphaned too; its
`file_name` simply contains no path separators, so the rename is a no-op and only
the move matters.

For each completed job:

- **Google Drive → search** `name = '<job.file_name>'` (exact match; the flattened
  name is highly specific, making this deterministic).
- **Google Drive → update** the file: `addParents = <target folder id>`, and
  rename to the basename (final path segment of `job.file_name`).

Both are metadata operations — no bytes move, so cost is independent of size.

> If TorBox later honours its Folder ID setting and stops flattening names, this
> entire step becomes dead weight and should be deleted rather than left in place.

### 7. Report

- **Telegram editMessageText** with a final summary and Drive links.
- **Google Sheets append** one row per attempt: timestamp, source link, folder
  name, byte size, file count, outcome, stage reached, error text, duration.

## Error handling

| Stage | Detection | Response |
| --- | --- | --- |
| Invalid link | IF regex fails | Usage reply; stop |
| Create rejected | non-2xx or `success: false` | Alert + log; stop |
| Cloudflare block | 403 with `1010` in body | Alert naming it as a UA problem — **not** an auth failure |
| Download error | `error` non-null | Alert with error; log; stop |
| Download stalled | speed 0 for 10 consecutive polls | Alert as stalled; leave TorBox job running |
| Queue rejected | non-2xx | One retry after re-minting the token, then alert |
| Job failed | `status: "failed"` | One re-queue attempt; on second failure alert with `detail` |
| Drive lookup empty | search returns nothing | Alert — upload reported success but the file is unfindable |
| Rate limited | 429 | Exponential backoff, resume |

All alerts edit the tracked Telegram message and append a Sheet row. Retries are
capped at one per stage; there is no unbounded retry anywhere.

## Configuration

**n8n credentials**
- TorBox API key (community node credential `torBoxApi`, plus a generic header
  auth credential for the raw HTTP Request calls)
- Google Drive OAuth2 — for the fix-up step
- Telegram bot token
- Google Sheets

**Workflow-level values**
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` — for minting
  the upload token
- `DRIVE_TARGET_FOLDER_ID`
- `FILE_COUNT_THRESHOLD` (default 30)
- `TORBOX_USER_AGENT`

**External prerequisites**
- TorBox paid plan (web downloads are not available on free)
- Google Cloud project with the Drive API enabled and **two OAuth clients**:
  - **Desktop app** — used by `scripts/google_oauth.py` for the loopback flow that
    produces `GOOGLE_REFRESH_TOKEN` (minting upload tokens). Desktop clients only
    permit `http://localhost` redirects.
  - **Web application** — used by n8n's Google Drive and Sheets credentials, with
    n8n's callback (`https://<your-n8n>/rest/oauth2-credential/callback`) added as
    an authorized redirect URI. A Desktop client cannot serve this flow.
- **Consent screen published.** While in Testing, Google expires refresh tokens
  after 7 days and the workflow dies weekly. This applies to n8n's own Drive
  credential too — it is not specific to the token-minting path.
- Google account used for the OAuth client should match the one connected in
  TorBox settings

## Known risks and untested assumptions

1. **The 30-file threshold is a guess.** Derived from the documented 300/min limit,
   not measured. TorBox's own Drive-side throttling is unknown. Validate with a
   folder of 50–100 files before trusting it.
2. **Per-file behaviour at 1000 files is untested.** Only a 4-file folder was
   exercised.
3. **Zip has never succeeded here.** Both zip attempts failed for unrelated
   reasons (queued too early; empty token). The zip branch is unproven and must
   be tested before relying on it for large folders.
4. **Drive lookup by exact name could collide** if the same Mega folder is
   submitted twice concurrently. Acceptable for single-user use; would need a
   createdTime filter or a claim marker under concurrency.
5. **TorBox behaviour may change.** The orphaning and name flattening are
   observed behaviour, not documented contract.
6. **Account trial expiry:** the account used for verification has premium until
   2026-08-18. Web downloads stop working when it lapses.

## Testing plan

Build and verify in this order — each step depends on the previous:

1. Telegram intake and regex rejection, with no TorBox calls.
2. Create + download poll against a **small** folder; confirm progress edits and
   that the first-poll ETA is suppressed.
3. Token minting in isolation; confirm a usable access token.
4. Single-file upload; confirm the job reaches `completed`.
5. Drive fix-up; confirm the file lands in the target folder with a clean name.
6. Per-file fan-out on the 4-file folder.
7. Zip branch on a folder above the threshold. **Currently unproven.**
8. Error paths, by deliberately submitting a dead Mega link and by revoking the
   Google token mid-run.

## Tooling produced during design

- `scripts/probe-torbox.sh` — whoami / submit / status / queue / queuefile / dl / jobs
- `scripts/watch-webdl.sh` — poll a download to completion
- `scripts/watch-job.sh` — poll an integration job to a terminal state
- `scripts/google_oauth.py` — one-time loopback OAuth, writes the refresh token
- `scripts/queue_with_token.py` — queue an upload with a freshly minted token
- `scripts/check_drive_placement.py` — read-only check of where a file landed

These remain useful for debugging the live workflow and should be kept.
