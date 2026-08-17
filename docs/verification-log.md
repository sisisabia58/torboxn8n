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

---

## Executions 27 & 28 — Google Drive rate limit, and a crash

### 27: the whole pipeline ran, then Drive refused the burst

Every node executed — account check, download, 253 uploads, the full folder
tree — and it died on the very last step:

```
File Into Place -> "Forbidden - perhaps check your credentials?"
description:       "User rate limit exceeded."
```

Not a credential problem despite the message. **Google rate-limited us.**

This is the direct cost of the thing recorded as a win after execution 18:
`Find File` and `File Into Place` each process every item in ONE node run. That
made 123 files fast; it also means 253 PATCH requests fired back-to-back with no
pacing at all. The ceiling sits somewhere between 123 and 253.

**Fix:** the Drive phase now runs through `Drive Batch` (50 per batch) with a 3s
`Drive Throttle`, mirroring the TorBox queueing pattern, and both Drive nodes
carry `retryOnFail` with a 5s wait — Google documents retry-with-backoff as the
correct response to this error. Batching keeps us under the limit; retry catches
what slips through.

`Summarize` now counts from `Plan Tree` rather than `$input`, since with batching
the last batch is all it would otherwise see.

### 28: crashed, not failed

Status `crashed`, last node `This Run's Jobs`, with
`{"isArtificialRecoveredEventItem": true}` — n8n's marker for state reconstructed
after the process died mid-execution. That is infrastructure, not workflow logic:
a Railway restart or an out-of-memory kill while holding 253 items.

Worth watching. If it recurs on large folders, memory during the fan-out is the
first thing to check.

### Note on the account

`Account OK?` returned `ok: true, plan: 1, cooldown_hours: 23.9` — confirming the
corrected pre-flight lets a healthy paid account through while still reporting
the cooldown value for information.

---

## Execution 29 — out of memory, and why the instance is being replaced

```
status: crashed        last node: Check Jobs
error : "Node crashed, possible out-of-memory issue"
```

Almost no run data survived — the crash took it with it, which is itself a
diagnostic problem: an OOM erases the evidence of what consumed the memory.

### It is not payload volume

Measured directly rather than assumed. `GET /integration/jobs/{hash}` for the
253-file folder returns:

```
254 jobs · 0.15 MB · ~601 bytes per job
```

That is small, and it does not grow fast enough across polls to explain an OOM.
The conclusion is that the container's memory ceiling was simply low relative to
n8n's baseline plus a 253-item execution — not that the workflow is wasteful.

Note the job list is scoped per hash and had reached 254 entries for a folder of
253 files, i.e. it accumulates across runs of the same link. That matters for
roadmap 1.1, but it is not the OOM cause at this size.

### Consequence

Executions 27, 28 and 29 all failed at or after the upload phase on a 253-file
folder, in three different ways — Google rate limit, process crash, OOM. Only
the first was a workflow defect. The instance is being migrated to a Railway
account with more memory and CPU rather than tuning the workflow further against
a ceiling it cannot see.

## Migration to a new n8n instance (in progress)

- Old: `https://n8n-production-2890c.up.railway.app` — workflow **deactivated**
  before migrating, so the two instances never contend for the Telegram webhook
  (Telegram permits one webhook per bot token).
- New: `https://n8n-production-564b.up.railway.app` — reachable, `/healthz` 200.

The workflow needs no export. `scripts/build_workflow.py --deploy` reconstructs
all 53 nodes against any instance and resolves credentials **by name**, which is
precisely why no credential IDs have to be copied by hand. Execution history does
not carry over and is not needed.

Highest-risk item on the new host is `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. If it
is missed, `Mint Token` fails with `invalid_client`, which points at Google
rather than at the real cause. `scripts/check_env_access.py` settles it in
seconds.

### Migration complete

New instance `https://n8n-production-564b.up.railway.app`, workflow id
`kT5wpQZKNhEX8qsM`, 53 nodes, active.

