# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Download an entire Google Drive to local disk, preserving folder structure.

Google-native files (Docs/Sheets/Slides) are exported to Office formats so they
can be re-imported later; everything else downloads byte-for-byte. Writes
manifest.json, which import_drive.py consumes.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from gauth import (
    DRIVE_READONLY,
    FOLDER_MIME,
    SHORTCUT_MIME,
    authed_session,
    credentials_for,
    drive_service,
    require_account,
    with_retry,
)

sys.setrecursionlimit(20000)

# Google-native type -> (export mime, file extension). Only the first three
# round-trip back into native Drive files on import.
EXPORT_MAP = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("image/svg+xml", ".svg"),
    "application/vnd.google-apps.jam": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.script": ("application/vnd.google-apps.script+json", ".json"),
}

# Native types Drive refuses to export in any useful form.
UNEXPORTABLE = {
    "application/vnd.google-apps.form": "Forms have no export API - recreate or share manually",
    "application/vnd.google-apps.site": "Sites have no export API",
    "application/vnd.google-apps.map": "My Maps have no export API",
    "application/vnd.google-apps.fusiontable": "Fusion Tables are discontinued",
}

LIST_FIELDS = (
    "nextPageToken, files(id,name,mimeType,parents,modifiedTime,createdTime,size,"
    "md5Checksum,ownedByMe,owners(emailAddress),webViewLink,exportLinks,trashed,"
    "capabilities(canDownload,canCopy),shortcutDetails(targetId,targetMimeType))"
)

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}

_local = threading.local()
_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def sanitize(name: str, maxlen: int = 110) -> str:
    name = _ILLEGAL.sub("_", name or "").strip().rstrip(". ")
    if not name:
        return "unnamed"
    base, ext = os.path.splitext(name)
    if base.upper() in _RESERVED:
        base = "_" + base
    if len(base) + len(ext) > maxlen:
        base = base[: max(1, maxlen - len(ext))]
    return (base + ext).rstrip(". ") or "unnamed"


def unique_in(taken: set[str], name: str) -> str:
    if name.lower() not in taken:
        taken.add(name.lower())
        return name
    base, ext = os.path.splitext(name)
    n = 2
    while f"{base} ({n}){ext}".lower() in taken:
        n += 1
    out = f"{base} ({n}){ext}"
    taken.add(out.lower())
    return out


def long_path(p: Path) -> str:
    """Windows caps paths at 260 chars unless the \\\\?\\ prefix is used."""
    s = os.path.abspath(str(p))
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def list_all_files(service, corpora: str) -> list[dict]:
    files, page, seen = [], None, 0
    while True:
        resp = with_retry(
            lambda: service.files()
            .list(
                q="trashed = false",
                fields=LIST_FIELDS,
                pageSize=1000,
                pageToken=page,
                corpora=corpora,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                orderBy="folder,name",
            )
            .execute(),
            label="files.list",
        )
        batch = resp.get("files", [])
        files.extend(batch)
        seen += len(batch)
        log(f"  listed {seen} items...")
        page = resp.get("nextPageToken")
        if not page:
            return files


