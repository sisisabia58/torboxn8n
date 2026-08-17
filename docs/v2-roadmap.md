# v2.0 roadmap

Every item traces to something observed while building and running v1.0, not to
speculation. Where an item exists because a real failure happened, the evidence
is named — see `verification-log.md` for full detail.

Ordered by what I would do first, not by category.

---

## Tier 1 — Known defects

### 1.0 The job list grows without bound per hash
`GET /integration/jobs/{hash}` returned **1742 jobs for a 441-file folder** —
jobs accumulate across every re-run of the same link. Each poll fetches all of
them (~1 MB and climbing), and that is now the single largest contributor to
execution data.

It is not yet a problem: the 441-file run used 3.0 MB total. But it grows
monotonically with every resend, so a frequently retried link will eventually
recreate the memory failure that `Poll Once` just fixed.

**Not fixed by the 1.1 change, and worth being clear why.** Filtering by job id
makes selection exact but cannot shrink the response — the API offers no way to
request a subset, so the full list still arrives and is still retained per poll.

Polling ids individually via `GET /integration/job/{id}` was considered and
rejected: that is one request *per job*, so a 441-file folder would make 441
calls per poll cycle instead of one, blowing the 300 req/min limit outright.

**The only real fix is deleting jobs** once a run completes, via
`DELETE /integration/job/{id}`. That costs one request per job, but once per
transfer rather than once per poll. Worth doing when the list gets large enough
to matter; at 1742 jobs / ~1 MB it does not yet.

### 1.1 ~~Same-link concurrency~~ — FIXED
`This Run's Jobs` filters by `created_at >= <telegram message time>`. Two runs of
the **same** link share a TorBox hash, so the earlier run's cutoff also matches
the later run's jobs: it waits on them, then tries to file another run's files
into its own tree.

Different links are unaffected — job polling is scoped per hash.