Verified before and after deploying, in this order:

| Check | Result |
| --- | --- |
| API reachable, key accepted | OK, 0 workflows (clean host) |
| Credentials by exact name | 4/4 present |
| `$env` in Code node and expression | readable, lengths 72/35/102 — same client |
| Deployed graph | 53 nodes, no dangling connections |
| Community node types | none |
| Activation | succeeded |

Nothing was exported or hand-copied. `build_workflow.py --deploy` rebuilt the
workflow from source and resolved credentials by name, which is why a host move
costs one command rather than a JSON import plus manual credential repointing.

The old instance's workflow was deactivated first, so the two never competed for
the Telegram webhook.

Only `scripts/watch_execution.py` carried a hardcoded workflow id, now updated.

---

## Execution 5 (new instance) — 150 MB of execution data from a 0.15 MB response

A folder of 400+ files failed with `dataTooLargeToDisplay` and no retained run
data. Sizes across three runs on the new host:

```
exec 3 (small)     0.0 MB   success
exec 4 (253 files) 34.5 MB  success
exec 5 (400+)     150.7 MB  error
```

Measuring exec 4 per node found a single dominant cause:

| Node | MB | runs | items | share |
| --- | --- | --- | --- | --- |
| **Check Jobs** | **77.48** | 2 | 254 | **97.9%** |
| All Jobs Done? | 0.30 | 2 | 2 | 0.4% |
| This Run's Jobs | 0.30 | 2 | 2 | 0.4% |
| everything else | <0.2 each | | | ~1% |

### Cause: an HTTP node runs once per input item

`Batch Files`' **done** output emits every item the loop processed — 253 of them.
`Check Jobs` is an HTTP Request node, so it executed **253 times**, made 253
identical API calls, and retained 253 full copies of the job list.

The response itself is 0.15 MB. Retained 253 times it becomes 38 MB per run, and
77 MB across two runs. At 400+ files it reached 150 MB and the execution died.

Note this was never visible as an API problem: TorBox served 253 duplicate
requests without complaint, and the workflow produced correct results at 253
files. Only the memory ceiling exposed it.

### Fix

A `Poll Once` Code node (`runOnceForAllItems`, returns a single item) now sits
between the loop's done output and `Check Jobs`, so the poll runs exactly once
per cycle regardless of file count. Both entry points into the poll loop —
`Batch Files` done and `Queue Zip` — route through it.

Expected effect: ~500x less retained data for the polling phase, and 252 fewer
API calls per cycle.

**General rule:** before an HTTP node, check how many items reach it. Fan-in from
a batch loop's done output is easy to miss and multiplies both traffic and memory
by the item count.

---

## Execution 6 — 441 files, and the Poll Once fix confirmed

```
Matt Par - Tube Mastery and Monetization 3.0
441 files · 16.29 GB · 183 seconds · 0 failures
Drive: 6 sections, 441 files, 0 loose, 0 filenames with a path separator
```

Execution data across comparable runs:

```
exec 4 (253 files)   34.5 MB  success
exec 5 (400+ files) 150.7 MB  FAILED
exec 6 (441 files)    3.0 MB  success
```

50x smaller than the run that died, on more files. The completion message and
sheet row both landed.

### New finding: the job list accumulates per hash

`Check Jobs` returned **1742 jobs** for a 441-file folder — roughly 4x the file
count, because jobs accumulate across every re-run of the same link. At ~601
bytes each that is ~1 MB per poll, and it is now the largest single contributor
to execution data.

Harmless at this size, but it grows monotonically with every resend. A link
retried often enough will eventually recreate the memory problem `Poll Once`
just solved. Recorded as roadmap 1.0; the fix is to poll the specific job ids
returned by `Queue File` rather than the whole hash, which also closes the
same-link concurrency defect.

---

## Execution 7 — job-id filtering verified

```
Temlis - AI Website School
100 files · 3.42 GB · 258 seconds · 0 failures · 0.4 MB execution data
```

