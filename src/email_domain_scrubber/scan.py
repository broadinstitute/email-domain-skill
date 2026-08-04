"""Scanning a metrics workbook for email domain names."""

from __future__ import annotations

from dataclasses import dataclass

from .domains import extract_domains
from .errors import UnsupportedWorkbook
from .sheets import (
    CONVERTIBLE_MIMES,
    SPREADSHEET_MIME,
    SheetsBackend,
    SpreadsheetInfo,
    a1_cell,
    cell_link,
    parse_file_id,
)

_SPREADSHEET_SUFFIXES = ('.xlsx', '.xls', '.csv', '.tsv', '.ods')
CONVERTED_SUFFIX = ' (Sheets)'


@dataclass(frozen=True)
class ScanHit:
    """One domain occurrence in one cell."""

    sheet_title: str
    a1: str
    reference: str
    domain: str
    cell_text: str


@dataclass(frozen=True)
class ResolvedWorkbook:
    """A native Google Sheet ready to scan, plus how we got there."""

    info: SpreadsheetInfo
    source_file_id: str
    source_name: str
    converted_from_mime: str | None = None

    @property
    def converted(self) -> bool:
        return self.converted_from_mime is not None


def _converted_name(name: str) -> str:
    stem = name
    for suffix in _SPREADSHEET_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f'{stem}{CONVERTED_SUFFIX}'


def resolve_workbook(backend: SheetsBackend, url_or_id: str) -> ResolvedWorkbook:
    """Return a native Google Sheet for `url_or_id`, converting a Drive upload if necessary.

    XLSX/CSV uploads are not readable through the Sheets API and have no cell links, so they are
    converted to a native sheet named ``<name> (Sheets)`` next to the original. A previously
    converted sheet with that name is reused, so re-scanning does not litter Drive with copies.
    """
    file_id = parse_file_id(url_or_id)
    source = backend.get_file(file_id)

    if source.mime_type == SPREADSHEET_MIME:
        return ResolvedWorkbook(
            info=backend.get_spreadsheet(file_id), source_file_id=file_id, source_name=source.name
        )

    if source.mime_type not in CONVERTIBLE_MIMES:
        raise UnsupportedWorkbook(
            f'{source.name!r} ({source.mime_type}) is not a spreadsheet. Provide a Google Sheet, '
            'or an XLSX/CSV file stored in Drive.'
        )

    target_name = _converted_name(source.name)
    parent = source.parents[0] if source.parents else None
    existing = backend.find_file(target_name, parent)
    converted = (
        existing
        if existing is not None and existing.mime_type == SPREADSHEET_MIME
        else backend.copy_file(file_id, target_name, to_spreadsheet=True)
    )
    return ResolvedWorkbook(
        info=backend.get_spreadsheet(converted.file_id),
        source_file_id=file_id,
        source_name=source.name,
        converted_from_mime=source.mime_type,
    )


def scan_spreadsheet(backend: SheetsBackend, info: SpreadsheetInfo) -> list[ScanHit]:
    """Every domain occurrence in every cell of every sheet, in reading order."""
    titles = [sheet.title for sheet in info.sheets]
    sheet_ids = {sheet.title: sheet.sheet_id for sheet in info.sheets}

    hits: list[ScanHit] = []
    for block in backend.read_sheets(info.spreadsheet_id, titles):
        sheet_id = sheet_ids[block.sheet_title]
        for row_index, row in enumerate(block.values):
            for column_index, value in enumerate(row):
                text = '' if value is None else str(value)
                if '.' not in text:
                    continue
                domains = extract_domains(text)
                if not domains:
                    continue
                a1 = a1_cell(row_index, column_index)
                reference = cell_link(info.spreadsheet_id, sheet_id, a1)
                hits.extend(
                    ScanHit(
                        sheet_title=block.sheet_title,
                        a1=a1,
                        reference=reference,
                        domain=domain,
                        cell_text=text,
                    )
                    for domain in domains
                )
    return hits


def unique_domains(hits: list[ScanHit]) -> list[str]:
    return list(dict.fromkeys(hit.domain for hit in hits))
