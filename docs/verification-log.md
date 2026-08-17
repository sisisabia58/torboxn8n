# Verification log

Evidence from real executions against live services. Recorded here because
several findings are only observable at runtime and would otherwise be lost.

Workflow: `Mega -> TorBox -> Drive` (n8n id `lXx9PnCwig2H2sQV`)

---

## Tasks 2–4 — executions 1–4 (2026-08-17)

### Execution 1 — junk input
Input `hello` → `Is Mega Link` false → `Reject`. Gate gets it right.

### Execution 3 — bot command
Input `/start` → rejected. Worth noting: `/start` is the first thing Telegram
sends any new bot, so the reject path is hit before a user ever sends a link.

### Execution 2 — cached link
Input: the folder used during design probing.

```
Create Download → "Found cached web download. Using cached download."
                  webdownload_id 1555206
Check Download  → download_finished already true
Download Done?  → true, exits immediately
```

**TorBox short-circuits a previously seen link.** The whole run took one second.
A test that finishes suspiciously fast is probably this, not a broken loop.

### Execution 4 — fresh link, the real test
Input: an uncached Mega folder (`a2oylZTA`).

```
Create Download    → "Mega download started"
Check Download r0  → finished=False progress=0 speed=0
Download Done?  r0 → false
Progress: Download → edited message_id 8
Wait 30s           → resumed
Check Download r1  → finished=True progress=1
Download Done?  r1 → true, exits
```

Duration 34s.

**Rendered Telegram text on the first poll:**

```
Anneke Odendaal - 100 Reel Ideas

Downloading from Mega: 0.0%
measuring speed...
```

Findings:

- **`$runIndex` persists across `Wait` resumptions.** This was genuinely uncertain.
  It is what suppresses TorBox's nonsense first-poll reading (~258 B/s, ETA ~180
  days) before the transfer ramps. Had it reset, every run would open by telling
  the user their download finishes in half a year.
- **`bypass_cache` is nested correctly** — run1 returned fresh state rather than a
  stale `finished=False`, which would have spun the loop forever.
- **`$('Ack').item.json.result.message_id` is the correct path.** Telegram returns
  `{"ok": true, "result": {"message_id": N}}`.
- **Progress edits land in place**, so the chat stays one live status line.

Not yet exercised: the multi-iteration loop. This download finished in a single
cycle, so the `$runIndex >= 1` branch that renders MB/s has never rendered.

---

## Infrastructure findings (Railway + Postgres, 2026-08-17)

Three failures during deployment, none of them workflow logic. Recorded because
each presented as something other than its cause.

### Community node vanished
`n8n-nodes-torbox` disappeared from the instance mid-session. Symptom: the
Telegram bot stopped responding entirely, with no error visible to the user.
Cause: n8n could not activate the workflow (`Unrecognized node type:
n8n-nodes-torbox.torBox`), so it never registered the Telegram webhook.

Confirmed independently: creating a `torBoxApi` credential succeeded earlier in
the session and later failed with `req.body.type is not a known type`.

Resolved by removing the dependency — both operations are plain HTTP calls.

### Encryption key changed once
Symptom: `Credentials could not be decrypted. The likely reason is that a
different "encryptionKey" was used`.

Initially misdiagnosed as the key rotating on every deploy. A volume **is**
mounted at `/home/node/.n8n`, so the key does persist. Testing showed a
newly created credential activates fine, so the current key is stable — the
key changed **once**, most plausibly when the volume was first attached and
masked the pre-existing `.n8n` directory.

Consequence: credentials created before that event are permanently unreadable
and must be recreated. Credentials created after are fine.

### $env blocked, Variables licensed
`n8n Variables` is a licensed feature (403 on community edition), and `$env`
was blocked by `N8N_BLOCK_ENV_ACCESS_IN_NODE` in both Code-node and expression
contexts. Verified with a throwaway probe workflow rather than by burning a
download run.

Note that setting `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` exposes every
environment variable — including `N8N_ENCRYPTION_KEY` — to every workflow on
the instance.

---

