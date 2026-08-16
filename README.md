# google-exporter

Move an entire Google Drive from one account to another, with a verified local
copy in the middle.

Built for a specific mess: you sign up for Google Workspace, later cancel it, and
Google hands all your documents to some admin account you barely use. You can
still open the files, but they aren't in the account you actually live in — and
Workspace's own transfer tools are gone with the subscription.

It works for any account-to-account move, including personal Gmail to personal
Gmail. Nothing is hardcoded to a particular address or domain.

- **`check_auth.py`** — two-second connection test. Confirms OAuth works and
  shows what the account can actually see.
- **`export_drive.py`** — walks the whole Drive, mirrors the folder tree to
  disk, exports Docs/Sheets/Slides to `.docx`/`.xlsx`/`.pptx`, and writes
  `manifest.json` plus a spreadsheet-friendly `index.csv`.
- **`import_drive.py`** — reads the manifest, rebuilds the tree under one new
  folder in the destination, converting the Office files back to native Google
  formats.
- **`auth_paste.py`** — fallback sign-in for when the normal browser flow hangs.
- **`verify_migration.py`** — proves the destination matches the source, file by
  file, using md5 for binaries and content hashing for native Docs/Sheets/Slides.
- **`export_tabs.py`** — audits which Docs use tabs, and can export each tab as
  its own file. Needs the Docs API enabled.
- **`clear_flattened.py`** — lists, and optionally trashes, the flattened copies
  of tabbed Docs so you can replace them with native copies.

Both main scripts are resumable and idempotent. Re-running skips whatever
already finished, so a partial failure just needs the same command again.

Throughout this README, `source@example.com` is the account that currently holds
the files and `dest@example.com` is the account you want them in.