def build_tree(items: list[dict], root_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (folder_id -> relative path, file_id -> relative directory)."""
    by_id = {it["id"]: it for it in items}
    folders = {i: it for i, it in by_id.items() if it["mimeType"] == FOLDER_MIME}

    reachable: dict[str, list[str] | None] = {}

    def chain(fid: str, stack: tuple[str, ...] = ()) -> list[str] | None:
        if fid == root_id:
            return []
        if fid in reachable:
            return reachable[fid]
        if fid in stack or fid not in folders:
            reachable[fid] = None
            return None
        result = None
        for parent in folders[fid].get("parents") or []:
            up = chain(parent, stack + (fid,))
            if up is not None:
                result = up + [fid]
                break
        reachable[fid] = result
        return result

    for fid in folders:
        chain(fid)

    # Name folders breadth-first so uniqueness is enforced per parent directory.
    children = defaultdict(list)
    for fid in folders:
        chain_ids = reachable.get(fid)
        if chain_ids is None:
            continue
        parent = chain_ids[-2] if len(chain_ids) > 1 else root_id
        children[parent].append(fid)

    folder_path: dict[str, str] = {root_id: ""}
    taken_per_dir: dict[str, set[str]] = defaultdict(set)
    queue = deque([root_id])
    while queue:
        parent = queue.popleft()
        for fid in sorted(children.get(parent, []), key=lambda x: folders[x]["name"].lower()):
            name = unique_in(taken_per_dir[parent], sanitize(folders[fid]["name"]))
            base = folder_path[parent]
            folder_path[fid] = f"{base}/{name}" if base else name
            queue.append(fid)

    file_dir: dict[str, str] = {}
    for it in items:
        if it["mimeType"] == FOLDER_MIME:
            continue
        placed = None
        for parent in it.get("parents") or []:
            if parent in folder_path:
                placed = folder_path[parent]
                break
        if placed is None:
            placed = "_unfiled" if it.get("ownedByMe") else "_shared_with_me"
        file_dir[it["id"]] = placed

    return folder_path, file_dir


def session_for(creds):
    if not hasattr(_local, "session"):
        _local.session = authed_session(creds)
    return _local.session


def service_for(creds):
    if not hasattr(_local, "service"):
        _local.service = drive_service(creds)
    return _local.service


def stream_to_disk(session, url: str, dest: Path) -> int:
    """GET `url` following redirects manually so the bearer token survives hops."""
    for _ in range(6):
        resp = session.get(url, stream=True, allow_redirects=False, timeout=600)
        if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
            url = resp.headers["location"]
            resp.close()
            continue
        break
    else:
        raise RuntimeError("too many redirects")

    if resp.status_code >= 400:
        detail = resp.text[:400]
        resp.close()
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")

    tmp = Path(str(dest) + ".part")
    written = 0
    os.makedirs(long_path(dest.parent), exist_ok=True)
    with open(long_path(tmp), "wb") as fh:
        for chunk in resp.iter_content(1 << 20):
            if chunk:
                fh.write(chunk)
                written += len(chunk)
    resp.close()
    os.replace(long_path(tmp), long_path(dest))
    return written


def download_one(entry: dict, creds, out_root: Path, force: bool) -> dict:
    dest = out_root / entry["rel_path"]
    if not force and os.path.exists(long_path(dest)):
        entry["status"] = "skipped-exists"
        entry["local_size"] = os.path.getsize(long_path(dest))
        return entry

    session = session_for(creds)
    fid = entry["id"]

    try:
        if entry["export_mime"]:
            url = entry.get("export_link")
            if not url:
                svc = service_for(creds)
                meta = with_retry(
                    lambda: svc.files().get(fileId=fid, fields="exportLinks").execute(),
                    label=f"exportLinks {fid}",
                )
                url = (meta.get("exportLinks") or {}).get(entry["export_mime"])
            if not url:
                url = (
                    f"https://www.googleapis.com/drive/v3/files/{fid}/export"
                    f"?mimeType={quote(entry['export_mime'])}"
                )
        else:
            url = (
                f"https://www.googleapis.com/drive/v3/files/{fid}"
                "?alt=media&supportsAllDrives=true"
            )

        size = with_retry(lambda: stream_to_disk(session, url, dest), label=entry["rel_path"])
        entry["status"] = "ok"
        entry["local_size"] = size
    except Exception as exc:  # noqa: BLE001 - recorded per file, run continues
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description="Export a Google Drive to local disk.")
    ap.add_argument("--out", default="export", help="output directory (default: ./export)")
    ap.add_argument("--expect-email", help="abort if the signed-in account differs")
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--profile", default="source", help="token file suffix")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--include-shared", action="store_true", help="also pull Shared with me")
    ap.add_argument("--all-drives", action="store_true", help="include shared drives")
    ap.add_argument("--limit", type=int, help="stop after N downloads (smoke test)")
    ap.add_argument("--force", action="store_true", help="re-download files already on disk")
    ap.add_argument("--dry-run", action="store_true", help="inventory only, no downloads")
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    out_root = Path(args.out)
    if not out_root.is_absolute():
        out_root = workdir / out_root
    os.makedirs(long_path(out_root), exist_ok=True)

    creds = credentials_for(args.profile, DRIVE_READONLY, args.client_secret, workdir)
    service = drive_service(creds)
    account = require_account(service, args.expect_email)
    log(f"Signed in as {account}")

    root_id = with_retry(
        lambda: service.files().get(fileId="root", fields="id").execute(), label="root"
    )["id"]

    log("Listing Drive contents...")
    items = list_all_files(service, "allDrives" if args.all_drives else "user")
    log(f"Found {len(items)} items (folders + files).")

    folder_path, file_dir = build_tree(items, root_id)

    entries: list[dict] = []
    taken_per_dir: dict[str, set[str]] = defaultdict(set)
    for it in sorted(items, key=lambda x: (file_dir.get(x["id"], ""), x["name"].lower())):
        mime = it["mimeType"]
        if mime == FOLDER_MIME:
            continue
        rel_dir = file_dir[it["id"]]
        export_mime, ext = EXPORT_MAP.get(mime, (None, ""))
        name = sanitize(it["name"])
        if export_mime and not name.lower().endswith(ext):
            name = sanitize(it["name"], maxlen=110 - len(ext)) + ext
        name = unique_in(taken_per_dir[rel_dir], name)

        entry = {
            "id": it["id"],
            "name": it["name"],
            "mime_type": mime,
            "export_mime": export_mime,
            "export_link": (it.get("exportLinks") or {}).get(export_mime) if export_mime else None,
            "rel_dir": rel_dir,
            "rel_path": f"{rel_dir}/{name}" if rel_dir else name,
            "modified_time": it.get("modifiedTime"),
            "created_time": it.get("createdTime"),
            "drive_size": int(it["size"]) if it.get("size") else None,
            "md5": it.get("md5Checksum"),
            "owned_by_me": it.get("ownedByMe"),
            "owner": (it.get("owners") or [{}])[0].get("emailAddress"),
            "web_view_link": it.get("webViewLink"),
            "status": "pending",
            "error": None,
        }

        caps = it.get("capabilities") or {}
        if mime == SHORTCUT_MIME:
            entry["status"] = "skipped-shortcut"
            entry["error"] = f"points at {(it.get('shortcutDetails') or {}).get('targetId')}"
        elif mime in UNEXPORTABLE:
            entry["status"] = "skipped-unexportable"
            entry["error"] = UNEXPORTABLE[mime]
        elif caps.get("canDownload") is False:
            entry["status"] = "skipped-no-download"
            entry["error"] = "owner disabled download/copy for viewers"
        elif rel_dir == "_shared_with_me" and not args.include_shared:
            entry["status"] = "skipped-shared"
            entry["error"] = "owned by someone else; re-run with --include-shared to fetch"
        entries.append(entry)

    for rel in sorted(folder_path.values()):
        if rel:
            os.makedirs(long_path(out_root / rel), exist_ok=True)

    todo = [e for e in entries if e["status"] == "pending"]
    if args.limit:
        todo = todo[: args.limit]
    log(f"{len(entries)} files catalogued; {len(todo)} to download.")

    if not args.dry_run and todo:
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(download_one, e, creds, out_root, args.force): e for e in todo
            }
            for fut in as_completed(futures):
                entry = fut.result()
                done += 1
                mark = {"ok": "OK", "skipped-exists": "--"}.get(entry["status"], "!!")
                log(f"[{done}/{len(todo)}] {mark} {entry['rel_path']}")
                if entry["status"] == "failed":
                    log(f"        {entry['error']}")

    buckets: dict[str, int] = defaultdict(int)
    owners: dict[str, int] = defaultdict(int)
    for e in entries:
        rel = e["rel_dir"]
        if rel == "_shared_with_me":
            bucket = "Shared with me (another owner)"
        elif rel == "_unfiled":
            bucket = "Orphaned - owned, but in no folder"
        elif rel == "":
            bucket = "My Drive (top level)"
        else:
            bucket = f"My Drive/{rel.split('/')[0]}"
        buckets[bucket] += 1
        owners[e["owner"]] += 1

    manifest = {
        "source_account": account,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "root_folder_id": root_id,
        "out_root": str(out_root),
        "dry_run": args.dry_run,
        "counts": {
            k: sum(1 for e in entries if e["status"] == k)
            for k in sorted({e["status"] for e in entries})
        },
        "locations": dict(buckets),
        "owners": dict(owners),
        "files": entries,
    }
    (workdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    with open(workdir / "index.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["rel_path", "name", "mime_type", "status", "drive_size", "modified_time",
             "owner", "web_view_link", "error"]
        )
        for e in entries:
            writer.writerow(
                [e["rel_path"], e["name"], e["mime_type"], e["status"], e["drive_size"],
                 e["modified_time"], e["owner"], e["web_view_link"], e["error"] or ""]
            )

    log("\nSummary:")
    for status, count in sorted(manifest["counts"].items()):
        log(f"  {status:24} {count}")

    log("\nWhere these files live:")
    for bucket, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        log(f"  {bucket:34} {count}")
    log("\nOwned by:")
    for owner, count in sorted(owners.items(), key=lambda kv: -kv[1]):
        log(f"  {(owner or 'unknown'):34} {count}")

    shared = buckets.get("Shared with me (another owner)", 0)
    unfiled = buckets.get("Orphaned - owned, but in no folder", 0)
    if unfiled:
        log(
            f"\nNOTE: {unfiled} files are owned by this account but sit in no folder, which is "
            "why\n      browsing My Drive looks empty. They are exported to export/_unfiled/."
        )
    if shared and not args.include_shared:
        log(
            f"\nNOTE: {shared} files are owned by a different account and were NOT downloaded.\n"
            "      Re-run with --include-shared to fetch them too."
        )

    failed = [e for e in entries if e["status"] == "failed"]
    log(f"\nFiles written under {out_root}")
    log(f"Manifest: {workdir / 'manifest.json'}   Index: {workdir / 'index.csv'}")
    if failed:
        log(f"\n{len(failed)} failures - re-run to retry just those (finished files are skipped).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
