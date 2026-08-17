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