## Tasks 5-7, first execution (exec 11)

Reached `Mint Token` and beyond for the first time.

Worked: `Mint Token` returned a real 253-char token with `scope=drive`, proving
`$env` resolves in the live pipeline. `Many Files?` evaluated and branched.

### Bug: TorBox sets download_finished before populating files[]

The run ended at `Expand Files` having emitted zero items, and reported
**success**. Polls showed:

```
run3: finished=False progress=0.94  files=0
run4: finished=True  progress=1     files=0
```

Queried directly a few minutes later, the same download had **72 files**.

So `download_finished` flips before `files[]` is filled. Gating on the flag
alone let the workflow proceed with an empty list: `Expand Files` mapped over
`[]`, emitted nothing, downstream nodes never ran, and the execution finished
green with nothing uploaded.

This also silently corrupted the fan-out decision — `Many Files?` compared
`0 > 30` and chose per-file for what is really a 72-file folder.

**Fix:** `Download Done?` now requires `download_finished === true` AND
`files.length > 0`.

**Note:** a green execution is not evidence of a completed transfer. This one
did nothing at all and looked identical to success.

### Threshold raised 30 -> 150

30 was set before any real folder was measured. At 30, a routine 72-file folder
would collapse into a single 3 GB zip, making its videos unstreamable in Drive.
Batching submits ~200 req/min against a 300/min ceiling, so 150 files is ~45s of
queueing. Zip is now reserved for genuinely huge folders.

### Open risk

The poll loop still has no stall guard (Task 9). If `files[]` never populates,
the loop runs until n8n's execution timeout rather than reporting a failure.

---

## Execution 12 — the upload path works; progress reporting killed the run

With the `files[]` gate fixed and the download cached, this run reached every
remaining node.

**Verified independently against the Drive API, not from execution status:**

| Check | Result |
| --- | --- |
| Files from this run in the target folder | 72 / 72 |
| Correctly parented (not orphaned) | 72 / 72 |
| Renamed to clean basenames | 72 / 72 |

So the Drive fix-up — search, rename, move — works. It had been my predicted
failure point; that prediction was wrong.

### But the execution reported `error`

```
Progress: Upload
Bad Request: message is not modified: specified new message content and
reply markup are exactly the same as a current content
```

Telegram refuses an edit whose text is byte-identical to the current message.
With 72 files, two consecutive polls rendered the same completed-count and the
same rounded percentage, so the edit was a no-op and returned HTTP 400.

**A cosmetic progress update aborted a transfer that had already fully
succeeded.** Files were in place; the run still ended in error and never
reported completion.

**Fix, two layers:**
- Every progress message now ends with `updated HH:mm:ss`, so no two edits are
  ever identical.
- Both progress nodes carry `onError: continueRegularOutput`. Display failures
  must not be able to abort the pipeline.

### Basename collisions

Three files named `Read me.txt` arrived from different subfolders of the same
Mega folder. Renaming to `split('/').pop()` collapses distinct paths into
identical names in one flat Drive folder. Drive permits duplicates so nothing
failed, but the files are no longer distinguishable. Worth prefixing with the
parent segment if this matters.

### Note on counting

The folder listed 75 files, not 72. Three were leftovers from earlier manual
probing, with the old flattened names. Checking `createdTime` separated them
cleanly — raw counts alone would have misread this as the workflow duplicating
work.

---

## Execution 17 — folder tree works; reporting silently failed

The two-level mirror produced exactly the intended structure: 72/72 files in
9 section folders under a root named after the source, no loose files at the
destination top level, and no filename still containing a path separator. The
three `Read me.txt` files landed in different sections, ending the collision.

But the chat stopped at "Filing 72 files..." and no sheet row appeared, while
the execution reported **success**. Both completion nodes had failed and been
swallowed by `onError: continueRegularOutput`:

```
Done        -> Paired item data for item from node 'Build Map' is unavailable
Log Success -> `columns.schema` is required when `columns.mappingMode` is `defineBelow`
```

