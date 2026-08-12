"""MCP server for scanning, recording, and anonymizing email domains in Excel metrics reports.

Division of labour, now across three servers:

* **This server** does everything deterministic and auditable — fetching workbooks from Drive
  through Google's Drive MCP connector, finding domains, persisting them, minting aliases,
  planning the redaction, and verifying it landed.
* **The Excel MCP server** applies the planned writes to the copied workbook. Point it at the
  `redacted_path` and `write_blocks` that `plan_redaction` returns.
* **The analysis skill** does the judgement — deciding each domain's `Risk` and whether it needs
  anonymizing — and gets user approval before calling `store_domain_analysis` or
  `finish_redaction`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from . import redact as redaction
from .drive import Drive, DriveMcpClient
from .scan import scan_path, stage_workbook, unique_domains
from .staging import ANALYSIS_WORKBOOK_ENV, Staging, analysis_workbook_path
from .workbook import RISKS, AnalysisWorkbook, DomainReference, today

mcp = MCPServer(
    'email-domain-scrubber',
    instructions=(
        'Tools for the email domain privacy workflow on Excel (.xlsx) usage metric reports in '
        'Google Drive. Typical order: scan_workbook -> list_domains_for_analysis -> (analyze, '
        'then get user approval) -> store_domain_analysis -> plan_redaction -> apply every '
        'returned write block with the Excel MCP server write_data_to_excel tool -> '
        'finish_redaction. The analysis workbook is a local .xlsx holding the durable record; '
        f'pass its path to every tool or set the {ANALYSIS_WORKBOOK_ENV} environment variable.'
    ),
)

_drive_override: Drive | None = None
_staging_override: Staging | None = None


def set_backend(drive: Drive | None, staging: Staging | None = None) -> None:
    """Inject a Drive client and staging root (used by tests); `None` restores live access."""
    global _drive_override, _staging_override
    _drive_override = drive
    _staging_override = staging


def _drive() -> Drive:
    return _drive_override if _drive_override is not None else DriveMcpClient()


def _staging() -> Staging:
    return _staging_override if _staging_override is not None else Staging()


def _open_analysis(analysis_workbook: str | None) -> AnalysisWorkbook:
    return AnalysisWorkbook.open(analysis_workbook_path(analysis_workbook))


WorkbookUrl = Annotated[
    str,
    Field(
        description='Google Drive URL or file id of the .xlsx metrics workbook. Google Sheets '
        'and CSV files are not supported.'
    ),
]
AnalysisWorkbookPath = Annotated[
    str | None,
    Field(
        description='Path to the local domain analysis workbook (.xlsx). Defaults to the '
        f'{ANALYSIS_WORKBOOK_ENV} environment variable, then to analysis.xlsx in the work '
        'directory. Created on first use.'
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
        default_factory=list, description='Locators for a few of those cells.'
    )


class ScanResult(BaseModel):
    scanned_workbook_url: str
    scanned_workbook_title: str
    local_path: str = Field(description='Where the workbook was staged on disk.')
    downloaded: bool = Field(
        description='False if an up-to-date local copy was reused instead of re-downloading.'
    )
    analysis_workbook_path: str
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
    analysis_workbook_path: str
    stored: list[StoredAnalysis]
    still_pending: list[str]


class RedactionCell(BaseModel):
    sheet: str
    cell: str
    before: str
    after: str


class WriteBlockOut(BaseModel):
    """One `write_data_to_excel` call for the Excel MCP server."""

    sheet_name: str = Field(description='Pass as `sheet_name`.')
    start_cell: str = Field(description='Pass as `start_cell`.')
    data: list[list[str]] = Field(description='Pass as `data`. Rows of a single column.')


class PlanResult(BaseModel):
    source_workbook_url: str
    redacted_path: str = Field(
        description='The copy to edit. Pass as `filepath` to write_data_to_excel. Empty if '
        'there is nothing to change.'
    )
    cells_to_change: int
    write_blocks: list[WriteBlockOut] = Field(
        description='Apply every one of these with the Excel MCP server write_data_to_excel '
        'tool, then call finish_redaction. Applying only some leaves domains exposed, which '
        'finish_redaction will reject.'
    )
    domains_anonymized: dict[str, str] = Field(
        default_factory=dict, description='Domain -> alias, for domains that will be replaced.'
    )
    domains_left_as_is: list[str] = Field(
        default_factory=list,
        description='Analyzed as not needing anonymization, so intentionally untouched.',
    )
    sample_changes: list[RedactionCell] = Field(
        default_factory=list, description='A few before/after examples to show the user.'
    )


class FinishResult(BaseModel):
    source_workbook_url: str
    redacted_workbook_url: str = Field(description='The published .xlsx in Drive.')
    redacted_workbook_title: str
    cells_changed: int
    domains_anonymized: dict[str, str] = Field(default_factory=dict)
    domains_left_as_is: list[str] = Field(default_factory=list)
    remaining_domains: list[str] = Field(
        default_factory=list,
        description='Domains still present in the published copy — the ones left as is, plus '
        'anything a re-scan turned up. Verify this looks expected before sharing the file.',
    )
    redactions_recorded: int


# -- tools -------------------------------------------------------------------------------------
@mcp.tool()
async def scan_workbook(
    workbook: WorkbookUrl, analysis_workbook: AnalysisWorkbookPath = None
) -> ScanResult:
    """Find every email domain in an Excel metrics workbook and record it for analysis.

    Downloads the .xlsx from Drive through the Drive MCP connector into the local work
    directory, then reads all cells of all sheets, extracting domains from email addresses and
    from bare domain values. Each occurrence is appended to DomainReferences with a locator for
    its cell, and each unique domain gets a DomainAnalysis row with an empty Risk.

    Re-scanning the same workbook is safe: existing references and verdicts are preserved, and
    an unchanged workbook is not re-downloaded. The analysis workbook is created if absent.
    """
    drive = _drive()
    staging = _staging()
    analysis = _open_analysis(analysis_workbook)
    staged = await stage_workbook(drive, staging, workbook)

    hits = scan_path(staged.path, staged.info.file_id)
    domains = unique_domains(hits)
    date = today()

    analysis.record_workbook(staged.url, staged.info.name)
    recorded = analysis.record_references(
        [
            DomainReference(reference=hit.reference, domain=hit.domain, date_extracted=date)
            for hit in hits
        ]
    )
    new_domains = analysis.ensure_analysis_rows(domains)

    return ScanResult(
        scanned_workbook_url=staged.url,
        scanned_workbook_title=staged.info.name,
        local_path=str(staged.path),
        downloaded=staged.downloaded,
        analysis_workbook_path=analysis.url,
        domains_found=len(domains),
        new_domains=new_domains,
        pending_analysis=[row.domain for row in analysis.pending_domains()],
        references_recorded=len(recorded),
        date_extracted=date,
    )


@mcp.tool()
def list_domains_for_analysis(
    analysis_workbook: AnalysisWorkbookPath = None,
    include_analyzed: Annotated[
        bool, Field(description='Also return domains that already have a Risk recorded.')
    ] = False,
) -> list[DomainSummary]:
    """List the domains to analyze, with how often and where each was seen.

    By default returns only domains with no Risk yet — the work queue for the risk analysis
    skill. Example references locate a cell, useful for seeing a domain in context.
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
    analysis_workbook: AnalysisWorkbookPath = None,
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
        analysis_workbook_path=analysis.url,
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
async def plan_redaction(
    workbook: WorkbookUrl, analysis_workbook: AnalysisWorkbookPath = None
) -> PlanResult:
    """Copy the workbook locally and return the cell writes that anonymize it.

    Nothing is published and neither the Drive original nor the staged copy of it is modified.
    This makes `<name> (anonymized).xlsx` beside the staged workbook and tells you what to write
    into it; the Excel MCP server does the writing.

    Only domains whose analysis assigned an alias are replaced; domains analyzed as not needing
    anonymization are left in place on purpose. Fails if any domain in the workbook has no
    recorded analysis, so nothing unreviewed can be published.

    Show the user `cells_to_change` and `sample_changes` and get approval. Then pass each entry
    of `write_blocks` to the Excel MCP server's write_data_to_excel with `filepath` set to
    `redacted_path`, and finally call finish_redaction.
    """
    drive = _drive()
    staging = _staging()
    analysis = _open_analysis(analysis_workbook)
    staged = await stage_workbook(drive, staging, workbook)
    hits = scan_path(staged.path, staged.info.file_id)
    plan = redaction.plan_redaction(staged, hits, analysis)
    copy = redaction.create_copy(staged, staging) if plan.edits else None

    return PlanResult(
        source_workbook_url=plan.source_url,
        redacted_path=str(copy) if copy else '',
        cells_to_change=plan.cells_to_change,
        write_blocks=[
            WriteBlockOut(sheet_name=block.sheet, start_cell=block.start_cell, data=block.values)
            for block in plan.blocks
        ],
        domains_anonymized=plan.mapped_domains,
        domains_left_as_is=plan.left_as_is,
        sample_changes=[
            RedactionCell(
                sheet=edit.sheet_title, cell=edit.a1, before=edit.before, after=edit.after
            )
            for edit in plan.edits[:10]
        ],
    )


