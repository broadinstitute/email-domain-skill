"""The MCP tool surface, driven the way the analysis skill drives it."""

import asyncio
from pathlib import Path

import pytest

from email_domain_scrubber import server, xlsx
from email_domain_scrubber.errors import RedactionNotApplied, UnanalyzedDomains
from email_domain_scrubber.staging import ANALYSIS_WORKBOOK_ENV
from email_domain_scrubber.workbook import REDACTIONS, WORKBOOKS

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
def _use_fakes(staging):
    server.set_backend(staging)
    yield
    server.set_backend(None)


@pytest.fixture
def metrics(tmp_path):
    return str(write_xlsx(tmp_path / 'Q1 Metrics.xlsx', USERS))


@pytest.fixture
def book(tmp_path):
    """An explicit analysis workbook path, as the skill would pass."""
    return str(tmp_path / 'analysis.xlsx')


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


def _blocks(plan):
    """The plan's write blocks in the shape the Recorder (and Excel MCP server) applies."""
    from email_domain_scrubber.redact import WriteBlock

    return [
        WriteBlock(sheet=block.sheet_name, start_cell=block.start_cell, values=block.data)
        for block in plan.write_blocks
    ]


# -- registration ------------------------------------------------------------------------------
def test_all_tools_are_registered():
    """Guards against a tool being added without its @mcp.tool() decorator."""
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == {
        'scan_workbook',
        'list_domains_for_analysis',
        'store_domain_analysis',
        'plan_redaction',
        'finish_redaction',
    }


def test_finish_redaction_takes_no_upload_target():
    """Nothing is published anywhere, so the tool exposes no destination parameter."""
    tools = asyncio.run(server.mcp.list_tools())
    finish = next(tool for tool in tools if tool.name == 'finish_redaction')

    assert set(finish.input_schema['properties']) == {
        'workbook',
        'redacted_path',
        'analysis_workbook',
    }


# -- the analysis workbook ---------------------------------------------------------------------
def test_the_analysis_workbook_is_created_on_first_use(metrics, book):
    assert not Path(book).exists()
    result = server.scan_workbook(metrics, book)

    assert Path(book).is_file()
    assert result.analysis_workbook_path == book


def test_the_analysis_workbook_defaults_to_the_environment(monkeypatch, metrics, book):
    monkeypatch.setenv(ANALYSIS_WORKBOOK_ENV, book)

    result = server.scan_workbook(metrics)

    assert result.analysis_workbook_path == book
    assert result.domains_found == 2


def test_the_analysis_workbook_falls_back_to_the_work_directory(metrics, staging):
    result = server.scan_workbook(metrics)
    assert result.analysis_workbook_path.endswith('analysis.xlsx')


# -- the happy path ----------------------------------------------------------------------------
def test_scan_list_store_plan_write_finish(metrics, book, excel):
    scan = server.scan_workbook(metrics, book)
    assert scan.domains_found == 2
    assert scan.references_recorded == 3
    assert set(scan.new_domains) == {'smithlab.io', 'broadinstitute.org'}
    assert set(scan.pending_analysis) == {'smithlab.io', 'broadinstitute.org'}
    assert scan.scanned_workbook_path == metrics
    assert scan.scanned_workbook_title == 'Q1 Metrics.xlsx'

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

    plan = server.plan_redaction(metrics, book)
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

    finished = server.finish_redaction(metrics, plan.redacted_path, book)
    assert finished.cells_changed == 2
    assert finished.remaining_domains == ['broadinstitute.org']
    assert finished.redactions_recorded == 2
    assert finished.redacted_workbook_title == 'Q1 Metrics (anonymized).xlsx'
    assert finished.source_workbook_path == metrics
    assert finished.redacted_workbook_path == plan.redacted_path


def test_a_contiguous_column_needs_one_write_call(book, tmp_path, excel):
    rows = [['Email'], *[[f'user{index}@lab.io'] for index in range(30)]]
    path = str(write_xlsx(tmp_path / 'Big.xlsx', {'Users': rows}))
    server.scan_workbook(path, book)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='lab.io', risk='High', explanation='a lab')], book
    )

    plan = server.plan_redaction(path, book)

    assert plan.cells_to_change == 30
    assert len(plan.write_blocks) == 1


def test_scattered_edits_cost_one_write_call_each(book, tmp_path):
    """The bound on write calls: one per redacted cell when kept cells break up the runs."""
    rows = [
        ['Email'],
        *[[f'u{i}@{"lab.io" if i % 2 else "broadinstitute.org"}'] for i in range(10)],
    ]
    path = str(write_xlsx(tmp_path / 'Mixed.xlsx', {'Users': rows}))
    server.scan_workbook(path, book)
    server.store_domain_analysis(
        [
            server.AnalysisInput(domain='lab.io', risk='High', explanation='a lab'),
            server.AnalysisInput(domain='broadinstitute.org', risk='Low', explanation='Broad'),
        ],
        book,
    )

    plan = server.plan_redaction(path, book)

    assert plan.cells_to_change == 5
    assert len(plan.write_blocks) == 5


