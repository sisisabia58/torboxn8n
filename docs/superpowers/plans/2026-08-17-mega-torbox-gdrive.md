# Mega → TorBox → Google Drive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Telegram bot that accepts a Mega.nz folder link, has TorBox fetch it and upload it to a designated Google Drive folder, and reports live progress back in Telegram.

**Architecture:** Server-side relay. n8n issues API calls and polls; the file payload never touches the n8n host. Two metadata-only Google Drive operations correct TorBox's placement behaviour (it orphans uploads and flattens paths into filenames).

**Tech Stack:** n8n (self-hosted), `n8n-nodes-torbox@0.2.1` community node, TorBox API v1, Google Drive API v3, Google Sheets, Telegram Bot API.

## Global Constraints

- **Build in the n8n editor UI.** Nodes are specified by type, operation, and UI field label. Do not hand-write workflow JSON — n8n accepts unknown parameter keys as inert strings and fails silently at runtime.
- **Every TorBox HTTP Request node must set an explicit `User-Agent` header.** Cloudflare answers unfamiliar clients with `403` and `error code: 1010` in the body, which reads like an auth failure.
- **Never gate on a queue response.** `POST integration/googledrive` returns `"success": true` for uploads that subsequently fail. Only a job's terminal `status` is authoritative.
- **Never queue an upload before `download_finished` is `true`.** It returns success and then fails.
- **Secrets go in n8n credentials, never in Set nodes or text fields.**
- **Rate limit: 300 requests/min per TorBox API key.** All fan-out must be throttled beneath it.
- TorBox base URL: `https://api.torbox.app/v1/api`
- Community node type: `n8n-nodes-torbox.torBox`, credential name `torBoxApi`

---

## File Structure

This project produces one n8n workflow plus supporting configuration. There is no source tree.

| Artifact | Responsibility |
| --- | --- |
| n8n workflow **"Mega → TorBox → Drive"** | The entire pipeline |
| n8n credential: TorBox API (`torBoxApi`) | Community node auth |
| n8n credential: Header Auth "TorBox Bearer" | Raw HTTP Request calls to TorBox |
| n8n credential: Google Drive OAuth2 | The fix-up step |
| n8n credential: Google Sheets OAuth2 | Attempt logging |
| n8n credential: Telegram API | Bot messaging |
| Google Sheet **"TorBox Transfers"** | Attempt log |
| `scripts/*` (existing) | Out-of-band probing and debugging |

---

### Task 1: Prerequisites and credentials

No workflow logic. This task exists because every later task fails confusingly without it.

**Files:** none (n8n UI + external consoles)

**Interfaces:**
- Produces: credentials named exactly `TorBox API`, `TorBox Bearer`, `Google Drive`, `Google Sheets`, `Telegram` — later tasks reference these names.

- [ ] **Step 1: Verify the TorBox plan is active**

Web downloads require a paid plan. Run:

```bash
bash scripts/probe-torbox.sh whoami
```

Expected: `"success": true`, and `premium_expires_at` in the future. If it has lapsed, renew before continuing — every later task will fail with permission errors that look like workflow bugs.

- [ ] **Step 2: Install the community node**

In n8n: **Settings → Community nodes → Install** → `n8n-nodes-torbox`.

Expected: a **TorBox** node appears in the node panel.

- [ ] **Step 3: Create the TorBox API credential**

New credential → **TorBox API**. Paste the API key. Save.

Expected: n8n's credential test passes (it calls `/user/me`).

- [ ] **Step 4: Create the header-auth credential for raw calls**

New credential → **Header Auth**. Name it `TorBox Bearer`.
- Name: `Authorization`
- Value: `Bearer <your torbox api key>`

This is needed because the upload call must inject a per-run token via expression, which the community node cannot do.

- [ ] **Step 5: Create the Google Sheet**

Create a sheet named **TorBox Transfers** with a header row:

```
timestamp | telegram_user | source_link | folder_name | size_bytes | file_count | outcome | stage | error | duration_sec
```

