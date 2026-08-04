"""MCP server for scanning, recording, and anonymizing email domains in metrics workbooks.

Division of labour: this server does everything deterministic and auditable — finding domains,
persisting them, minting aliases, rewriting cells. The analysis skill does the judgement —
deciding each domain's `Risk` and whether it needs anonymizing — and gets user approval before
calling `store_domain_analysis` or `redact_workbook`.
"""

from __future__ import annotations

import os
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from . import redact as redaction
from .auth import google_backend
from .errors import InvalidWorkbookReference
from .scan import resolve_workbook, scan_spreadsheet, unique_domains
from .sheets import SheetsBackend, parse_file_id, spreadsheet_url
from .workbook import DEFAULT_TITLE, RISKS, AnalysisWorkbook, DomainReference, today

#: Set this so callers can omit `analysis_workbook` on every call.
ANALYSIS_WORKBOOK_ENV = 'EMAIL_DOMAIN_ANALYSIS_WORKBOOK'

mcp = MCPServer(
    'email-domain-scrubber',
    instructions=(
        'Tools for the email domain privacy workflow on platform usage metric reports. '
        'Typical order: scan_workbook -> list_domains_for_analysis -> (analyze, then get user '
        'approval) -> store_domain_analysis -> redact_workbook. The analysis workbook is the '
        'durable record; pass its URL to every tool or set the '
        f'{ANALYSIS_WORKBOOK_ENV} environment variable.'
    ),
)

_backend_override: SheetsBackend | None = None


def set_backend(backend: SheetsBackend | None) -> None:
    """Inject a backend (used by tests); `None` restores live Google access."""
    global _backend_override
    _backend_override = backend


def _backend() -> SheetsBackend:
    return _backend_override if _backend_override is not None else google_backend()


def _open_analysis(analysis_workbook: str | None) -> AnalysisWorkbook:
    reference = (analysis_workbook or os.environ.get(ANALYSIS_WORKBOOK_ENV) or '').strip()
    if not reference:
        raise InvalidWorkbookReference(
            'No analysis workbook given. Pass analysis_workbook, set the '
            f'{ANALYSIS_WORKBOOK_ENV} environment variable, or call '
            'create_analysis_workbook to make one.'
        )
    return AnalysisWorkbook.open(_backend(), parse_file_id(reference))


WorkbookUrl = Annotated[
    str, Field(description='Google Sheets URL, Drive URL, or file id of the metrics workbook.')
]
AnalysisWorkbookUrl = Annotated[
    str | None,
    Field(
        description='URL or id of the domain analysis workbook. Defaults to the '
        f'{ANALYSIS_WORKBOOK_ENV} environment variable.'
    ),
]


# -- models ------------------------------------------------------------------------------------
class DomainSummary(BaseModel):
    domain: str
    risk: str = Field(description=f'One of {", ".join(RISKS)}, or empty if not yet analyzed.')
    explanation: str = ''
    anonymized_domain: str = Field(
        default='', description='Alias substituted at redaction time; empty if none is assigned.'
    )
    reference_count: int = Field(default=0, description='Cells this domain was found in.')
    example_references: list[str] = Field(
        default_factory=list, description='Direct links to a few of those cells.'
    )


class ScanResult(BaseModel):
    scanned_workbook_url: str
    scanned_workbook_title: str
    converted_from_upload: bool = Field(
        default=False,
        description='True if a Drive XLSX/CSV upload was converted to a Google Sheet in order '
        'to be scanned and linked; scanned_workbook_url points at that conversion.',
    )
    analysis_workbook_url: str
    domains_found: int
    new_domains: list[str] = Field(description='Domains seen here for the first time.')
    pending_analysis: list[str] = Field(description='All domains still awaiting a Risk verdict.')
    references_recorded: int
    date_extracted: str


class StoredAnalysis(BaseModel):
    domain: str
    risk: str
    anonymized_domain: str = ''
    action: Literal['will_be_anonymized', 'left_as_is']


class StoreResult(BaseModel):
    analysis_workbook_url: str
    stored: list[StoredAnalysis]
    still_pending: list[str]


class RedactionCell(BaseModel):
    sheet: str
    cell: str
    before: str
    after: str


