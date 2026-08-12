"""The MCP tool surface, driven the way the analysis skill drives it."""

import asyncio

import pytest

from email_domain_scrubber import server, xlsx
from email_domain_scrubber.errors import RedactionNotApplied, UnanalyzedDomains, UnsupportedWorkbook
from email_domain_scrubber.staging import ANALYSIS_WORKBOOK_ENV
from email_domain_scrubber.workbook import REDACTIONS, WORKBOOKS

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
def metrics(drive, tmp_path):
    return drive.add_workbook('Q1 Metrics.xlsx', USERS, tmp_path, parent='folder1')


@pytest.fixture
def book(tmp_path):
    """An explicit analysis workbook path, as the skill would pass."""
    return str(tmp_path / 'analysis.xlsx')


def run(coroutine):
    return asyncio.run(coroutine)


def analyze_all(book):
    return server.store_domain_analysis(
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


# -- registration ------------------------------------------------------------------------------
def test_all_tools_are_registered():
    """Guards against a tool being added without its @mcp.tool() decorator."""
    tools = run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == {
        'scan_workbook',
        'list_domains_for_analysis',
        'store_domain_analysis',
        'plan_redaction',
        'finish_redaction',
    }


# -- the analysis workbook ---------------------------------------------------------------------
def test_the_analysis_workbook_is_created_on_first_use(metrics, book):
    from pathlib import Path

    assert not Path(book).exists()
    result = run(server.scan_workbook(metrics.file_id, book))

    assert Path(book).is_file()
    assert result.analysis_workbook_path == book


def test_the_analysis_workbook_defaults_to_the_environment(monkeypatch, metrics, book):
    monkeypatch.setenv(ANALYSIS_WORKBOOK_ENV, book)

    result = run(server.scan_workbook(metrics.file_id))

    assert result.analysis_workbook_path == book
    assert result.domains_found == 2


def test_the_analysis_workbook_falls_back_to_the_work_directory(metrics, staging):
    result = run(server.scan_workbook(metrics.file_id))
    assert result.analysis_workbook_path.endswith('analysis.xlsx')


# -- the happy path ----------------------------------------------------------------------------
def test_scan_list_store_plan_write_finish(drive, metrics, book, excel):
    scan = run(server.scan_workbook(metrics.file_id, book))
    assert scan.domains_found == 2
    assert scan.references_recorded == 3
    assert set(scan.new_domains) == {'smithlab.io', 'broadinstitute.org'}
    assert set(scan.pending_analysis) == {'smithlab.io', 'broadinstitute.org'}
    assert scan.downloaded

    pending = server.list_domains_for_analysis(book)
    by_domain = {item.domain: item for item in pending}
    assert by_domain['smithlab.io'].reference_count == 2
    assert len(by_domain['smithlab.io'].example_references) == 2
    assert by_domain['broadinstitute.org'].reference_count == 1

    stored = analyze_all(book)
    assert stored.still_pending == []
    assert {item.domain: item.action for item in stored.stored} == {
        'smithlab.io': 'will_be_anonymized',
        'broadinstitute.org': 'left_as_is',
    }
    alias = next(item.anonymized_domain for item in stored.stored if item.domain == 'smithlab.io')

    plan = run(server.plan_redaction(metrics.file_id, book))
    assert plan.cells_to_change == 2
    assert plan.domains_anonymized == {'smithlab.io': alias}
    assert plan.domains_left_as_is == ['broadinstitute.org']
    assert plan.sample_changes[0].after == f'alice@{alias}'
    # B3 is kept as-is, so B2 and B4 cannot share a block without rewriting it.
    assert [(block.sheet_name, block.start_cell, block.data) for block in plan.write_blocks] == [
        ('Users', 'B2', [[f'alice@{alias}']]),
        ('Users', 'B4', [[f'carol@{alias}']]),
    ]

    excel.apply(plan.redacted_path, _blocks(plan))

    finished = run(server.finish_redaction(metrics.file_id, plan.redacted_path, book))
    assert finished.cells_changed == 2
    assert finished.remaining_domains == ['broadinstitute.org']
    assert finished.redactions_recorded == 2
    assert finished.redacted_workbook_title == 'Q1 Metrics (anonymized).xlsx'

    published = drive.created[-1]
    assert published.mime_type.endswith('spreadsheetml.sheet')
    assert finished.redacted_workbook_url.endswith(f'/d/{published.file_id}/view')


def _blocks(plan):
    """The plan's write blocks in the shape the Recorder (and Excel MCP server) applies."""
    from email_domain_scrubber.redact import WriteBlock

    return [
        WriteBlock(sheet=block.sheet_name, start_cell=block.start_cell, values=block.data)
        for block in plan.write_blocks
    ]


def test_a_contiguous_column_needs_one_write_call(drive, book, tmp_path, excel):
    rows = [['Email'], *[[f'user{index}@lab.io'] for index in range(30)]]
    file = drive.add_workbook('Big.xlsx', {'Users': rows}, tmp_path)
    run(server.scan_workbook(file.file_id, book))
    server.store_domain_analysis(
        [server.AnalysisInput(domain='lab.io', risk='High', explanation='a lab')], book
    )

    plan = run(server.plan_redaction(file.file_id, book))

    assert plan.cells_to_change == 30
    assert len(plan.write_blocks) == 1


def test_scattered_edits_cost_one_write_call_each(drive, book, tmp_path):
    """The bound on write calls: one per redacted cell when kept cells break up the runs."""
    rows = [
        ['Email'],
        *[[f'u{i}@{"lab.io" if i % 2 else "broadinstitute.org"}'] for i in range(10)],
    ]
    file = drive.add_workbook('Mixed.xlsx', {'Users': rows}, tmp_path)
    run(server.scan_workbook(file.file_id, book))
    server.store_domain_analysis(
        [
            server.AnalysisInput(domain='lab.io', risk='High', explanation='a lab'),
            server.AnalysisInput(domain='broadinstitute.org', risk='Low', explanation='Broad'),
        ],
        book,
    )

    plan = run(server.plan_redaction(file.file_id, book))

    assert plan.cells_to_change == 5
    assert len(plan.write_blocks) == 5


# -- the source is never touched ---------------------------------------------------------------
def test_the_drive_original_is_never_modified(drive, metrics, book, excel):
    before = drive.files[metrics.file_id].content
    scan = run(server.scan_workbook(metrics.file_id, book))
    analyze_all(book)
    plan = run(server.plan_redaction(metrics.file_id, book))
    excel.apply(plan.redacted_path, _blocks(plan))
    run(server.finish_redaction(metrics.file_id, plan.redacted_path, book))

    assert drive.files[metrics.file_id].content == before
    # Nor is the staged local copy of it, which the next scan will reuse.
    assert xlsx.read_rows(scan.local_path, 'Users')[1] == ['Alice', 'alice@smithlab.io']


def test_planning_publishes_nothing(drive, metrics, book):
    run(server.scan_workbook(metrics.file_id, book))
    analyze_all(book)

    run(server.plan_redaction(metrics.file_id, book))

    assert drive.created == []


# -- refusals ----------------------------------------------------------------------------------
def test_plan_refuses_before_analysis_is_complete(metrics, book):
    run(server.scan_workbook(metrics.file_id, book))
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )

    with pytest.raises(UnanalyzedDomains, match='broadinstitute.org'):
        run(server.plan_redaction(metrics.file_id, book))