# -- the source is never touched ---------------------------------------------------------------
def test_the_original_is_never_modified(metrics, book, excel):
    before = Path(metrics).read_bytes()
    server.scan_workbook(metrics, book)
    analyze_all(book)
    plan = server.plan_redaction(metrics, book)
    excel.apply(plan.redacted_path, _blocks(plan))
    server.finish_redaction(metrics, plan.redacted_path, book)

    assert Path(metrics).read_bytes() == before
    assert xlsx.read_rows(metrics, 'Users')[1] == ['Alice', 'alice@smithlab.io']


def test_the_redacted_copy_lands_in_the_work_directory(metrics, book, staging):
    server.scan_workbook(metrics, book)
    analyze_all(book)

    plan = server.plan_redaction(metrics, book)

    redacted = Path(plan.redacted_path)
    assert staging.root in redacted.parents
    assert redacted.parent != Path(metrics).parent


def test_planning_writes_only_the_untouched_copy(metrics, book):
    server.scan_workbook(metrics, book)
    analyze_all(book)

    plan = server.plan_redaction(metrics, book)

    # The copy exists but still holds the original values: planning changes nothing.
    assert xlsx.read_rows(plan.redacted_path, 'Users')[1] == ['Alice', 'alice@smithlab.io']


# -- refusals ----------------------------------------------------------------------------------
def test_plan_refuses_before_analysis_is_complete(metrics, book):
    server.scan_workbook(metrics, book)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )

    with pytest.raises(UnanalyzedDomains, match='broadinstitute.org'):
        server.plan_redaction(metrics, book)


def test_finish_refuses_when_the_blocks_were_never_applied(metrics, book):
    server.scan_workbook(metrics, book)
    analyze_all(book)
    plan = server.plan_redaction(metrics, book)

    with pytest.raises(RedactionNotApplied, match='smithlab.io'):
        server.finish_redaction(metrics, plan.redacted_path, book)


def test_finish_refuses_a_path_that_does_not_exist(metrics, book, tmp_path):
    server.scan_workbook(metrics, book)
    analyze_all(book)

    with pytest.raises(FileNotFoundError):
        server.finish_redaction(metrics, str(tmp_path / 'nope.xlsx'), book)


# -- re-scanning -------------------------------------------------------------------------------
def test_rescanning_preserves_analysis_and_adds_only_new_domains(metrics, book):
    server.scan_workbook(metrics, book)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )
    before = {
        item.domain: item.anonymized_domain
        for item in server.list_domains_for_analysis(book, include_analyzed=True)
    }

    grown = {'Users': [*USERS['Users'], ['Dave', 'dave@newlab.io']]}
    write_xlsx(Path(metrics), grown)

    again = server.scan_workbook(metrics, book)

    assert again.new_domains == ['newlab.io']
    assert again.references_recorded == 1
    assert sorted(again.pending_analysis) == ['broadinstitute.org', 'newlab.io']

    after = {
        item.domain: item.anonymized_domain
        for item in server.list_domains_for_analysis(book, include_analyzed=True)
    }
    assert after['smithlab.io'] == before['smithlab.io']


def test_rescanning_records_the_workbook_once(metrics, book):
    server.scan_workbook(metrics, book)
    server.scan_workbook(metrics, book)

    rows = xlsx.read_rows(book, WORKBOOKS)[1:]
    assert len(rows) == 1
    assert rows[0][0] == metrics
    assert rows[0][1] == 'Q1 Metrics.xlsx'


# -- listing and storing -----------------------------------------------------------------------
def test_list_domains_can_include_analyzed(metrics, book):
    server.scan_workbook(metrics, book)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )

    assert [item.domain for item in server.list_domains_for_analysis(book)] == [
        'broadinstitute.org'
    ]
    everything = server.list_domains_for_analysis(book, include_analyzed=True)
    assert {item.domain for item in everything} == {'smithlab.io', 'broadinstitute.org'}


def test_store_respects_an_explicit_anonymize_override(metrics, book):
    server.scan_workbook(metrics, book)

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


def test_aliases_are_stable_across_quarters(book, tmp_path):
    q1 = str(write_xlsx(tmp_path / 'Q1.xlsx', {'S': [['a@lab.io']]}))
    server.scan_workbook(q1, book)
    first = server.store_domain_analysis(
        [server.AnalysisInput(domain='lab.io', risk='High', explanation='a lab')], book
    )

    q2 = str(write_xlsx(tmp_path / 'Q2.xlsx', {'S': [['b@lab.io']]}))
    server.scan_workbook(q2, book)
    plan = server.plan_redaction(q2, book)

    assert plan.domains_anonymized == {'lab.io': first.stored[0].anonymized_domain}


# -- the audit trail ---------------------------------------------------------------------------
def test_finish_records_the_redacted_file(metrics, book, excel):
    server.scan_workbook(metrics, book)
    analyze_all(book)
    plan = server.plan_redaction(metrics, book)
    excel.apply(plan.redacted_path, _blocks(plan))

    finished = server.finish_redaction(metrics, plan.redacted_path, book)

    rows = xlsx.read_rows(book, REDACTIONS)[1:]
    assert len(rows) == 2
    assert {row[1] for row in rows} == {metrics}
    assert {row[2] for row in rows} == {finished.redacted_workbook_path}
    # The Reference locator stays a file:// URL with the cell in the fragment.
    redacted_uri = Path(plan.redacted_path).as_uri()
    assert {row[3] for row in rows} == {f'{redacted_uri}#Users!B2', f'{redacted_uri}#Users!B4'}
