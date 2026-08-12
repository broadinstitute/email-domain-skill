"""Scanning a metrics workbook for email domain names.

A local `.xlsx` is read where it lies. A Drive reference is fetched through the MCP connector into
the local staging directory first. Either way the workbook is read with openpyxl. Only `.xlsx` is
in scope: the conversion dance that used to turn Drive uploads into native Google Sheets is gone,
and with it the stale-conversion and shared-drive lookup edge cases it needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import local, xlsx
from .domains import extract_domains
from .drive import Drive, FileInfo, cell_reference, file_url, parse_file_id
from .errors import UnsupportedWorkbook
from .staging import Staging


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
    """A workbook on local disk and ready to read — opened in place, or downloaded from Drive."""

    info: FileInfo
    path: Path
    downloaded: bool
    source_url: str = ''

    @property
    def url(self) -> str:
        return self.source_url or file_url(self.info.file_id)


async def stage_workbook(
    drive: Drive, staging: Staging, url_or_id: str, *, force: bool = False
) -> StagedWorkbook:
    """Make a `.xlsx` available on local disk.

    A local path is used where it lies: there is nothing to download and nothing to cache, and
    copying it would only create a second file to keep straight. A Drive reference is downloaded
    into the staging directory, reusing an up-to-date local copy.
    """
    if local.is_local_reference(url_or_id):
        path = local.resolve(url_or_id)
        return StagedWorkbook(
            info=local.info(path), path=path, downloaded=False, source_url=local.url(path)
        )

    file_id = parse_file_id(url_or_id)
    info = await drive.get_metadata(file_id)

    if not info.is_xlsx:
        raise UnsupportedWorkbook(
            f'{info.name!r} ({info.mime_type or "unknown type"}) is not an Excel workbook. This '
            'server handles .xlsx files only — convert Google Sheets and CSV files to .xlsx in '
            'Drive first (File > Download > Microsoft Excel, then upload).'
        )

    path = staging.workbook_path(file_id, info.name)
    if not force and staging.is_current(path, info.modified_time):
        return StagedWorkbook(info=info, path=path, downloaded=False)

    staging.write(path, await drive.download(file_id), info.modified_time)
    return StagedWorkbook(info=info, path=path, downloaded=True)


def scan_path(path: Path, file_id: str) -> list[ScanHit]:
    """Every domain occurrence in every cell of a local workbook, in reading order."""
    hits: list[ScanHit] = []
    for cell in xlsx.read_cells(path):
        if '.' not in cell.text:
            continue
        domains = extract_domains(cell.text)
        if not domains:
            continue
        reference = cell_reference(file_id, cell.sheet_title, cell.a1)
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
