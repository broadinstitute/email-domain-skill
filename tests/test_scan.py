import pytest

from email_domain_scrubber.errors import InvalidWorkbookReference, UnsupportedWorkbook
from email_domain_scrubber.scan import resolve_workbook, scan_spreadsheet, unique_domains
from email_domain_scrubber.sheets import (
    a1_cell,
    cell_link,
    column_letter,
    parse_file_id,
    quote_sheet_title,
)

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def test_parse_file_id_from_a_sheets_url():
    url = 'https://docs.google.com/spreadsheets/d/abc123XYZ_-/edit#gid=0&range=A2'
    assert parse_file_id(url) == 'abc123XYZ_-'


def test_parse_file_id_from_a_drive_url():
    assert parse_file_id('https://drive.google.com/file/d/abc123XYZ/view') == 'abc123XYZ'


def test_parse_file_id_from_a_bare_id():
    bare = 'a' * 25
    assert parse_file_id(bare) == bare


@pytest.mark.parametrize('value', ['', '   ', 'not a workbook', 'https://example.com/thing'])
def test_parse_file_id_rejects_junk(value):
    with pytest.raises(InvalidWorkbookReference):
        parse_file_id(value)


@pytest.mark.parametrize(('index', 'letter'), [(0, 'A'), (25, 'Z'), (26, 'AA'), (27, 'AB')])
def test_column_letter(index, letter):
    assert column_letter(index) == letter


def test_a1_cell():
    assert a1_cell(6, 1) == 'B7'


def test_quote_sheet_title_escapes_apostrophes():
    assert quote_sheet_title("Bob's Sheet") == "'Bob''s Sheet'"


def test_cell_link_points_at_the_sheet_and_cell():
    link = cell_link('abc', 1234, 'B7')
    assert link == 'https://docs.google.com/spreadsheets/d/abc/edit#gid=1234&range=B7'


def test_scan_finds_domains_with_cell_level_references(backend):
    file = backend.add_spreadsheet(
        'Q1 Metrics',
        {
            'Users': [
                ['User', 'Email'],
                ['Alice', 'alice@smithlab.io'],
                ['Bob', 'bob@harvard.edu'],
            ],
            'Domains': [['Domain'], ['pluralistic.net']],
        },
    )
    hits = scan_spreadsheet(backend, backend.get_spreadsheet(file.file_id))

    assert unique_domains(hits) == ['smithlab.io', 'harvard.edu', 'pluralistic.net']
    first = hits[0]
    assert (first.sheet_title, first.a1) == ('Users', 'B2')
    assert first.reference.endswith('&range=B2')
    assert f'/d/{file.file_id}/' in first.reference


def test_scan_reports_every_domain_in_a_multi_domain_cell(backend):
    file = backend.add_spreadsheet('M', {'S': [['a@one.org, b@two.org']]})
    hits = scan_spreadsheet(backend, backend.get_spreadsheet(file.file_id))
    assert [(hit.a1, hit.domain) for hit in hits] == [('A1', 'one.org'), ('A1', 'two.org')]


def test_scan_ignores_cells_without_domains(backend):
    file = backend.add_spreadsheet(
        'M', {'S': [['Total.Count', '1.2.3', 'report.csv', '', 'plain text']]}
    )
    assert scan_spreadsheet(backend, backend.get_spreadsheet(file.file_id)) == []


def test_scan_handles_ragged_rows(backend):
    file = backend.add_spreadsheet('M', {'S': [['a@one.org'], [], ['x', 'y', 'b@two.org']]})
    hits = scan_spreadsheet(backend, backend.get_spreadsheet(file.file_id))
    assert [(hit.a1, hit.domain) for hit in hits] == [('A1', 'one.org'), ('C3', 'two.org')]


def test_resolve_returns_a_native_sheet_unchanged(backend):
    file = backend.add_spreadsheet('Q1', {'S': []})
    resolved = resolve_workbook(
        backend, f'https://docs.google.com/spreadsheets/d/{file.file_id}/edit'
    )
    assert resolved.info.spreadsheet_id == file.file_id
    assert not resolved.converted


def test_resolve_converts_a_drive_xlsx_upload(backend):
    upload = backend.add_upload('Q1 Metrics.xlsx', XLSX, parent='folder1')
    resolved = resolve_workbook(backend, upload.file_id)

    assert resolved.converted
    assert resolved.converted_from_mime == XLSX
    assert resolved.info.spreadsheet_id != upload.file_id
    assert resolved.info.title == 'Q1 Metrics (Sheets)'
    assert backend.files[resolved.info.spreadsheet_id].parents == ('folder1',)


def test_resolve_reuses_a_previous_conversion(backend):
    upload = backend.add_upload('Q1 Metrics.xlsx', XLSX, parent='folder1')
    first = resolve_workbook(backend, upload.file_id)
    second = resolve_workbook(backend, upload.file_id)
    assert first.info.spreadsheet_id == second.info.spreadsheet_id


def test_resolve_rejects_a_non_spreadsheet(backend):
    doc = backend.add_upload('Notes.docx', 'application/vnd.google-apps.document')
    with pytest.raises(UnsupportedWorkbook, match='not a spreadsheet'):
        resolve_workbook(backend, doc.file_id)
