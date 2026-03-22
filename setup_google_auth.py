#!/usr/bin/env python3
"""
One-time Google OAuth setup for host-bridge.
Run this on the Mac to generate secrets/google_token.json.

  pip3 install google-auth-oauthlib
  python3 setup_google_auth.py

Opens a browser for consent, then saves token to secrets/google_token.json.
That file is mounted read-only into host-bridge as a Docker secret.
"""

import json
import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit("Run: pip3 install google-auth-oauthlib")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
]

# Read OAuth client credentials from gogcli (already configured for these scopes)
CREDS_FILE = os.path.expanduser("~/Library/Application Support/gogcli/credentials.json")
if not os.path.exists(CREDS_FILE):
    sys.exit(f"Not found: {CREDS_FILE}\nInstall gogcli or create credentials.json manually.")

with open(CREDS_FILE) as f:
    raw = json.load(f)

client_config = {
    "installed": {
        "client_id": raw["client_id"],
        "client_secret": raw["client_secret"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

out = os.path.join(os.path.dirname(__file__), "secrets", "google_token.json")
os.makedirs(os.path.dirname(out), exist_ok=True)

print("Opening browser for Google OAuth consent…")
flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=0)

with open(out, "w") as f:
    f.write(creds.to_json())

print(f"✓ Saved to {out}")
print("Rebuild host-bridge to pick up the new secret:")
print("  docker compose build host-bridge && docker compose up -d host-bridge")
