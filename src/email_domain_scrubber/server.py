"""MCP server for scanning, researching, recording, and anonymizing email domains in Excel
metrics reports.

Division of labour, across two parts:

* **This server** does everything factual and everything that writes — reading the workbook from
  disk, finding domains, researching them, persisting verdicts, minting aliases, rewriting the
  redacted copy, verifying it, and recording what changed.
* **The analysis skill** does the judgement — reading the evidence this server gathers, deciding
  each domain's `Risk`, and getting the user's approval. It runs no searches of its own and
  writes to no spreadsheet.

The analysis workbook is the boundary between them. Its `AnonymizedDomain` column *is* the
redaction plan, so the user can edit the sheet between analysis and redaction and have the edit
take effect.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from . import redact as redaction
from . import research as osint
from .scan import open_workbook, scan_path, unique_domains
from .staging import ANALYSIS_WORKBOOK_ENV, Staging, analysis_workbook_path
from .workbook import RISKS, AnalysisWorkbook, DomainReference, today

mcp = MCPServer(
    'email-domain-scrubber',
    instructions=(
        'Tools for the email domain privacy workflow on local Excel (.xlsx) usage metric '
        'reports. Order: scan_workbook -> list_domains_for_analysis -> research_domains -> '
        '(judge, then get user approval) -> store_domain_analysis -> let the user review and '
        'edit the analysis workbook -> plan_redaction -> apply_redaction. This server performs '
        'all research and all writes; the caller judges risk and talks to the user. The analysis '
        'workbook is a local .xlsx holding the durable record, and its AnonymizedDomain column '
        'is the redaction plan; pass its path to every tool or set the '
        f'{ANALYSIS_WORKBOOK_ENV} environment variable.'
    ),
)

_staging_override: Staging | None = None


def set_backend(staging: Staging | None = None) -> None:
    """Inject a work directory (used by tests); `None` restores the default."""
    global _staging_override
    _staging_override = staging


def _staging() -> Staging:
    return _staging_override if _staging_override is not None else Staging()


def _open_analysis(analysis_workbook: str | None) -> AnalysisWorkbook:
    return AnalysisWorkbook.open(analysis_workbook_path(analysis_workbook))


WorkbookPath = Annotated[
    str,
    Field(
        description='Path to the local .xlsx metrics workbook. Relative paths, absolute paths, '
        'and ~ all work. Google Sheets and CSV files are not supported; convert them to .xlsx '
        'first. This server reads from disk only — fetch remote files yourself and pass the path.'
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
    scanned_workbook_path: str
    scanned_workbook_title: str
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


class PlanResult(BaseModel):
    source_workbook_path: str
    cells_to_change: int
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
    aliases_minted: dict[str, str] = Field(
        default_factory=dict,
        description='Aliases created just now for rows hand-edited to High risk without one. '
        'Empty in the normal case; non-empty means the analysis workbook was edited after '
        'store_domain_analysis ran, and those edits are now in effect.',
    )


class RegistrationOut(BaseModel):
    """What RDAP published. Usually less than you would hope — see `registrant_name`."""

    status: Literal['found', 'not_found', 'unavailable']
    registrant_name: str = Field(
        default='',
        description='Almost always empty for .com/.org/.net: since GDPR those registries redact '
        'the registrant entirely. An empty value is the norm and means nothing either way. A '
        'value here, which some ccTLDs still publish, is strong evidence.',
    )
    registrant_organization: str = ''
    registrant_kind: str = Field(
        default='', description="The registry's vCard kind, e.g. 'individual' or 'org'."
    )
    privacy_shielded: bool = Field(
        default=False,
        description='The registrant field holds a privacy service rather than a name. Weak '
        'evidence of an individual, not proof — organizations shield too. Say so if you rely '
        'on it.',
    )
    registered_on: str = Field(
        default='', description='Registration date. Context, not evidence of who.'
    )
    registrar: str = Field(
        default='',
        description='Usually published even when the registrant is not. A consumer registrar is '
        'weak evidence of an individual; a corporate brand-protection one, of a company.',
    )
    detail: str = Field(default='', description='Why a lookup came back empty, when it did.')


class LiteratureHitOut(BaseModel):
    title: str = ''
    authors: str = ''
    first_author: str = ''
    year: str = ''
    journal: str = ''
    doi: str = ''
    is_preprint: bool = False


class LiteratureOut(BaseModel):
    status: Literal['found', 'not_found', 'unavailable']
    hit_count: int = 0
    hits: list[LiteratureHitOut] = Field(default_factory=list)
    distinct_first_authors: list[str] = Field(
        default_factory=list,
        description='First authors across the hits. One name over several papers points at a '
        'single-principal domain; many names point at an institution.',
    )
    detail: str = ''


class DomainEvidenceOut(BaseModel):
    """The evidence for one domain. Judgement is the caller's; this is only what was found."""

    domain: str
    registration: RegistrationOut
    literature: LiteratureOut
    resolved: bool = Field(
        description='Whether any source said anything substantive. False means unidentifiable on '
        'the evidence available — classify conservatively and tell the user it is unresolved.'
    )
    not_searched: list[str] = Field(
        default_factory=list,
        description='Sources this server did not consult. Do not search them yourself; say what '
        'is missing instead.',
    )


