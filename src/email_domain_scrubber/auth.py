"""Google credentials for the Sheets and Drive APIs.

Credentials come from an **rclone** Google Drive remote, named by
`EMAIL_DOMAIN_RCLONE_REMOTE`. rclone already holds a Drive OAuth client and refresh token, and
rclone's full `drive` scope covers the Sheets API too (Sheets v4 accepts `auth/drive` for both
reads and writes). The config is only ever read; refreshed access tokens are kept in memory, not
written back.

The server acts as the signed-in user, so it can only reach workbooks that user can already open.

The remote's `scope` is checked up front, but only against what rclone recorded at `rclone
config` time; if the grant itself is narrower, `sheets._execute` translates the resulting 403
into the same actionable message.
"""

from __future__ import annotations

import configparser
import functools
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import RcloneConfigError, ScrubberError

DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive'

#: Name of an rclone remote of `type = drive` to borrow credentials from.
RCLONE_REMOTE_ENV = 'EMAIL_DOMAIN_RCLONE_REMOTE'

_TOKEN_URI = 'https://oauth2.googleapis.com/token'


def credentials() -> Any:
    """Credentials from the rclone remote named in the environment."""
    return rclone_credentials(configured_remote())


def configured_remote() -> str:
    """The rclone remote to borrow credentials from, or raise saying how to name one."""
    remote = os.environ.get(RCLONE_REMOTE_ENV, '').strip()
    if not remote:
        raise ScrubberError(
            f'No Google credentials configured. Set {RCLONE_REMOTE_ENV} to the name of an rclone '
            'remote with `type = drive` and `scope = drive`, for example:\n'
            f'  export {RCLONE_REMOTE_ENV}=aso'
        )
    return remote


def login_hint() -> str:
    """The command a human runs to sign in again. Opens a browser.

    Error messages ask for this rather than spelling out the command, so the remote name in it is
    always the configured one.
    """
    remote = os.environ.get(RCLONE_REMOTE_ENV, '').strip()
    if not remote:
        return f'set {RCLONE_REMOTE_ENV} to an rclone Google Drive remote, then `rclone config`'
    return f'rclone config reconnect {remote}:'


def credential_source() -> str:
    """Human-readable description of where credentials are coming from."""
    return f'rclone remote {configured_remote()!r} in {rclone_config_path()}'


def rclone_config_path() -> Path:
    """Where rclone keeps its config, honouring rclone's own `RCLONE_CONFIG` override."""
    override = os.environ.get('RCLONE_CONFIG', '').strip()
    return Path(override) if override else Path.home() / '.config' / 'rclone' / 'rclone.conf'


def rclone_credentials(remote: str, config_path: Path | None = None) -> Any:
    """Build Google credentials from an rclone `type = drive` remote.

    Reads the OAuth client and refresh token rclone stored at `rclone config` time. The stored
    access token is usually expired; google-auth refreshes it on first use.
    """
    from google.oauth2.credentials import Credentials

    path = config_path or rclone_config_path()
    if not path.is_file():
        raise RcloneConfigError(
            f'No rclone config at {path}. Set RCLONE_CONFIG to its location, or run '
            '`rclone config` to create a Google Drive remote.'
        )

    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as exc:
        raise RcloneConfigError(f'Could not parse {path}: {exc}') from exc

    if not parser.has_section(remote):
        available = ', '.join(parser.sections()) or 'none'
        raise RcloneConfigError(
            f'No rclone remote named {remote!r} in {path}. Available remotes: {available}.'
        )
    section = parser[remote]

    kind = section.get('type', '')
    if kind != 'drive':
        raise RcloneConfigError(
            f'rclone remote {remote!r} is type {kind!r}, not "drive". This server needs a Google '
            'Drive remote.'
        )

    scope = section.get('scope', 'drive').strip() or 'drive'
    if scope != 'drive':
        raise RcloneConfigError(
            f'rclone remote {remote!r} was authorized with scope {scope!r}. Reading and writing '
            'workbooks needs the full "drive" scope; re-run `rclone config` for that remote.'
        )

    client_id = section.get('client_id', '').strip()
    client_secret = section.get('client_secret', '').strip()
    if not (client_id and client_secret):
        raise RcloneConfigError(
            f"rclone remote {remote!r} has no client_id/client_secret, so it is using rclone's "
            'built-in OAuth client, whose secret is not in the config. Re-run `rclone config` '
            'for that remote with your own Google OAuth client credentials.'
        )

    try:
        token = json.loads(section.get('token', '') or '{}')
    except json.JSONDecodeError as exc:
        raise RcloneConfigError(f'rclone remote {remote!r} has an unreadable token: {exc}') from exc

    refresh_token = token.get('refresh_token')
    if not refresh_token:
        raise RcloneConfigError(
            f'rclone remote {remote!r} has no refresh token, so its access cannot be renewed. '
            'Re-run `rclone config reconnect` for that remote.'
        )

    return Credentials(
        token=token.get('access_token'),
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        # rclone's "drive" scope is the full auth/drive scope, which the Sheets API accepts.
        scopes=[DRIVE_SCOPE],
        expiry=_parse_expiry(token.get('expiry')),
    )


def _parse_expiry(value: str | None) -> datetime | None:
    """rclone's RFC3339 expiry as the naive UTC datetime google-auth expects.

    Passing it lets google-auth refresh before making a doomed call. Returning None on anything
    unparseable is safe: the credential is then treated as unexpired and refreshed reactively.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


#: A syntactically valid file id that cannot exist. Asking for it proves the Sheets API accepted
#: our token — a 404 means "authorized, no such file", where a scope problem would be a 403.
_UNUSED_FILE_ID = '1' + 'z' * 43


@dataclass(frozen=True)
class AccessCheck:
    """What `verify_access` established about the current credentials."""

    source: str
    account: str
    drive_ok: bool
    sheets_ok: bool


def verify_access() -> AccessCheck:
    """Prove the credentials actually work, and report which account they belong to.

    Every failure mode here is one a human has to fix, so each raises a `ScrubberError` saying
    what to do rather than surfacing Google's wording. The server acts as the signed-in user, so
    the account name is the first thing to check when a workbook "cannot be found".
    """
    from googleapiclient.discovery import build

    from .errors import WorkbookNotFound
    from .sheets import _execute

    creds = credentials()
    drive = build('drive', 'v3', credentials=creds, cache_discovery=False)
    about = _execute(drive.about().get(fields='user(emailAddress)'))
    account = about.get('user', {}).get('emailAddress', '')

    sheets_service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    try:
        _execute(
            sheets_service.spreadsheets().get(
                spreadsheetId=_UNUSED_FILE_ID, fields='spreadsheetId'
            )
        )
    except WorkbookNotFound:
        pass  # Expected, and exactly what we wanted to learn: the token was accepted.

    return AccessCheck(source=credential_source(), account=account, drive_ok=True, sheets_ok=True)


@functools.cache
def google_backend() -> Any:
    """Build the live `SheetsBackend`. Cached so the discovery documents load once."""
    from googleapiclient.discovery import build

    from .sheets import GoogleBackend

    creds = credentials()
    return GoogleBackend(
        sheets_service=build('sheets', 'v4', credentials=creds, cache_discovery=False),
        drive_service=build('drive', 'v3', credentials=creds, cache_discovery=False),
    )