def test_finish_refuses_when_the_blocks_were_never_applied(drive, metrics, book):
    run(server.scan_workbook(metrics.file_id, book))
    analyze_all(book)
    plan = run(server.plan_redaction(metrics.file_id, book))

    with pytest.raises(RedactionNotApplied, match='smithlab.io'):
        run(server.finish_redaction(metrics.file_id, plan.redacted_path, book))
    assert drive.created == []


def test_finish_refuses_a_path_that_does_not_exist(metrics, book, tmp_path):
    run(server.scan_workbook(metrics.file_id, book))
    analyze_all(book)

    with pytest.raises(FileNotFoundError):
        run(server.finish_redaction(metrics.file_id, str(tmp_path / 'nope.xlsx'), book))


def test_a_google_sheet_is_refused(drive, book):
    sheet = drive.add_bytes('Q1', b'', mime_type='application/vnd.google-apps.spreadsheet')

    with pytest.raises(UnsupportedWorkbook, match='.xlsx'):
        run(server.scan_workbook(sheet.file_id, book))


# -- re-scanning -------------------------------------------------------------------------------
def test_rescanning_preserves_analysis_and_adds_only_new_domains(drive, metrics, book, tmp_path):
    run(server.scan_workbook(metrics.file_id, book))
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )
    before = {
        item.domain: item.anonymized_domain
        for item in server.list_domains_for_analysis(book, include_analyzed=True)
    }

    grown = dict(USERS)
    grown['Users'] = [*USERS['Users'], ['Dave', 'dave@newlab.io']]
    replacement = drive.add_workbook('grown.xlsx', grown, tmp_path)
    drive.touch(metrics.file_id, replacement.content, '2026-07-01T00:00:00Z')

    again = run(server.scan_workbook(metrics.file_id, book))

    assert again.new_domains == ['newlab.io']
    assert again.references_recorded == 1
    assert sorted(again.pending_analysis) == ['broadinstitute.org', 'newlab.io']

    after = {
        item.domain: item.anonymized_domain
        for item in server.list_domains_for_analysis(book, include_analyzed=True)
    }
    assert after['smithlab.io'] == before['smithlab.io']


