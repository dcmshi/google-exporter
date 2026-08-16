# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Run export_drive.main() end-to-end against a stubbed Drive API (no network)."""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import export_drive as ed

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
FORM = "application/vnd.google-apps.form"
PDF = "application/pdf"

ADMIN = "admin@example.com"
OTHER = "someone-else@example.com"

FILES = [
    {"id": "f1", "name": "Invoices", "mimeType": FOLDER, "parents": ["ROOT"],
     "ownedByMe": True, "owners": [{"emailAddress": ADMIN}]},
    # Orphaned-but-owned: the symptom of "Drive looks empty".
    {"id": "d1", "name": "2025 Budget", "mimeType": SHEET, "parents": [],
     "ownedByMe": True, "owners": [{"emailAddress": ADMIN}],
     "capabilities": {"canDownload": True}},
    {"id": "d2", "name": "Vendor Notes", "mimeType": DOC, "parents": [],
     "ownedByMe": True, "owners": [{"emailAddress": ADMIN}],
     "capabilities": {"canDownload": True}},
    # Filed normally.
    {"id": "d3", "name": "March", "mimeType": SHEET, "parents": ["f1"],
     "ownedByMe": True, "owners": [{"emailAddress": ADMIN}],
     "capabilities": {"canDownload": True}},
    # Owned by someone else -> shared bucket.
    {"id": "d4", "name": "Shared Plan", "mimeType": DOC, "parents": ["ZZZ"],
     "ownedByMe": False, "owners": [{"emailAddress": OTHER}],
     "capabilities": {"canDownload": True}},
    # Download blocked by owner.
    {"id": "d5", "name": "Locked", "mimeType": DOC, "parents": ["ZZZ"],
     "ownedByMe": False, "owners": [{"emailAddress": OTHER}],
     "capabilities": {"canDownload": False}},
    # No export API.
    {"id": "d6", "name": "Intake Form", "mimeType": FORM, "parents": ["f1"],
     "ownedByMe": True, "owners": [{"emailAddress": ADMIN}],
     "capabilities": {"canDownload": True}},
    {"id": "d7", "name": "scan.pdf", "mimeType": PDF, "parents": ["f1"], "size": "2048",
     "ownedByMe": True, "owners": [{"emailAddress": ADMIN}],
     "capabilities": {"canDownload": True}},
]


class Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self, **kw):
        return self.payload


class Files:
    def get(self, **kw):
        return Exec({"id": "ROOT"})

    def list(self, **kw):
        return Exec({"files": FILES})


class About:
    def get(self, **kw):
        return Exec({"user": {"emailAddress": ADMIN, "displayName": "Admin"}})


class Service:
    def files(self):
        return Files()

    def about(self):
        return About()


ed.credentials_for = lambda *a, **k: object()
ed.drive_service = lambda creds: Service()

workdir = Path(__file__).resolve().parent
saved = {}
for name in ("manifest.json", "index.csv"):
    p = workdir / name
    if p.exists():
        saved[name] = p.read_bytes()

tmp = Path(tempfile.mkdtemp(prefix="ge-smoke-"))
sys.argv = ["export_drive.py", "--dry-run", "--out", str(tmp),
            "--expect-email", ADMIN]

try:
    rc = ed.main()
    manifest = json.loads((workdir / "manifest.json").read_text(encoding="utf-8"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
    for name in ("manifest.json", "index.csv"):
        p = workdir / name
        if name in saved:
            p.write_bytes(saved[name])
        elif p.exists():
            p.unlink()

by_id = {e["id"]: e for e in manifest["files"]}
checks = [
    ("exit code clean", rc == 0),
    ("orphaned owned sheet routed to _unfiled with .xlsx",
     by_id["d1"]["rel_path"] == "_unfiled/2025 Budget.xlsx"),
    ("orphaned owned doc routed to _unfiled with .docx",
     by_id["d2"]["rel_path"] == "_unfiled/Vendor Notes.docx"),
    ("filed sheet keeps its folder", by_id["d3"]["rel_path"] == "Invoices/March.xlsx"),
    ("shared file skipped by default", by_id["d4"]["status"] == "skipped-shared"),
    ("download-blocked file flagged", by_id["d5"]["status"] == "skipped-no-download"),
    ("form flagged unexportable", by_id["d6"]["status"] == "skipped-unexportable"),
    ("pdf keeps original name, no conversion",
     by_id["d7"]["rel_path"] == "Invoices/scan.pdf" and by_id["d7"]["export_mime"] is None),
    ("orphan bucket counted", manifest["locations"].get("Orphaned - owned, but in no folder") == 2),
    ("shared bucket counted", manifest["locations"].get("Shared with me (another owner)") == 2),
    ("owner tally splits the two accounts",
     manifest["owners"].get(ADMIN) == 5 and manifest["owners"].get(OTHER) == 2),
    ("downloadable files marked pending in dry run",
     sum(1 for e in manifest["files"] if e["status"] == "pending") == 4),
]

failed = 0
print("\n--- assertions ---")
for label, ok in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    failed += not ok
print(f"\n{len(checks) - failed}/{len(checks)} passed")
raise SystemExit(1 if failed else 0)