- [ ] **Step 6: Create Google Drive, Google Sheets, and Telegram credentials**

n8n's Google credentials need a **Web application** OAuth client — not the Desktop
app client used by `scripts/google_oauth.py`. Desktop clients only allow
`http://localhost` redirects, so they cannot serve n8n's callback.

In Google Cloud, create a second OAuth client of type **Web application** and add
n8n's redirect URI (shown in the n8n credential screen, of the form
`https://<your-n8n>/rest/oauth2-credential/callback`) to its authorized redirect URIs.

Keep the Desktop client — it still produces `GOOGLE_REFRESH_TOKEN` for Task 5.

**Critical:** the consent screen must be **published**, not left in Testing. Google expires refresh tokens after 7 days while in Testing, which silently kills the workflow weekly.

- [ ] **Step 7: Record the target Drive folder ID**

Open the destination folder in Drive. The ID is the last URL segment. Keep it — Task 7 needs it.

- [ ] **Step 8: Verify the Google account matches**

The Google account used for these credentials must be the same one connected inside TorBox settings. A mismatch produces uploads that report success and land in a different account entirely.

---

### Task 2: Telegram intake and link validation

**Files:** n8n workflow — new

**Interfaces:**
- Produces: `$('Telegram Trigger').item.json.message.text` (the raw message), `.message.chat.id`, and an acknowledgement `message_id` used by every later progress edit.

- [ ] **Step 1: Add the trigger**

Add node **Telegram Trigger**. Credential: `Telegram`. Updates: **message**.

- [ ] **Step 2: Add link validation**

Add an **If** node named `Is Mega Link`. Condition — String → **Matches regex**:

- Left value:
```
{{ $json.message.text }}
```
- Right value:
```
https?://mega\.nz/(folder|file)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+
```

- [ ] **Step 3: Add the rejection reply**

On the **false** branch, add **Telegram → Send Message** named `Reject`.
- Chat ID: `{{ $('Telegram Trigger').item.json.message.chat.id }}`
- Text:
```
Send me a Mega.nz folder or file link, e.g. https://mega.nz/folder/XXXX#YYYY
```

- [ ] **Step 4: Add the acknowledgement**

On the **true** branch, add **Telegram → Send Message** named `Ack`.
- Chat ID: `{{ $('Telegram Trigger').item.json.message.chat.id }}`
- Text:
```
Queued. Submitting to TorBox...
```

Every later status update **edits** this message rather than sending a new one, so the chat stays a single live status line.

- [ ] **Step 5: Test**

Execute the workflow, send the bot a junk message, then a real Mega link.

Expected: junk gets the usage reply; a valid link gets `Queued.` The `Ack` node output must contain `result.message_id` — later tasks depend on it.

- [ ] **Step 6: Save the workflow** as `Mega → TorBox → Drive`.

---

### Task 3: Create the web download

**Files:** n8n workflow — modify

**Interfaces:**
- Consumes: the `true` branch of `Is Mega Link`
- Produces: `$('Create Download').item.json.data.webdownload_id` and `.data.hash` — the join keys for every remaining task.

- [ ] **Step 1: Add the TorBox node**

Add **TorBox** node named `Create Download`, after `Ack`.
- Credential: `TorBox API`
- Resource: **Web Download**
- Operation: **Create Web Download**
- Link:
```
{{ $('Telegram Trigger').item.json.message.text.match(/https?:\/\/mega\.nz\/\S+/)[0] }}
```

The regex extraction matters — users routinely send a link with surrounding text, and TorBox rejects the whole string.

- [ ] **Step 2: Enable retry**

In the node's **Settings** tab: **Retry On Fail** on, Max Tries `3`, Wait Between Tries `2000`.

- [ ] **Step 3: Test**

Send a real Mega folder link.

Expected output:
```json
{ "success": true,
  "data": { "webdownload_id": 1555206, "hash": "cd4101eb…" } }
```

- [ ] **Step 4: Commit**

If the repo is initialized, export the workflow JSON and commit:

```bash
git add . && git commit -m "feat: telegram intake and torbox web download creation"
```

