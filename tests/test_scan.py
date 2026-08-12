"""Opening a local workbook, and scanning it for domains."""

import pytest

from email_domain_scrubber.errors import InvalidWorkbookReference, UnsupportedWorkbook
from email_domain_scrubber.scan import open_workbook, scan_path, unique_domains

from .fakes import write_xlsx

USERS = {
    'Users': [
        ['User', 'Email'],
        ['Alice', 'alice@smithlab.io'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', 'carol@smithlab.io'],
    ]
}


@pytest.fixture
def metrics(tmp_path):
    return write_xlsx(tmp_path / 'Q1 Metrics.xlsx', USERS)


def test_a_workbook_is_opened_in_place(metrics):
    staged = open_workbook(str(metrics))

    assert staged.path == metrics
    assert staged.title == 'Q1 Metrics.xlsx'
    assert staged.url == metrics.as_uri()


def test_a_missing_file_is_reported(tmp_path):
    with pytest.raises(InvalidWorkbookReference, match='No such file'):
        open_workbook(str(tmp_path / 'nope.xlsx'))


def test_a_non_xlsx_file_is_refused(tmp_path):
    csv = tmp_path / 'metrics.csv'
    csv.write_text('a,b\n')

    with pytest.raises(UnsupportedWorkbook, match='.xlsx files only'):
        open_workbook(str(csv))


def test_a_url_is_refused_with_a_download_hint():
    with pytest.raises(InvalidWorkbookReference, match='is a URL, not a path'):
        open_workbook('https://example.com/a/report.xlsx')


def test_an_empty_reference_is_refused():
    with pytest.raises(InvalidWorkbookReference, match='No workbook path'):
        open_workbook('   ')


def test_scan_finds_domains_with_cell_locators(metrics):
    staged = open_workbook(str(metrics))
    hits = scan_path(staged.path, staged.url)

    assert unique_domains(hits) == ['smithlab.io', 'broadinstitute.org']
    smithlab = [hit for hit in hits if hit.domain == 'smithlab.io']
    assert [hit.a1 for hit in smithlab] == ['B2', 'B4']
    assert smithlab[0].reference == f'{metrics.as_uri()}#Users!B2'


def test_scan_records_row_and_column_for_block_planning(metrics):
    staged = open_workbook(str(metrics))
    hits = {hit.a1: hit for hit in scan_path(staged.path, staged.url)}

    assert (hits['B2'].row, hits['B2'].column) == (2, 2)
    assert (hits['B4'].row, hits['B4'].column) == (4, 2)


def test_scan_covers_all_sheets(tmp_path):
    path = write_xlsx(tmp_path / 'Multi.xlsx', {'One': [['a@one.org']], 'Two': [['b@two.org']]})
    hits = scan_path(path, path.as_uri())

    assert {(hit.sheet_title, hit.domain) for hit in hits} == {
        ('One', 'one.org'),
        ('Two', 'two.org'),
    }


def test_a_cell_with_two_domains_yields_two_hits_at_one_address(tmp_path):
    path = write_xlsx(tmp_path / 'D.xlsx', {'S': [['a@one.org and b@two.org']]})
    hits = scan_path(path, path.as_uri())

    assert [hit.domain for hit in hits] == ['one.org', 'two.org']
    assert {hit.a1 for hit in hits} == {'A1'}


def test_non_domain_dotted_tokens_are_ignored(tmp_path):
    path = write_xlsx(
        tmp_path / 'N.xlsx',
        {'S': [['Total.Count', 'report.csv', '1.2.3', 'Fig.2A', 'pi@reallab.io']]},
    )

    assert unique_domains(scan_path(path, path.as_uri())) == ['reallab.io']
