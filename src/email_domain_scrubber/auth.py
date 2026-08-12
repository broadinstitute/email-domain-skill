"""OAuth access tokens for the Google Drive MCP connector.

Credentials come from an **rclone** Google Drive remote, named by
`EMAIL_DOMAIN_RCLONE_REMOTE`. rclone already holds a Drive OAuth client and refresh token. The
config is only ever read; refreshed access tokens are kept in memory, not written back.

The connector wants the literal scopes `drive.readonly` and `drive.file`, and it checks for those
strings rather than for equivalent authority. A token carrying only the full `drive` scope — a
strict superset in capability, and rclone's default — is refused with "The caller does not have
permission". This was confirmed against the live endpoint, so the check below insists on both.
rclone's `scope` takes a comma-separated list, which is how you grant them.

The server acts as the signed-in user, so it can only reach files that user can already open.

The OAuth client may be overridden with `EMAIL_DOMAIN_OAUTH_CLIENT_ID` /
`EMAIL_DOMAIN_OAUTH_CLIENT_SECRET`. That matters because the connector is billed to the Cloud
project owning the client, and *that* project is the one that has to have
`drivemcp.googleapis.com` enabled — which may not be the project rclone was configured against.
"""

from __future__ import annotations

import configparser
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import CredentialsExpired, RcloneConfigError, ScrubberError

#: Name of an rclone remote of `type = drive` to borrow credentials from.
RCLONE_REMOTE_ENV = 'EMAIL_DOMAIN_RCLONE_REMOTE'
CLIENT_ID_ENV = 'EMAIL_DOMAIN_OAUTH_CLIENT_ID'
CLIENT_SECRET_ENV = 'EMAIL_DOMAIN_OAUTH_CLIENT_SECRET'

#: The scopes the connector requires by name. `drive.readonly` to read reports, `drive.file` to
#: upload redacted copies. It will not accept the broader `drive` scope in their place.
CONNECTOR_SCOPES = ('drive.readonly', 'drive.file')

TOKEN_URI = 'https://oauth2.googleapis.com/token'

#: Refresh this many seconds before the token actually expires, so a call that starts just
#: under the wire does not land just over it.
_EXPIRY_MARGIN = 60


@dataclass(frozen=True)
class OAuthClient:
    """Everything needed to mint access tokens, read out of the rclone config."""

    client_id: str
    client_secret: str
    refresh_token: str


def configured_remote() -> str:
    """The rclone remote to borrow credentials from, or raise saying how to name one."""
    remote = os.environ.get(RCLONE_REMOTE_ENV, '').strip()
    if not remote:
        raise ScrubberError(
            f'No Google credentials configured. Set {RCLONE_REMOTE_ENV} to the name of an rclone '
            f'remote with `type = drive` and `scope = drive,{",".join(CONNECTOR_SCOPES)}`, '
            'for example:\n'
            f'  export {RCLONE_REMOTE_ENV}=aso'
        )
    return remote


def login_hint() -> str:
    """The command a human runs to sign in again. Opens a browser."""
    remote = os.environ.get(RCLONE_REMOTE_ENV, '').strip()
    if not remote:
        return f'set {RCLONE_REMOTE_ENV} to an rclone Google Drive remote, then `rclone config`'
    return f'rclone config reconnect {remote}:'


def rclone_config_path() -> Path:
    """Where rclone keeps its config, honouring rclone's own `RCLONE_CONFIG` override."""
    override = os.environ.get('RCLONE_CONFIG', '').strip()
    return Path(override) if override else Path.home() / '.config' / 'rclone' / 'rclone.conf'


def credential_source() -> str:
    """Human-readable description of where credentials are coming from."""
    return f'rclone remote {configured_remote()!r} in {rclone_config_path()}'


def oauth_client(remote: str | None = None, config_path: Path | None = None) -> OAuthClient:
    """Read the OAuth client and refresh token rclone stored at `rclone config` time."""
    remote = remote or configured_remote()
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

    granted = {
        part.strip() for part in (section.get('scope') or 'drive').split(',') if part.strip()
    }
    missing = [scope for scope in CONNECTOR_SCOPES if scope not in granted]
    if missing:
        raise RcloneConfigError(
            f'rclone remote {remote!r} was authorized with scope '
            f'{",".join(sorted(granted)) or "drive"!r}, which is missing '
            f'{" and ".join(missing)}. The Drive MCP connector checks for those exact scope '
            'names and refuses a token that only carries the broader "drive" scope, even though '
            'it grants strictly more. Set both on the remote and re-consent:\n'
            f'  rclone config update {remote} scope drive,{",".join(CONNECTOR_SCOPES)}\n'
            f'  rclone config reconnect {remote}:'
        )

    # An explicit client wins: the connector is billed to the Cloud project owning the client,
    # and that project is the one that needs drivemcp.googleapis.com enabled.
    client_id = os.environ.get(CLIENT_ID_ENV, '').strip() or section.get('client_id', '').strip()
    client_secret = (
        os.environ.get(CLIENT_SECRET_ENV, '').strip() or section.get('client_secret', '').strip()
    )
    if not (client_id and client_secret):
        raise RcloneConfigError(
            f"rclone remote {remote!r} has no client_id/client_secret, so it is using rclone's "
            'built-in OAuth client, whose secret is not in the config. Re-run `rclone config` '
            f'for that remote with your own Google OAuth client, or set {CLIENT_ID_ENV} and '
            f'{CLIENT_SECRET_ENV}.'
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

    return OAuthClient(client_id, client_secret, refresh_token)


class TokenSource:
    """Mints and caches Drive access tokens from a refresh token.

    google-auth used to do this. It is a single form POST, so doing it directly drops a large
    dependency tree in exchange for about twenty lines.
    """

    def __init__(self, client: OAuthClient | None = None) -> None:
        self._client = client
        self._token = ''
        self._expires_at = 0.0

    async def token(self) -> str:
        """A valid access token, refreshing if the cached one is close to expiry."""
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        if self._client is None:
            self._client = oauth_client()
        self._token, lifetime = await self._refresh(self._client)
        self._expires_at = time.monotonic() + max(lifetime - _EXPIRY_MARGIN, 0)
        return self._token

    @staticmethod
    async def _refresh(client: OAuthClient) -> tuple[str, float]:
        import httpx2

        async with httpx2.AsyncClient(timeout=30) as http:
            response = await http.post(
                TOKEN_URI,
                data={
                    'client_id': client.client_id,
                    'client_secret': client.client_secret,
                    'refresh_token': client.refresh_token,
                    'grant_type': 'refresh_token',
                },
            )
        if response.status_code != 200:
            # Google says only `invalid_grant: Bad Request` when a refresh token has been
            # revoked — by a password change, an admin, or seven days of an app in testing —
            # so the fix has to be spelled out for the caller.
            raise CredentialsExpired(_token_error(response), login_hint())
        payload = response.json()
        return payload['access_token'], float(payload.get('expires_in', 3600))


def _token_error(response: object) -> str:
    """Everything Google told us about a failed refresh.

    Both halves matter: `error` is the diagnostic code (`invalid_grant` for a revoked token),
    while `error_description` is often no more than "Bad Request".
    """
    status = f'HTTP {getattr(response, "status_code", "?")}'
    try:
        payload = response.json()  # type: ignore[attr-defined]
    except Exception:
        return status
    parts = [payload.get('error'), payload.get('error_description')]
    detail = ': '.join(part for part in parts if part)
    return f'{status}: {detail}' if detail else status