---

### Task 4: Poll the download to completion

This is **Gate 1**. Queuing an upload before this gate passes produces a job that reports success and then fails.

**Files:** n8n workflow — modify

**Interfaces:**
- Consumes: `webdownload_id` from Task 3
- Produces: `$('Check Download').item.json.data` containing `files[]`, `name`, `size`, `download_finished`

- [ ] **Step 1: Add the status check**

Add **TorBox** node named `Check Download`.
- Resource: **Web Download**
- Operation: **Get Web Download List**
- Options → **ID**: `{{ $('Create Download').item.json.data.webdownload_id }}`
- Options → **Bypass Cache**: `true`

Bypass Cache is required — without it the response is stale and the loop can spin forever.

- [ ] **Step 2: Add the completion gate**

Add an **If** node named `Download Done?`. Condition — Boolean → **is true**:

```
{{ $json.data.download_finished }}
```

- [ ] **Step 3: Add the progress message**

On the **false** branch add **Telegram → Edit Message Text** named `Progress: Download`.
- Chat ID: `{{ $('Telegram Trigger').item.json.message.chat.id }}`
- Message ID: `{{ $('Ack').item.json.result.message_id }}`
- Text:
```
{{ $('Check Download').item.json.data.name }}

Downloading from Mega: {{ ($('Check Download').item.json.data.progress * 100).toFixed(1) }}%
{{ $runIndex < 1 ? 'measuring speed...' : ($('Check Download').item.json.data.download_speed / 1048576).toFixed(1) + ' MB/s — ETA ' + Math.round($('Check Download').item.json.data.eta / 60) + ' min' }}
```

The `$runIndex < 1` guard suppresses the first poll's speed and ETA. Verified behaviour: the first reading reported 258 B/s and an ETA of ~180 days before settling at 24.5 MB/s. Showing it would be alarming and wrong.

- [ ] **Step 4: Add the wait**

Add a **Wait** node named `Wait 30s` after `Progress: Download`. Resume: **After Time Interval**, 30 seconds. Connect its output back to `Check Download`.

- [ ] **Step 5: Add a stall guard**

Add a **Code** node named `Stalled?` between `Wait 30s` and `Check Download` (**Run Once for All Items**). It trips on either a zero-speed stall or an absolute timeout:

```javascript
// Two independent stall signals:
//  - 10 consecutive polls at zero speed (~5 min of no movement)
//  - 120 polls total (~1 hour) as a hard ceiling
// A Code node is used because n8n has no cross-iteration counter primitive.
const d = $('Check Download').first().json.data;
const state = $getWorkflowStaticData('node');

state.zeroPolls = (d.download_speed === 0) ? (state.zeroPolls || 0) + 1 : 0;

if (state.zeroPolls >= 10) {
  state.zeroPolls = 0;
  return [{ json: { stalled: true, stage: 'download',
    reason: 'Download stalled: no progress for 10 consecutive polls (~5 min).' } }];
}
if ($runIndex >= 120) {
  return [{ json: { stalled: true, stage: 'download',
    reason: 'Download exceeded the 1 hour ceiling without finishing.' } }];
}
return [{ json: { stalled: false } }];
```

Follow it with an **If** node `Stall Trip?` on `{{ $json.stalled }}` **is true** → route to the error path in Task 9. The false branch continues to `Check Download`.

This prevents an unbounded loop when TorBox goes quiet — the TorBox-side download is left running, only n8n stops waiting.

- [ ] **Step 6: Test with a small folder**

Expected: the Telegram message updates in place with a rising percentage; the first update shows `measuring speed...`; the loop exits when `download_finished` becomes true.

- [ ] **Step 7: Commit**

```bash
git add . && git commit -m "feat: download polling loop with live telegram progress"
```

---

### Task 5: Mint a Google access token

TorBox requires a real `google_token` on every upload call. An empty value passes schema validation and then fails the job. TorBox does **not** fall back to the Drive connection stored in its own settings.

**Files:** n8n workflow — modify

