"""In-memory stand-in for the Sheets and Drive APIs.

Models the behaviours the package actually depends on: A1 range writes that extend a sheet,
appends that land after the last non-empty row, Drive copies that get fresh ids, and — because
this is not guaranteed by Drive — sheet ids that change when a spreadsheet is copied.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from email_domain_scrubber.sheets import (
    SPREADSHEET_MIME,
    FileInfo,
    SheetInfo,
    SpreadsheetInfo,
    ValueRange,
)

_RANGE = re.compile(
    r"^(?:'(?P<quoted>(?:[^']|'')+)'|(?P<plain>[^!]+))"
    r'(?:!(?P<col>[A-Z]+)(?P<row>\d+))?$'
)


def _parse_range(range_: str) -> tuple[str, int, int]:
    """`'Sheet'!B7` -> (title, row0, col0). A bare sheet title anchors at A1."""
    match = _RANGE.match(range_)
    if not match:
        raise ValueError(f'fake backend cannot parse range {range_!r}')
    title = (
        match.group('quoted').replace("''", "'")
        if match.group('quoted') is not None
        else match.group('plain')
    )
    if not match.group('col'):
        return title, 0, 0
    column = 0
    for char in match.group('col'):
        column = column * 26 + (ord(char) - ord('A') + 1)
    return title, int(match.group('row')) - 1, column - 1


@dataclass
class FakeSheet:
    sheet_id: int
    title: str
    values: list[list[str]] = field(default_factory=list)

    def set(self, row: int, column: int, value: str) -> None:
        while len(self.values) <= row:
            self.values.append([])
        line = self.values[row]
        while len(line) <= column:
            line.append('')
        line[column] = value

    def next_row(self) -> int:
        for index in range(len(self.values) - 1, -1, -1):
            if any(cell.strip() for cell in self.values[index]):
                return index + 1
        return 0

    def copy(self, sheet_id: int) -> FakeSheet:
        return FakeSheet(sheet_id, self.title, [list(row) for row in self.values])


@dataclass
class FakeFile:
    file_id: str
    name: str
    mime_type: str
    parents: tuple[str, ...] = ()
    sheets: list[FakeSheet] = field(default_factory=list)
    trashed: bool = False

    def info(self) -> FileInfo:
        return FileInfo(self.file_id, self.name, self.mime_type, self.parents)


class FakeBackend:
    """A `SheetsBackend` over dictionaries."""

    def __init__(self) -> None:
        self.files: dict[str, FakeFile] = {}
        self._ids = itertools.count(1)
        self._sheet_ids = itertools.count(1000)
        self.write_calls = 0

    # -- test helpers --------------------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        return f'{prefix}{next(self._ids):017d}'

    def add_spreadsheet(
        self, name: str, sheets: dict[str, list[list[str]]], parent: str | None = None
    ) -> FakeFile:
        file = FakeFile(
            file_id=self._new_id('sheet'),
            name=name,
            mime_type=SPREADSHEET_MIME,
            parents=(parent,) if parent else (),
            sheets=[
                FakeSheet(next(self._sheet_ids), title, [list(row) for row in rows])
                for title, rows in sheets.items()
            ],
        )
        self.files[file.file_id] = file
        return file

    def add_upload(self, name: str, mime_type: str, parent: str | None = None) -> FakeFile:
        file = FakeFile(
            file_id=self._new_id('upload'),
            name=name,
            mime_type=mime_type,
            parents=(parent,) if parent else (),
        )
        self.files[file.file_id] = file
        return file

    def sheet_values(self, file_id: str, title: str) -> list[list[str]]:
        return self._sheet(file_id, title).values

    def _file(self, file_id: str) -> FakeFile:
        try:
            return self.files[file_id]
        except KeyError:
            raise LookupError(f'no such file: {file_id}') from None

    def _sheet(self, file_id: str, title: str) -> FakeSheet:
        for sheet in self._file(file_id).sheets:
            if sheet.title == title:
                return sheet
        raise LookupError(f'no such sheet {title!r} in {file_id}')

    # -- SheetsBackend -------------------------------------------------------------------
    def get_file(self, file_id: str) -> FileInfo:
        return self._file(file_id).info()

    def find_file(self, name: str, parent_id: str | None) -> FileInfo | None:
        for file in self.files.values():
            if file.trashed or file.name != name:
                continue
            if parent_id and parent_id not in file.parents:
                continue
            return file.info()
        return None

    def copy_file(self, file_id: str, name: str, *, to_spreadsheet: bool = False) -> FileInfo:
        source = self._file(file_id)
        sheets = source.sheets
        if not sheets and to_spreadsheet:
            # A converted upload: the fake has no cell data for uploads, so start with one sheet.
            sheets = [FakeSheet(next(self._sheet_ids), 'Sheet1')]
        copy = FakeFile(
            file_id=self._new_id('copy'),
            name=name,
            mime_type=SPREADSHEET_MIME if to_spreadsheet else source.mime_type,
            parents=source.parents,
            # Fresh sheet ids: Drive does not promise to preserve them.
            sheets=[sheet.copy(next(self._sheet_ids)) for sheet in sheets],
        )
        self.files[copy.file_id] = copy
        return copy.info()

    def move_to_folder(self, file_id: str, folder_id: str) -> FileInfo:
        file = self._file(file_id)
        file.parents = (folder_id,)
        return file.info()

    def get_spreadsheet(self, spreadsheet_id: str) -> SpreadsheetInfo:
        file = self._file(spreadsheet_id)
        return SpreadsheetInfo(
            spreadsheet_id=file.file_id,
            title=file.name,
            sheets=tuple(SheetInfo(sheet.sheet_id, sheet.title) for sheet in file.sheets),
        )

    def create_spreadsheet(self, title: str, sheet_titles: list[str]) -> SpreadsheetInfo:
        file = self.add_spreadsheet(title, {name: [] for name in sheet_titles})
        return self.get_spreadsheet(file.file_id)

    def add_sheets(self, spreadsheet_id: str, sheet_titles: list[str]) -> SpreadsheetInfo:
        file = self._file(spreadsheet_id)
        existing = {sheet.title for sheet in file.sheets}
        for title in sheet_titles:
            if title in existing:
                raise ValueError(f'sheet {title!r} already exists')
            file.sheets.append(FakeSheet(next(self._sheet_ids), title))
        return self.get_spreadsheet(spreadsheet_id)

    def read_sheets(self, spreadsheet_id: str, sheet_titles: list[str]) -> list[ValueRange]:
        return [
            ValueRange(title, [list(row) for row in self._sheet(spreadsheet_id, title).values])
            for title in sheet_titles
        ]

    def write_ranges(self, spreadsheet_id: str, updates: dict[str, list[list[str]]]) -> None:
        self.write_calls += 1
        for range_, values in updates.items():
            title, row, column = _parse_range(range_)
            sheet = self._sheet(spreadsheet_id, title)
            for row_offset, line in enumerate(values):
                for column_offset, value in enumerate(line):
                    sheet.set(row + row_offset, column + column_offset, value)

    def append_rows(self, spreadsheet_id: str, sheet_title: str, rows: list[list[str]]) -> None:
        sheet = self._sheet(spreadsheet_id, sheet_title)
        start = sheet.next_row()
        for row_offset, line in enumerate(rows):
            for column_offset, value in enumerate(line):
                sheet.set(start + row_offset, column_offset, value)
