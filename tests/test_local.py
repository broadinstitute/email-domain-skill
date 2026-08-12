"""Resolving a reference to a workbook on disk."""

import pytest

from email_domain_scrubber import local
from email_domain_scrubber.errors import InvalidWorkbookReference, UnsupportedWorkbook

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
def report(tmp_path):
    return write_xlsx(tmp_path / 'Q1 Metrics.xlsx', USERS)


def test_an_absolute_path_resolves(report):
    assert local.resolve(str(report)) == report


def test_a_relative_path_resolves(report, monkeypatch):
    monkeypatch.chdir(report.parent)

    assert local.resolve('Q1 Metrics.xlsx') == report
    assert local.resolve('./Q1 Metrics.xlsx') == report


def test_a_tilde_path_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    report = write_xlsx(tmp_path / 'Home.xlsx', USERS)

    assert local.resolve('~/Home.xlsx') == report


def test_a_file_url_resolves(report):
    assert local.resolve(report.as_uri()) == report


def test_a_file_url_with_percent_escapes_resolves(tmp_path):
    report = write_xlsx(tmp_path / 'Q1 Metrics.xlsx', USERS)

    assert '%20' in report.as_uri()
    assert local.resolve(report.as_uri()) == report


@pytest.mark.parametrize(
    'reference',
    [
        'https://example.com/report.xlsx',
        'https://files.example.org/file/d/abc123/view',
        's3://bucket/report.xlsx',
    ],
)
def test_a_remote_url_is_refused_with_a_download_hint(reference):
    with pytest.raises(InvalidWorkbookReference, match='is a URL, not a path'):
        local.resolve(reference)


def test_an_empty_reference_is_refused():
    with pytest.raises(InvalidWorkbookReference, match='No workbook path'):
        local.resolve('')


def test_a_missing_file_is_reported(tmp_path):
    with pytest.raises(InvalidWorkbookReference, match='No such file'):
        local.resolve(str(tmp_path / 'absent.xlsx'))


def test_a_directory_is_not_a_workbook(tmp_path):
    with pytest.raises(InvalidWorkbookReference, match='not a file'):
        local.resolve(f'{tmp_path}/')


def test_a_non_xlsx_file_is_refused(tmp_path):
    csv = tmp_path / 'metrics.csv'
    csv.write_text('User,Email\nAlice,alice@smithlab.io\n')

    with pytest.raises(UnsupportedWorkbook, match='.xlsx files only'):
        local.resolve(str(csv))


def test_url_is_a_file_url(report):
    assert local.url(report) == report.as_uri()
    assert local.url(report).startswith('file://')


def test_cell_reference_hangs_the_cell_off_the_fragment(report):
    reference = local.cell_reference(local.url(report), 'Users', 'B2')

    assert reference == f'{report.as_uri()}#Users!B2'