**Interfaces:**
- Produces: `$('Mint Token').item.json.access_token` — consumed by Task 6

- [ ] **Step 1: Store the OAuth values**

In n8n: **Settings → Variables**, add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`.

If Variables are unavailable on your license, create a **Header Auth** credential per value instead. Do **not** put them in a Set node — that is a credential leak with extra steps.

- [ ] **Step 2: Add the token request**

On the **true** branch of `Download Done?`, add **HTTP Request** named `Mint Token`.
- Method: **POST**
- URL: `https://oauth2.googleapis.com/token`
- Body Content Type: **Form Urlencoded**
- Parameters:
  - `client_id` → `{{ $vars.GOOGLE_CLIENT_ID }}`
  - `client_secret` → `{{ $vars.GOOGLE_CLIENT_SECRET }}`
  - `refresh_token` → `{{ $vars.GOOGLE_REFRESH_TOKEN }}`
  - `grant_type` → `refresh_token`

- [ ] **Step 3: Test**

Expected: `access_token`, `expires_in: 3599`, and `scope` containing `https://www.googleapis.com/auth/drive`.

If this returns `invalid_grant`, the refresh token has expired — the consent screen is still in Testing mode (see Task 1, Step 6).

- [ ] **Step 4: Commit**

```bash
git add . && git commit -m "feat: google access token minting"
```

---

### Task 6: Queue the upload(s)

**Files:** n8n workflow — modify

**Interfaces:**
- Consumes: `access_token` (Task 5), `webdownload_id` and `files[]` (Tasks 3–4)
- Produces: queued jobs discoverable via the `hash` from Task 3

- [ ] **Step 1: Add the fan-out decision**

Add an **If** node named `Many Files?` after `Mint Token`. Condition — Number → **larger**:

```
{{ $('Check Download').item.json.data.files.length }}
```
larger than `30`

Rationale: per-file uploads cost one request each against a 300/min ceiling. A 1000-file folder would need ~3.5 minutes of pure submission and risks TorBox-side Drive throttling. Above the threshold, one zip is used instead.

- [ ] **Step 2: Add the zip branch**

On **true**, add **HTTP Request** named `Queue Zip`.
- Method: **POST**
- URL: `https://api.torbox.app/v1/api/integration/googledrive`
- Authentication: **Generic → Header Auth** → `TorBox Bearer`
- Headers: `User-Agent` → `n8n-torbox-workflow/1.0`
- Body Content Type: **JSON**
- Body:
```json
{
  "id": {{ $('Create Download').item.json.data.webdownload_id }},
  "type": "webdownload",
  "zip": true,
  "google_token": "{{ $('Mint Token').item.json.access_token }}"
}
```

> **Unproven.** The zip branch has never completed successfully during design — both attempts failed for unrelated reasons. Test it explicitly before relying on it.

- [ ] **Step 3: Add the per-file split**

On **false**, add a **Code** node named `Expand Files` (mode: **Run Once for All Items**):

```javascript
// Fan the download's file list out into one item per file.
// A Code node is justified here: the source array lives on a nested
// property of a different node's output, which Edit Fields cannot expand.
const files = $('Check Download').first().json.data.files;
return files.map(f => ({ json: { file_id: f.id, file_name: f.name || f.short_name } }));
```

- [ ] **Step 4: Add the batch loop**

Add **Loop Over Items** named `Batch Files`, Batch Size `10`.

- [ ] **Step 5: Add the per-file upload**

Inside the loop, add **HTTP Request** named `Queue File` — same URL, auth, and `User-Agent` as Step 2. Body:

```json
{
  "id": {{ $('Create Download').item.json.data.webdownload_id }},
  "type": "webdownload",
  "file_id": {{ $json.file_id }},
  "google_token": "{{ $('Mint Token').item.json.access_token }}"
}
```

- [ ] **Step 6: Add throttling**

After `Queue File`, add **Wait** named `Throttle`, 3 seconds, looping back to `Batch Files`.

10 files per batch with a 3s pause is ~200 requests/min — comfortably under the 300/min ceiling with headroom for the polling calls running alongside.

