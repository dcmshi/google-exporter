# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Upload a local export back into a different Google account.

Reads manifest.json produced by export_drive.py, recreates the folder tree under
a single new root folder, and converts .docx/.xlsx/.pptx back into native Google
Docs/Sheets/Slides. Safe to re-run: import_map.json records what already landed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.http import MediaFileUpload

from gauth import (
    DRIVE_FILE,
    DRIVE_FULL,
    FOLDER_MIME,
    credentials_for,
    drive_service,
    require_account,
    with_retry,
)

# Only these round-trip cleanly; Drawings/Apps Script land as plain files.
CONVERT_BACK = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
}

OCTET = "application/octet-stream"


def long_path(p: Path) -> str:
    s = os.path.abspath(str(p))
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


class Importer:
    def __init__(self, service, state_path: Path, dry_run: bool):
        self.service = service
        self.state_path = state_path
        self.dry_run = dry_run
        self.map: dict[str, str] = {}
        if state_path.exists():
            self.map = json.loads(state_path.read_text(encoding="utf-8"))

    def save(self) -> None:
        if not self.dry_run:
            self.state_path.write_text(json.dumps(self.map, indent=2), encoding="utf-8")

    def folder_id(self, rel_dir: str, root_id: str) -> str:
        """Create (or reuse) the folder chain for `rel_dir`, returning its id."""
        if not rel_dir:
            return root_id
        key = f"dir::{rel_dir}"
        if key in self.map:
            return self.map[key]

        parent = rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""
        parent_id = self.folder_id(parent, root_id)
        name = rel_dir.rsplit("/", 1)[-1]

        if self.dry_run:
            self.map[key] = f"dry-run-{rel_dir}"
            return self.map[key]

        created = with_retry(
            lambda: self.service.files()
            .create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute(),
            label=f"mkdir {rel_dir}",
        )
        self.map[key] = created["id"]
        self.save()
        return created["id"]

    def upload(self, entry: dict, local: Path, parent_id: str, convert: bool) -> str:
        body = {
            "name": entry["name"],
            "parents": [parent_id],
            "modifiedTime": entry.get("modified_time"),
        }
        media_mime = entry["export_mime"] or entry["mime_type"] or OCTET
        if convert and entry["export_mime"] and entry["mime_type"] in CONVERT_BACK:
            body["mimeType"] = entry["mime_type"]
        elif not entry["export_mime"]:
            body["mimeType"] = entry["mime_type"] or OCTET

        if media_mime.startswith("application/vnd.google-apps"):
            media_mime = OCTET

        media = MediaFileUpload(
            long_path(local), mimetype=media_mime, resumable=True, chunksize=8 * 1024 * 1024
        )
        request = self.service.files().create(
            body=body, media_body=media, fields="id", supportsAllDrives=True
        )

        def run():
            response = None
            while response is None:
                _, response = request.next_chunk()
            return response

        return with_retry(run, label=entry["rel_path"])["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Restore a local Drive export into another account.")
    ap.add_argument("--manifest", default="manifest.json")
    ap.add_argument("--expect-email", help="abort if the signed-in account differs")
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--profile", default="dest", help="token file suffix")
    ap.add_argument("--folder-name", help="name of the new root folder in the destination")
    ap.add_argument("--no-convert", action="store_true", help="upload Office files as-is")
    ap.add_argument("--limit", type=int, help="stop after N uploads (smoke test)")
    ap.add_argument("--dry-run", action="store_true", help="plan only, upload nothing")
    ap.add_argument(
        "--full-scope",
        action="store_true",
        help="request full Drive access instead of drive.file (only if you hit permission errors)",
    )
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = workdir / manifest_path
    if not manifest_path.exists():
        sys.exit(f"No manifest at {manifest_path}. Run export_drive.py first.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out_root = Path(manifest["out_root"])
    entries = [e for e in manifest["files"] if e["status"] in ("ok", "skipped-exists")]

    scopes = DRIVE_FULL if args.full_scope else DRIVE_FILE
    creds = credentials_for(args.profile, scopes, args.client_secret, workdir)
    service = drive_service(creds)
    account = require_account(service, args.expect_email)
    print(f"Signed in as {account}")

    if account.lower() == str(manifest.get("source_account", "")).lower():
        sys.exit("Destination account is the same as the source account. Aborting.")

    state = Importer(service, workdir / "import_map.json", args.dry_run)

    root_name = args.folder_name or (
        f"Restored from {manifest.get('source_account', 'export')} "
        f"({datetime.now(timezone.utc):%Y-%m-%d})"
    )
    if "root::" in state.map:
        root_id = state.map["root::"]
        print(f"Reusing existing destination folder ({root_id})")
    elif args.dry_run:
        root_id = "dry-run-root"
    else:
        root_id = with_retry(
            lambda: service.files()
            .create(body={"name": root_name, "mimeType": FOLDER_MIME}, fields="id")
            .execute(),
            label="create root",
        )["id"]
        state.map["root::"] = root_id
        state.save()
        print(f"Created destination folder {root_name!r} ({root_id})")

    todo = [e for e in entries if f"file::{e['rel_path']}" not in state.map]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(entries)} files in manifest; {len(todo)} still to upload.")

    uploaded = failed = missing = 0
    for i, entry in enumerate(todo, 1):
        local = out_root / entry["rel_path"]
        if not os.path.exists(long_path(local)):
            print(f"[{i}/{len(todo)}] ?? missing locally: {entry['rel_path']}")
            missing += 1
            continue

        try:
            parent_id = state.folder_id(entry["rel_dir"], root_id)
            if args.dry_run:
                print(f"[{i}/{len(todo)}] -> would upload {entry['rel_path']}")
                uploaded += 1
                continue
            new_id = state.upload(entry, local, parent_id, convert=not args.no_convert)
            state.map[f"file::{entry['rel_path']}"] = new_id
            state.save()
            uploaded += 1
            print(f"[{i}/{len(todo)}] OK {entry['rel_path']}")
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            failed += 1
            print(f"[{i}/{len(todo)}] !! {entry['rel_path']}\n        {type(exc).__name__}: {exc}")

    print(f"\nUploaded {uploaded}, failed {failed}, missing locally {missing}.")
    if not args.dry_run:
        print(f"Destination folder: https://drive.google.com/drive/folders/{root_id}")
        print("Re-run this script to retry failures; finished uploads are skipped.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
