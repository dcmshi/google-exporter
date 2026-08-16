# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Prove the destination matches the source, file by file.

Byte-for-byte hashing only works for files Google stores verbatim. A Google Doc
has no stable bytes at all - exporting the same document twice can differ in
boilerplate - so this uses the strongest check available per file type:

  binary files (PDF, CSV, images, .docx uploaded as-is)
      Drive's own md5Checksum on both sides. A true byte-level 1:1 check,
      and free - no download required.

  native Docs / Sheets / Slides
      export both sides to the same Office format, extract the content
      (document text, cell values, slide text), normalize whitespace, and
      compare SHA-256. Catches any real content difference while ignoring
      export boilerplate.

Works for files this tool imported AND for ones you copied by hand in Drive,
so you can verify a native "Make a copy" the same way as an import.

  uv run verify_migration.py --source-email old@example.com --dest-email new@example.com

The destination side defaults to read-only full-Drive access, which is what
lets it see manually copied files. Add --dest-scope file to reuse the narrow
token from import_drive.py instead; that only sees files this tool created.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import random
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from gauth import (
    DRIVE_FILE,
    DRIVE_READONLY,
    authed_session,
    credentials_for,
    drive_service,
    require_account,
    with_retry,
)

DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"

EXPORT_AS = {
    DOC: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    SHEET: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    SLIDES: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def norm(text: str) -> str:
    return " ".join(text.split())


def _text_nodes(xml: str, container: str, leaf: str) -> str:
    """Join leaf text within each container, so runs split mid-word rejoin cleanly.

    Google splits styled words across several <w:t> runs ("EX" + "PERIENCE"), and
    re-exporting can merge them differently. Concatenating without a separator
    makes the comparison immune to that.
    """
    # The leaf pattern must not also match its siblings: <w:t[^>]*> happily
    # matches <w:tbl>, <w:tc> and <w:tr>, which drags table markup in as text.
    leaf_open = rf"<{leaf}(?:\s[^>]*)?>"
    blocks = re.findall(rf"(?s)<{container}(?:\s[^>]*)?>.*?</{container}>", xml)
    lines = []
    for block in blocks:
        parts = re.findall(rf"(?s){leaf_open}(.*?)</{leaf}>", block)
        if parts:
            lines.append(html.unescape("".join(parts)))
    return "\n".join(lines)


def _sheet_cells(z: zipfile.ZipFile) -> str:
    """Canonical 'sheet!ref=value' list, resolved through the shared-string table.

    Cells are matched allowing the self-closing form: a styled-but-empty cell is
    <c r="A11" s="23"/>, and treating it as an open tag swallows a later cell's
    value. Style and shared-string indexes are deliberately ignored - Google
    renumbers both on every export, which is not a content change.
    """
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        ss = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        for si in re.findall(r"(?s)<si>(.*?)</si>", ss):
            shared.append(html.unescape("".join(re.findall(r"(?s)<t(?:\s[^>]*)?>(.*?)</t>", si))))

    wb = z.read("xl/workbook.xml").decode("utf-8", "replace") if "xl/workbook.xml" in names else ""
    sheet_names = re.findall(r'<sheet[^>]*\bname="([^"]*)"', wb)

    rows = []
    sheet_files = sorted(
        (n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
        key=lambda n: int(re.search(r"(\d+)", n).group(1)),
    )
    for i, n in enumerate(sheet_files):
        sname = html.unescape(sheet_names[i]) if i < len(sheet_names) else n
        x = z.read(n).decode("utf-8", "replace")
        for m in re.finditer(r'(?s)<c\b([^>]*?)(?:/>|>(.*?)</c>)', x):
            attrs, body = m.group(1), m.group(2) or ""
            ref = re.search(r'r="([A-Z]+\d+)"', attrs)
            if not ref:
                continue
            v = re.search(r"(?s)<v>(.*?)</v>", body)
            if v:
                val = html.unescape(v.group(1))
                if 't="s"' in attrs and val.isdigit():
                    idx = int(val)
                    val = shared[idx] if idx < len(shared) else val
            else:
                inline = re.findall(r"(?s)<t(?:\s[^>]*)?>(.*?)</t>", body)
                if not inline:
                    continue
                val = html.unescape("".join(inline))
            rows.append(f"{sname}!{ref.group(1)}={norm(val)}")
    return "\n".join(sorted(rows))


def content_of(mime: str, data: bytes) -> str:
    """Extract comparable content, ignoring formatting and file boilerplate."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if mime == DOC:
            parts = sorted(n for n in names if re.match(r"word/document.*\.xml$", n))
            joined = "\n".join(
                _text_nodes(z.read(n).decode("utf-8", "replace"), "w:p", "w:t") for n in parts
            )
            return norm(joined)
        if mime == SLIDES:
            parts = sorted(
                (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)", n).group(1)),
            )
            joined = "\n".join(
                _text_nodes(z.read(n).decode("utf-8", "replace"), "a:p", "a:t") for n in parts
            )
            return norm(joined)
        if mime == SHEET:
            return _sheet_cells(z)
    return ""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ExportFailed(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:160]}")


def _fetch(session, url: str, tries: int = 5) -> bytes:
    """Follow redirects manually so the bearer token survives the hop.

    Retries network faults and 5xx/429 here rather than leaving it to the outer
    helper, which cannot tell a permanent 403 from a transient one and would
    burn six backoffs on something that will never succeed.
    """
    last = None
    for attempt in range(tries):
        target = url
        try:
            for _ in range(6):
                r = session.get(target, allow_redirects=False, timeout=600)
                if r.status_code in (301, 302, 303, 307, 308) and "location" in r.headers:
                    target = r.headers["location"]
                    continue
                break
            else:
                raise ExportFailed(0, "too many redirects")

            if r.status_code < 400:
                return r.content
            if r.status_code in (429, 500, 502, 503, 504):
                last = ExportFailed(r.status_code, r.text[:300])
            else:
                raise ExportFailed(r.status_code, r.text[:300])
        except (OSError, ConnectionError) as exc:  # includes requests' SSLError
            last = exc
        time.sleep(min(2**attempt, 30) + random.uniform(0, 1))
    raise last or ExportFailed(0, "export failed")


def export_bytes(session, file_id: str, mime: str, export_link: str | None = None) -> bytes:
    """Export via the API host; fall back to exportLinks only when it must.

    The /export endpoint is served by googleapis.com and is the reliable path,
    but it refuses files over 10MB. exportLinks has no size cap yet is served by
    googleusercontent.com, which is noticeably flakier - so it is the fallback,
    not the default.
    """
    rest = (
        f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
        f"?mimeType={quote(mime)}"
    )
    try:
        return _fetch(session, rest)
    except ExportFailed as exc:
        oversized = "exportSizeLimitExceeded" in exc.body
        if export_link and oversized:
            return _fetch(session, export_link)
        raise


def fingerprint(session, service, file_id: str, mime: str) -> tuple[str, str]:
    """('md5'|'text', hash) - md5 is byte-exact, text is content-exact."""
    meta = with_retry(
        lambda: service.files()
        .get(
            fileId=file_id,
            fields="md5Checksum,mimeType,size,exportLinks",
            supportsAllDrives=True,
        )
        .execute(),
        label=file_id,
    )
    if meta.get("md5Checksum"):
        return "md5", meta["md5Checksum"]
    export_mime = EXPORT_AS.get(mime)
    if not export_mime:
        return "skip", ""
    link = (meta.get("exportLinks") or {}).get(export_mime)
    data = with_retry(
        lambda: export_bytes(session, file_id, export_mime, link), label=file_id
    )
    return "text", sha(content_of(mime, data))


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify destination matches source.")
    ap.add_argument("--source-email")
    ap.add_argument("--dest-email")
    ap.add_argument("--client-secret", default="client_secret.json")
    ap.add_argument("--source-profile", default="source")
    ap.add_argument("--dest-profile", default="dest_verify")
    ap.add_argument("--dest-scope", choices=["readonly", "file"], default="readonly")
    ap.add_argument("--only-problems", action="store_true")
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="check only files whose name contains NAME; repeatable. Use to re-check "
        "a few stragglers without re-exporting everything.",
    )
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    workdir = Path(__file__).resolve().parent
    manifest = json.loads((workdir / "manifest.json").read_text(encoding="utf-8"))
    state_path = workdir / "import_map.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    src_creds = credentials_for(args.source_profile, DRIVE_READONLY, args.client_secret, workdir)
    src_svc = drive_service(src_creds)
    src_session = authed_session(src_creds)
    src_account = require_account(src_svc, args.source_email)

    dest_scopes = DRIVE_READONLY if args.dest_scope == "readonly" else DRIVE_FILE
    dst_creds = credentials_for(args.dest_profile, dest_scopes, args.client_secret, workdir)
    dst_svc = drive_service(dst_creds)
    dst_session = authed_session(dst_creds)
    dst_account = require_account(dst_svc, args.dest_email)

    print(f"source: {src_account}\ndest:   {dst_account}\n")

    entries = [e for e in manifest["files"] if e["status"] in ("ok", "skipped-exists")]
    if args.only:
        wanted = [w.lower() for w in args.only]
        entries = [e for e in entries if any(w in e["name"].lower() for w in wanted)]
        if not entries:
            sys.exit(f"Nothing in the manifest matches {args.only}")
    if args.limit:
        entries = entries[: args.limit]

    def find_in_dest(entry) -> tuple[str | None, str]:
        fid = state.get(f"file::{entry['rel_path']}")
        if fid:
            meta = with_retry(
                lambda: dst_svc.files().get(fileId=fid, fields="id,trashed").execute(),
                label=entry["name"],
            )
            if not meta.get("trashed"):
                return fid, "import"
            found_trashed = True
        else:
            found_trashed = False

        if args.dest_scope != "readonly":
            return None, "trashed" if found_trashed else "missing"

        # Manual copies aren't in import_map, and Drive's "Make a copy" prefixes
        # the name with "Copy of" unless you rename it.
        base = entry["name"].rsplit(".", 1)[0].replace("\\", "\\\\").replace("'", "\\'")
        # "'me' in owners" matters: with read-only Drive access the SOURCE file
        # also shows up here as shared-with-me, and matching it would compare the
        # source against itself and report a meaningless pass.
        mine = "and 'me' in owners and trashed = false"
        queries = [
            (f"name = '{base}' {mine}", "by-name"),
            (f"name = 'Copy of {base}' {mine}", "copy-of"),
            (f"name contains '{base}' {mine}", "fuzzy"),
        ]
        for q, via_label in queries:
            try:
                res = with_retry(
                    lambda: dst_svc.files()
                    .list(q=q, fields="files(id,name)", pageSize=10)
                    .execute(),
                    label=base,
                )
            except Exception:  # noqa: BLE001 - a malformed query shouldn't end the run
                continue
            hits = res.get("files", [])
            if len(hits) == 1:
                return hits[0]["id"], via_label
            if len(hits) > 1:
                return hits[0]["id"], f"{via_label}?"
        return None, "trashed" if found_trashed else "missing"

    counts = {}
    print(f"{'verdict':<10} {'via':<10} {'kind':<5}  file")
    print("-" * 78)
    for entry in entries:
        name = entry["name"][:40]
        dest_id, via = find_in_dest(entry)
        if not dest_id:
            verdict = via.upper()
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict != "MATCH":
                print(f"{verdict:<10} {'-':<10} {'-':<5}  {name}")
            continue

        try:
            skind, shash = fingerprint(src_session, src_svc, entry["id"], entry["mime_type"])
            dmeta = with_retry(
                lambda: dst_svc.files().get(fileId=dest_id, fields="mimeType").execute(),
                label=name,
            )
            dkind, dhash = fingerprint(dst_session, dst_svc, dest_id, dmeta["mimeType"])
        except Exception as exc:  # noqa: BLE001
            counts["ERROR"] = counts.get("ERROR", 0) + 1
            print(f"{'ERROR':<10} {via:<10} {'-':<5}  {name}  {type(exc).__name__}: {exc}"[:110])
            continue

        if skind == "skip" or dkind == "skip":
            verdict = "SKIP"
        elif skind != dkind:
            verdict = "KIND-DIFF"
        elif shash == dhash:
            verdict = "MATCH"
        else:
            verdict = "DIFFERS"
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict != "MATCH" or not args.only_problems:
            print(f"{verdict:<10} {via:<10} {skind:<5}  {name}")

    print("\nsummary")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<12} {v}")
    problems = sum(v for k, v in counts.items() if k not in ("MATCH", "SKIP"))
    print(
        f"\n{counts.get('MATCH', 0)} verified identical to source."
        + (f"  {problems} need attention." if problems else "")
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
