"""The MCP tool surface, driven the way the analysis skill drives it."""

import pytest

from email_domain_scrubber import server
from email_domain_scrubber.errors import InvalidWorkbookReference, UnanalyzedDomains
from email_domain_scrubber.workbook import DOMAIN_ANALYSIS, REDACTIONS, WORKBOOKS


@pytest.fixture(autouse=True)
def _use_fake_backend(backend, monkeypatch):
    monkeypatch.delenv(server.ANALYSIS_WORKBOOK_ENV, raising=False)
    server.set_backend(backend)
    yield
    server.set_backend(None)


@pytest.fixture
def metrics(backend):
    return backend.add_spreadsheet(
        'Q1 Metrics',
        {
            'Users': [
                ['User', 'Email'],
                ['Alice', 'alice@smithlab.io'],
                ['Bob', 'bob@broadinstitute.org'],
                ['Carol', 'carol@smithlab.io'],
            ]
        },
        parent='folder1',
    )


def _analysis_url():
    return server.create_analysis_workbook('Email Domain Analysis').analysis_workbook_url


def test_create_analysis_workbook():
    created = server.create_analysis_workbook()
    assert created.sheets == [WORKBOOKS, 'DomainReferences', DOMAIN_ANALYSIS, REDACTIONS]
    assert created.analysis_workbook_url.startswith('https://docs.google.com/spreadsheets/d/')


def test_tools_require_an_analysis_workbook(metrics):
    with pytest.raises(InvalidWorkbookReference, match=server.ANALYSIS_WORKBOOK_ENV):
        server.scan_workbook(metrics.file_id)


def test_analysis_workbook_defaults_to_the_environment(monkeypatch, metrics):
    monkeypatch.setenv(server.ANALYSIS_WORKBOOK_ENV, _analysis_url())
    result = server.scan_workbook(metrics.file_id)
    assert result.domains_found == 2


def test_scan_then_list_then_store_then_redact(backend, metrics):
    analysis_url = _analysis_url()

    scan = server.scan_workbook(metrics.file_id, analysis_url)
    assert scan.domains_found == 2
    assert scan.references_recorded == 3
    assert set(scan.new_domains) == {'smithlab.io', 'broadinstitute.org'}
    assert set(scan.pending_analysis) == {'smithlab.io', 'broadinstitute.org'}
    assert not scan.converted_from_upload

    pending = server.list_domains_for_analysis(analysis_url)
    by_domain = {item.domain: item for item in pending}
    assert by_domain['smithlab.io'].reference_count == 2
    assert len(by_domain['smithlab.io'].example_references) == 2
    assert by_domain['broadinstitute.org'].reference_count == 1

    stored = server.store_domain_analysis(
        [
            server.AnalysisInput(
                domain='smithlab.io', risk='High', explanation='Personal lab domain'
            ),
            server.AnalysisInput(
                domain='broadinstitute.org', risk='Low', explanation='Broad Institute'
            ),
        ],
        analysis_url,
    )
    assert stored.still_pending == []
    actions = {item.domain: item.action for item in stored.stored}
    assert actions == {'smithlab.io': 'will_be_anonymized', 'broadinstitute.org': 'left_as_is'}
    alias = next(item.anonymized_domain for item in stored.stored if item.domain == 'smithlab.io')

    dry = server.redact_workbook(metrics.file_id, analysis_url, dry_run=True)
    assert dry.dry_run
    assert dry.cells_changed == 2
    assert dry.redacted_workbook_url == ''
    assert dry.domains_anonymized == {'smithlab.io': alias}
    assert dry.domains_left_as_is == ['broadinstitute.org']
    assert dry.sample_changes[0].after == f'alice@{alias}'

    real = server.redact_workbook(metrics.file_id, analysis_url)
    assert not real.dry_run
    assert real.redacted_workbook_title == 'Q1 Metrics (anonymized)'
    assert real.cells_changed == 2
    assert real.remaining_domains == ['broadinstitute.org']

    copy_id = real.redacted_workbook_url.split('/d/')[1].split('/')[0]
    assert backend.sheet_values(copy_id, 'Users')[1] == ['Alice', f'alice@{alias}']
    assert backend.sheet_values(metrics.file_id, 'Users')[1] == ['Alice', 'alice@smithlab.io']


