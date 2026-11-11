#!/usr/bin/env python3
"""gdrive_upload.py - check the Drive token / upload an existing bundle (§229).

Same code path, root folder and per-run subfolder as the agent's end-of-run
upload (NotificationManager.upload_to_drive), so a bundle whose upload failed
(e.g. run 20261109_210551: "invalid_grant: Token has been expired or revoked")
lands exactly where the agent would have put it:

    # after re-running gdrive_auth.py and copying the token to the EC2 box
    python3 asic_flow/utils/gdrive_upload.py --check
    python3 asic_flow/utils/gdrive_upload.py --block janus_soc_top \\
        --subfolder 2026-11-09_21-05-51 \\
        runs/20261109_210551/bundles/janus_soc_top/janus_soc_top_20261110_164458.tar.gz

--subfolder defaults to the run stamp parsed from a .../runs/YYYYMMDD_HHMMSS/...
path (the agent's _drive_run_stamp format). Env: JANUS_GDRIVE_CREDENTIALS,
JANUS_GDRIVE_ROOT, JANUS_GDRIVE_ROOT_ID (as for the agent).
Exit: 0 ok, 1 upload/preflight failed, 2 usage.
"""
import argparse
import os
import re
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notification import NotificationManager  # noqa: E402


def run_stamp(path: str) -> Optional[str]:
    """'.../runs/20261109_210551/...' -> '2026-11-09_21-05-51' (else None)."""
    m = re.search(r"(?:^|/)(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:/|$)",
                  os.path.abspath(path))
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}_{h}-{mi}-{s}"


def main(argv: Optional[List[str]] = None, nm: Optional[NotificationManager] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="bundle(s) to upload")
    ap.add_argument("--block", help="block folder under the Drive root")
    ap.add_argument("--subfolder", help="run subfolder (default: from the path)")
    ap.add_argument("--no-subfolder", action="store_true",
                    help="upload straight into <root>/<block>/")
    ap.add_argument("--creds", help="token/service-account json "
                                    "(default $JANUS_GDRIVE_CREDENTIALS or ~/.config)")
    ap.add_argument("--check", action="store_true",
                    help="only refresh the token and resolve the root folder")
    a = ap.parse_args(argv)
    nm = nm or NotificationManager(gdrive_enabled=True, gdrive_creds=a.creds,
                                   logger=lambda m: print(m, file=sys.stderr))
    prob = nm.gdrive_preflight()
    if prob:
        print(f"gdrive preflight FAILED: {prob}")
        return 1
    print(f"gdrive preflight OK: {nm.gdrive_identity or 'credential'} -> "
          f"{nm.gdrive_target_desc('')}")
    if a.check:
        return 0
    if not a.files or not a.block:
        print("need --block and at least one file (or --check)", file=sys.stderr)
        return 2
    missing = [f for f in a.files if not os.path.isfile(f)]
    if missing:
        print(f"not found: {' '.join(missing)}", file=sys.stderr)
        return 2
    sub = None if a.no_subfolder else (a.subfolder or run_stamp(a.files[0]))
    ids = nm.upload_to_drive(a.files, a.block, subfolder=sub)
    print(nm.last_drive_outcome)
    for link in nm.last_drive_links:
        print(f"  {link}")
    return 0 if ids and len(ids) == len(a.files) else 1


if __name__ == "__main__":
    sys.exit(main())
