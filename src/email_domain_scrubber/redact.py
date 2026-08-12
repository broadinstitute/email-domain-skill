"""Producing an anonymized copy of a metrics workbook.

The source workbook is never modified, in Drive or on disk. Redaction copies the staged `.xlsx`,
and the **Excel MCP server** writes the replacement values into that copy — this module only
plans the writes and, afterwards, verifies that they landed.

Planning emits *blocks* rather than individual cells because `write_data_to_excel` takes a
rectangle of rows at an offset. Edits are grouped into runs of consecutive rows within a single
column, which is exactly the shape a column of email addresses produces: one block per column
per run, rather than one tool call per cell. Runs never extend across a gap, so no cell outside
the edit set is ever written.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from openpyxl.utils import get_column_letter

from .domains import apply_redactions
from .errors import UnanalyzedDomains
from .scan import ScanHit, StagedWorkbook, scan_path
from .staging import Staging
from .workbook import AnalysisWorkbook, RedactionRecord


@dataclass(frozen=True)
class CellEdit:
    sheet_title: str
    a1: str
    row: int
    column: int
    before: str
    after: str
    domains: tuple[str, ...]


@dataclass(frozen=True)
class WriteBlock:
    """One `write_data_to_excel` call: a column run of replacement values."""

    sheet: str
    start_cell: str
    values: list[list[str]]

    @property
    def cell_count(self) -> int:
        return len(self.values)


@dataclass
class RedactionPlan:
    """What redaction will do. Computing it touches no files."""

    source_url: str
    source_title: str
    edits: list[CellEdit]
    blocks: list[WriteBlock]
    mapped_domains: dict[str, str]
    left_as_is: list[str]

    @property
    def cells_to_change(self) -> int:
        return len(self.edits)


def plan_redaction(
    staged: StagedWorkbook, hits: list[ScanHit], analysis: AnalysisWorkbook
) -> RedactionPlan:
    """Decide which cells to rewrite, refusing while any domain is unanalyzed.

    Pure: no file is read or written here, so the plan can be recomputed cheaply — which
    `finish_redaction` does, to check the writes against the same plan that produced them.

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

    edits = _plan_edits(hits, mapping)
    return RedactionPlan(
        source_url=staged.url,
        source_title=staged.info.name,
        edits=edits,
        blocks=coalesce(edits),
        mapped_domains=mapping,
        left_as_is=left_as_is,
    )


def create_copy(staged: StagedWorkbook, staging: Staging) -> Path:
    """Copy the staged workbook to `<name> (anonymized).xlsx` for the Excel MCP server to edit.

    A byte copy, so everything the source workbook contains survives up to the point where
    openpyxl rewrites it.
    """
    destination = staging.anonymized_path(staged.info.file_id, staged.info.name)
    shutil.copy2(staged.path, destination)
    return destination


def _plan_edits(hits: list[ScanHit], mapping: dict[str, str]) -> list[CellEdit]:
    """One edit per cell, even when a cell holds several domains."""
    by_cell: dict[tuple[str, int, int], ScanHit] = {}
    for hit in hits:
        by_cell.setdefault((hit.sheet_title, hit.row, hit.column), hit)

    edits: list[CellEdit] = []
    for (sheet_title, row, column), hit in by_cell.items():
        after, replaced = apply_redactions(hit.cell_text, mapping)
        if replaced:
            edits.append(
                CellEdit(
                    sheet_title=sheet_title,
                    a1=hit.a1,
                    row=row,
                    column=column,
                    before=hit.cell_text,
                    after=after,
                    domains=tuple(replaced),
                )
            )
    return sorted(edits, key=lambda edit: (edit.sheet_title, edit.column, edit.row))


def coalesce(edits: list[CellEdit]) -> list[WriteBlock]:
    """Group edits into runs of consecutive rows within one column of one sheet.

    A run stops at the first row gap, so a block never covers a cell that is not being edited.

    Gaps are deliberately *not* bridged, even where the intervening cell's value is known from
    the scan. Bridging would cut the number of write calls when redacted cells alternate with
    kept ones, but it would also rewrite cells nobody approved changing — and since cells are
    read with cached values, writing one back would replace a formula with its result. The cost
    of not bridging is bounded by how many cells are being redacted, which for this workload is
    a small minority of any email column.
    """
    blocks: list[WriteBlock] = []
    run: list[CellEdit] = []

    def flush() -> None:
        if not run:
            return
        first = run[0]
        blocks.append(
            WriteBlock(
                sheet=first.sheet_title,
                start_cell=f'{get_column_letter(first.column)}{first.row}',
                values=[[edit.after] for edit in run],
            )
        )
        run.clear()

    for edit in sorted(edits, key=lambda e: (e.sheet_title, e.column, e.row)):
        contiguous = (
            run
            and edit.sheet_title == run[-1].sheet_title
            and edit.column == run[-1].column
            and edit.row == run[-1].row + 1
        )
        if not contiguous:
            flush()
        run.append(edit)
    flush()
    return blocks


def verify(path: Path, mapping: dict[str, str]) -> list[str]:
    """Domains from `mapping` that are still present in the written file.

    An external server does the writing now, so a produced plan no longer implies applied
    edits. This is what catches a write step that was skipped or only half ran.
    """
    present = {hit.domain for hit in scan_path(path, file_id='')}
    return [domain for domain in mapping if domain in present]


def record(
    plan: RedactionPlan, redacted_url: str, analysis: AnalysisWorkbook
) -> list[RedactionRecord]:
    """Append one Redactions row per (cell, domain) actually rewritten."""
    from .drive import cell_reference

    records = [
        RedactionRecord(
            source_url=plan.source_url,
            redacted_url=redacted_url,
            reference=cell_reference(_id_from(redacted_url), edit.sheet_title, edit.a1),
            domain=domain,
            anonymized_domain=plan.mapped_domains[domain],
        )
        for edit in plan.edits
        for domain in edit.domains
    ]
    analysis.record_redactions(records)
    return records


def _id_from(url: str) -> str:
    """The Drive id inside a file URL, or the URL itself if it is not one."""
    from .drive import parse_file_id
    from .errors import InvalidWorkbookReference

    try:
        return parse_file_id(url)
    except InvalidWorkbookReference:
        return url


__all__ = [
    'CellEdit',
    'RedactionPlan',
    'WriteBlock',
    'coalesce',
    'create_copy',
    'plan_redaction',
    'record',
    'verify',
]