**Fixed:** `Poll Once` now captures the `job_id`s the queue step returned (the
batch loop's done output already carries every `Queue File` response), and
`This Run's Jobs` filters on that id set. Exact by construction and immune to
overlap. It also fails loudly if none of the ids have appeared yet, rather than
proceeding with an empty set.

### 1.2 Folder creation is not atomic
Find-or-create is two calls. Two concurrent runs sharing a root folder name can
both find nothing and both create it, producing duplicate roots.

**Fix:** re-query after creation and adopt the lowest-id winner, or serialise
tree creation behind a lock workflow.
**Effort:** small · **Risk:** low.

### 1.3 `Find File` matches globally by name
The lookup searches all of Drive for the flattened name and takes the newest
(`orderBy=createdTime desc`). It leans on ordering rather than isolation, and
gets less safe as duplicates accumulate.

**Fix:** capture the Drive file id at upload time if TorBox's job object ever
exposes one; otherwise scope the query to the destination folder and the current
run's time window.
**Effort:** small · **Risk:** low.

### 1.4 Only two folder levels
`Root/Section/file` is mirrored; anything deeper collapses into its level-2
section. Fine for the course-style folders tested (all were exactly depth 2), but
silently lossy for deeper trees.

**Fix:** a recursive path-resolver sub-workflow that walks segments and caches
`path -> folderId`.
**Effort:** medium · **Risk:** medium — new sub-workflow to build and test.

---

## Tier 2 — Deployed but never exercised

These are not known-broken. They are **unproven**, which is a different and
easier-to-forget kind of risk.

### 2.1 ~~The failure path~~ — PROVEN
Ran for real several times (TorBox cooldown, then a 401). The first attempt
reported `stage=unknown / No detail provided`; after fixing string-error
extraction and adding auth classification it now reports the exact cause and
remedy. Working as intended.

### 2.2 Loop timeout guards
Neither the 120-poll download cap nor the 180-poll job cap has ever tripped.
**Test: temporarily lower a cap to 2 and run.**

### 2.3 The zip branch — STILL NEVER EXECUTED
Threshold is now 1000 files and the largest real folder tested is 441, so it is
even less likely to fire by accident. Untested code that only runs on the biggest
transfers is the worst combination. **Test by temporarily lowering the
threshold**, or delete the branch if per-file is always preferred.

### 2.4 Partial upload failures
The warning line for "5 of 72 uploads failed" has never rendered, because no
upload has ever failed.

---

## Tier 3 — Robustness

### 3.1 Reporting failures are silenced by design
`onError: continueRegularOutput` on the Telegram and Sheets nodes stops a
reporting failure from aborting a completed transfer. It worked — and it also hid
two broken nodes for an entire run (execution 17: chat frozen at "Filing...",
no sheet row, execution green).

Robustness and observability pull in opposite directions here. v2 needs a path
where a suppressed failure still lands *somewhere* — a second channel, or a final
node that inspects prior node status and re-reports.

### 3.2 Retry on failed uploads
Not built. Failures are now visible and re-sending the link is a manual retry,
but an automatic single re-queue would handle transient TorBox errors without
user involvement.

### 3.3 ~~Explicit 429 backoff~~ — DONE
Built once throughput was raised from ~122 to ~238 req/min, which leaves less
headroom. `Queue File`'s error output now routes through a 429 check to a 60s
backoff that resumes the batch loop; anything else goes to the failure path.

### 3.4 Long transfers vs n8n's execution timeout
The download cap is one hour. A genuinely large Mega folder could exceed both
that and n8n's own timeout. Worth measuring the real ceiling and deciding whether
to split the download wait into a resumable sub-workflow.

---

## Tier 4 — Performance

### 4.0 ~~HTTP nodes fan out per input item~~ — FIXED
`Check Jobs` ran once per input item because a batch loop's done output emits
everything it processed: 253 identical API calls and 253 retained copies of the
job list, 97.9% of all execution data. A 400+ file folder reached 150 MB and
died. `Poll Once` collapses the fan-in; the same folder now uses 3.0 MB.

**Rule worth keeping:** before any HTTP node, check how many items reach it.

### 4.1 ~~Drive calls are sequential~~ — CORRECTED, not a bottleneck
Originally recorded as the dominant cost. Node-level timings from execution 18
disprove it: `Find File` and `File Into Place` each ran **once** for all 123
items, taking 4.6s and 5.1s. n8n batches items within a single node run, so
filing is ~10s of a 90s transfer.

The real cost was our own throttle — 39.0s of deliberate waiting against 21.1s
of actual API time. Addressed by retuning batching (25 per batch, 2s pause)
rather than by touching Drive at all.

This entry is kept as a reminder that a plausible-sounding bottleneck should be
measured before it is optimised.

### 4.2 Skip the lookup entirely
If the upload could report its Drive file id, `Find File` disappears — halving
per-file cost and removing defect 1.3 at the same time. Depends on TorBox's job
object, which currently exposes `file_name` but no id.

---

## Tier 5 — Product

### 5.1 Duplicate detection
Deliberately omitted in v1 (always re-upload was chosen). Every resend of a
completed transfer duplicates it in Drive, and that is easy to do by accident —
it happened repeatedly during development. An opt-in check against the sheet log
would catch it.

### 5.2 Link in the completion message
The ✅ message reports counts and duration but no Drive link. One extra field
from the root folder id.

### 5.3 Multiple links per message
Currently the regex takes the first match only. Accepting several and queueing
them would be natural for a chat interface.

### 5.4 Status and cancel commands
No way to ask what's running or to stop it. Cancelling means the n8n UI.

### 5.5 Sheet log growth
The attempt log grows without bound and has no summary view.

---

## Tier 6 — Operations

### 6.1 Railway ephemerality is documented, not enforced
Three separate failures came from the container filesystem being rebuilt:
community node wiped, encryption key rotated, `$env` blocked. v1 removed the
community-node dependency, and the key is now pinned — but nothing prevents a
future change from reintroducing this class of problem. A startup healthcheck
workflow could assert its own preconditions.

### 6.2 Google consent screen must stay published
In **Testing** mode Google expires refresh tokens after 7 days and the workflow
dies weekly. This affects both the minted upload token and n8n's own Drive
credential.

### 6.3 Memory ceiling on the host
Executions 27-29 failed on a 253-file folder in three different ways: a Google
Drive rate limit (a real defect, fixed), a process crash, and an out-of-memory
kill. The job payload was measured at 0.15 MB, so this was the container's
ceiling rather than workflow waste.

An OOM also destroys the run data that would explain it, so the failure is
self-obscuring. Worth setting `NODE_OPTIONS=--max-old-space-size` below the
container limit so Node collects garbage before the platform kills it, and
`EXECUTIONS_DATA_SAVE_ON_SUCCESS=none` to stop retaining full payloads for runs
that worked.

### 6.4 TorBox plan expiry
Web downloads are paid-only. When the plan lapses the failure looks like a
permissions error, not a billing one. Cheap to detect: `GET /user/me` exposes
`premium_expires_at`.

---

## Deliberately not doing

- **Streaming through n8n** (the rejected Option B). Verified viable — the CDN
  link supports byte ranges — but it moves 2× the file size through the host for
  no benefit now that the server-side path works.
- **Reinstating the community node.** Plain HTTP Request calls depend on n8n core
  only and cannot be wiped by a redeploy.
- **Switching the TorBox credential to `httpBearerAuth`.** The Header Auth
  credential requires typing `Bearer ` before the key, and pasting the bare key
  produced a 401 twice — once per instance. `httpBearerAuth` takes just the token
  and adds the prefix itself, which would make the mistake impossible rather than
  merely well-diagnosed. Considered and declined: it works as-is, and
  `Classify Failure` now reports the exact cause and remedy. Revisit if it
  happens a third time.

- **A sub-workflow split of the whole pipeline.** At 45 nodes it is large but
  linear and readable. Only the recursive path resolver (1.4) genuinely wants
  extraction.
