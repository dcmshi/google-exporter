# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Two-step OAuth that never needs the local callback server to be reachable.

Use when the normal flow hangs or lands on a blank page (HTTPS-Only Mode,
firewall, or an over-eager browser eating the http://localhost redirect).

  Step 1:  uv run auth_paste.py --profile dest
  Step 2:  uv run auth_paste.py --profile dest --url "<paste address bar here>"

The redirect target never has to load. Copy the URL out of the address bar even
if the page is blank - the authorization code is in the query string.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The loopback redirect is plain http, which oauthlib rejects unless told.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from google_auth_oauthlib.flow import InstalledAppFlow

from gauth import DRIVE_FILE, DRIVE_FULL, DRIVE_READONLY, check_client_type

REDIRECT = "http://localhost:8765/"


def main() -> int:
    ap = argparse.ArgumentParser(description="Manual paste-based OAuth fallback.")
    ap.add_argument("--profile", default="dest", choices=["source", "dest"])
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--full-scope", action="store_true")
    ap.add_argument("--url", help="the localhost URL you were redirected to")
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    secret = workdir / args.client_secret
    if not secret.exists():
        sys.exit(f"Missing {secret}")
    check_client_type(secret)

    if args.full_scope:
        scopes = DRIVE_FULL
    else:
        scopes = DRIVE_READONLY if args.profile == "source" else DRIVE_FILE

    pending = workdir / f".pending_{args.profile}.json"

    if not args.url:
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes)
        flow.redirect_uri = REDIRECT
        auth_url, state = flow.authorization_url(
            prompt="select_account consent", access_type="offline"
        )
        pending.write_text(
            json.dumps({"state": state, "code_verifier": flow.code_verifier}),
            encoding="utf-8",
        )
        print("\n1. Open this URL in your browser:\n")
        print(auth_url)
        print(
            "\n2. Sign in, click Advanced -> Go to ... (unsafe), tick the permission\n"
            "   checkbox, and continue.\n"
            "\n3. You will land on a localhost page that may be blank or show an error.\n"
            "   That is fine. Copy the ENTIRE URL from the address bar and run:\n"
        )
        print(f'   uv run auth_paste.py --profile {args.profile} --url "<paste it here>"\n')
        return 0

    if not pending.exists():
        sys.exit(f"No pending request. Run without --url first.")
    saved = json.loads(pending.read_text(encoding="utf-8"))

    if "code=" not in args.url:
        sys.exit(
            "That URL has no authorization code in it.\n"
            f"Got: {args.url[:200]}\n"
            "Expected something like http://localhost:8765/?state=...&code=4/0A...\n"
            "If the page showed 'Access blocked', add the account as a test user at\n"
            "https://console.cloud.google.com/auth/audience and start over."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(secret), scopes, state=saved["state"]
    )
    flow.redirect_uri = REDIRECT
    flow.code_verifier = saved["code_verifier"]
    flow.fetch_token(authorization_response=args.url.strip())

    token_path = workdir / f"token_{args.profile}.json"
    token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
    pending.unlink(missing_ok=True)

    print(f"\nToken saved to {token_path}")
    print(f"Refresh token present: {bool(flow.credentials.refresh_token)}")
    print("\nNow run the real command again - it will reuse this token, no browser needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
