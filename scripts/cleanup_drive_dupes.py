#!/usr/bin/env python3
"""Move selected files in the target Drive folder to trash (recoverable).

Selection is by createdTime prefix, so a specific upload batch can be removed
without touching others. Files are trashed, never hard-deleted.

Usage:
    python scripts/cleanup_drive_dupes.py                 # dry run
    python scripts/cleanup_drive_dupes.py --apply
"""

import json
import sys
import urllib.request

sys.path.insert(0, __import__("os").path.dirname(__file__))
from google_oauth import read_env            # noqa: E402
from queue_with_token import mint_access_token  # noqa: E402
from check_drive_placement import get        # noqa: E402

FOLDER = "1ERCHFMwp1jPVgFQPM4VPN4mlObA2pzwI"

# Batches to remove: execution 13's duplicate re-upload, plus three files left
# behind by manual probing earlier in the session.
REMOVE_PREFIXES = (
    "2026-08-17T04:56",
    "2026-08-17T04:57",
    "2026-08-17T01:44",
    "2026-08-17T01:55",
)


def trash(file_id, token):
    req = urllib.request.Request(
        "https://www.googleapis.com/drive/v3/files/%s?supportsAllDrives=true" % file_id,
        data=json.dumps({"trashed": True}).encode(),
        method="PATCH",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 "User-Agent": "curl/8.4.0"},
    )
    urllib.request.urlopen(req, timeout=60).read()


def main():
    apply = "--apply" in sys.argv
    env = read_env()
    token, _, _ = mint_access_token(env)

    files, page = [], None
    while True:
        kw = dict(q="'%s' in parents and trashed = false" % FOLDER,
                  fields="nextPageToken, files(id,name,createdTime)", pageSize=200)
        if page:
            kw["pageToken"] = page
        r = get("files", token, **kw)
        files += r.get("files", [])
        page = r.get("nextPageToken")
        if not page:
            break

    doomed = [f for f in files if f["createdTime"][:16] in REMOVE_PREFIXES]
    keep = [f for f in files if f not in doomed]

    print("folder currently holds : %d files" % len(files))
    print("selected for trash     : %d" % len(doomed))
    print("will remain            : %d" % len(keep))
    print()
    for f in doomed[:5]:
        print("  trash: %s  [%s]" % (f["name"][:64], f["createdTime"][11:19]))
    if len(doomed) > 5:
        print("  ... and %d more" % (len(doomed) - 5))

    if not apply:
        print("\nDRY RUN. Re-run with --apply to move these to trash.")
        return 0

    ok = err = 0
    for f in doomed:
        try:
            trash(f["id"], token)
            ok += 1
        except Exception as e:
            err += 1
            print("  FAILED %s -> %s" % (f["name"][:40], e))

    print("\ntrashed=%d failed=%d" % (ok, err))
    print("Recoverable from Drive's bin if anything looks wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
