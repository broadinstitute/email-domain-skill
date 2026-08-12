"""Scanning a metrics workbook for email domain names.

The workbook is a local `.xlsx`, read where it lies with openpyxl. Only `.xlsx` is in scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import local, xlsx
from .domains import extract_domains


@dataclass(frozen=True)
class ScanHit:
    """One domain occurrence in one cell."""

    sheet_title: str
    a1: str
    reference: str
    domain: str
    cell_text: str
    row: int
    column: int


@dataclass(frozen=True)
class StagedWorkbook:
    """A metrics workbook on local disk, ready to read."""

    path: Path

    @property
    def url(self) -> str:
        return local.url(self.path)

    @property
    def title(self) -> str:
        return self.path.name


def open_workbook(reference: str) -> StagedWorkbook:
    """Resolve a reference to a readable local `.xlsx`.

    The file is used where it lies: there is nothing to download and nothing to cache, and copying
    it up front would only create a second file to keep straight.
    """
    return StagedWorkbook(path=local.resolve(reference))


def scan_path(path: Path, source_url: str) -> list[ScanHit]:
    """Every domain occurrence in every cell of a local workbook, in reading order."""
    hits: list[ScanHit] = []
    for cell in xlsx.read_cells(path):
        if '.' not in cell.text:
            continue
        domains = extract_domains(cell.text)
        if not domains:
            continue
        reference = local.cell_reference(source_url, cell.sheet_title, cell.a1)
        hits.extend(
            ScanHit(
                sheet_title=cell.sheet_title,
                a1=cell.a1,
                reference=reference,
                domain=domain,
                cell_text=cell.text,
                row=cell.row,
                column=cell.column,
            )
            for domain in domains
        )
    return hits


def unique_domains(hits: list[ScanHit]) -> list[str]:
    return list(dict.fromkeys(hit.domain for hit in hits))