def test_dry_run_creates_no_files(backend, metrics):
    analysis_url = _analysis_url()
    server.scan_workbook(metrics.file_id, analysis_url)
    server.store_domain_analysis(
        [
            server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab'),
            server.AnalysisInput(domain='broadinstitute.org', risk='Low', explanation='Broad'),
        ],
        analysis_url,
    )
    before = set(backend.files)
    server.redact_workbook(metrics.file_id, analysis_url, dry_run=True)
    assert set(backend.files) == before


def test_redact_refuses_before_analysis_is_complete(metrics):
    analysis_url = _analysis_url()
    server.scan_workbook(metrics.file_id, analysis_url)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], analysis_url
    )
    with pytest.raises(UnanalyzedDomains, match='broadinstitute.org'):
        server.redact_workbook(metrics.file_id, analysis_url, dry_run=True)


def test_rescanning_preserves_analysis_and_adds_only_new_domains(backend, metrics):
    analysis_url = _analysis_url()
    server.scan_workbook(metrics.file_id, analysis_url)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], analysis_url
    )
    alias = server.list_domains_for_analysis(analysis_url, include_analyzed=True)
    before = {item.domain: item.anonymized_domain for item in alias}

    sheet = backend.files[metrics.file_id].sheets[0]
    sheet.set(4, 1, 'dave@newlab.io')
    again = server.scan_workbook(metrics.file_id, analysis_url)

    assert again.new_domains == ['newlab.io']
    assert again.references_recorded == 1
    assert again.pending_analysis == ['broadinstitute.org', 'newlab.io']

    after = {
        item.domain: item.anonymized_domain
        for item in server.list_domains_for_analysis(analysis_url, include_analyzed=True)
    }
    assert after['smithlab.io'] == before['smithlab.io']


def test_rescanning_records_the_workbook_once(backend, metrics):
    analysis_url = _analysis_url()
    server.scan_workbook(metrics.file_id, analysis_url)
    server.scan_workbook(metrics.file_id, analysis_url)

    analysis_id = analysis_url.split('/d/')[1].split('/')[0]
    rows = backend.sheet_values(analysis_id, WORKBOOKS)[1:]
    assert len(rows) == 1
    assert rows[0][1] == 'Q1 Metrics'


def test_scan_converts_a_drive_xlsx_upload(backend):
    analysis_url = _analysis_url()
    upload = backend.add_upload(
        'Q2 Metrics.xlsx',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        parent='folder1',
    )
    # The converted copy is what gets scanned, so put the data there.
    scan_first = server.scan_workbook(upload.file_id, analysis_url)
    assert scan_first.converted_from_upload
    assert scan_first.scanned_workbook_title == 'Q2 Metrics (Sheets)'

    converted_id = scan_first.scanned_workbook_url.split('/d/')[1].split('/')[0]
    backend.files[converted_id].sheets[0].set(0, 0, 'pi@tinylab.org')
    scan_second = server.scan_workbook(upload.file_id, analysis_url)

    assert scan_second.scanned_workbook_url == scan_first.scanned_workbook_url
    assert scan_second.new_domains == ['tinylab.org']


def test_list_domains_can_include_analyzed(metrics):
    analysis_url = _analysis_url()
    server.scan_workbook(metrics.file_id, analysis_url)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], analysis_url
    )
    assert [item.domain for item in server.list_domains_for_analysis(analysis_url)] == [
        'broadinstitute.org'
    ]
    everything = server.list_domains_for_analysis(analysis_url, include_analyzed=True)
    assert {item.domain for item in everything} == {'smithlab.io', 'broadinstitute.org'}


def test_store_respects_an_explicit_anonymize_override(metrics):
    analysis_url = _analysis_url()
    server.scan_workbook(metrics.file_id, analysis_url)
    stored = server.store_domain_analysis(
        [
            server.AnalysisInput(
                domain='broadinstitute.org',
                risk='Low',
                explanation='Low risk, but the customer asked for it',
                anonymize=True,
            )
        ],
        analysis_url,
    )
    assert stored.stored[0].action == 'will_be_anonymized'
    assert stored.stored[0].anonymized_domain.startswith('anon')


def test_all_tools_are_registered():
    """Guards against a tool being added without its @mcp.tool() decorator."""
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == {
        'create_analysis_workbook',
        'scan_workbook',
        'list_domains_for_analysis',
        'store_domain_analysis',
        'redact_workbook',
    }