- `Done` used `$('Telegram Trigger').item`, but `Summarize` collapses 72 items
  into one, severing the paired-item chain `.item` depends on. `.first()` is
  correct after any fan-in.
- The Sheets resourceMapper requires a `schema` array alongside `value`.

### The tradeoff this exposes

`onError: continueRegularOutput` was added deliberately so a reporting failure
could not abort a completed transfer — and it did that. The cost is that those
failures became invisible: the run looked green while two nodes were broken.

Robustness and observability pulled in opposite directions here. The resolution
is Task 9's error path, which is not yet built: failures suppressed by onError
still need to surface somewhere.

### Note

`Progress: Filing` sits between the job list and the tree builder, and a
Telegram node emits the Telegram API response rather than passing its input
through. Any node reading `$input` after it gets the wrong payload. Downstream
Code nodes now address their source node by name instead.

---

## Execution 18 — full pipeline verified end to end

A different and larger source folder than earlier tests.

```
Write Build Scale - Substack System
123 files · 3.45 GB · 88 seconds · 0 failures
```

Every node ran, from `Telegram Trigger` through `Done` and `Log Success`:

- Completion message succeeded: `{"ok": true, "message_id": 30}` — a real
  success, not an error silenced by `onError`, which is how the same node
  presented on execution 17.
- Sheet row written: `success | complete | 123 | 3447422669`.
- Drive tree correct: 11 sections, 123 files, **0** loose files at the root,
  **0** filenames still containing a path separator.

Both previously transferred folders remain correctly structured side by side,
so a second transfer does not disturb the first.

**Task 8 verified.** Tasks 1–8 are now confirmed against live services.

### Still unexercised

- The failure path (`Classify Failure` → `Report Failure` → `Log Failure`) has
  never run. It is deployed but unproven.
- Neither loop timeout guard has ever tripped.
- The zip branch has never executed — this folder was 123 files against a
  threshold of 150.

---

## Phase timings from execution 18 (123 files, 89.7s)

Node-level timings, which changed two assumptions.

| Phase | Runs | Time | Note |
| --- | --- | --- | --- |
| `Queue File` | 13 | 21.1s | real API time, ~172ms/request |
| `Throttle` | 13 | **39.0s** | our own deliberate waiting |
| `Check Jobs` | 1 | 6.2s | one call returns every job |
| `Create Section` | 11 | 8.6s | one per section |
| `Find File` | **1** | 4.6s | all 123 items in a single node run |
| `File Into Place` | **1** | 5.1s | all 123 items in a single node run |

**The rate limit was never the constraint.** Queueing ran at ~122 req/min against
TorBox's 300/min ceiling — 41% utilisation — because 39 of the 60 seconds spent
queueing were our own throttle.

**Drive filing is not sequential per file.** n8n processes every item within one
node run, so filing 123 files cost ~10s, not the per-file cost assumed when the
roadmap was written.

### Changes made

- `batchSize` 10 -> 25, throttle 3s -> 2s: ~238 req/min, a ~20% margin under the
  ceiling.
- Zip threshold 150 -> 1000. Per-file now covers any realistic folder; 1000 files
  is roughly 4 minutes of queueing.
- `MAX_JOB_POLLS` 180 -> 540 (3h), since a premature cap destroys a working large
  transfer while a generous one only wastes wall clock on a stuck one.
- Added a 429 backoff on `Queue File`'s error output, warranted now that
  headroom is smaller.

---

## Execution 23 — the failure path works; TorBox account was in cooldown

**253 files, 12.56 GiB.** Download completed cleanly
(`finished=True present=True cached=True files=253`), all 253 queue requests
were accepted with no HTTP errors, and then **all 253 upload jobs failed** with
`Failed to get file for upload. Please try again.`

Cause: the TorBox account was in cooldown.

```
web_downloads_downloaded   7
total_bytes_downloaded     33.28 GiB
cooldown_until             2026-08-18T06:49:50Z   (~23h out)
premium_expires_at         2026-08-18T00:39:14Z
```

Confirmed by queueing a **single** upload by hand — it failed identically, with
an empty `file_name`. So this was not volume, concurrency, or the retuned
throttle; the account simply could not serve files.

