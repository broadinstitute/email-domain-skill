"""Thin wrappers over the Google Sheets and Drive REST APIs.

Every Google call the package makes goes through `SheetsBackend`, so the scan, analysis and
redaction logic can be exercised against an in-memory fake (see `tests/fakes.py`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import (
    AccessDenied,
    InvalidWorkbookReference,
    MissingScopes,
    ScrubberError,
    WorkbookNotFound,
)

SPREADSHEET_MIME = 'application/vnd.google-apps.spreadsheet'

#: Drive file types Google can convert to a native Sheet. The Sheets API only reads native
#: sheets, and cell links only exist for native sheets, so uploads are converted before scanning.
CONVERTIBLE_MIMES = frozenset(
    {
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.ms-excel',
        'application/vnd.oasis.opendocument.spreadsheet',
        'application/x-vnd.oasis.opendocument.spreadsheet',
        'text/csv',
        'text/tab-separated-values',
    }
)

_SHEETS_URL_ID = re.compile(r'/spreadsheets/d/([A-Za-z0-9_-]+)')
_DRIVE_URL_ID = re.compile(r'(?:/file/d/|[?&]id=)([A-Za-z0-9_-]+)')
_BARE_ID = re.compile(r'^[A-Za-z0-9_-]{20,}$')


def parse_file_id(url_or_id: str) -> str:
    """Extract a Drive file id from a Sheets/Drive URL, or accept a bare id."""
    value = (url_or_id or '').strip()
    if not value:
        raise InvalidWorkbookReference('No workbook URL or id was provided.')
    for pattern in (_SHEETS_URL_ID, _DRIVE_URL_ID):
        match = pattern.search(value)
        if match:
            return match.group(1)
    if _BARE_ID.match(value):
        return value
    raise InvalidWorkbookReference(
        f'{value!r} is not a Google Sheets URL, Drive URL, or file id. Expected something like '
        'https://docs.google.com/spreadsheets/d/<id>/edit'
    )


def column_letter(index: int) -> str:
    """0-based column index to an A1 column label (0 -> A, 26 -> AA)."""
    if index < 0:
        raise ValueError(f'column index must be non-negative, got {index}')
    letters = ''
    remaining = index + 1
    while remaining:
        remaining, offset = divmod(remaining - 1, 26)
        letters = chr(ord('A') + offset) + letters
    return letters


def a1_cell(row: int, column: int) -> str:
    """0-based (row, column) to an A1 cell address."""
    return f'{column_letter(column)}{row + 1}'


def quote_sheet_title(title: str) -> str:
    """Quote a sheet title for use in an A1 range."""
    return "'" + title.replace("'", "''") + "'"


def spreadsheet_url(spreadsheet_id: str) -> str:
    return f'https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit'


def cell_link(spreadsheet_id: str, sheet_id: int, a1: str) -> str:
    """A direct link that opens the spreadsheet with `a1` selected."""
    return f'{spreadsheet_url(spreadsheet_id)}#gid={sheet_id}&range={a1}'


@dataclass(frozen=True)
class SheetInfo:
    sheet_id: int
    title: str


@dataclass(frozen=True)
class SpreadsheetInfo:
    spreadsheet_id: str
    title: str
    sheets: tuple[SheetInfo, ...]

    def sheet(self, title: str) -> SheetInfo | None:
        return next((s for s in self.sheets if s.title == title), None)


@dataclass(frozen=True)
class FileInfo:
    file_id: str
    name: str
    mime_type: str
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValueRange:
    """A block of formatted cell values anchored at the top-left of a sheet."""

    sheet_title: str
    values: list[list[str]]


class SheetsBackend(Protocol):
    """The Google surface this package depends on."""

    def get_file(self, file_id: str) -> FileInfo: ...

    def find_file(self, name: str, parent_id: str | None) -> FileInfo | None: ...

    def copy_file(self, file_id: str, name: str, *, to_spreadsheet: bool = False) -> FileInfo: ...

    def move_to_folder(self, file_id: str, folder_id: str) -> FileInfo: ...

    def get_spreadsheet(self, spreadsheet_id: str) -> SpreadsheetInfo: ...

    def create_spreadsheet(self, title: str, sheet_titles: list[str]) -> SpreadsheetInfo: ...

    def add_sheets(self, spreadsheet_id: str, sheet_titles: list[str]) -> SpreadsheetInfo: ...

    def read_sheets(self, spreadsheet_id: str, sheet_titles: list[str]) -> list[ValueRange]: ...

    def write_ranges(self, spreadsheet_id: str, updates: dict[str, list[list[str]]]) -> None: ...

    def append_rows(self, spreadsheet_id: str, sheet_title: str, rows: list[list[str]]) -> None: ...


#: Retried with backoff: Google's rate limits and transient backend failures.
_RETRY_STATUSES = frozenset({429, 500, 503})
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0


def _execute(request: Any) -> Any:
    """Run a googleapiclient request, translating Google's errors into `ScrubberError`s.

    Without this, a caller missing an OAuth scope gets a raw `HttpError` traceback from inside
    the client library rather than being told how to fix it — and a missing scope is the single
    most likely first-run failure.
    """
    from googleapiclient.errors import HttpError

    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, 'status', None)
            if status in _RETRY_STATUSES and attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BASE_DELAY * 2**attempt)
                continue
            raise _translate(exc, status) from exc
    raise AssertionError('unreachable')  # pragma: no cover


def _translate(exc: Any, status: int | None) -> Exception:
    detail = str(getattr(exc, 'reason', '') or exc)
    if status == 403:
        # Google reports `insufficientPermissions` for a missing scope *and* for a file the user
        # cannot reach, so only the message distinguishes them.
        if 'scope' in detail.lower():
            return MissingScopes(detail)
        return AccessDenied(
            f'Google denied access: {detail}. The server acts as the signed-in user, so make '
            'sure that account can open and edit the workbook.'
        )
    if status == 404:
        return WorkbookNotFound(
            f'Google could not find the file: {detail}. Check the URL, and that the file is '
            'shared with the signed-in account.'
        )
    if status in _RETRY_STATUSES:
        return ScrubberError(f'Google is rate-limiting or unavailable after retries: {detail}')
    return exc


class GoogleBackend:
    """`SheetsBackend` backed by the live Sheets and Drive v3 APIs."""

    def __init__(self, sheets_service: Any, drive_service: Any) -> None:
        self._sheets = sheets_service
        self._drive = drive_service

    # -- Drive ---------------------------------------------------------------------------
    _FILE_FIELDS = 'id, name, mimeType, parents'

    @staticmethod
    def _file(payload: dict[str, Any]) -> FileInfo:
        return FileInfo(
            file_id=payload['id'],
            name=payload.get('name', ''),
            mime_type=payload.get('mimeType', ''),
            parents=tuple(payload.get('parents', ())),
        )

    def get_file(self, file_id: str) -> FileInfo:
        payload = _execute(
            self._drive.files().get(
                fileId=file_id, fields=self._FILE_FIELDS, supportsAllDrives=True
            )
        )
        return self._file(payload)

    def find_file(self, name: str, parent_id: str | None) -> FileInfo | None:
        escaped = name.replace('\\', '\\\\').replace("'", "\\'")
        clauses = [f"name = '{escaped}'", 'trashed = false']
        if parent_id:
            clauses.append(f"'{parent_id}' in parents")
        payload = _execute(
            self._drive.files().list(
                q=' and '.join(clauses),
                fields=f'files({self._FILE_FIELDS})',
                pageSize=2,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                # Without this the search covers only My Drive, so a workbook on a shared drive
                # reads as absent — which would silently re-convert uploads on every scan and
                # let a second redaction reuse an already-published name.
                corpora='allDrives',
            )
        )
        files = payload.get('files', [])
        return self._file(files[0]) if files else None

    def copy_file(self, file_id: str, name: str, *, to_spreadsheet: bool = False) -> FileInfo:
        body: dict[str, Any] = {'name': name}
        if to_spreadsheet:
            body['mimeType'] = SPREADSHEET_MIME
        payload = _execute(
            self._drive.files().copy(
                fileId=file_id, body=body, fields=self._FILE_FIELDS, supportsAllDrives=True
            )
        )
        return self._file(payload)

    def move_to_folder(self, file_id: str, folder_id: str) -> FileInfo:
        """Reparent a file, e.g. to put a newly created workbook on a shared drive."""
        current = self.get_file(file_id)
        payload = _execute(
            self._drive.files().update(
                fileId=file_id,
                addParents=folder_id,
                removeParents=','.join(current.parents) if current.parents else None,
                fields=self._FILE_FIELDS,
                supportsAllDrives=True,
            )
        )
        return self._file(payload)

    # -- Sheets --------------------------------------------------------------------------
    @staticmethod
    def _spreadsheet(payload: dict[str, Any]) -> SpreadsheetInfo:
        return SpreadsheetInfo(
            spreadsheet_id=payload['spreadsheetId'],
            title=payload.get('properties', {}).get('title', ''),
            sheets=tuple(
                SheetInfo(
                    sheet_id=sheet['properties']['sheetId'], title=sheet['properties']['title']
                )
                for sheet in payload.get('sheets', [])
            ),
        )

    def get_spreadsheet(self, spreadsheet_id: str) -> SpreadsheetInfo:
        payload = _execute(
            self._sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields='spreadsheetId,properties.title,sheets.properties(sheetId,title)',
            )
        )
        return self._spreadsheet(payload)

    def create_spreadsheet(self, title: str, sheet_titles: list[str]) -> SpreadsheetInfo:
        body = {
            'properties': {'title': title},
            'sheets': [{'properties': {'title': name}} for name in sheet_titles],
        }
        payload = _execute(
            self._sheets.spreadsheets().create(
                body=body, fields='spreadsheetId,properties.title,sheets.properties(sheetId,title)'
            )
        )
        return self._spreadsheet(payload)

    def add_sheets(self, spreadsheet_id: str, sheet_titles: list[str]) -> SpreadsheetInfo:
        requests = [{'addSheet': {'properties': {'title': name}}} for name in sheet_titles]
        _execute(
            self._sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={'requests': requests}
            )
        )
        return self.get_spreadsheet(spreadsheet_id)

    def read_sheets(self, spreadsheet_id: str, sheet_titles: list[str]) -> list[ValueRange]:
        if not sheet_titles:
            return []
        payload = _execute(
            self._sheets.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=[quote_sheet_title(title) for title in sheet_titles],
                valueRenderOption='FORMATTED_VALUE',
                majorDimension='ROWS',
            )
        )
        ranges = payload.get('valueRanges', [])
        return [
            ValueRange(sheet_title=title, values=block.get('values', []) or [])
            for title, block in zip(sheet_titles, ranges, strict=False)
        ]

    def write_ranges(self, spreadsheet_id: str, updates: dict[str, list[list[str]]]) -> None:
        if not updates:
            return
        _execute(
            self._sheets.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    'valueInputOption': 'RAW',
                    'data': [
                        {'range': range_, 'values': values} for range_, values in updates.items()
                    ],
                },
            )
        )

    def append_rows(self, spreadsheet_id: str, sheet_title: str, rows: list[list[str]]) -> None:
        if not rows:
            return
        _execute(
            self._sheets.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=quote_sheet_title(sheet_title),
                valueInputOption='RAW',
                insertDataOption='INSERT_ROWS',
                body={'values': rows},
            )
        )
