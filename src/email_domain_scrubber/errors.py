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


class InvalidRisk(ScrubberError):
    """A DomainAnalysis row holds a Risk outside the taxonomy.

    Reachable only by hand-editing the analysis workbook, which is a supported thing to do — so
    the message names the row to fix rather than treating it as corruption.
    """

    def __init__(self, domain: str, risk: str) -> None:
        self.domain = domain
        self.risk = risk
        super().__init__(
            f'DomainAnalysis row for {domain!r} has Risk {risk!r}, which is not one of High, '
            'Medium, or Low. Fix that cell in the analysis workbook and try again.'
        )


class RedactionNotApplied(ScrubberError):
    """The redacted file still holds domains that should have been replaced.

    Written, then read back: this catches a write that did not land, whatever the reason — an
    unwritable copy, a value openpyxl declined to set, a domain reachable only through a formula
    result. Raised rather than reported, so a half-redacted file is never certified.
    """

    def __init__(self, path: str, domains: list[str]) -> None:
        self.domains = domains
        super().__init__(
            f'{path} still contains {len(domains)} domain(s) that should have been replaced: '
            f'{", ".join(domains[:20])}. The redacted copy was left in place for inspection; do '
            'not share it.'
        )


class AnonymizationSpaceExhausted(ScrubberError):
    """No unused anonNNNN token is available."""
