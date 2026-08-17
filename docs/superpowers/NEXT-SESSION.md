# Handoff — resume here

**Date written:** 2026-08-17
**Stage:** Design approved. Implementation plan not yet written.

## What to do first

1. Confirm the n8n-mcp tools are now loaded (they were missing last session because
   the server was added *after* the session started). A quick check: the tool list
   should include `n8n_health_check`, `get_node`, `validate_workflow`.
   - If they are still missing, the server is connected but not exposed — check
     `claude mcp list` and restart again before proceeding.
2. Read `docs/superpowers/specs/2026-08-17-mega-torbox-gdrive-design.md` in full.
   It is the approved design and contains every verified API fact.
3. Invoke `superpowers:writing-plans` and write the implementation plan to
   `docs/superpowers/plans/2026-08-17-mega-torbox-gdrive.md`.

## Why the plan was deferred

Node `parameters` objects and `typeVersion`s must come from `get_node`, not from
memory. n8n accepts wrong parameter names as plain strings and then does nothing at
runtime — a silent failure. The plan was blocked on live schema access.

Node types needing `get_node` before they are written into the plan:

- `nodes-base.telegramTrigger`, `nodes-base.telegram` (sendMessage + editMessageText)
- `nodes-base.if`, `nodes-base.wait`, `nodes-base.httpRequest`
- `nodes-base.googleDrive` (search + update/move), `nodes-base.googleSheets` (append)
- `nodes-base.splitInBatches` (per-file fan-out with throttling)

The **TorBox community node** does *not* need this — its source was read directly.
Node type `n8n-nodes-torbox.torBox`, credential `torBoxApi`, package `n8n-nodes-torbox@0.2.1`.

## Verified facts (do not re-probe)

All confirmed against the live API on 2026-08-17. Full detail in the spec.

- Mega **is** supported: 100 links/day, 200 GB/link. Web search claiming otherwise is wrong.
- A Mega **folder** link expands into `files[]`, each with a `file_id`.
- `google_token` must be **real**; empty passes schema then fails the job.
- A **foreign-client** Google token is accepted — TorBox does not require its own client.
- TorBox **ignores its Folder ID** setting and **flattens the path into the filename**.
  Uploads arrive orphaned (`parents` absent). This is why the Drive fix-up step exists.
- Must gate on `download_finished: true` before queuing, or the job fails.
- The queue response `"success": true` is **not** proof — only terminal job status is.
- Rate limit: **300 req/min** per API key.
- Cloudflare rejects unfamiliar user-agents with `403` / `error code: 1010` in the body.
  Set an explicit `User-Agent` on every TorBox request.

## Still unproven (flagged in the spec)

- The **zip branch has never succeeded** — both attempts failed for unrelated reasons.
- The **30-file threshold is derived, not measured.**
- **Per-file at ~1000 files is untested** (only 4 files exercised).

## Environment notes

- `.env.local` holds `TORBOX_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
  `GOOGLE_REFRESH_TOKEN`. Gitignored. **Do not read it** — the scripts load it themselves.
- Working probe scripts live in `scripts/` (see the spec's tooling section).
- `D:\n8n-torbox` is **not a git repo** yet. Nothing has been committed.
- The brainstorming companion may still be running; its screens are under
  `.superpowers/brainstorm/`. Harmless, and it idles out after 4 hours.

## Account warning

The TorBox account's premium expired **2026-08-18**. Web downloads require a paid
plan — if probes start failing with permission errors, check the plan before debugging
the workflow.
