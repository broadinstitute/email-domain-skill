"""Errors surfaced to MCP callers.

`ScrubberError` messages are written for the analysis skill to read and act on, so they
name the offending value and say what to do about it.
"""

#: The login users need for the server to reach Sheets and Drive. User ADC credentials carry the
#: scopes granted at login time, so `--scopes` is not optional.
GCLOUD_LOGIN_HINT = (
    'gcloud auth application-default login --scopes='
    'https://www.googleapis.com/auth/spreadsheets,'
    'https://www.googleapis.com/auth/drive,'
    'https://www.googleapis.com/auth/cloud-platform'
)


class ScrubberError(Exception):
    """Base class for errors that are the caller's problem, not a bug."""


class MissingScopes(ScrubberError):
    """Credentials are valid but were not granted the Sheets/Drive scopes."""

    def __init__(self, detail: str = '') -> None:
        super().__init__(
            'Google rejected the request for lack of scope'
            f'{f" ({detail})" if detail else ""}. Application default credentials only carry the '
            'scopes granted at login, so re-run:\n'
            f'  {GCLOUD_LOGIN_HINT}'
        )


class RcloneConfigError(ScrubberError):
    """The rclone remote named for credentials is missing or unusable."""


class CredentialsExpired(ScrubberError):
    """Credentials were found but Google would not renew them.

    A refresh token is revoked when the user changes their password, an admin revokes the grant,
    or (for an app in testing) after seven days. Google says only `invalid_grant: Bad Request`,
    so the fix — signing in again, in a browser — has to be spelled out here.
    """

    def __init__(self, detail: str, login: str) -> None:
        super().__init__(
            f'Google would not renew the stored credentials ({detail}). They have expired or been '
            f'revoked, so you need to sign in again:\n  {login}'
        )


class AccessDenied(ScrubberError):
    """The signed-in user cannot reach or modify the file."""


class WorkbookNotFound(ScrubberError):
    """No such Drive file, or it is not shared with the signed-in user."""


class InvalidWorkbookReference(ScrubberError):
    """A URL or id could not be resolved to a spreadsheet."""


class UnsupportedWorkbook(ScrubberError):
    """A Drive file exists but is not a spreadsheet we can read."""


class SchemaMismatch(ScrubberError):
    """An analysis workbook exists but its sheets do not match the expected schema."""


class UnanalyzedDomains(ScrubberError):
    """Redaction was requested while some domains still lack a recorded analysis."""

    def __init__(self, domains: list[str]) -> None:
        self.domains = domains
        listed = ', '.join(domains[:20])
        if len(domains) > 20:
            listed += f', ... ({len(domains)} total)'
        super().__init__(
            f'Refusing to redact: {len(domains)} domain(s) have no analysis recorded '
            f'in DomainAnalysis: {listed}. Analyze them and call store_domain_analysis first.'
        )


class AnonymizationSpaceExhausted(ScrubberError):
    """No unused anonNNNN token is available."""