- [ ] **Step 7: Test per-file on a small folder**

Expected: one `{"success": true, "data": {"job_id": N}}` per file.

**Do not treat this as success.** It only means the requests were accepted. Task 7 determines what actually happened.

- [ ] **Step 8: Commit**

```bash
git add . && git commit -m "feat: throttled per-file and zip upload queueing"
```

---

### Task 7: Poll jobs and correct Drive placement

This is **Gate 2**, plus the fix-up that exists because TorBox ignores its own Folder ID setting and flattens paths into filenames.

**Files:** n8n workflow — modify

**Interfaces:**
- Consumes: `hash` (Task 3)
- Produces: correctly named files in the target Drive folder

- [ ] **Step 1: Add the job poll**

Add **HTTP Request** named `Check Jobs`.
- Method: **GET**
- URL:
```
https://api.torbox.app/v1/api/integration/jobs/{{ $('Create Download').item.json.data.hash }}
```
- Authentication: **Generic → Header Auth** → `TorBox Bearer`
- Headers: `User-Agent` → `n8n-torbox-workflow/1.0`

One call returns every job for the hash, so polling cost is constant whether there are 4 files or 400.

- [ ] **Step 2: Add the aggregate gate**

Add an **If** node named `All Jobs Done?`. Condition — Boolean → **is true**:

```
{{ $json.data.every(j => j.status === 'completed' || j.status === 'failed') }}
```

Verified vocabulary: `status` is `completed` or `failed`; `progress` is a 0–1 float.

- [ ] **Step 3: Add upload progress reporting**

On **false**, add **Telegram → Edit Message Text** named `Progress: Upload` (same Chat ID and Message ID as Task 4):

```
{{ $('Check Download').item.json.data.name }}

Uploading to Drive: {{ $json.data.filter(j => j.status === 'completed').length }}/{{ $json.data.length }} files
{{ Math.round($json.data.reduce((a, j) => a + (j.progress || 0), 0) / $json.data.length * 100) }}%
```

Then a **Wait** node `Wait Jobs 20s` (20 seconds) looping back to `Check Jobs`.

- [ ] **Step 4: Split completed jobs**

On **true**, add a **Code** node named `Completed Jobs` (**Run Once for All Items**):

```javascript
// Emit one item per successfully uploaded file so the Drive fix-up
// can run per file. Failed jobs are routed separately in Task 9.
const jobs = $input.first().json.data;
return jobs
  .filter(j => j.status === 'completed')
  .map(j => ({ json: { job_id: j.id, file_name: j.file_name } }));
```

- [ ] **Step 5: Find the uploaded file in Drive**

Add **Google Drive → File → Search** named `Find In Drive`.
- Credential: `Google Drive`
- Search Method: **Advanced Search** (query string)
- Query:
```
name = '{{ $json.file_name.replace(/'/g, "\\'") }}' and trashed = false
```

The flattened name TorBox produces is highly specific (it contains the full source path), which makes this lookup effectively unique.

- [ ] **Step 6: Rename, then move — two separate nodes**

> **Verified constraint:** Google Drive's `file:update` operation **cannot move a
> file**. Its execute function PATCHes only `name` and binary content and never
> touches `parents`; there is no `addParents` parameter on it. Moving is the
> separate `file:move` operation. Setting a folder on the Update node would be
> silently ignored and the file would stay orphaned while everything reported
> success. See `docs/node-schemas.md`.

Add **Google Drive → File → Update** named `Rename File`:
- File ID: resourceLocator, mode **By ID**, value `{{ $json.id }}`
- New Updated File Name:
```
{{ $('Completed Jobs').item.json.file_name.split('/').pop() }}
```

Then add **Google Drive → File → Move** named `Move To Folder`:
- File ID: resourceLocator, mode **By ID**, value `{{ $('Rename File').item.json.id }}`
- Parent Folder: resourceLocator, mode **By ID**, value = your target folder ID from Task 1 Step 7

Both are metadata-only. No bytes move, so this costs the same for a 30 KB text file
and a 200 GB video.