@mcp.tool()
async def finish_redaction(
    workbook: WorkbookUrl,
    redacted_path: Annotated[
        str, Field(description='The `redacted_path` that plan_redaction returned.')
    ],
    analysis_workbook: AnalysisWorkbookPath = None,
    folder_id: Annotated[
        str | None,
        Field(
            description='Drive folder or shared drive id to upload into. Defaults to the '
            "user's My Drive."
        ),
    ] = None,
) -> FinishResult:
    """Verify the redacted copy, upload it to Drive, and record what was changed.

    Call this after applying every write block from plan_redaction with the Excel MCP server.
    It re-scans the local file first and refuses to publish if any domain that should have been
    replaced is still there — a produced plan does not prove the writes landed, since another
    server performs them.

    This is the step that publishes, so it is the one to get approval for. On success every
    rewritten cell is appended to the Redactions sheet of the analysis workbook. Check
    `remaining_domains`: it should hold exactly the domains analyzed as not needing
    anonymization.
    """
    drive = _drive()
    staging = _staging()
    analysis = _open_analysis(analysis_workbook)
    staged = await stage_workbook(drive, staging, workbook)
    hits = scan_path(staged.path, staged.info.file_id)
    # Recomputed from the source, so the record reflects the same plan the writes came from.
    plan = redaction.plan_redaction(staged, hits, analysis)

    path = Path(redacted_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f'{path} does not exist. Pass the redacted_path that plan_redaction returned, after '
            'the Excel MCP server has written to it.'
        )

    missed = redaction.verify(path, plan.mapped_domains)
    if missed:
        from .errors import RedactionNotApplied

        raise RedactionNotApplied(str(path), missed)

    uploaded = await drive.create(path.name, path.read_bytes(), parent_id=folder_id)
    from .drive import file_url

    redacted_url = file_url(uploaded.file_id) if uploaded.file_id else str(path)
    records = redaction.record(plan, redacted_url, analysis)
    remaining = unique_domains(scan_path(path, uploaded.file_id))

    return FinishResult(
        source_workbook_url=plan.source_url,
        redacted_workbook_url=redacted_url,
        redacted_workbook_title=uploaded.name or path.name,
        cells_changed=plan.cells_to_change,
        domains_anonymized=plan.mapped_domains,
        domains_left_as_is=plan.left_as_is,
        remaining_domains=remaining,
        redactions_recorded=len(records),
    )


def main() -> None:
    mcp.run()
