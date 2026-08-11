"""`GoogleBackend` against the real Sheets and Drive APIs.

The rest of the suite runs against `tests/fakes.FakeBackend`. These tests are what entitles it
to stand in for Google: each one pins a behaviour the fake models, so a wrong assumption shows
up here rather than in production. Where the fake is deliberately stricter than Google (it hands
out fresh sheet ids on copy, which Drive does not promise either way) the test pins what the
package actually relies on instead.
"""

from __future__ import annotations

import pytest

from email_domain_scrubber.errors import WorkbookNotFound
from email_domain_scrubber.sheets import SPREADSHEET_MIME

pytestmark = pytest.mark.live


def test_credentials_identify_the_signed_in_user(live_access):
    """Which account this is is the first thing to check when a workbook 'cannot be found'."""
    assert '@' in live_access.account
    assert live_access.drive_ok and live_access.sheets_ok


def test_write_then_read_returns_the_same_cell_text(scratch):
    info = scratch.spreadsheet('roundtrip', {'Usage': [['User', 'Domain'], ['a@lab.io', 'lab.io']]})

    blocks = scratch.backend.read_sheets(info.spreadsheet_id, ['Usage'])

    assert [block.sheet_title for block in blocks] == ['Usage']
    assert blocks[0].values == [['User', 'Domain'], ['a@lab.io', 'lab.io']]


def test_reading_a_sheet_with_no_data_gives_an_empty_block(scratch):
    """Google omits `values` entirely for an empty sheet; the backend must not KeyError."""
    info = scratch.spreadsheet('empty', {'Usage': []})

    assert scratch.backend.read_sheets(info.spreadsheet_id, ['Usage'])[0].values == []


def test_append_rows_lands_after_the_last_non_empty_row(scratch):
    """`FakeSheet.next_row` models this; the analysis workbook's appends depend on it."""
    info = scratch.spreadsheet('append', {'Usage': [['Header'], ['first']]})

    scratch.backend.append_rows(info.spreadsheet_id, 'Usage', [['second'], ['third']])

    values = scratch.backend.read_sheets(info.spreadsheet_id, ['Usage'])[0].values
    assert values == [['Header'], ['first'], ['second'], ['third']]


def test_writing_past_the_end_extends_the_sheet(scratch):
    """The analysis workbook updates rows by number, including rows that do not exist yet."""
    info = scratch.spreadsheet('extend', {'Usage': [['Header']]})

    scratch.backend.write_ranges(info.spreadsheet_id, {"'Usage'!A4": [['far', 'below']]})

    values = scratch.backend.read_sheets(info.spreadsheet_id, ['Usage'])[0].values
    assert values[3] == ['far', 'below']


def test_add_sheets_leaves_the_existing_sheets_alone(scratch):
    """`AnalysisWorkbook.ensure_schema` adds missing sheets to a workbook already in use."""
    info = scratch.spreadsheet('add-sheets', {'Usage': [['keep me']]})

    updated = scratch.backend.add_sheets(info.spreadsheet_id, ['Added'])

    assert [sheet.title for sheet in updated.sheets] == ['Usage', 'Added']
    assert scratch.backend.read_sheets(info.spreadsheet_id, ['Usage'])[0].values == [['keep me']]


def test_sheet_titles_containing_apostrophes_survive_quoting(scratch):
    """`quote_sheet_title` doubles the apostrophe; if it got that wrong Google rejects the range."""
    info = scratch.spreadsheet('quoting', {"Bob's Data": [['x@lab.io']]})

    scratch.backend.write_ranges(info.spreadsheet_id, {"'Bob''s Data'!A2": [['y@lab.io']]})

    values = scratch.backend.read_sheets(info.spreadsheet_id, ["Bob's Data"])[0].values
    assert values == [['x@lab.io'], ['y@lab.io']]


def test_find_file_matches_the_whole_name_and_returns_none_when_absent(scratch):
    """Redaction asks 'is this name taken?' before copying, so a false negative overwrites."""
    info = scratch.spreadsheet('findable', {'Usage': [['a@lab.io']]})
    name = scratch.name('findable')

    found = scratch.backend.find_file(name, None)

    assert found is not None
    assert found.file_id == info.spreadsheet_id
    assert found.mime_type == SPREADSHEET_MIME
    # A prefix of a real name must not match: Drive's `contains` is prefix-based, `=` is not.
    assert scratch.backend.find_file(name[:-3], None) is None


def test_find_file_tolerates_apostrophes_in_the_name(scratch):
    """An unescaped apostrophe would be a query syntax error, not a miss."""
    scratch.spreadsheet("O'Brien", {'Usage': [['a@lab.io']]})

    assert scratch.backend.find_file(scratch.name("O'Brien"), None) is not None


def test_copy_produces_an_independent_file_whose_sheets_resolve_by_title(scratch):
    """Redaction copies, then addresses the copy's sheets by title — never by remembered id."""
    info = scratch.spreadsheet('copy-source', {'Usage': [['a@lab.io']], 'Notes': [['hello']]})

    copy = scratch.backend.copy_file(info.spreadsheet_id, scratch.name('copy-target'))
    copy_info = scratch.backend.get_spreadsheet(copy.file_id)

    assert copy.file_id != info.spreadsheet_id
    assert {sheet.title for sheet in copy_info.sheets} == {'Usage', 'Notes'}
    # Editing the copy must not touch the source.
    scratch.backend.write_ranges(copy.file_id, {"'Usage'!A1": [['redacted']]})
    assert scratch.backend.read_sheets(info.spreadsheet_id, ['Usage'])[0].values == [['a@lab.io']]


def test_a_missing_file_is_reported_as_workbook_not_found(scratch):
    """Google's raw 404 says nothing about sharing, which is the usual cause."""
    missing = '1' + 'z' * 43

    with pytest.raises(WorkbookNotFound, match='shared with the signed-in account'):
        scratch.backend.get_file(missing)

    with pytest.raises(WorkbookNotFound):
        scratch.backend.get_spreadsheet(missing)
