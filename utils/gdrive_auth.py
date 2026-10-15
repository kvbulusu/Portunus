#!/usr/bin/env python3
"""gdrive_auth.py — one-time OAuth consent -> authorized-user token json (§138).

Use this when janus_ec2_runs is a plain *My Drive* folder (service accounts
cannot upload there: they have no storage quota). Run it ONCE on a machine
with a browser (your laptop), then copy the output to the EC2 box:

    pip install google-auth-oauthlib
    # client_secret.json: Google Cloud Console -> APIs & Services ->
    #   Credentials -> Create OAuth client ID -> "Desktop app" -> Download JSON
    #   (and enable the Google Drive API for that project)
    python3 gdrive_auth.py client_secret.json [-o janus_gdrive_credentials.json]
    scp janus_gdrive_credentials.json janus:~/.config/janus_gdrive_credentials.json

The token holds a refresh token, so the agent keeps working unattended.
Keep it private (chmod 600): it grants Drive access as you.
"""
import argparse
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("client_secret", help="OAuth 'Desktop app' client json")
    ap.add_argument("-o", "--out", default="janus_gdrive_credentials.json")
    a = ap.parse_args()
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip install google-auth-oauthlib", file=sys.stderr)
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(a.client_secret, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent",
                                  access_type="offline")
    if not creds.refresh_token:
        print("no refresh token returned; revoke the app at "
              "myaccount.google.com/permissions and retry", file=sys.stderr)
        return 1
    with open(a.out, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(a.out, 0o600)
    print(f"wrote {a.out} -> copy it to ~/.config/janus_gdrive_credentials.json "
          "on the EC2 box")
    return 0


if __name__ == "__main__":
    sys.exit(main())
