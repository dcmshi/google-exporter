"""Shared OAuth + retry helpers for the Drive export/import scripts.

One OAuth client (client_secret.json) is reused for both accounts; tokens are
kept in separate token_<profile>.json files so the two logins never collide.
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Drive names routinely contain en-dashes, emoji and CJK. Without this, printing
# a filename to a cp1252 console or a redirected stream kills the whole run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

DRIVE_READONLY = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_FILE = ["https://www.googleapis.com/auth/drive.file"]
DRIVE_FULL = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
_RETRY_REASONS = (
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backendError",
    "internalError",
    "sharingRateLimitExceeded",
)


def check_client_type(secret: Path) -> None:
    """A 'web' client cannot use the loopback flow, and fails cryptically if tried."""
    try:
        config = json.loads(secret.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"{secret} is not readable JSON: {exc}")
    if "installed" not in config:
        found = ", ".join(config) or "nothing"
        sys.exit(
            f"{secret} is not a Desktop app OAuth client (found: {found}).\n"
            "In Google Cloud Console create a client with application type "
            "'Desktop app' and download that one instead. See README.md."
        )


def credentials_for(profile: str, scopes: list[str], client_secret: str, workdir: Path):
    """Load cached credentials for `profile`, running the browser flow if needed."""
    token_path = workdir / f"token_{profile}.json"
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except ValueError:
            creds = None

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        secret = Path(client_secret)
        if not secret.is_absolute():
            secret = workdir / secret
        if not secret.exists():
            sys.exit(
                f"Missing {secret}. Create a Desktop-app OAuth client in Google Cloud "
                "Console and save the downloaded JSON there (see README.md)."
            )
        check_client_type(secret)
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes)
        creds = flow.run_local_server(
            port=0,
            prompt="select_account consent",
            authorization_prompt_message=(
                f"\n>>> Sign in as the {profile.upper()} account, then return here.\n"
                ">>> If the browser does not open, visit:\n{url}\n"
            ),
            success_message="Authorized. You can close this tab and return to the terminal.",
        )
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def drive_service(creds):
    return build("drive", "v3", credentials=creds, cache_discovery=False, static_discovery=True)


def authed_session(creds) -> AuthorizedSession:
    return AuthorizedSession(creds)


def whoami(service) -> str:
    about = service.about().get(fields="user(emailAddress,displayName)").execute()
    return about["user"]["emailAddress"]


def require_account(service, expected: str | None) -> str:
    email = whoami(service)
    if expected and email.lower() != expected.lower():
        sys.exit(
            f"Signed in as {email} but --expect-email said {expected}.\n"
            "Delete the matching token_*.json and re-run to pick the other account."
        )
    return email


def with_retry(fn, *, tries: int = 7, label: str = ""):
    """Run `fn`, backing off on Drive's rate-limit and transient server errors."""
    for attempt in range(tries):
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            body = exc.content.decode("utf-8", "replace") if isinstance(exc.content, bytes) else str(exc.content)
            retryable = status in _RETRY_STATUS or (
                status == 403 and any(r in body for r in _RETRY_REASONS)
            )
            if not retryable or attempt == tries - 1:
                raise
        except (ConnectionError, TimeoutError, OSError):
            if attempt == tries - 1:
                raise
        delay = min(2**attempt, 64) + random.uniform(0, 1.0)
        if label:
            print(f"    retry {attempt + 1}/{tries - 1} in {delay:.1f}s ({label})", flush=True)
        time.sleep(delay)
