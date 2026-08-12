"""Staging a Drive workbook locally, and scanning it for domains."""

import asyncio

import pytest

from email_domain_scrubber.drive import XLSX_MIME
from email_domain_scrubber.errors import InvalidWorkbookReference, UnsupportedWorkbook
from email_domain_scrubber.scan import scan_path, stage_workbook, unique_domains

USERS = {
    'Users': [
        ['User', 'Email'],
        ['Alice', 'alice@smithlab.io'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', 'carol@smithlab.io'],
    ]
}


def stage(drive, staging, reference, **kwargs):
    return asyncio.run(stage_workbook(drive, staging, reference, **kwargs))


@pytest.fixture
def metrics(drive, tmp_path):
    return drive.add_workbook('Q1 Metrics.xlsx', USERS, tmp_path, parent='folder1')


def test_staging_downloads_the_workbook_to_disk(drive, staging, metrics):
    staged = stage(drive, staging, metrics.file_id)

    assert staged.downloaded
    assert staged.path.is_file()
    assert staged.path.read_bytes() == metrics.content
    assert staged.url == f'https://drive.google.com/file/d/{metrics.file_id}/view'


def test_an_unchanged_workbook_is_not_downloaded_again(drive, staging, metrics):
    stage(drive, staging, metrics.file_id)
    second = stage(drive, staging, metrics.file_id)

    assert not second.downloaded
    assert drive.downloads == [metrics.file_id]


def test_a_modified_workbook_is_downloaded_again(drive, staging, metrics, tmp_path):
    stage(drive, staging, metrics.file_id)
    replacement = drive.add_workbook('other.xlsx', {'Users': [['x@newlab.io']]}, tmp_path)
    drive.touch(metrics.file_id, replacement.content, '2026-06-01T00:00:00Z')

    again = stage(drive, staging, metrics.file_id)

    assert again.downloaded
    assert drive.downloads == [metrics.file_id, metrics.file_id]
    assert unique_domains(scan_path(again.path, metrics.file_id)) == ['newlab.io']


def test_force_redownloads_an_unchanged_workbook(drive, staging, metrics):
    stage(drive, staging, metrics.file_id)
    assert stage(drive, staging, metrics.file_id, force=True).downloaded


def test_a_workbook_with_no_modified_time_is_always_refetched(drive, staging, tmp_path):
    """Freshness cannot be proven without a modifiedTime, so re-download rather than guess."""
    file = drive.add_workbook('N.xlsx', USERS, tmp_path)
    drive.files[file.file_id].modified_time = ''

    stage(drive, staging, file.file_id)
    assert stage(drive, staging, file.file_id).downloaded


def test_a_drive_url_is_accepted(drive, staging, metrics):
    url = f'https://drive.google.com/file/d/{metrics.file_id}/view?usp=sharing'
    assert stage(drive, staging, url).path.is_file()


def test_a_non_excel_file_is_refused(drive, staging):
    sheet = drive.add_bytes('Q1 Metrics', b'', mime_type='application/vnd.google-apps.spreadsheet')
    with pytest.raises(UnsupportedWorkbook, match='.xlsx'):
        stage(drive, staging, sheet.file_id)


def test_a_csv_is_refused(drive, staging):
    csv = drive.add_bytes('data.csv', b'a,b', mime_type='text/csv')
    with pytest.raises(UnsupportedWorkbook):
        stage(drive, staging, csv.file_id)


def test_an_xlsx_named_file_is_accepted_despite_a_vague_mime_type(drive, staging, tmp_path):
    """Drive sometimes reports a generic type for an uploaded .xlsx."""
    real = drive.add_workbook('R.xlsx', USERS, tmp_path)
    vague = drive.add_bytes('Q3 Metrics.xlsx', real.content, mime_type='application/octet-stream')

    assert stage(drive, staging, vague.file_id).path.is_file()


def test_garbage_references_are_rejected(drive, staging):
    with pytest.raises(InvalidWorkbookReference):
        stage(drive, staging, 'not a workbook')


def test_scan_finds_domains_with_cell_locators(drive, staging, metrics):
    staged = stage(drive, staging, metrics.file_id)
    hits = scan_path(staged.path, metrics.file_id)

    assert unique_domains(hits) == ['smithlab.io', 'broadinstitute.org']
    smithlab = [hit for hit in hits if hit.domain == 'smithlab.io']
    assert [hit.a1 for hit in smithlab] == ['B2', 'B4']
    assert smithlab[0].reference.endswith('#Users!B2')
    assert metrics.file_id in smithlab[0].reference


def test_scan_records_row_and_column_for_block_planning(drive, staging, metrics):
    staged = stage(drive, staging, metrics.file_id)
    hits = {hit.a1: hit for hit in scan_path(staged.path, metrics.file_id)}

    assert (hits['B2'].row, hits['B2'].column) == (2, 2)
    assert (hits['B4'].row, hits['B4'].column) == (4, 2)


def test_scan_covers_all_sheets(drive, staging, tmp_path):
    file = drive.add_workbook(
        'Multi.xlsx', {'One': [['a@one.org']], 'Two': [['b@two.org']]}, tmp_path
    )
    staged = stage(drive, staging, file.file_id)
    hits = scan_path(staged.path, file.file_id)

    assert {(hit.sheet_title, hit.domain) for hit in hits} == {
        ('One', 'one.org'),
        ('Two', 'two.org'),
    }


def test_a_cell_with_two_domains_yields_two_hits_at_one_address(drive, staging, tmp_path):
    file = drive.add_workbook('D.xlsx', {'S': [['a@one.org and b@two.org']]}, tmp_path)
    staged = stage(drive, staging, file.file_id)
    hits = scan_path(staged.path, file.file_id)

    assert [hit.domain for hit in hits] == ['one.org', 'two.org']
    assert {hit.a1 for hit in hits} == {'A1'}


def test_non_domain_dotted_tokens_are_ignored(drive, staging, tmp_path):
    file = drive.add_workbook(
        'N.xlsx',
        {'S': [['Total.Count', 'report.csv', '1.2.3', 'Fig.2A', 'pi@reallab.io']]},
        tmp_path,
    )
    staged = stage(drive, staging, file.file_id)

    assert unique_domains(scan_path(staged.path, file.file_id)) == ['reallab.io']


def test_the_staged_name_reflects_the_drive_name(drive, staging, metrics):
    assert stage(drive, staging, metrics.file_id).path.name == 'Q1 Metrics.xlsx'


def test_an_awkward_drive_name_is_made_safe_on_disk(drive, staging, tmp_path):
    file = drive.add_workbook('Q1/Q2: metrics *final*.xlsx', USERS, tmp_path)
    staged = stage(drive, staging, file.file_id)

    assert staged.path.is_file()
    assert '/' not in staged.path.name
    assert staged.path.suffix == '.xlsx'


def test_mime_type_alone_is_enough_without_an_xlsx_extension(drive, staging, tmp_path):
    real = drive.add_workbook('R.xlsx', USERS, tmp_path)
    named = drive.add_bytes('Q4 Metrics', real.content, mime_type=XLSX_MIME)

    assert stage(drive, staging, named.file_id).path.name == 'Q4 Metrics.xlsx'
