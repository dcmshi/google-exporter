# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Confirm OAuth works and show a sample of what the account can actually see.

Fast: reads one page of results instead of walking the whole Drive.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gauth import (
    DRIVE_FILE,
    DRIVE_FULL,
    DRIVE_READONLY,
    FOLDER_MIME,
    credentials_for,
    drive_service,
    require_account,
    with_retry,
)

SHORT_MIME = {
    "application/vnd.google-apps.document": "Doc",
    "application/vnd.google-apps.spreadsheet": "Sheet",
    "application/vnd.google-apps.presentation": "Slides",
    "application/vnd.google-apps.form": "Form",
    "application/vnd.google-apps.drawing": "Drawing",
    FOLDER_MIME: "Folder",
}


def human(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main() -> int:
    ap = argparse.ArgumentParser(description="Test the Google Drive connection.")
    ap.add_argument("--profile", default="source", choices=["source", "dest"])
    ap.add_argument("--expect-email")
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--full-scope", action="store_true")
    ap.add_argument("--sample", type=int, default=10, help="how many files to show")
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    if args.full_scope:
        scopes = DRIVE_FULL
    else:
        scopes = DRIVE_READONLY if args.profile == "source" else DRIVE_FILE

    creds = credentials_for(args.profile, scopes, args.client_secret, workdir)
    service = drive_service(creds)
    email = require_account(service, args.expect_email)

    about = with_retry(
        lambda: service.about()
        .get(fields="user(displayName,emailAddress),storageQuota")
        .execute(),
        label="about.get",
    )
    quota = about.get("storageQuota", {})
    print(f"\nAuth OK.")
    print(f"  Account : {email} ({about['user'].get('displayName', '')})")
    print(f"  Scope   : {scopes[0]}")
    print(f"  Storage : {human(quota.get('usage'))} used of {human(quota.get('limit'))}")

    if args.profile == "dest" and not args.full_scope:
        print(
            "\nThis is the destination profile using the narrow drive.file scope, so it can\n"
            "only see files this tool creates. An empty listing here is correct."
        )

    resp = with_retry(
        lambda: service.files()
        .list(
            q="trashed = false",
            fields="files(name,mimeType,owners(emailAddress),modifiedTime),nextPageToken",
            pageSize=max(args.sample, 1),
            corpora="user",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            orderBy="modifiedTime desc",
        )
        .execute(),
        label="files.list",
    )
    files = resp.get("files", [])

    if not files:
        print("\nDrive API returned no files for this account.")
        return 0

    more = " (more pages follow)" if resp.get("nextPageToken") else ""
    print(f"\nMost recently modified {len(files)} items{more}:\n")
    width = max(len(f["name"][:50]) for f in files)
    for f in files:
        kind = SHORT_MIME.get(f["mimeType"], f["mimeType"].rsplit(".", 1)[-1][:12])
        owner = (f.get("owners") or [{}])[0].get("emailAddress", "?")
        mine = "you" if owner.lower() == email.lower() else owner
        print(f"  {f['name'][:50]:<{width}}  {kind:<8}  owner: {mine}")

    owners = {(f.get("owners") or [{}])[0].get("emailAddress", "?") for f in files}
    if owners - {email}:
        print(
            "\nSome of these are owned by another account, so they are 'shared with me'\n"
            "rather than yours. Use --include-shared when you run export_drive.py."
        )
    print("\nConnection works. Next: uv run export_drive.py --dry-run "
          f"--expect-email {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
