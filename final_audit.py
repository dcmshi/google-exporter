# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Pre-cancellation audit: is anything in the source account not backed up?

Run this immediately before giving up access to the source account. It re-lists
the source live and diffs it against the manifest, which catches the one thing
a post-hoc verification cannot: files created AFTER the export was taken.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from gauth import DRIVE_READONLY, FOLDER_MIME, SHORTCUT_MIME, credentials_for, drive_service, require_account, with_retry
from export_drive import UNEXPORTABLE, long_path

LIST_FIELDS = (
    "nextPageToken, files(id,name,mimeType,modifiedTime,size,ownedByMe,"
    "owners(emailAddress),webViewLink)"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit before losing the source account.")
    ap.add_argument("--expect-email")
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--profile", default="source")
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    manifest = json.loads((workdir / "manifest.json").read_text(encoding="utf-8"))
    out_root = Path(manifest["out_root"])
    known = {e["id"]: e for e in manifest["files"]}

    creds = credentials_for(args.profile, DRIVE_READONLY, args.client_secret, workdir)
    svc = drive_service(creds)
    account = require_account(svc, args.expect_email)
    print(f"source account: {account}")
    print(f"manifest taken: {manifest['exported_at']}\n")

    live, page = [], None
    while True:
        resp = with_retry(
            lambda: svc.files()
            .list(q="trashed = false", fields=LIST_FIELDS, pageSize=1000, pageToken=page,
                  corpora="user", includeItemsFromAllDrives=True, supportsAllDrives=True)
            .execute(),
            label="files.list",
        )
        live.extend(resp.get("files", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    live_files = [f for f in live if f["mimeType"] != FOLDER_MIME]
    print(f"live in source now : {len(live_files)} files ({len(live) - len(live_files)} folders)")
    print(f"recorded in manifest: {len(known)} files\n")

    new = [f for f in live_files if f["id"] not in known]
    gone = [e for i, e in known.items() if i not in {f["id"] for f in live_files}]

    print("=" * 74)
    if new:
        print(f"!! {len(new)} FILES IN SOURCE BUT NOT BACKED UP — created since the export:\n")
        for f in new:
            owner = (f.get("owners") or [{}])[0].get("emailAddress", "?")
            print(f"   {f['name'][:52]:<52} {f.get('modifiedTime', '')[:10]}  {owner}")
        print("\n   Re-run export_drive.py before cancelling.")
    else:
        print("OK  Nothing in the source is missing from the backup.")

    if gone:
        print(f"\n  {len(gone)} manifest files no longer live in source (deleted or trashed since):")
        for e in gone:
            print(f"   {e['name'][:52]}")

    print("\n" + "=" * 74)
    print("LOCAL BACKUP")
    on_disk, empty, total = 0, [], 0
    for e in manifest["files"]:
        if e["status"] not in ("ok", "skipped-exists"):
            continue
        p = out_root / e["rel_path"]
        if os.path.exists(long_path(p)):
            size = os.path.getsize(long_path(p))
            on_disk += 1
            total += size
            if size == 0:
                empty.append(e["rel_path"])
        else:
            empty.append(f"MISSING: {e['rel_path']}")
    downloadable = sum(1 for e in manifest["files"] if e["status"] in ("ok", "skipped-exists"))
    print(f"  {on_disk}/{downloadable} files present, {total / 1024 / 1024:.2f} MB, at {out_root}")
    for bad in empty:
        print(f"  !! {bad}")
    if not empty and on_disk == downloadable:
        print("  OK  every exported file is on disk and non-empty.")

    print("\n" + "=" * 74)
    print("NOT RECOVERABLE (and never was)")
    skipped = [e for e in manifest["files"] if e["status"].startswith("skipped")]
    for e in skipped:
        why = {"skipped-shortcut": "shortcut to a file owned by someone else",
               "skipped-unexportable": "no export API exists",
               "skipped-shared": "owned by another account",
               "skipped-no-download": "download disabled by owner"}.get(e["status"], e["status"])
        print(f"   {e['name'][:48]:<48} {why}")
    if not skipped:
        print("   nothing")

    print("\n" + "=" * 74)
    verdict = "SAFE TO CANCEL" if not new and not empty else "DO NOT CANCEL YET"
    print(verdict)
    return 0 if verdict.startswith("SAFE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
