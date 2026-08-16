# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""List (and optionally trash) the flattened copies of tabbed Google Docs.

Exporting a tabbed Doc to .docx concatenates every tab into one linear
document, so the re-imported copy has the content but no tab structure. The
only way to keep tabs is a native Drive copy, which means removing the
flattened version and copying the original instead.

  List what would be removed:
    uv run clear_flattened.py --expect-email dest@example.com

  Move them to the trash (recoverable for 30 days):
    uv run clear_flattened.py --expect-email dest@example.com --trash
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gauth import DRIVE_FILE, credentials_for, drive_service, require_account, with_retry


def main() -> int:
    ap = argparse.ArgumentParser(description="Remove flattened copies of tabbed Docs.")
    ap.add_argument("--expect-email")
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--profile", default="dest")
    ap.add_argument("--trash", action="store_true", help="actually move them to the trash")
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    tabs_manifest = workdir / "manifest_tabs.json"
    if not tabs_manifest.exists():
        sys.exit("No manifest_tabs.json. Run export_tabs.py --apply first.")

    tabs = json.loads(tabs_manifest.read_text(encoding="utf-8"))
    manifest = json.loads((workdir / "manifest.json").read_text(encoding="utf-8"))
    state = json.loads((workdir / "import_map.json").read_text(encoding="utf-8"))

    # manifest_tabs groups each tab under <original rel_dir>/<doc name>
    docs: dict[str, int] = {}
    for e in tabs["files"]:
        docs[e["rel_dir"]] = docs.get(e["rel_dir"], 0) + 1

    src_by_path = {e["rel_path"]: e for e in manifest["files"]}

    creds = credentials_for(args.profile, DRIVE_FILE, args.client_secret, workdir)
    service = drive_service(creds)
    account = require_account(service, args.expect_email)
    print(f"Signed in as {account}\n")

    targets = []
    for rel_dir, tab_count in sorted(docs.items()):
        doc_name = rel_dir.rsplit("/", 1)[-1]
        rel_path = f"{rel_dir}.docx"
        src = src_by_path.get(rel_path)
        dest_id = state.get(f"file::{rel_path}")
        targets.append((doc_name, tab_count, src, dest_id, rel_path))

    print(f"{'document':<14} {'tabs':>4}  source (share this)")
    print("-" * 78)
    for name, count, src, dest_id, _ in targets:
        link = src.get("web_view_link") if src else "?"
        print(f"{name:<14} {count:>4}  {link}")

    print(f"\n{'document':<14} {'tabs':>4}  flattened copy in {account}")
    print("-" * 78)
    for name, count, _, dest_id, _ in targets:
        link = f"https://docs.google.com/document/d/{dest_id}/edit" if dest_id else "NOT FOUND"
        print(f"{name:<14} {count:>4}  {link}")

    if not args.trash:
        print("\nList only. Re-run with --trash to move the flattened copies to the trash.")
        return 0

    print("\nTrashing flattened copies...")
    trashed = 0
    for name, _, _, dest_id, rel_path in targets:
        if not dest_id:
            print(f"  -- {name}: no destination id recorded, skipping")
            continue
        meta = with_retry(
            lambda: service.files().get(fileId=dest_id, fields="name,mimeType,trashed").execute(),
            label=name,
        )
        if meta.get("trashed"):
            print(f"  -- {name}: already in trash")
            continue
        with_retry(
            lambda: service.files().update(fileId=dest_id, body={"trashed": True}).execute(),
            label=name,
        )
        # Let the importer re-upload it later if the native copy falls through.
        state.pop(f"file::{rel_path}", None)
        trashed += 1
        print(f"  OK {name} -> trash")

    (workdir / "import_map.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"\nTrashed {trashed}. Recoverable from Drive's trash for 30 days.")
    print("Now share the source docs with this account and use Make a copy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
