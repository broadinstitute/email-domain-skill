"""Errors surfaced to MCP callers.

`ScrubberError` messages are written for the analysis skill to read and act on, so they
name the offending value and say what to do about it.
"""


class ScrubberError(Exception):
    """Base class for errors that are the caller's problem, not a bug."""


class InvalidWorkbookReference(ScrubberError):
    """A reference could not be resolved to a readable file on disk."""


class UnsupportedWorkbook(ScrubberError):
    """The file exists but is not an Excel workbook we can read."""


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

    An external MCP server does the writing, so "the plan was produced" does not imply "the edits
    landed". This is what catches a write step that was skipped or only half ran.
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
