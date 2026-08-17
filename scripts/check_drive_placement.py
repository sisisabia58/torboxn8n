#!/usr/bin/env python3
"""Read-only check: where did TorBox actually put the uploaded file?

Searches Drive for one exact filename and reports its parent folder, to confirm
whether TorBox honours the Folder ID setting when the access token comes from a
foreign client_id. Metadata only - no file contents are read.

Usage:
    python scripts/check_drive_placement.py "00-Group Buy.txt"
"""

import json
import sys
import urllib.parse
import urllib.request

from google_oauth import read_env
from queue_with_token import mint_access_token

API = "https://www.googleapis.com/drive/v3"
UA = {"User-Agent": "curl/8.4.0", "Accept": "application/json"}


def get(path, token, **params):
    url = "%s/%s?%s" % (API, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers=dict(UA, Authorization="Bearer " + token))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "00-Group Buy.txt"
    env = read_env()

    try:
        token, _, _ = mint_access_token(env)
    except Exception as e:
        print("ERROR minting token: %s" % e)
        return 1

    q = "name = '%s' and trashed = false" % name.replace("'", "\\'")
    try:
        res = get(
            "files",
            token,
            q=q,
            fields="files(id,name,parents,size,createdTime,mimeType)",
            pageSize=10,
        )
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
        print("ERROR searching Drive: %s %s" % (e, detail))
        return 1

    files = res.get("files", [])
    if not files:
        print("No file named %r found in this Drive account." % name)
        print("=> TorBox reported success but the file is not visible to this token.")
        return 0

    for f in files:
        print("file: %s" % f["name"])
        print("  id:      %s" % f["id"])
        print("  created: %s" % f.get("createdTime"))
        print("  size:    %s bytes" % f.get("size"))
        for pid in f.get("parents", []):
            try:
                p = get("files/%s" % pid, token, fields="id,name,parents")
                root = " (ROOT / My Drive)" if not p.get("parents") else ""
                print("  parent:  %s  [%s]%s" % (p.get("name"), pid, root))
            except Exception as e:
                print("  parent:  %s (could not resolve: %s)" % (pid, e))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