def test_rescanning_reuses_the_local_copy(drive, metrics, book):
    run(server.scan_workbook(metrics.file_id, book))
    again = run(server.scan_workbook(metrics.file_id, book))

    assert not again.downloaded
    assert drive.downloads == [metrics.file_id]


def test_rescanning_records_the_workbook_once(metrics, book):
    run(server.scan_workbook(metrics.file_id, book))
    run(server.scan_workbook(metrics.file_id, book))

    rows = xlsx.read_rows(book, WORKBOOKS)[1:]
    assert len(rows) == 1
    assert rows[0][1] == 'Q1 Metrics.xlsx'


# -- listing and storing -----------------------------------------------------------------------
def test_list_domains_can_include_analyzed(metrics, book):
    run(server.scan_workbook(metrics.file_id, book))
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )

    assert [item.domain for item in server.list_domains_for_analysis(book)] == [
        'broadinstitute.org'
    ]
    everything = server.list_domains_for_analysis(book, include_analyzed=True)
    assert {item.domain for item in everything} == {'smithlab.io', 'broadinstitute.org'}


def test_store_respects_an_explicit_anonymize_override(metrics, book):
    run(server.scan_workbook(metrics.file_id, book))

    stored = server.store_domain_analysis(
        [
            server.AnalysisInput(
                domain='broadinstitute.org',
                risk='Low',
                explanation='Low risk, but the customer asked for it',
                anonymize=True,
            )
        ],
        book,
    )

    assert stored.stored[0].action == 'will_be_anonymized'
    assert stored.stored[0].anonymized_domain.startswith('anon')


def test_aliases_are_stable_across_quarters(drive, book, tmp_path, excel):
    q1 = drive.add_workbook('Q1.xlsx', {'S': [['a@lab.io']]}, tmp_path)
    run(server.scan_workbook(q1.file_id, book))
    first = server.store_domain_analysis(
        [server.AnalysisInput(domain='lab.io', risk='High', explanation='a lab')], book
    )

    q2 = drive.add_workbook('Q2.xlsx', {'S': [['b@lab.io']]}, tmp_path)
    run(server.scan_workbook(q2.file_id, book))
    plan = run(server.plan_redaction(q2.file_id, book))

    assert plan.domains_anonymized == {'lab.io': first.stored[0].anonymized_domain}


# -- the audit trail ---------------------------------------------------------------------------
def test_finish_records_the_published_url(drive, metrics, book, excel):
    run(server.scan_workbook(metrics.file_id, book))
    analyze_all(book)
    plan = run(server.plan_redaction(metrics.file_id, book))
    excel.apply(plan.redacted_path, _blocks(plan))

    finished = run(server.finish_redaction(metrics.file_id, plan.redacted_path, book))

    rows = xlsx.read_rows(book, REDACTIONS)[1:]
    assert len(rows) == 2
    assert {row[2] for row in rows} == {finished.redacted_workbook_url}
    assert all(row[3].startswith('https://drive.google.com/file/d/') for row in rows)


def test_finish_uploads_into_the_requested_folder(drive, metrics, book, excel):
    run(server.scan_workbook(metrics.file_id, book))
    analyze_all(book)
    plan = run(server.plan_redaction(metrics.file_id, book))
    excel.apply(plan.redacted_path, _blocks(plan))

    run(server.finish_redaction(metrics.file_id, plan.redacted_path, book, folder_id='shared1'))

    assert drive.created[-1].parent_id == 'shared1'
