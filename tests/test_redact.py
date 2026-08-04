import random

import pytest

from email_domain_scrubber.errors import UnanalyzedDomains
from email_domain_scrubber.redact import (
    execute_redaction,
    plan_redaction,
    rescan_for_verification,
    unique_copy_name,
)
from email_domain_scrubber.scan import resolve_workbook, scan_spreadsheet
from email_domain_scrubber.workbook import REDACTIONS


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


def _resolve_and_scan(backend, file_id):
    resolved = resolve_workbook(backend, file_id)
    return resolved, scan_spreadsheet(backend, resolved.info)


def test_plan_refuses_while_a_domain_is_unanalyzed(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    analysis.ensure_analysis_rows(['smithlab.io', 'broadinstitute.org'])
    analysis.store_analysis([('smithlab.io', 'High', 'personal lab', None)], random.Random(1))

    with pytest.raises(UnanalyzedDomains) as caught:
        plan_redaction(resolved, hits, analysis)
    assert caught.value.domains == ['broadinstitute.org']


def _analyze_all(analysis):
    analysis.store_analysis(
        [
            ('smithlab.io', 'High', 'Personal lab domain', None),
            ('broadinstitute.org', 'Low', 'Broad Institute', None),
        ],
        random.Random(5),
    )


def test_plan_targets_only_domains_with_an_alias(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)

    plan = plan_redaction(resolved, hits, analysis)
    assert list(plan.mapped_domains) == ['smithlab.io']
    assert plan.left_as_is == ['broadinstitute.org']
    assert [edit.a1 for edit in plan.edits] == ['B2', 'B4']


def test_planning_writes_nothing(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)
    before = backend.write_calls
    files_before = set(backend.files)

    plan_redaction(resolved, hits, analysis)

    assert backend.write_calls == before
    assert set(backend.files) == files_before


def test_execute_leaves_the_source_untouched(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)
    original = [list(row) for row in backend.sheet_values(metrics.file_id, 'Users')]

    execute_redaction(backend, resolved, plan_redaction(resolved, hits, analysis), analysis)

    assert backend.sheet_values(metrics.file_id, 'Users') == original


def test_execute_rewrites_the_copy(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)
    plan = plan_redaction(resolved, hits, analysis)
    alias = plan.mapped_domains['smithlab.io']

    result = execute_redaction(backend, resolved, plan, analysis)

    assert result.redacted_title == 'Q1 Metrics (anonymized)'
    copy_id = result.redacted_url.split('/d/')[1].split('/')[0]
    assert backend.sheet_values(copy_id, 'Users') == [
        ['User', 'Email'],
        ['Alice', f'alice@{alias}'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', f'carol@{alias}'],
    ]
    assert backend.files[copy_id].parents == ('folder1',)


def test_execute_records_one_redaction_row_per_cell(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)
    plan = plan_redaction(resolved, hits, analysis)
    result = execute_redaction(backend, resolved, plan, analysis)

    rows = backend.sheet_values(analysis.spreadsheet_id, REDACTIONS)[1:]
    assert len(rows) == 2
    assert {row[4] for row in rows} == {'smithlab.io'}
    assert {row[2] for row in rows} == {result.redacted_url}
    assert all(row[3].startswith('https://docs.google.com/spreadsheets/') for row in rows)


def test_redaction_references_point_at_the_copys_sheet_ids(backend, analysis, metrics):
    """A Drive copy may renumber sheets, so links must be built from the copy's own gids."""
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)
    plan = plan_redaction(resolved, hits, analysis)
    result = execute_redaction(backend, resolved, plan, analysis)

    copy_id = result.redacted_url.split('/d/')[1].split('/')[0]
    copy_gid = backend.get_spreadsheet(copy_id).sheets[0].sheet_id
    source_gid = backend.get_spreadsheet(metrics.file_id).sheets[0].sheet_id
    assert copy_gid != source_gid
    assert all(f'gid={copy_gid}' in record.reference for record in result.records)


def test_rescan_of_the_copy_shows_only_the_domains_left_as_is(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)
    result = execute_redaction(
        backend, resolved, plan_redaction(resolved, hits, analysis), analysis
    )

    copy_id = result.redacted_url.split('/d/')[1].split('/')[0]
    remaining = rescan_for_verification(backend, backend.get_spreadsheet(copy_id))
    assert remaining == ['broadinstitute.org']


def test_a_second_redaction_does_not_overwrite_the_first_copy(backend, analysis, metrics):
    resolved, hits = _resolve_and_scan(backend, metrics.file_id)
    _analyze_all(analysis)

    first = execute_redaction(backend, resolved, plan_redaction(resolved, hits, analysis), analysis)
    second = execute_redaction(
        backend, resolved, plan_redaction(resolved, hits, analysis), analysis
    )

    assert first.redacted_title == 'Q1 Metrics (anonymized)'
    assert second.redacted_title == 'Q1 Metrics (anonymized) 2'
    assert first.redacted_url != second.redacted_url


def test_unique_copy_name_skips_taken_names(backend):
    backend.add_spreadsheet('Report (anonymized)', {'S': []}, parent='folder1')
    assert unique_copy_name(backend, 'Report (anonymized)', 'folder1') == 'Report (anonymized) 2'
    assert unique_copy_name(backend, 'Other (anonymized)', 'folder1') == 'Other (anonymized)'


def test_a_cell_holding_two_domains_is_rewritten_once(backend, analysis):
    file = backend.add_spreadsheet('M', {'S': [['a@one.org and b@two.org']]}, parent='f')
    resolved, hits = _resolve_and_scan(backend, file.file_id)
    analysis.store_analysis(
        [('one.org', 'High', 'person one', None), ('two.org', 'High', 'person two', None)],
        random.Random(2),
    )
    plan = plan_redaction(resolved, hits, analysis)
    assert len(plan.edits) == 1

    result = execute_redaction(backend, resolved, plan, analysis)
    copy_id = result.redacted_url.split('/d/')[1].split('/')[0]
    one = plan.mapped_domains['one.org']
    two = plan.mapped_domains['two.org']
    assert backend.sheet_values(copy_id, 'S')[0][0] == f'a@{one} and b@{two}'
    # One row per (cell, domain) so the record accounts for both replacements.
    assert len(result.records) == 2
