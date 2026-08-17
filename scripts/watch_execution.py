#!/usr/bin/env python3
"""Watch the newest execution of the pipeline and emit one line per state change.

Exits on any terminal state. Emits on failure signatures as well as success, so
silence never gets mistaken for progress.

Usage: python scripts/watch_execution.py [workflow_id] [interval] [max_polls]
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_workflow import api  # noqa: E402

# Default workflow id on the current n8n instance. Changes whenever the
# workflow is deployed to a different host -- build_workflow.py finds it by
# NAME, so only this convenience default needs updating.
WID = sys.argv[1] if len(sys.argv) > 1 else "kT5wpQZKNhEX8qsM"
INTERVAL = int(sys.argv[2]) if len(sys.argv) > 2 else 20
MAX = int(sys.argv[3]) if len(sys.argv) > 3 else 60

TERMINAL = {"success", "error", "crashed", "canceled"}


def pick_execution():
    """Prefer an in-flight execution. The default listing excludes running and
    waiting ones, so limit=1 silently returns the last FINISHED run instead."""
    for st in ("running", "waiting"):
        try:
            q = api("GET", "/executions?limit=1&status=%s&workflowId=%s"
                           "&includeData=true" % (st, WID))
        except SystemExit:
            continue
        d = q.get("data") or []
        if d:
            return d[0]
    r = api("GET", "/executions?limit=1&workflowId=%s&includeData=true" % WID)
    d = r.get("data") or []
    return d[0] if d else None


def main():
    seen = None
    pinned = sys.argv[4] if len(sys.argv) > 4 else None

    for _ in range(MAX):
        try:
            if pinned:
                e = api("GET", "/executions/%s?includeData=true" % pinned)
            else:
                e = pick_execution()
        except SystemExit as err:
            print("POLL: api error (transient?) %s" % str(err)[:120])
            time.sleep(INTERVAL)
            continue

        if not e:
            print("POLL: no executions yet")
            time.sleep(INTERVAL)
            continue
        eid, status = e.get("id"), e.get("status")
        rd = (e.get("data") or {}).get("resultData") or {}
        run_data = rd.get("runData") or {}
        last = rd.get("lastNodeExecuted")

        # Node-level errors surface before the execution itself turns terminal.
        node_errs = [n for n, runs in run_data.items()
                     if any(x.get("error") for x in runs)]

        fingerprint = (eid, status, last, len(run_data), tuple(sorted(node_errs)))
        if fingerprint != seen:
            seen = fingerprint
            print("POLL: exec=%s status=%s last=%s nodes=%d%s" % (
                eid, status, last, len(run_data),
                ("  NODE_ERRORS=" + ",".join(node_errs)) if node_errs else ""))

        if status in TERMINAL:
            print("TERMINAL: exec=%s status=%s last=%s" % (eid, status, last))
            if rd.get("error"):
                print("TERMINAL: error=%s" % json.dumps(rd["error"].get("message"))[:200])
            for n in node_errs:
                for x in run_data[n]:
                    if x.get("error"):
                        msg = (x["error"].get("message") or "")[:160]
                        print("TERMINAL: node %s -> %s" % (n, msg))
                        break
            print("TERMINAL: nodes_run=%s" % ",".join(run_data.keys()))
            return 0

        time.sleep(INTERVAL)

    print("TERMINAL: gave up after %d polls (still running)" % MAX)
    return 0


if __name__ == "__main__":
    sys.exit(main())
