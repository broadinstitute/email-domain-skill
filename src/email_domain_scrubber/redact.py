"""Producing an anonymized copy of a metrics workbook.

The source workbook is never modified. Redaction copies it, rewrites the domain cells in the
copy, and records every rewrite in the analysis workbook's ``Redactions`` sheet.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domains import apply_redactions
from .errors import UnanalyzedDomains
from .scan import ResolvedWorkbook, ScanHit, scan_spreadsheet
from .sheets import SheetsBackend, SpreadsheetInfo, cell_link, quote_sheet_title, spreadsheet_url
from .workbook import AnalysisWorkbook, RedactionRecord

ANONYMIZED_SUFFIX = ' (anonymized)'

#: Ranges per values.batchUpdate request. Keeps a wide scan from building one enormous payload.
_WRITE_CHUNK = 500


@dataclass(frozen=True)
class CellEdit:
    sheet_title: str
    a1: str
    before: str
    after: str
    domains: tuple[str, ...]


@dataclass
class RedactionPlan:
    """What redaction would do, computed before anything is copied or written."""

    source_url: str
    source_title: str
    edits: list[CellEdit]
    mapped_domains: dict[str, str]
    left_as_is: list[str]

    @property
    def cells_to_change(self) -> int:
        return len(self.edits)


def plan_redaction(
    resolved: ResolvedWorkbook, hits: list[ScanHit], analysis: AnalysisWorkbook
) -> RedactionPlan:
    """Decide which cells to rewrite, refusing to proceed while any domain is unanalyzed.

    Domains analyzed as not needing anonymization have no token and are deliberately left
    untouched — that is the approved outcome, not an omission.
    """
    by_domain = analysis.analysis_by_domain()
    found = list(dict.fromkeys(hit.domain for hit in hits))

    unanalyzed = [
        domain for domain in found if domain not in by_domain or not by_domain[domain].analyzed
    ]
    if unanalyzed:
        raise UnanalyzedDomains(unanalyzed)

    assigned = analysis.anonymized_mapping()
    mapping = {domain: assigned[domain] for domain in found if domain in assigned}
    left_as_is = [domain for domain in found if domain not in mapping]

    # One edit per cell, even when a cell holds several domains.
    by_cell: dict[tuple[str, str], str] = {}
    for hit in hits:
        by_cell.setdefault((hit.sheet_title, hit.a1), hit.cell_text)

    edits: list[CellEdit] = []
    for (sheet_title, a1), before in by_cell.items():
        after, replaced = apply_redactions(before, mapping)
        if replaced:
            edits.append(
                CellEdit(
                    sheet_title=sheet_title,
                    a1=a1,
                    before=before,
                    after=after,
                    domains=tuple(replaced),
                )
            )

    return RedactionPlan(
        source_url=spreadsheet_url(resolved.info.spreadsheet_id),
        source_title=resolved.info.title,
        edits=edits,
        mapped_domains=mapping,
        left_as_is=left_as_is,
    )


def unique_copy_name(backend: SheetsBackend, base_name: str, parent_id: str | None) -> str:
    """`base_name`, or `base_name 2`, `base_name 3`, ... if that name is taken.

    Never reuses or overwrites an existing file: an older anonymized copy may already be
    published, and re-redacting an already-redacted sheet would be a silent no-op.
    """
    if backend.find_file(base_name, parent_id) is None:
        return base_name
    for suffix in range(2, 100):
        candidate = f'{base_name} {suffix}'
        if backend.find_file(candidate, parent_id) is None:
            return candidate
    raise RuntimeError(f'Could not find an unused name based on {base_name!r} after 99 tries.')


@dataclass
class RedactionResult:
    plan: RedactionPlan
    redacted_url: str
    redacted_title: str
    records: list[RedactionRecord]


def execute_redaction(
    backend: SheetsBackend,
    resolved: ResolvedWorkbook,
    plan: RedactionPlan,
    analysis: AnalysisWorkbook,
) -> RedactionResult:
    """Copy the workbook, rewrite the planned cells in the copy, and record what changed."""
    source_file = backend.get_file(resolved.info.spreadsheet_id)
    parent = source_file.parents[0] if source_file.parents else None
    name = unique_copy_name(backend, f'{resolved.info.title}{ANONYMIZED_SUFFIX}', parent)
    copy = backend.copy_file(resolved.info.spreadsheet_id, name)

    # Sheet ids are not guaranteed to survive a Drive copy, so re-read them and key by title.
    copy_info = backend.get_spreadsheet(copy.file_id)
    sheet_ids = {sheet.title: sheet.sheet_id for sheet in copy_info.sheets}

    updates = {
        f'{quote_sheet_title(edit.sheet_title)}!{edit.a1}': [[edit.after]] for edit in plan.edits
    }
    for chunk in _chunked(list(updates.items()), _WRITE_CHUNK):
        backend.write_ranges(copy.file_id, dict(chunk))

    redacted_url = spreadsheet_url(copy.file_id)
    records = [
        RedactionRecord(
            source_url=plan.source_url,
            redacted_url=redacted_url,
            reference=cell_link(copy.file_id, sheet_ids[edit.sheet_title], edit.a1),
            domain=domain,
            anonymized_domain=plan.mapped_domains[domain],
        )
        for edit in plan.edits
        for domain in edit.domains
    ]
    analysis.record_redactions(records)

    return RedactionResult(
        plan=plan, redacted_url=redacted_url, redacted_title=name, records=records
    )


def _chunked(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def rescan_for_verification(backend: SheetsBackend, info: SpreadsheetInfo) -> list[str]:
    """Domains still present after redaction, for the caller to sanity-check the result."""
    return list(dict.fromkeys(hit.domain for hit in scan_spreadsheet(backend, info)))


__all__ = [
    'ANONYMIZED_SUFFIX',
    'CellEdit',
    'RedactionPlan',
    'RedactionResult',
    'execute_redaction',
    'plan_redaction',
    'rescan_for_verification',
    'unique_copy_name',
]