class RedactionResult(BaseModel):
    dry_run: bool
    source_workbook_url: str
    redacted_workbook_url: str = Field(
        default='', description='The anonymized copy. Empty on a dry run.'
    )
    redacted_workbook_title: str = ''
    cells_changed: int
    domains_anonymized: dict[str, str] = Field(
        default_factory=dict, description='Domain -> alias, for domains actually replaced.'
    )
    domains_left_as_is: list[str] = Field(
        default_factory=list,
        description='Analyzed as not needing anonymization, so intentionally untouched.',
    )
    sample_changes: list[RedactionCell] = Field(default_factory=list)
    remaining_domains: list[str] = Field(
        default_factory=list,
        description='Domains still present in the copy after redaction — the ones left as is, '
        'plus anything a re-scan turned up. Verify this looks expected before publishing.',
    )


class WorkbookCreated(BaseModel):
    analysis_workbook_url: str
    title: str
    sheets: list[str]


# -- tools -------------------------------------------------------------------------------------
@mcp.tool()
def create_analysis_workbook(
    title: Annotated[str, Field(description='Title for the new workbook.')] = DEFAULT_TITLE,
    folder_id: Annotated[
        str | None,
        Field(
            description='Drive folder or shared drive id to create it in. Defaults to the '
            "user's My Drive."
        ),
    ] = None,
) -> WorkbookCreated:
    """Create an empty domain analysis workbook with the required sheets and headers.

    Use once per project. Save the returned URL (or set it as the
    EMAIL_DOMAIN_ANALYSIS_WORKBOOK environment variable) and reuse it for every later call so
    analysis and aliases accumulate in one place. Pass folder_id to keep it beside the metrics
    reports on a shared drive rather than in your own My Drive.
    """
    workbook = AnalysisWorkbook.create(_backend(), title, folder_id)
    return WorkbookCreated(
        analysis_workbook_url=workbook.url, title=workbook.title, sheets=workbook.sheet_titles
    )


@mcp.tool()
def scan_workbook(
    workbook: WorkbookUrl, analysis_workbook: AnalysisWorkbookUrl = None
) -> ScanResult:
    """Find every email domain in a metrics workbook and record it for analysis.

    Reads all cells of all sheets, extracting domains from email addresses and from bare domain
    values. Each occurrence is appended to DomainReferences with a direct link to its cell, and
    each unique domain gets a DomainAnalysis row with an empty Risk. Re-scanning the same
    workbook is safe: existing references and verdicts are preserved.

    A Drive XLSX/CSV upload is converted to a Google Sheet first, since only native sheets can
    be read cell-by-cell and linked to.
    """
    backend = _backend()
    analysis = _open_analysis(analysis_workbook)
    resolved = resolve_workbook(backend, workbook)

    hits = scan_spreadsheet(backend, resolved.info)
    domains = unique_domains(hits)
    date = today()

    scanned_url = spreadsheet_url(resolved.info.spreadsheet_id)
    analysis.record_workbook(scanned_url, resolved.info.title)
    recorded = analysis.record_references(
        [
            DomainReference(reference=hit.reference, domain=hit.domain, date_extracted=date)
            for hit in hits
        ]
    )
    new_domains = analysis.ensure_analysis_rows(domains)

    return ScanResult(
        scanned_workbook_url=scanned_url,
        scanned_workbook_title=resolved.info.title,
        converted_from_upload=resolved.converted,
        analysis_workbook_url=analysis.url,
        domains_found=len(domains),
        new_domains=new_domains,
        pending_analysis=[row.domain for row in analysis.pending_domains()],
        references_recorded=len(recorded),
        date_extracted=date,
    )


@mcp.tool()
def list_domains_for_analysis(
    analysis_workbook: AnalysisWorkbookUrl = None,
    include_analyzed: Annotated[
        bool, Field(description='Also return domains that already have a Risk recorded.')
    ] = False,
) -> list[DomainSummary]:
    """List the domains to analyze, with how often and where each was seen.

    By default returns only domains with no Risk yet — the work queue for the risk analysis
    skill. Example references are direct cell links, useful for seeing a domain in context.
    """
    analysis = _open_analysis(analysis_workbook)
    counts = analysis.reference_counts()
    samples = analysis.sample_references()
    rows = analysis.analysis_rows() if include_analyzed else analysis.pending_domains()
    return [
        DomainSummary(
            domain=row.domain,
            risk=row.risk,
            explanation=row.explanation,
            anonymized_domain=row.anonymized_domain,
            reference_count=counts.get(row.domain, 0),
            example_references=samples.get(row.domain, []),
        )
        for row in rows
    ]


