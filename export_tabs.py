# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Audit Google Docs that use tabs, and optionally split them one file per tab.

Exporting a tabbed Doc does NOT lose content: Drive concatenates every tab into
one linear document, in tab order. What it loses is the structure - the tabs
become plain headings, and re-importing cannot put them back, because .docx has
no concept of tabs.

So this is not a recovery tool. Use it to find out which documents are affected
(they are the ones worth copying natively instead - see README), and, if you
would rather have each tab as its own document, to split them.

  Audit only (safe, read-only, no files written):
    uv run export_tabs.py --expect-email you@example.com

  Re-export every tab of every multi-tab doc:
    uv run export_tabs.py --expect-email you@example.com --apply

  Then upload the rescued tabs with the normal importer:
    uv run import_drive.py --manifest manifest_tabs.json --expect-email dest@example.com

Requires the Google Docs API to be enabled in the same Cloud project:
  https://console.cloud.google.com/apis/library/docs.googleapis.com
It authorizes under its own profile, so your existing token_source.json is
untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gauth import (
    DRIVE_READONLY,
    authed_session,
    credentials_for,
    drive_service,
    require_account,
    with_retry,
)
from export_drive import log, long_path, sanitize, unique_in

DOCS_READONLY = "https://www.googleapis.com/auth/documents.readonly"
SCOPES = DRIVE_READONLY + [DOCS_READONLY]

DOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def flatten_tabs(tabs: list[dict], depth: int = 0) -> list[tuple[str, str, int]]:
    """Return [(tabId, title, depth)] depth-first, including nested child tabs."""
    out = []
    for tab in tabs or []:
        props = tab.get("tabProperties", {})
        out.append((props.get("tabId", ""), props.get("title", "Untitled"), depth))
        out.extend(flatten_tabs(tab.get("childTabs", []), depth + 1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Rescue tabbed Google Docs.")
    ap.add_argument("--manifest", default="manifest.json")
    ap.add_argument("--expect-email")
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--profile", default="source_tabs")
    ap.add_argument("--out", default="export_tabs")
    ap.add_argument("--apply", action="store_true", help="write files (default: audit only)")
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    manifest_path = workdir / args.manifest
    if not manifest_path.exists():
        sys.exit(f"No manifest at {manifest_path}. Run export_drive.py first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    out_root = Path(args.out)
    if not out_root.is_absolute():
        out_root = workdir / out_root

    creds = credentials_for(args.profile, SCOPES, args.client_secret, workdir)
    drive = drive_service(creds)
    account = require_account(drive, args.expect_email)
    log(f"Signed in as {account}")

    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    session = authed_session(creds)

    targets = [
        e for e in manifest["files"]
        if e["mime_type"] == DOC_MIME and e["status"] in ("ok", "skipped-exists")
    ]
    log(f"Checking {len(targets)} Google Docs for tabs...\n")

    multi, single, failed = [], 0, 0
    for entry in targets:
        try:
            doc = with_retry(
                lambda: docs.documents()
                .get(documentId=entry["id"], includeTabsContent=True)
                .execute(),
                label=entry["name"],
            )
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status == 403 and "SERVICE_DISABLED" in str(exc.content):
                sys.exit(
                    "\nThe Google Docs API is not enabled for this Cloud project.\n"
                    "Enable it here, wait a minute, then re-run:\n"
                    "  https://console.cloud.google.com/apis/library/docs.googleapis.com"
                )
            log(f"  !! {entry['name']}: HTTP {status}")
            failed += 1
            continue

        tabs = flatten_tabs(doc.get("tabs", []))
        if len(tabs) > 1:
            multi.append((entry, tabs))
            log(f"  {len(tabs):>2} tabs  {entry['name']}")
            for _, title, depth in tabs:
                log(f"           {'  ' * depth}- {title}")
        else:
            single += 1

    log(f"\n{len(multi)} documents have multiple tabs, {single} have one, {failed} unreadable.")
    if not multi:
        log("Nothing was lost to tabs. The plain export already has everything.")
        return 0

    if not args.apply:
        log("\nAudit only. Re-run with --apply to export every tab individually.")
        return 0

    entries = []
    for entry, tabs in multi:
        base = sanitize(entry["name"])
        rel_dir = f"{entry['rel_dir']}/{base}" if entry["rel_dir"] else base
        os.makedirs(long_path(out_root / rel_dir), exist_ok=True)
        taken: set[str] = set()

        for i, (tab_id, title, _) in enumerate(tabs, 1):
            name = unique_in(taken, sanitize(f"{i:02d} {title}") + ".docx")
            dest = out_root / rel_dir / name
            url = (
                f"https://docs.google.com/document/d/{entry['id']}/export"
                f"?format=docx&tab={tab_id}"
            )

            def fetch():
                r = session.get(url, timeout=300)
                r.raise_for_status()
                return r.content

            try:
                data = with_retry(fetch, label=name)
                with open(long_path(dest), "wb") as fh:
                    fh.write(data)
                status, err = "ok", None
                log(f"  OK {rel_dir}/{name}  ({len(data):,} B)")
            except Exception as exc:  # noqa: BLE001
                status, err = "failed", f"{type(exc).__name__}: {exc}"[:300]
                log(f"  !! {rel_dir}/{name}: {err}")

            entries.append({
                "id": f"{entry['id']}#{tab_id}",
                "name": f"{title}.docx",
                "mime_type": DOC_MIME,
                "export_mime": DOCX_MIME,
                "export_link": None,
                "rel_dir": rel_dir,
                "rel_path": f"{rel_dir}/{name}",
                "modified_time": entry.get("modified_time"),
                "created_time": entry.get("created_time"),
                "drive_size": None,
                "md5": None,
                "owned_by_me": entry.get("owned_by_me"),
                "owner": entry.get("owner"),
                "web_view_link": entry.get("web_view_link"),
                "status": status,
                "error": err,
            })

    out_manifest = {
        "source_account": account,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "root_folder_id": manifest.get("root_folder_id"),
        "out_root": str(out_root),
        "dry_run": False,
        "counts": {"ok": sum(1 for e in entries if e["status"] == "ok")},
        "locations": {},
        "owners": {},
        "files": entries,
    }
    (workdir / "manifest_tabs.json").write_text(
        json.dumps(out_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    ok = sum(1 for e in entries if e["status"] == "ok")
    log(f"\nWrote {ok}/{len(entries)} tab files under {out_root}")
    log(f"Manifest: {workdir / 'manifest_tabs.json'}")
    log("\nUpload them with:")
    log("  uv run import_drive.py --manifest manifest_tabs.json --expect-email <dest>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