Note the ordering trap: cooldown lifts ~6 hours **after** premium expires, so the
account goes straight from throttled to unpaid.

### What worked

The failure path ran for the first time: `Plan Tree` threw, routed to its error
output, and `Classify Failure → Report Failure → Log Failure` delivered a ❌ to
Telegram and a `failure` row to the sheet. That is roadmap 2.1 proven.

### Two real bugs found

**1. The classifier discarded the reason.** It reported
`stage=unknown, reason=No detail provided.` while `Plan Tree`'s output held
`{"error": "Failed to get file for upload..."}`.

n8n places a thrown message at `.error` as a **plain string**. The code read
`j.error?.message` — undefined on a string — and fell through to the default.
The same shape had already appeared on the `Done` node earlier in the session;
the lesson was not generalised then. Now handled via a `pick()` helper that
accepts either a string or an object, plus a specific `torbox-unavailable`
classification for this message that names cooldown and plan expiry as the
usual causes.

**2. No pre-flight check.** The run downloaded 12.56 GiB and queued 253 uploads
before discovering a condition that one call to `/user/me` would have revealed
up front. Added `Check Account` → `Account OK?` ahead of `Create Download`,
which fails fast and specifically on an active cooldown or an expired plan.

---

## Execution 24 — n8n truncates thrown error messages at colons

The new pre-flight fired correctly and stopped the run before any download. But
the message reaching Telegram was mangled:

```
thrown:    "TorBox account is in cooldown for another 22.9h
            (until 2026-08-18T06:49:50Z). Downloads would succeed but ..."
n8n error: "50Z). Downloads would succeed but every upload fails."
```

**n8n splits a thrown error message on `:` and keeps only the last segment.** The
ISO timestamp in the text (`T06:49:50Z`) destroyed everything before it. The
surviving fragment then matched no classifier pattern, so the stage stayed
`unknown` — a second symptom of the same cause.

### Fix: stop passing failure information through a string

`Account OK?` no longer throws. It returns a structured verdict
(`{ok, stage, reason}`) and a new `Account Gate` IF routes on `ok`.
`Classify Failure` now passes through any input that already carries its own
`stage` and `reason`, only falling back to string parsing for genuine node
errors.

Structured fields cannot be mangled by error-message formatting. Any future
check that knows why it failed should report it this way rather than throwing.

**General rule:** avoid colons in thrown n8n error messages, and prefer a
structured verdict over an exception wherever the caller can route on it.

---

## Execution 26 — 401, and a false-positive pre-flight

Two problems, one of them self-inflicted.

### 401 from a valid key

`Check Account` returned `401 {"detail":"Not authenticated"}` even though the
same key worked from curl. The `TorBox Bearer` credential is a Header Auth whose
value must be the word `Bearer`, a space, then the key. Pasting the bare key
yields `Authorization: <key>` and exactly this 401.

Now classified as `torbox-auth`, with a reason that names the credential and the
prefix, so the next occurrence is self-diagnosing.

**Operational note:** editing that credential in place preserves its ID and needs
no redeploy. Deleting and recreating it changes the ID, and all six nodes that
use it must be repointed — `build_workflow.py --deploy` does that automatically
by resolving credentials by name.

### cooldown_until is not an enforcement flag

The pre-flight added after execution 23 blocked when `cooldown_until` was in the
future. That was inferred from a single account and is **wrong**:

| Account | `cooldown_until` | Uploads |
| --- | --- | --- |
| 969506 (24h trial) | ~24h out | all 253 failed |
| 234926 (paid, 73 downloads) | ~24h out | working |

Both show the field set the same distance ahead; only one is actually blocked. It
behaves like a rolling timestamp, not a block. Gating on it would have rejected a
perfectly healthy paid account.

The check now blocks **only** on expired premium, which is unambiguous, and
carries `cooldown_hours` through as information.

**Lesson:** one account's correlation is not a mechanism. The trial's uploads
probably failed for a plan-tier reason that happened to coexist with a cooldown
timestamp.