The uploaded files are orphaned (no parents), so `move`'s internal `removeParents`
resolves to an empty string — harmless.

- [ ] **Step 7: Test**

Expected: the file appears **inside the target folder**, named just the basename (e.g. `Claude Code for Beginners Part 1.mp4`, not the full flattened path).

Verify independently:

```bash
python scripts/check_drive_placement.py "Claude Code for Beginners Part 1.mp4"
```

Expected: a `parent:` line naming your target folder — **not** `NONE (orphaned)`.

- [ ] **Step 8: Commit**

```bash
git add . && git commit -m "feat: job polling and drive placement fix-up"
```

---

### Task 8: Final report and attempt log

**Files:** n8n workflow — modify

- [ ] **Step 1: Add the final message**

After `Fix Placement`, add **Telegram → Edit Message Text** named `Done` (same Chat ID / Message ID):

```
✅ {{ $('Check Download').item.json.data.name }}

{{ $('Check Download').item.json.data.files.length }} files · {{ ($('Check Download').item.json.data.size / 1073741824).toFixed(2) }} GB
Uploaded to Google Drive.
```

- [ ] **Step 2: Add the log row**

Add **Google Sheets → Append Row** named `Log Success`, targeting **TorBox Transfers**:

| Column | Value |
| --- | --- |
| timestamp | `{{ $now.toISO() }}` |
| telegram_user | `{{ $('Telegram Trigger').item.json.message.from.username }}` |
| source_link | `{{ $('Telegram Trigger').item.json.message.text }}` |
| folder_name | `{{ $('Check Download').item.json.data.name }}` |
| size_bytes | `{{ $('Check Download').item.json.data.size }}` |
| file_count | `{{ $('Check Download').item.json.data.files.length }}` |
| outcome | `success` |
| stage | `complete` |
| error | *(leave empty)* |
| duration_sec | `{{ Math.round(($now.toMillis() - new Date($('Telegram Trigger').item.json.message.date * 1000).getTime()) / 1000) }}` |

- [ ] **Step 3: Test end to end**, then **commit**

```bash
git add . && git commit -m "feat: completion reporting and attempt logging"
```

---

### Task 9: Error paths

**Files:** n8n workflow — modify

- [ ] **Step 1: Add the shared error reporter**

Add **Telegram → Edit Message Text** named `Report Failure` (same Chat ID / Message ID):

```
❌ Transfer failed

Stage: {{ $json.stage }}
{{ $json.reason }}
```

Follow it with **Google Sheets → Append Row** named `Log Failure` — same columns as Task 8, `outcome` = `failure`, and `stage` / `error` from the incoming item.

- [ ] **Step 2: Add failure classification**

Add a **Code** node named `Classify Failure` feeding `Report Failure` (**Run Once for All Items**):

```javascript
// Normalize every failure source into { stage, reason } for one reporter.
// Cloudflare's 1010 is called out explicitly: it arrives as a 403 with an
// unstructured body and is otherwise misread as an auth problem.
const item = $input.first().json;
const raw = JSON.stringify(item);

let stage = item.stage || 'unknown';
let reason = item.reason || item.detail || item.error || 'No detail provided.';

if (raw.includes('error code: 1010')) {
  stage = 'cloudflare';
  reason = 'Blocked by Cloudflare (1010). The User-Agent header is missing or rejected — this is NOT an API key problem.';
}

return [{ json: { stage, reason } }];
```

- [ ] **Step 3: Wire the failure sources**

Connect each of these into `Classify Failure`:

| Source | Condition |
| --- | --- |
| `Create Download` | error output (enable **On Error → Continue (using error output)**) |
| `Stalled?` (Task 4 Step 5) | true branch |
| `Check Download` | error output, and any item where `data.error` is non-null |
| `Mint Token` | error output |
| `Queue Zip` / `Queue File` | error output |
| `All Jobs Done?` | any job with `status === 'failed'` — pass its `detail` verbatim as `reason` |
| `Find In Drive` | returns zero results → `reason: 'Upload reported success but the file was not found in Drive.'` |