The filter change behaved exactly as designed:

```
Poll Once        queued   = 100
This Run's Jobs  mine     = 100
                 expected = 100
```

Every queued job id matched, none missed, nothing extra. Selection is now exact
by construction rather than inferred from a timestamp window, so two concurrent
runs of the same link can no longer see each other's jobs.

Drive verified independently: 8 sections, 99 files in sections, 1 file at the
root, no filename retaining a path separator. The loose file is correct — it sits
at the top level of the source folder, so the root is where it belongs.

Execution data was 0.4 MB. Note this folder was new, so its hash carried only
this run's 100 jobs; the accumulation described in roadmap 1.0 only appears on
links transferred repeatedly.

### Process note

The edit that introduced this change asserted on two replacements and the second
failed, so Python raised before writing the file. `Poll Once` silently kept its
old code while `This Run's Jobs` was deployed referencing `job_ids` that did not
exist — a runtime failure on the next transfer. Verifying the *stored* workflow
after deploying caught it. "Deploy succeeded" is not evidence the intended change
landed.

---

## Scaling to ~100 GB / 1600 files

Asked whether the workflow could handle 100 GB across 1600 files. Measured
answer was **no**, for three separate reasons:

1. 1600 exceeded the 1000-file zip threshold, so it would have been zipped.
2. 100 GB at the measured 24 MB/s is ~71 minutes; the download guard capped at
   120 polls x 30s = 60 minutes and would have killed it mid-transfer.
3. Memory. Each poll retains its whole response, and the response scales with
   file count:

```
Check Jobs      ~0.64 KB per job,  per poll
Check Download  ~0.77 KB per file, per poll
```

At 1600 files that is ~1.0 and ~1.2 MB per poll. Over a 71-minute transfer the
two loops would retain ~380 MB. Execution 5 died at 150 MB.

`EXECUTIONS_TIMEOUT` is unset, so n8n imposes no wall-clock limit — time was
never the constraint, retention was.

### Considered and rejected: zip then unzip in n8n

Keeping the zip and unpacking it inside n8n would mean downloading a 100 GB
archive, extracting it (~200 GB of disk) and uploading 1600 files byte by byte —
roughly 200 GB through the container, which is exactly what the server-side
design exists to avoid.

It also only saves TorBox API calls, which were never the binding constraint,
while converting 1600 cheap Drive *metadata* calls into 1600 Drive *uploads*
carrying real bytes. Strictly worse on every axis except call count.

### Implemented

**Stage A** — zip threshold 1000 -> 5000, and adaptive poll intervals (30s/20s
for six polls, then 60s, then 120s). Retention is (duration / interval) x
payload, so a longer interval cuts memory and extends the time ceiling at once:
120 download polls now cover ~3.4 hours rather than 1.

**Stage B** — polling moved into a `TorBox Job Poll` sub-workflow. Each poll runs
as its own execution: it fetches the job list, filters to this run's ids, and
returns `{seen, expected, completed, failed, progress, done}` — about 100 bytes.
The large payload lives and dies inside the sub-execution.

The parent now:
- builds `{hash, job_ids}` in `Prep Poll` each iteration (the only loop node
  carrying the id list),
- calls the sub-workflow via `Execute Workflow` (typeVersion 1, so `workflowId`
  is a plain string rather than the resourceLocator/resourceMapper shapes of
  later versions),
- fetches the full job list **once** in `Fetch Final Jobs` after the loop ends,
  purely to get file names for filing.

`This Run's Jobs` is gone — the sub-workflow does that filtering now.

### Deployment note

n8n refuses to publish a workflow whose `Execute Workflow` node references an
**unpublished** sub-workflow:

```
Cannot publish workflow: Node "Check Jobs" references workflow ... which is
not published. Please publish all referenced sub-workflows first.
```

`build_workflow.py` now activates the sub-workflow before updating the parent.
