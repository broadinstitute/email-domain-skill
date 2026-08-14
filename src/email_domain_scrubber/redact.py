"""Producing an anonymized copy of a metrics workbook.

The source workbook on disk is never modified. Redaction byte-copies it, writes the replacement
values into the copy, reads the copy back to check the writes landed, and records what changed.

All of that happens here, in one process. The substitutions are not decided here: they are read
from the analysis workbook's ``AnonymizedDomain`` column, which is the whole of the plan. A
domain with a token is replaced, a domain without one is left alone, and nothing else — not the
risk level, not the skill, not an argument to these functions — gets a say.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import local, xlsx
from .domains import apply_redactions
from .errors import RedactionNotApplied, UnanalyzedDomains
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


@dataclass
class RedactionPlan:
    """What redaction will do. Computing it touches no files."""

    source_path: str
    source_title: str
    edits: list[CellEdit]
    mapped_domains: dict[str, str]
    left_as_is: list[str]

    @property
    def cells_to_change(self) -> int:
        return len(self.edits)


def plan_redaction(
    staged: StagedWorkbook, hits: list[ScanHit], analysis: AnalysisWorkbook
) -> RedactionPlan:
    """Decide which cells to rewrite, refusing while any domain is unanalyzed.

    Pure: no file is read or written here, so the plan can be recomputed cheaply — which the
    apply step does, rather than trusting a plan handed back to it.

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

    return RedactionPlan(
        source_path=str(staged.path),
        source_title=staged.title,
        edits=_plan_edits(hits, mapping),
        mapped_domains=mapping,
        left_as_is=left_as_is,
    )


def create_copy(staged: StagedWorkbook, staging: Staging) -> Path:
    """Copy the staged workbook to `<name> (anonymized).xlsx`, ready to be written into.

    A byte copy, so everything the source workbook contains survives up to the point where
    openpyxl rewrites it.
    """
    destination = staging.anonymized_path(str(staged.path), staged.path.name)
    shutil.copy2(staged.path, destination)
    return destination


def apply(plan: RedactionPlan, copy: Path) -> int:
    """Write every planned edit into `copy`, and return how many cells were written.

    Only the planned cells are touched. A cell that was scanned but not edited is left exactly as
    the byte copy found it, so nothing outside the approved set is rewritten — which matters
    because cells are read with cached values, and writing one back would replace a formula with
    its result.
    """
    by_sheet: dict[str, list[tuple[int, int, str]]] = {}
    for edit in plan.edits:
        by_sheet.setdefault(edit.sheet_title, []).append((edit.row, edit.column, edit.after))
    if not by_sheet:
        return 0
    return xlsx.write_cells(copy, by_sheet)


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


def verify(path: Path, mapping: dict[str, str]) -> list[str]:
    """Domains from `mapping` that are still present in the written file."""
    present = {hit.domain for hit in scan_path(path, source_url='')}
    return [domain for domain in mapping if domain in present]


def redact(
    staged: StagedWorkbook, hits: list[ScanHit], analysis: AnalysisWorkbook, staging: Staging
) -> tuple[RedactionPlan, Path, list[RedactionRecord]]:
    """Plan, copy, write, verify, and record — the whole redaction, in that order.

    Verification is a re-read of the file just written, not a check of the plan against itself:
    producing the right values proves nothing about whether they reached the disk. If any mapped
    domain survives, this raises and records nothing, leaving the copy for inspection rather than
    an audit trail asserting a redaction that did not happen.
    """
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)
    apply(plan, copy)

    missed = verify(copy, plan.mapped_domains)
    if missed:
        raise RedactionNotApplied(str(copy), missed)

    return plan, copy, record(plan, copy, analysis)


def record(
    plan: RedactionPlan, redacted_path: Path, analysis: AnalysisWorkbook
) -> list[RedactionRecord]:
    """Append one Redactions row per (cell, domain) rewritten, with the cell's before and after.

    A cell holding two anonymized domains produces two rows sharing the same before and after:
    keeping `Domain` a single value is what lets the log be joined against `DomainAnalysis`.
    """
    redacted_url = local.url(redacted_path)
    records = [
        RedactionRecord(
            source_path=plan.source_path,
            redacted_path=str(redacted_path),
            reference=local.cell_reference(redacted_url, edit.sheet_title, edit.a1),
            domain=domain,
            anonymized_domain=plan.mapped_domains[domain],
            before=edit.before,
            after=edit.after,
        )
        for edit in plan.edits
        for domain in edit.domains
    ]
    analysis.record_redactions(records)
    return records


__all__ = [
    'CellEdit',
    'RedactionPlan',
    'apply',
    'create_copy',
    'plan_redaction',
    'record',
    'redact',
    'verify',
]