- [ ] **Step 4: Add one retry for failed jobs**

Add a **Code** node named `Retry Gate` on the failed-job path (**Run Once for All Items**):

```javascript
// Allow exactly one re-queue attempt per workflow execution.
// Unbounded retry against a failing upload burns rate limit and never recovers.
const state = $getWorkflowStaticData('node');
const failed = $input.first().json.data.filter(j => j.status === 'failed');

state.retries = (state.retries || 0) + 1;

if (state.retries > 1) {
  return [{ json: { stage: 'upload', retry: false,
    reason: 'Upload failed twice: ' + (failed[0]?.detail || 'no detail') }}];
}
return [{ json: { retry: true, failed_count: failed.length } }];
```

Add an **If** node `Retry?` on `{{ $json.retry }}` **is true** → back to `Mint Token` (fresh token, then re-queue). False → `Report Failure`.

Reset `state.retries = 0` in the `Done` path so a later execution starts clean.

- [ ] **Step 5: Add rate-limit backoff**

On the error output of `Queue File`, `Queue Zip`, and `Check Jobs`, add a **Code** node named `Is Rate Limited?`:

```javascript
// TorBox allows 300 requests/min per API key. A 429 is recoverable —
// wait and resume rather than failing the whole transfer.
const e = $input.first().json;
const code = e.error?.statusCode || e.statusCode;
return [{ json: { rateLimited: code === 429, original: e } }];
```

On `true`, route to a **Wait** node `Backoff` (60 seconds) that loops back to the failed call. On `false`, continue to `Classify Failure`.

- [ ] **Step 6: Test the failure paths**

- Send a malformed Mega link (valid regex, dead content) → expect a download-stage failure.
- Temporarily blank the `User-Agent` header on `Check Jobs` → expect the Cloudflare-specific message, proving the classifier works.
- Temporarily corrupt `GOOGLE_REFRESH_TOKEN` → expect a `Mint Token` failure.

- [ ] **Step 7: Commit**

```bash
git add . && git commit -m "feat: error classification, reporting, and bounded retry"
```

---

### Task 10: End-to-end validation

- [ ] **Step 1: Full run on the known-good folder**

```
https://mega.nz/folder/RClXRRwI#RreYgbkQWLfbW5qJyMdXVw
```

Known quantities: 4 files, 3.75 GiB, two large `.mp4`s plus two small `.txt`s.

Expected: one Telegram message that transitions Queued → Downloading % → Uploading n/4 → ✅; four correctly named files inside the target Drive folder; one success row in the sheet.

- [ ] **Step 2: Verify placement independently**

```bash
python scripts/check_drive_placement.py "Claude Code for Beginners Part 2.mp4"
```

Expected: `parent:` naming the target folder.

- [ ] **Step 3: Test the zip branch**

Temporarily lower the `Many Files?` threshold to `1` and re-run. **This path has never succeeded and is the least-proven part of the design.**

Expected: a single `.zip` in the target folder. If it fails, capture `detail` and either fix or remove the branch — do not ship an untested path silently.

- [ ] **Step 4: Restore the threshold to `30` and activate**

- [ ] **Step 5: Commit**

```bash
git add . && git commit -m "test: end-to-end validation of mega to drive pipeline"
```

---

## Known-unproven areas

Carried forward from the spec. Do not let these quietly become assumptions:

1. **The zip branch has never completed successfully.** Task 10 Step 3 is its first real test.
2. **The 30-file threshold is derived from the documented 300/min limit, not measured.** TorBox's own Drive-side throttling is unknown. Validate against a 50–100 file folder before trusting it.
3. **Per-file behaviour at ~1000 files is untested.** Only 4 files were exercised.
4. **Drive lookup by exact name could collide** if the same folder is submitted twice concurrently. Acceptable for single-user use; add a `createdTime` filter if that changes.
5. **TorBox's orphaning and name-flattening are observed behaviour, not documented contract.** If they fix it, Task 7 Steps 5–6 become dead weight and should be deleted rather than left in place.