class AnalysisInput(BaseModel):
    """One domain's verdict from the risk analysis skill."""

    domain: Annotated[str, Field(description='The domain exactly as listed for analysis.')]
    risk: Annotated[
        Literal['High', 'Medium', 'Low'],
        Field(description='Risk that the domain name itself identifies an individual.'),
    ]
    explanation: Annotated[
        str,
        Field(
            description='Why, in one or two sentences, citing what the research found. This is '
            'the audit trail for the decision.'
        ),
    ]
    anonymize: Annotated[
        bool | None,
        Field(
            description='Whether this domain should be replaced in published reports. Defaults '
            'to true for High risk and false otherwise.'
        ),
    ] = None


@mcp.tool()
def store_domain_analysis(
    analyses: Annotated[
        list[AnalysisInput], Field(description='One entry per analyzed domain.', min_length=1)
    ],
    analysis_workbook: AnalysisWorkbookUrl = None,
) -> StoreResult:
    """Record approved risk verdicts, assigning an alias to each domain to be anonymized.

    Call this only after presenting the analysis to the user and getting their approval. Writing
    is idempotent per domain: re-storing updates Risk and Explanation, and a domain that already
    has an alias keeps it, so aliases stay stable across quarterly reports.
    """
    analysis = _open_analysis(analysis_workbook)
    stored = analysis.store_analysis(
        [(entry.domain, entry.risk, entry.explanation, entry.anonymize) for entry in analyses]
    )
    return StoreResult(
        analysis_workbook_url=analysis.url,
        stored=[
            StoredAnalysis(
                domain=row.domain,
                risk=row.risk,
                anonymized_domain=row.anonymized_domain,
                action='will_be_anonymized' if row.anonymized_domain else 'left_as_is',
            )
            for row in stored
        ],
        still_pending=[row.domain for row in analysis.pending_domains()],
    )


@mcp.tool()
def redact_workbook(
    workbook: WorkbookUrl,
    analysis_workbook: AnalysisWorkbookUrl = None,
    dry_run: Annotated[
        bool, Field(description='Report what would change without copying or writing anything.')
    ] = False,
) -> RedactionResult:
    """Write an anonymized copy of a metrics workbook, replacing domains with their aliases.

    The source workbook is never modified — this copies it to "<title> (anonymized)" and edits
    the copy. Only domains whose analysis assigned an alias are replaced; domains analyzed as
    not needing anonymization are left in place on purpose. Fails if any domain in the workbook
    has no recorded analysis, so nothing unreviewed can be published.

    Every rewritten cell is appended to the Redactions sheet of the analysis workbook. Run with
    dry_run=true first to show the user what will change.
    """
    backend = _backend()
    analysis = _open_analysis(analysis_workbook)
    resolved = resolve_workbook(backend, workbook)
    hits = scan_spreadsheet(backend, resolved.info)
    plan = redaction.plan_redaction(resolved, hits, analysis)

    samples = [
        RedactionCell(sheet=edit.sheet_title, cell=edit.a1, before=edit.before, after=edit.after)
        for edit in plan.edits[:10]
    ]

    if dry_run:
        return RedactionResult(
            dry_run=True,
            source_workbook_url=plan.source_url,
            cells_changed=plan.cells_to_change,
            domains_anonymized=plan.mapped_domains,
            domains_left_as_is=plan.left_as_is,
            sample_changes=samples,
            remaining_domains=plan.left_as_is,
        )

    result = redaction.execute_redaction(backend, resolved, plan, analysis)
    remaining = redaction.rescan_for_verification(
        backend, backend.get_spreadsheet(parse_file_id(result.redacted_url))
    )
    return RedactionResult(
        dry_run=False,
        source_workbook_url=plan.source_url,
        redacted_workbook_url=result.redacted_url,
        redacted_workbook_title=result.redacted_title,
        cells_changed=plan.cells_to_change,
        domains_anonymized=plan.mapped_domains,
        domains_left_as_is=plan.left_as_is,
        sample_changes=samples,
        remaining_domains=remaining,
    )


def main() -> None:
    mcp.run()
