"""Local `.xlsx` workbooks: the default source, with no Google involvement at all."""

import asyncio

import pytest

from email_domain_scrubber import local, server, xlsx
from email_domain_scrubber.errors import InvalidWorkbookReference, UnsupportedWorkbook
from email_domain_scrubber.scan import scan_path, stage_workbook, unique_domains

from .fakes import write_xlsx

USERS = {
    'Users': [
        ['User', 'Email'],
        ['Alice', 'alice@smithlab.io'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', 'carol@smithlab.io'],
    ]
}


@pytest.fixture(autouse=True)
def _use_fakes(drive, staging):
    server.set_backend(drive, staging)
    yield
    server.set_backend(None, None)


@pytest.fixture
def report(tmp_path):
    """A metrics workbook sitting on disk, nowhere near Drive."""
    return write_xlsx(tmp_path / 'Q1 Metrics.xlsx', USERS)


@pytest.fixture
def book(tmp_path):
    return str(tmp_path / 'analysis.xlsx')


def run(coroutine):
    return asyncio.run(coroutine)


def _blocks(plan):
    from email_domain_scrubber.redact import WriteBlock

    return [
        WriteBlock(sheet=block.sheet_name, start_cell=block.start_cell, values=block.data)
        for block in plan.write_blocks
    ]


# -- telling a path from a Drive reference ------------------------------------------------------
@pytest.mark.parametrize(
    'reference',
    [
        '/Users/someone/report.xlsx',
        './report.xlsx',
        'report.xlsx',
        '~/reports/Q1.xlsx',
        'file:///Users/someone/report.xlsx',
        'subdir/report.xlsx',
    ],
)
def test_a_path_is_a_local_reference(reference):
    assert local.is_local_reference(reference)


@pytest.mark.parametrize(
    'reference',
    [
        'file00000000000000001',
        '1A2b3C4d5E6f7G8h9I0j1K2l3M4n5O6p',
        'https://drive.google.com/file/d/abc123/view',
        'https://docs.google.com/spreadsheets/d/abc123/edit',
        '',
    ],
)
def test_a_drive_reference_is_not_local(reference):
    assert not local.is_local_reference(reference)


# -- staging -----------------------------------------------------------------------------------
def test_a_local_workbook_is_read_in_place(drive, staging, report):
    staged = run(stage_workbook(drive, staging, str(report)))

    assert staged.path == report
    assert not staged.downloaded
    assert staged.url == report.as_uri()
    assert drive.downloads == []


def test_staging_a_local_workbook_touches_no_drive_client(staging, report):
    """`None` for the Drive client proves the local path never reaches it."""
    staged = run(stage_workbook(None, staging, str(report)))

    assert staged.path == report


def test_a_file_url_is_accepted(drive, staging, report):
    staged = run(stage_workbook(drive, staging, report.as_uri()))

    assert staged.path == report


def test_a_missing_local_file_is_reported(drive, staging, tmp_path):
    with pytest.raises(InvalidWorkbookReference, match='No such file'):
        run(stage_workbook(drive, staging, str(tmp_path / 'absent.xlsx')))


def test_a_local_directory_is_not_a_workbook(drive, staging, tmp_path):
    with pytest.raises(InvalidWorkbookReference, match='not a file'):
        run(stage_workbook(drive, staging, f'{tmp_path}/'))


def test_a_local_non_xlsx_is_refused(drive, staging, tmp_path):
    csv = tmp_path / 'metrics.csv'
    csv.write_text('User,Email\nAlice,alice@smithlab.io\n')

    with pytest.raises(UnsupportedWorkbook, match='.xlsx files only'):
        run(stage_workbook(drive, staging, str(csv)))


def test_local_references_locate_cells_by_file_url(drive, staging, report):
    staged = run(stage_workbook(drive, staging, str(report)))
    hits = {hit.a1: hit for hit in scan_path(staged.path, staged.url)}

    assert hits['B2'].reference == f'{report.as_uri()}#Users!B2'
    assert unique_domains(list(hits.values())) == ['smithlab.io', 'broadinstitute.org']


# -- the whole workflow, offline ----------------------------------------------------------------
def test_scan_store_plan_write_finish_without_drive(drive, report, book, excel):
    scan = run(server.scan_workbook(str(report), book))
    assert scan.domains_found == 2
    assert scan.references_recorded == 3
    assert not scan.downloaded
    assert scan.scanned_workbook_url == report.as_uri()
    assert scan.local_path == str(report)

    stored = server.store_domain_analysis(
        [
            server.AnalysisInput(
                domain='smithlab.io', risk='High', explanation='Personal lab domain'
            ),
            server.AnalysisInput(
                domain='broadinstitute.org', risk='Low', explanation='Broad Institute'
            ),
        ],
        book,
    )
    alias = next(item.anonymized_domain for item in stored.stored if item.domain == 'smithlab.io')

    plan = run(server.plan_redaction(str(report), book))
    assert plan.cells_to_change == 2
    assert plan.domains_anonymized == {'smithlab.io': alias}

    excel.apply(plan.redacted_path, _blocks(plan))

    finished = run(server.finish_redaction(str(report), plan.redacted_path, book))
    assert finished.cells_changed == 2
    assert finished.remaining_domains == ['broadinstitute.org']
    assert finished.redactions_recorded == 2
    assert finished.redacted_workbook_title == 'Q1 Metrics (anonymized).xlsx'
    assert finished.redacted_workbook_url.startswith('file://')
    assert finished.redacted_workbook_url.endswith('Q1%20Metrics%20%28anonymized%29.xlsx')

    # The whole point: nothing was uploaded, downloaded, or looked up.
    assert drive.created == []
    assert drive.downloads == []


def test_the_local_original_is_never_modified(drive, report, book, excel):
    before = report.read_bytes()

    run(server.scan_workbook(str(report), book))
    server.store_domain_analysis(
        [server.AnalysisInput(domain=d, risk='High', explanation='x') for d in ('smithlab.io',)]
        + [server.AnalysisInput(domain='broadinstitute.org', risk='Low', explanation='x')],
        book,
    )
    plan = run(server.plan_redaction(str(report), book))
    excel.apply(plan.redacted_path, _blocks(plan))
    run(server.finish_redaction(str(report), plan.redacted_path, book))

    assert report.read_bytes() == before


def test_the_redacted_copy_lands_in_the_work_directory_not_beside_the_original(
    drive, report, book, staging
):
    run(server.scan_workbook(str(report), book))
    server.store_domain_analysis(
        [
            server.AnalysisInput(domain='smithlab.io', risk='High', explanation='x'),
            server.AnalysisInput(domain='broadinstitute.org', risk='Low', explanation='x'),
        ],
        book,
    )
    plan = run(server.plan_redaction(str(report), book))

    assert plan.redacted_path
    assert str(staging.root) in plan.redacted_path
    assert list(report.parent.glob('*(anonymized)*')) == []


def test_the_redaction_is_recorded_against_the_local_file(drive, report, book, excel):
    run(server.scan_workbook(str(report), book))
    server.store_domain_analysis(
        [
            server.AnalysisInput(domain='smithlab.io', risk='High', explanation='x'),
            server.AnalysisInput(domain='broadinstitute.org', risk='Low', explanation='x'),
        ],
        book,
    )
    plan = run(server.plan_redaction(str(report), book))
    excel.apply(plan.redacted_path, _blocks(plan))
    run(server.finish_redaction(str(report), plan.redacted_path, book))

    rows = list(xlsx.read_cells(plan.redacted_path))
    assert rows  # the redacted file is readable after the writes

    from email_domain_scrubber.workbook import REDACTIONS

    recorded = [cell.text for cell in xlsx.read_cells(book) if cell.sheet_title == REDACTIONS]
    assert any(report.as_uri() in text for text in recorded)
    assert any('#Users!B2' in text for text in recorded)
