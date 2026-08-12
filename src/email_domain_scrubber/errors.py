"""Errors surfaced to MCP callers.

`ScrubberError` messages are written for the analysis skill to read and act on, so they
name the offending value and say what to do about it.
"""


class ScrubberError(Exception):
    """Base class for errors that are the caller's problem, not a bug."""


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


class DriveMcpApiDisabled(ScrubberError):
    """The Drive MCP API is not enabled in the Cloud project owning the OAuth client.

    The single most likely first-run failure, and invisible from the Drive side: the plain Drive
    API being enabled says nothing about `drivemcp.googleapis.com`. Google's own message names
    the project and the console URL, so it is passed through verbatim.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            f'The Google Drive MCP connector is not available to this OAuth client: {detail}\n'
            'The connector is billed to the Cloud project that owns the client_id, which is the '
            'project that must have drivemcp.googleapis.com enabled. Either enable it there, or '
            'point EMAIL_DOMAIN_OAUTH_CLIENT_ID/EMAIL_DOMAIN_OAUTH_CLIENT_SECRET at a client in '
            'a project where it is enabled.'
        )


class DriveMcpError(ScrubberError):
    """The Drive MCP connector refused a call for a reason we cannot classify further."""

    def __init__(self, tool: str, detail: str) -> None:
        self.tool = tool
        super().__init__(f'The Drive MCP connector failed on {tool}: {detail}')


class MissingScopes(ScrubberError):
    """Credentials are valid but were not granted the Drive scopes the connector needs."""

    def __init__(self, detail: str = '') -> None:
        super().__init__(
            'Google rejected the request for lack of scope'
            f'{f" ({detail})" if detail else ""}. Credentials only carry the scopes granted at '
            'login, so re-run `rclone config` for the remote named by '
            'EMAIL_DOMAIN_RCLONE_REMOTE and authorize it with the full "drive" scope.'
        )


class AccessDenied(ScrubberError):
    """The signed-in user cannot reach or modify the file."""


class WorkbookNotFound(ScrubberError):
    """No such Drive file, or it is not shared with the signed-in user."""


class InvalidWorkbookReference(ScrubberError):
    """A URL or id could not be resolved to a Drive file."""


class UnsupportedWorkbook(ScrubberError):
    """A Drive file exists but is not an Excel workbook we can read."""


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


class RedactionNotApplied(ScrubberError):
    """The redacted file still holds domains that the plan said would be replaced.

    An external MCP server does the writing now, so "the plan was produced" no longer implies
    "the edits landed". This is what catches a write step that was skipped or only half ran.
    """

    def __init__(self, path: str, domains: list[str]) -> None:
        self.domains = domains
        super().__init__(
            f'{path} still contains {len(domains)} domain(s) that should have been replaced: '
            f'{", ".join(domains[:20])}. Apply every write block from plan_redaction with the '
            'Excel MCP server before calling finish_redaction.'
        )


class AnonymizationSpaceExhausted(ScrubberError):
    """No unused anonNNNN token is available."""