## Requirements

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** — recommended. Every script carries
  [PEP 723](https://peps.python.org/pep-0723/) inline dependencies, so
  `uv run <script>.py` installs what it needs automatically. No virtualenv, no
  `requirements.txt`.

Without uv, install the dependencies yourself and use `python` instead:

```
pip install "google-api-python-client>=2.120" "google-auth-oauthlib>=1.2" "requests>=2.31"
```

## Setup: create a Google Cloud project and OAuth client

The Drive API needs an OAuth client. This is the only fiddly part, and it is a
one-time cost of about five minutes.

Two things that trip people up before you start:

- **This is `console.cloud.google.com`, not `admin.google.com`.** The Workspace
  admin console does not issue OAuth clients for scripts. (There *is* a
  service-account flow that uses the admin console via domain-wide delegation,
  but it requires an active Workspace subscription with super-admin rights —
  which is exactly what you no longer have if you cancelled.)
- **Create the project in whichever account is healthiest**, normally the
  destination. A cancelled or suspended Workspace account may refuse to create
  Cloud projects. This does not matter: an OAuth client is *not* bound to the
  account that owns it. A client created under one account can authorize sign-in
  as a completely different account, so long as that address is on the test-user
  list.

### 1. Create the project

<https://console.cloud.google.com/projectcreate> — name it anything, e.g.
`drive-migration`. Confirm the project picker at the top shows your new project
for every step that follows.

### 2. Enable the Drive API

<https://console.cloud.google.com/apis/library/drive.googleapis.com> → **Enable**.

Skipping this is the most common cause of a `403` on the first real run.

### 3. Configure the consent screen

<https://console.cloud.google.com/auth/overview>

Google renamed this area to **Google Auth Platform**, so if you are hunting for
"OAuth consent screen" under APIs & Services, that is where it moved.

**Audience: choose External.** Internal only exists for projects owned by a
Google Workspace organization and restricts sign-in to members of that same org
— which would lock out the very account you need. Under a personal account
Internal is normally greyed out entirely. If it *is* selectable, your project
got created under an organization by accident; back out and recreate it.

**Branding is mandatory, but only three fields are:**

| Field | Value |
|---|---|
| App name | Anything, e.g. `drive-migration`. This is what you'll see on the consent screen, so make it recognizable. |
| User support email | Your own address, from the dropdown. |
| Developer contact information | Your own address. |

Leave the logo, app home page, privacy policy, terms of service, and authorized
domains blank. Those only become required if you submit for verification, which
you are not doing.

### 4. Add test users — both accounts

<https://console.cloud.google.com/auth/audience> → **Test users** → **Add users**

Add **both** the source and destination addresses. This is the step that lets a
client owned by one account authorize sign-in as the other. Missing the second
address is what produces "Access blocked" later.

### 5. Create and download the client

<https://console.cloud.google.com/auth/clients> → **Create client** →
Application type **Desktop app** → **Create**, then use the **download JSON**
icon on the row.

In the older console UI this lives at **APIs & Services → Credentials → Create
credentials → OAuth client ID**.

It must be a **Desktop app** client. The scripts check, and reject a `web` type
with a clear message. Desktop clients are allowed to use any `http://localhost`
port as a redirect, so there is nothing to register.

### 6. Install the file

Save it as `client_secret.json` next to the scripts. The downloaded name is
long and generated, so:

```powershell
# PowerShell
Get-ChildItem "$env:USERPROFILE\Downloads\client_secret*.json" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
  Copy-Item -Destination .\client_secret.json
```

```bash
# bash / Git Bash / macOS / Linux
cp "$(ls -t ~/Downloads/client_secret*.json | head -1)" ./client_secret.json
```

### Leave the app in "Testing"

Do **not** click **Publish app**. `drive.readonly` is a restricted scope, so
publishing kicks off Google's verification review and blocks you for days.
Testing mode with both addresses as test users works immediately.

The only cost: **refresh tokens for unverified apps expire after 7 days.** Do
the migration within a week, or just sign in again.

You will also see an "app isn't verified" interstitial at sign-in. Click
**Advanced → Go to \<app name\> (unsafe)**. It is your own client ID.

### There is no .env and no API key

Drive uses OAuth, not API keys, so nothing gets typed into a config file. The
only credential you place by hand is `client_secret.json`. Sign-in happens in
the browser, and `token_source.json` / `token_dest.json` are written for you.
All of those are in `.gitignore` — never commit or share them.

### Scopes requested

| Profile | Scope | Why |
|---|---|---|
| `source` | `drive.readonly` | Read-only. The exporter can never modify the account it is rescuing. |
| `dest` | `drive.file` | Per-file access limited to what this tool creates. It cannot see or touch anything already in the destination. |

`import_drive.py --full-scope` upgrades the destination to full `drive` access,
which you should only need if you hit a permissions error.

## Usage

### Step 0 — test the connection

```
uv run check_auth.py --expect-email source@example.com
```

Confirms the OAuth client works and prints which account you actually signed in
as, storage used, and the 10 most recently modified files with their owners —
enough to tell at a glance whether the documents are yours or someone else's.

`--expect-email` aborts if the wrong account authorizes, so a misclick cannot
silently point the tool at the wrong Drive. Use it on every command; you will be
signing into two accounts minutes apart.

Add `--profile dest --expect-email dest@example.com` to test the other account.
That profile uses the narrow `drive.file` scope, so an **empty file listing
there is the correct result**.

### Step 1 — inventory, no downloads

```
uv run export_drive.py --expect-email source@example.com --dry-run
```

Catalogues everything and writes `manifest.json` and `index.csv` without
transferring a byte. Open `index.csv` to see the full picture.

### Step 2 — download

```
uv run export_drive.py --expect-email source@example.com
```

Files land under `./export/`, mirroring the Drive folder tree.

| Flag | Effect |
|---|---|
| `--include-shared` | Also fetch files owned by other people ("Shared with me"). |
| `--all-drives` | Include shared drives. |
| `--workers N` | Parallel downloads, default 4. Drop to 2 if you hit rate limits. |
| `--limit N` | Stop after N files, for a smoke test. |
| `--force` | Re-download files already on disk. |
| `--out DIR` | Output directory, default `./export`. |

### Step 3 — restore into the other account

Smoke-test with a handful first, then check the result in Drive:

```
uv run import_drive.py --expect-email dest@example.com --limit 5
```

```
uv run import_drive.py --expect-email dest@example.com
```

Everything lands in one new folder named
`Restored from source@example.com (<date>)`. Override with
`--folder-name "My Docs"`, skip format conversion with `--no-convert`, or plan
without uploading via `--dry-run`.

### Step 4 — verify the destination matches the source

```
uv run verify_migration.py --source-email source@example.com --dest-email dest@example.com
```

Checks every file end to end and prints `MATCH`, `DIFFERS`, `MISSING` or
`TRASHED` per file. Add `--only-problems` to hide the matches.

A plain byte hash cannot work here, because Google files have no stable bytes —
exporting the same untouched document twice can differ in boilerplate, style
numbering and shared-string ordering. So the check adapts to the file type:

| File type | Check |
|---|---|
| Binary (PDF, CSV, images, `.docx` uploaded as-is) | Drive's own `md5Checksum` on both sides. Byte-exact, and free — no download needed. |
| Google Docs / Sheets / Slides | Export both sides to the same Office format, extract the content, normalize whitespace, compare SHA-256. |

Content extraction deliberately ignores three things Google changes on every
export, none of which are content: style-table numbering, shared-string index
order, and where a styled word is split across formatting runs. Comparing raw
XML instead produces false alarms on all three — `EX PERIENCE` versus
`EXPERIENCE` is the same word, and `w:color="auto"` versus `w:color="000000"`
is the same black.

Use `--only NAME` (repeatable) to re-check a few files without re-exporting
everything — a full run exports every file from *both* accounts and takes
minutes.

Two `DIFFERS` results are expected rather than alarming:

- **A natively copied doc whose tabs had default names.** Tab titles are written
  into the export as headings, and Google auto-titles untitled tabs from their
  content when copying — so `Tab 1` in the original becomes something like
  `chat with CEO David Jeong` in the copy. The body text is identical; only the
  heading differs.
- **A file you edited after migrating.** The check compares against the source
  as it is now, not as it was at export time.

It also verifies files you copied by hand. The destination side defaults to
read-only full-Drive access so it can find them; that is a separate consent
screen from the import token. Pass `--dest-scope file` to reuse the narrow
`import_drive.py` token instead, which only sees files this tool created.

## "The files show up in Docs/Sheets but Drive looks empty"

This is the symptom that motivated the tool, and the dry run diagnoses it.

`docs.google.com` lists every document the account can *open*.
`drive.google.com` only shows what is filed in My Drive. After a Workspace
ownership transfer the files usually land in one of three states, and the run
prints a **"Where these files live"** breakdown naming which:

- **A transfer folder** — Workspace often files everything into a folder named
  `<departing-user>@domain <timestamp>`. It is in My Drive; you may just have
  missed it.
- **`Orphaned - owned, but in no folder`** — the account owns them but they have
  no parent, so browsing My Drive shows nothing. The API still finds them and
  they export normally, into `export/_unfiled/`. In Drive's web UI you can see
  this set yourself by searching `is:unorganized owner:me`.
- **`Shared with me (another owner)`** — still owned by a different account and
  only shared with this one. Catalogued but **not downloaded by default**;
  re-run with `--include-shared`. Read access is enough to export, and the
  import makes the destination account the owner of the new copies.

The **"Owned by"** breakdown in the same output confirms which case you are in.

## Status values in `index.csv`

| Status | Meaning |
|---|---|
| `ok` | Downloaded. |
| `skipped-exists` | Already on disk from an earlier run. |
| `skipped-shared` | Owned by another account; re-run with `--include-shared`. |
| `skipped-shortcut` | A Drive shortcut, not a real file. Its target is exported separately if you have access to it. |
| `skipped-unexportable` | Forms/Sites/My Maps — no export API. Open the `web_view_link` and handle by hand. |
| `skipped-no-download` | The owner disabled download/copy for viewers. |
| `failed` | Transient error; the `error` column has details. Re-run to retry. |

## What does not survive the round trip

| Thing | What happens |
|---|---|
| Version history | Lost. Files arrive as a single new revision. |
| **Google Docs tabs** | **Flattened.** All tab content survives, concatenated into one linear document in tab order, but the tab structure is gone. See below. |
| Comments | Preserved for Docs/Sheets/Slides via the Office formats; resolved threads may flatten. |
| Sharing permissions | Not copied. Re-share manually. |
| Google Forms, Sites, My Maps | No export API exists. Listed in `index.csv`; handle by hand. |
| Google Drawings | Exported to `.svg`, uploaded as an SVG file, not a Drawing. |
| Apps Script projects | Exported to `.json`, uploaded as a JSON file. |
| Shortcuts | Skipped. If a shortcut pointed at someone else's file and you have lost access, that file is simply gone — it was never yours to export. |

If version history matters more than a local copy, the higher-fidelity path for
the transfer half is to share the files from the source account to the
destination, then use **Make a copy** in Drive. That keeps native format
throughout but leaves you with no local backup, so running the export anyway is
worthwhile.

### Google Docs tabs are flattened

Google Docs [tabs](https://support.google.com/docs/answer/15499791) have no
equivalent in `.docx`, so a seven-tab document exports as one continuous file
with the seven sections stacked in order. Re-imported, it is a single doc where
each former tab reads as a big heading.

**No text is lost.** This was worth measuring rather than assuming: exporting
each tab individually via the Docs API and summing the results gives essentially
the same character count as the single flattened export (for one 7-tab document,
5,058 characters across tabs versus 5,161 in the flat file — the difference is
per-file boilerplate, not content). Every format Drive offers behaves the same
way; `.docx`, `.odt`, `.html`, `.rtf`, `.epub` and `.txt` all produce the
identical flattened text.

To find out whether this affects you:

```
uv run export_tabs.py --expect-email source@example.com
```

Read-only. It prints every Doc with more than one tab, and the tab titles.

**There is no automated way to rebuild the tabs.** The Docs API does expose an
`addDocumentTab` request, and `EndOfSegmentLocation` accepts a `tabId`, so tabs
*can* be created programmatically. But populating them means reconstructing
every paragraph, style, table and inline image as a `batchUpdate` request, and
anything not explicitly handled vanishes silently. That trades a known,
harmless flattening for unpredictable fidelity loss.

**Use a native copy instead.** For the affected documents only:

1. `uv run clear_flattened.py --expect-email dest@example.com` to list them, then
   add `--trash` to remove the flattened versions (recoverable for 30 days).
2. In the source Drive, share those documents with the destination account.
3. In the destination Drive, open **Shared with me**, select them, and choose
   **Make a copy**.

Copies made this way are owned by the destination account and keep tabs,
formatting, images, tables and comments intact, because Google never converts
the file. This is normally a handful of documents, so it is quicker by hand than
any script — and scripting it would need full `drive` write scope on both
accounts.

If you would rather have each tab as a separate document,
`uv run export_tabs.py --expect-email source@example.com --apply` writes one
`.docx` per tab and a `manifest_tabs.json`, which the normal importer accepts:

```
uv run import_drive.py --manifest manifest_tabs.json --expect-email dest@example.com
```

### Sheets with multiple worksheets are fine

`.xlsx` supports multiple worksheets natively, so multi-tab spreadsheets survive
intact — worksheet names, order and cell contents all round-trip.

One cosmetic difference: Google drops empty-but-formatted cells on the way back
in, so a workbook can report several hundred fewer cells after the round trip.
That is styling on blank cells, not data. Verified on two workbooks that each
showed ~780 fewer cell elements: cells actually containing a value were
identical (52 → 52 and 36 → 36), and every distinct text string was present.

## Files this creates

| File | Purpose |
|---|---|
| `client_secret.json` | Your OAuth client. **Secret.** |
| `token_source.json` / `token_dest.json` | Cached logins, one per account. Delete either to force re-authentication. |
| `manifest.json` | Full record of every file: Drive id, path, mime type, status. Input to the importer. |
| `index.csv` | Same data, spreadsheet-friendly. |
| `import_map.json` | Which local paths already uploaded, so re-runs don't duplicate. |
| `export/` | The downloaded files. |
| `manifest_tabs.json` / `export_tabs/` | Per-tab exports, only if you ran `export_tabs.py --apply`. |

Everything except the scripts is gitignored. `manifest.json`, `index.csv` and
`import_map.json` list your private file names and Drive IDs — treat them as
personal data even though they contain no file contents.

## Troubleshooting

**"Signed in as X but --expect-email said Y"** — delete the matching
`token_source.json` or `token_dest.json` and re-run to get the account picker.

**The browser hangs, or redirects to a blank page.** The sign-in flow spins up a
temporary local web server and waits for the browser to hand back an
authorization code. A blank page means that handoff never happened. Causes, in
order of likelihood:

1. *The script was not still running.* The listener dies with the process. Start
   the command and leave it alone until the terminal prints `Signed in as …`.
2. *HTTPS-Only Mode.* If the browser silently upgrades `http://localhost:PORT`
   to `https://`, the plain-HTTP local server cannot answer. Check
   `chrome://settings/security` for "Always use secure connections", or use a
   different browser.
3. *The consent checkbox.* The `drive.file` scope shows a permissions screen
   with a checkbox that is **not ticked by default**. Skipping it aborts the
   grant.

The terminal is the source of truth, not the browser tab. If it prints
`Signed in as …`, it worked regardless of what the page looked like.

If it keeps failing, use the fallback, which never needs the local server to be
reachable:

```
uv run auth_paste.py --profile dest
# open the printed URL, complete sign-in, copy the whole localhost URL
# from the address bar even though the page is blank, then:
uv run auth_paste.py --profile dest --url "http://localhost:8765/?state=...&code=..."
```

Keep the quotes — the URL contains `&`. Then re-run the real command; it reuses
the saved token with no browser step.

**"Access blocked: … has not completed verification"** — the account you are
signing in as is not on the test-user list. Add it at
<https://console.cloud.google.com/auth/audience>.

**`403 insufficientPermissions` on import** — delete `token_dest.json` and re-run
with `--full-scope`.

**Rate limits** — handled automatically with exponential backoff. If it still
crawls, drop `--workers` to 2. The Drive API is free; there is no per-request
billing, and no billing account is needed to enable it.

**`storageQuotaExceeded` on import** — the destination is out of space. Personal
accounts get 15 GB shared across Gmail, Drive and Photos, and since June 2021
native Google files count against that quota. `check_auth.py` prints usage for
both accounts so you can compare before starting.

**`uv run import_drive.py` says "program not found"** — you are not in the
project directory. The scripts resolve credentials, tokens and the manifest
relative to their own location, so an absolute path works from anywhere:
`uv run /path/to/import_drive.py …`. In Git Bash use forward slashes;
`cd C:\path` silently fails there because `\` is an escape character.

**A few files fail** — the run continues and records them. Run the same command
again; completed files are skipped.

## License

MIT — see [LICENSE](LICENSE).

## Development

```
uv run selftest.py    # path sanitizing, folder tree, dedup, cycles - no network
uv run smoketest.py   # export_drive.main() end-to-end against a stubbed API
```

Both run fully offline. `selftest.py` covers Windows filename hazards (reserved
device names, illegal characters, long paths, duplicate names) and folder-tree
edge cases including parent cycles and orphans. `smoketest.py` stubs the Drive
API and asserts on the resulting manifest, so it exercises routing, status
assignment and the location/owner tallies without touching a real account.