class ApplyResult(BaseModel):
    source_workbook_path: str
    redacted_workbook_path: str = Field(description='The redacted .xlsx on disk.')
    redacted_workbook_title: str
    cells_changed: int
    domains_anonymized: dict[str, str] = Field(default_factory=dict)
    domains_left_as_is: list[str] = Field(default_factory=list)
    remaining_domains: list[str] = Field(
        default_factory=list,
        description='Domains still present in the redacted copy — the ones left as is, plus '
        'anything a re-scan turned up. Verify this looks expected before sharing the file.',
    )
    redactions_recorded: int


# -- tools -------------------------------------------------------------------------------------
@mcp.tool()
def scan_workbook(
    workbook: WorkbookPath, analysis_workbook: AnalysisWorkbookPath = None
) -> ScanResult:
    """Find every email domain in an Excel metrics workbook and record it for analysis.

    Reads a local .xlsx in place, then reads all cells of all sheets, extracting domains from
    email addresses and from bare domain values. Each occurrence is appended to DomainReferences
    with a locator for its cell, and each unique domain gets a DomainAnalysis row with an empty
    Risk. The source workbook is never modified.

    Re-scanning the same workbook is safe: existing references and verdicts are preserved. The
    analysis workbook is created if absent.
    """
    analysis = _open_analysis(analysis_workbook)
    staged = open_workbook(workbook)

    hits = scan_path(staged.path, staged.url)
    domains = unique_domains(hits)
    date = today()

    analysis.record_workbook(str(staged.path), staged.title)
    recorded = analysis.record_references(
        [
            DomainReference(reference=hit.reference, domain=hit.domain, date_extracted=date)
            for hit in hits
        ]
    )
    new_domains = analysis.ensure_analysis_rows(domains)

    return ScanResult(
        scanned_workbook_path=str(staged.path),
        scanned_workbook_title=staged.title,
        analysis_workbook_path=analysis.location,
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


@mcp.tool()
def research_domains(
    domains: Annotated[
        list[str],
        Field(
            description='Domains to research, as listed by list_domains_for_analysis. Batch them '
            'in one call.',
            min_length=1,
            max_length=60,
        ),
    ],
) -> list[DomainEvidenceOut]:
    """Gather the registration and publication evidence for domains, from this server.

    Two sources, both free and unauthenticated: RDAP (the structured successor to WHOIS) for who
    registered the domain and whether the registrant is an organization, a person, or a privacy
    shield; and Europe PMC for the scientific literature, PubMed and bioRxiv/medRxiv preprints
    together, searched full text.

    This is the only research the workflow does. Do not run web searches or fetch pages yourself:
    a verdict has to rest on evidence that is the same from one run to the next, and `not_searched`
    names the sources nobody consulted so an explanation can say so honestly. Reasoning from what
    you already know about a well-known institution is fine and needs no lookup.

    Each source degrades on its own — a timeout or a registry that does not answer RDAP comes back
    as a status and a reason, not an error. A domain with `resolved: false` is unidentifiable on
    this evidence: classify it conservatively and flag it to the user rather than inventing a
    rationale.
    """
    return [_evidence(item) for item in osint.research_domains(domains)]


def _evidence(found: osint.DomainEvidence) -> DomainEvidenceOut:
    registration = found.registration
    literature = found.literature
    return DomainEvidenceOut(
        domain=found.domain,
        registration=RegistrationOut(
            status=registration.status,
            registrant_name=registration.registrant_name,
            registrant_organization=registration.registrant_organization,
            registrant_kind=registration.registrant_kind,
            privacy_shielded=registration.privacy_shielded,
            registered_on=registration.registered_on,
            registrar=registration.registrar,
            detail=registration.detail,
        ),
        literature=LiteratureOut(
            status=literature.status,
            hit_count=literature.hit_count,
            hits=[
                LiteratureHitOut(
                    title=hit.title,
                    authors=hit.authors,
                    first_author=hit.first_author,
                    year=hit.year,
                    journal=hit.journal,
                    doi=hit.doi,
                    is_preprint=hit.is_preprint,
                )
                for hit in literature.hits
            ],
            distinct_first_authors=literature.distinct_first_authors,
            detail=literature.detail,
        ),
        resolved=found.resolved,
        not_searched=found.not_searched,
    )


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

    Afterwards, point the user at `analysis_workbook_path` and let them review it before
    redaction. They may edit it: `AnonymizedDomain` is the plan, so clearing a token spares a
    domain, and a Risk edited up to High gets a token minted by plan_redaction.
    """
    analysis = _open_analysis(analysis_workbook)
    stored = analysis.store_analysis(
        [(entry.domain, entry.risk, entry.explanation, entry.anonymize) for entry in analyses]
    )
    return StoreResult(
        analysis_workbook_path=analysis.location,
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
def plan_redaction(
    workbook: WorkbookPath, analysis_workbook: AnalysisWorkbookPath = None
) -> PlanResult:
    """Preview what redaction will change, reading the report and the analysis workbook.

    Writes nothing to the report and creates no copy. The substitutions come entirely from the
    analysis workbook's `AnonymizedDomain` column — that column is the plan. Domains with no alias
    are left in place on purpose, and a domain in the report with no recorded analysis at all is
    refused, so nothing unreviewed can reach a shared report.

    The one thing this does write is to the analysis workbook itself, and only to make it
    self-consistent: a row the user hand-edited to High risk without an alias gets one, reported
    back as `aliases_minted`. Clearing an alias is left alone — that is how the user says "leave
    this domain in".

    Show the user `cells_to_change` and `sample_changes`, get approval, then call apply_redaction.
    """
    analysis = _open_analysis(analysis_workbook)
    staged = open_workbook(workbook)
    hits = scan_path(staged.path, staged.url)

    minted = analysis.reconcile_aliases(unique_domains(hits))
    plan = redaction.plan_redaction(staged, hits, analysis)

    return PlanResult(
        source_workbook_path=plan.source_path,
        cells_to_change=plan.cells_to_change,
        domains_anonymized=plan.mapped_domains,
        domains_left_as_is=plan.left_as_is,
        sample_changes=[
            RedactionCell(
                sheet=edit.sheet_title, cell=edit.a1, before=edit.before, after=edit.after
            )
            for edit in plan.edits[:10]
        ],
        aliases_minted=minted,
    )


@mcp.tool()
def apply_redaction(
    workbook: WorkbookPath, analysis_workbook: AnalysisWorkbookPath = None
) -> ApplyResult:
    """Write the anonymized copy of the report, verify it, and record what changed.

    Call this after plan_redaction and after the user has approved. Everything happens here: the
    report is byte-copied to `<name> (anonymized).xlsx` in the work directory, the planned cells
    are written into the copy, the copy is read back, and every rewritten cell is appended to the
    Redactions sheet with its before and after.

    The plan is recomputed from the report and the analysis workbook rather than taken as an
    argument, so what gets written is whatever the workbook says at this moment — including any
    edit the user made while reviewing it. If the report has changed since plan_redaction, the
    result will differ from the preview; the counts returned describe what was actually done.

    Verification is a re-read of the file just written. If any domain that should have been
    replaced is still present, this raises and records nothing, leaving the copy on disk to
    inspect. The source report is never modified and nothing leaves the machine — sharing the copy
    is the user's to do. Check `remaining_domains`: it should hold exactly the domains analyzed as
    not needing anonymization.
    """
    analysis = _open_analysis(analysis_workbook)
    staged = open_workbook(workbook)
    hits = scan_path(staged.path, staged.url)

    analysis.reconcile_aliases(unique_domains(hits))
    plan, copy, records = redaction.redact(staged, hits, analysis, _staging())
    remaining = unique_domains(scan_path(copy, source_url=''))

    return ApplyResult(
        source_workbook_path=plan.source_path,
        redacted_workbook_path=str(copy),
        redacted_workbook_title=copy.name,
        cells_changed=plan.cells_to_change,
        domains_anonymized=plan.mapped_domains,
        domains_left_as_is=plan.left_as_is,
        remaining_domains=remaining,
        redactions_recorded=len(records),
    )


def main() -> None:
    mcp.run()
