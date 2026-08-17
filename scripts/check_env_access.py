#!/usr/bin/env python3
"""Verify that n8n expressions can read $env before we spend a Mega link on it.

Creates a throwaway webhook-triggered workflow whose Code node reports only
whether each variable is present and its length - never its value. Triggers it,
reads the execution back, prints the verdict, then deletes the workflow.

Usage: python scripts/check_env_access.py
"""

import json
import sys
import time
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__file__))
from build_workflow import api, BASE, APIKEY  # noqa: E402

NAME = "__env access probe (temporary)"
PATH = "torbox-env-probe"

JS = """
// Report presence only. Never emit a secret value.
const keys = ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN'];
const out = {};
let blocked = false;
for (const k of keys) {
  let v;
  try { v = $env[k]; } catch (e) { blocked = true; v = undefined; }
  out[k] = { present: v !== undefined && v !== null && v !== '', length: v ? String(v).length : 0 };
}
out.env_access_blocked = blocked;
return [{ json: out }];
""".strip()


def cleanup():
    for w in api("GET", "/workflows?limit=250").get("data", []):
        if w.get("name") == NAME:
            try:
                api("POST", "/workflows/%s/deactivate" % w["id"])
            except SystemExit:
                pass
            api("DELETE", "/workflows/%s" % w["id"])
            print("cleaned up previous probe id=%s" % w["id"])


def main():
    cleanup()

    wf = {
        "name": NAME,
        "nodes": [
            {
                "parameters": {
                    "httpMethod": "POST",
                    "path": PATH,
                    "responseMode": "onReceived",
                    "options": {},
                },
                "id": "probe-webhook",
                "name": "Probe Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [0, 0],
                "webhookId": "a1b2c3d4-0000-4000-8000-0000000000ff",
            },
            {
                "parameters": {
                    "mode": "runOnceForAllItems",
                    "language": "javaScript",
                    "jsCode": JS,
                },
                "id": "probe-code",
                "name": "Read Env",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [220, 0],
            },
            # The Code node and node expressions are sandboxed differently, and
            # the workflow reads $env from an expression -- so test that path too.
            {
                "parameters": {
                    "conditions": {
                        "combinator": "and",
                        "options": {"caseSensitive": True, "leftValue": "",
                                    "typeValidation": "strict", "version": 2},
                        "conditions": [{
                            "id": "env-expr",
                            "leftValue": "={{ $env.GOOGLE_CLIENT_ID ? 'set' : 'unset' }}",
                            "rightValue": "set",
                            "operator": {"type": "string", "operation": "equals"},
                        }],
                    },
                    "looseTypeValidation": True,
                    "options": {},
                },
                "id": "probe-if",
                "name": "Env In Expression",
                "type": "n8n-nodes-base.if",
                "typeVersion": 2.2,
                "position": [220, 200],
            },
        ],
        "connections": {
            "Probe Webhook": {"main": [[
                {"node": "Read Env", "type": "main", "index": 0},
                {"node": "Env In Expression", "type": "main", "index": 0},
            ]]}
        },
        "settings": {"executionOrder": "v1"},
    }

    created = api("POST", "/workflows", wf)
    wid = created["id"]
    api("POST", "/workflows/%s/activate" % wid)
    print("probe workflow id=%s active" % wid)

    url = "%s/webhook/%s" % (BASE, PATH)
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "curl/8.4.0"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        print("webhook call returned: %s (usually fine for onReceived)" % e)

    verdict = None
    expr_verdict = None
    for _ in range(10):
        time.sleep(2)
        ex = api("GET", "/executions?limit=5&workflowId=%s&includeData=true" % wid)
        for e in ex.get("data", []):
            rd = (e.get("data") or {}).get("resultData") or {}
            run_data = rd.get("runData") or {}

            runs = run_data.get("Read Env") or []
            if runs:
                r = runs[0]
                if r.get("error"):
                    verdict = {"error": json.dumps(r["error"])[:400]}
                else:
                    out = (((r.get("data") or {}).get("main") or [[]])[0] or [])
                    if out:
                        verdict = out[0]["json"]

            iruns = run_data.get("Env In Expression") or []
            if iruns:
                ir = iruns[0]
                if ir.get("error"):
                    expr_verdict = "ERROR: " + json.dumps(ir["error"])[:250]
                else:
                    branches = (ir.get("data") or {}).get("main") or []
                    took_true = bool(branches and branches[0])
                    expr_verdict = "READABLE" if took_true else "empty/undefined"
            if verdict or expr_verdict:
                break
        if verdict or expr_verdict:
            break

    print()
    if verdict is None:
        print("VERDICT: no execution data captured - check the webhook fired.")
    elif "error" in verdict:
        print("VERDICT: Code node errored:")
        print(" ", verdict["error"])
    else:
        blocked = verdict.pop("env_access_blocked", False)
        allset = all(v["present"] for v in verdict.values())
        for k, v in verdict.items():
            print("  %-24s present=%-5s length=%d" % (k, v["present"], v["length"]))
        print()
        if blocked:
            print("VERDICT: $env access is BLOCKED (N8N_BLOCK_ENV_ACCESS_IN_NODE).")
        elif allset:
            print("VERDICT: OK - all three readable via $env. Safe to run the pipeline.")
        else:
            print("VERDICT: some variables are missing from the n8n process environment.")
            print("         The container may not have been recreated after editing them.")

    print()
    print("  $env inside a node EXPRESSION (what the workflow actually uses): %s"
          % (expr_verdict or "no data"))

    api("POST", "/workflows/%s/deactivate" % wid)
    api("DELETE", "/workflows/%s" % wid)
    print("\nprobe workflow deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
