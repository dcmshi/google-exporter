# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-api-python-client>=2.120",
#   "google-auth-oauthlib>=1.2",
#   "requests>=2.31",
# ]
# ///
"""Offline checks for the path/tree logic and the Drive discovery doc."""

from export_drive import EXPORT_MAP, build_tree, sanitize, unique_in

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
ROOT = "ROOT"

items = [
    {"id": "f1", "name": "Finance", "mimeType": FOLDER, "parents": [ROOT]},
    {"id": "f2", "name": "Finance", "mimeType": FOLDER, "parents": [ROOT]},   # dup name
    {"id": "f3", "name": "Q1/Q2: notes", "mimeType": FOLDER, "parents": ["f1"]},
    {"id": "f4", "name": "CON", "mimeType": FOLDER, "parents": ["f1"]},       # reserved
    {"id": "cyc1", "name": "Loop", "mimeType": FOLDER, "parents": ["cyc2"]},
    {"id": "cyc2", "name": "Loop2", "mimeType": FOLDER, "parents": ["cyc1"]},
    {"id": "d1", "name": "Budget", "mimeType": DOC, "parents": ["f3"], "ownedByMe": True},
    {"id": "d2", "name": "Budget", "mimeType": DOC, "parents": ["f3"], "ownedByMe": True},
    {"id": "d3", "name": "Orphan", "mimeType": DOC, "parents": [], "ownedByMe": True},
    {"id": "d4", "name": "Someone else", "mimeType": DOC, "parents": ["ZZZ"], "ownedByMe": False},
    {"id": "d5", "name": "Top level", "mimeType": DOC, "parents": [ROOT], "ownedByMe": True},
]

folder_path, file_dir = build_tree(items, ROOT)

checks = [
    ("two same-named folders disambiguated",
     sorted([folder_path["f1"], folder_path["f2"]]) == ["Finance", "Finance (2)"]),
    ("slash and colon stripped from folder name",
     folder_path["f3"] == "Finance/Q1_Q2_ notes"),
    ("reserved device name escaped", folder_path["f4"] == "Finance/_CON"),
    ("folder cycle did not hang or get placed",
     "cyc1" not in folder_path and "cyc2" not in folder_path),
    ("nested file gets its folder path", file_dir["d1"] == "Finance/Q1_Q2_ notes"),
    ("orphan owned file goes to _unfiled", file_dir["d3"] == "_unfiled"),
    ("unreachable parent, not owned -> _shared_with_me",
     file_dir["d4"] == "_shared_with_me"),
    ("root-level file has empty dir", file_dir["d5"] == ""),
]

taken = set()
names = [unique_in(taken, "Budget.docx") for _ in range(3)]
checks.append(("duplicate filenames get (2)/(3)",
               names == ["Budget.docx", "Budget (2).docx", "Budget (3).docx"]))
checks.append(("long name truncated but keeps extension",
               sanitize("x" * 300 + ".docx").endswith(".docx")
               and len(sanitize("x" * 300 + ".docx")) <= 110))
checks.append(("trailing dot/space stripped", sanitize("report. ") == "report"))
checks.append(("docs/sheets/slides all have export targets",
               all(m in EXPORT_MAP for m in (
                   DOC,
                   "application/vnd.google-apps.spreadsheet",
                   "application/vnd.google-apps.presentation"))))

# The bundled discovery doc must load without network, since every worker
# thread builds its own service object.
from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build

svc = build("drive", "v3", credentials=AnonymousCredentials(),
            cache_discovery=False, static_discovery=True)
checks.append(("offline discovery doc builds a drive client", hasattr(svc, "files")))

failed = 0
for label, ok in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    failed += not ok

print(f"\n{len(checks) - failed}/{len(checks)} passed")
raise SystemExit(1 if failed else 0)
