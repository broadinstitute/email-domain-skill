import random

import pytest

from email_domain_scrubber.errors import SchemaMismatch
from email_domain_scrubber.workbook import (
    DOMAIN_ANALYSIS,
    DOMAIN_REFERENCES,
    HEADERS,
    REDACTIONS,
    SHEET_ORDER,
    WORKBOOKS,
    AnalysisWorkbook,
    DomainReference,
    RedactionRecord,
    normalize_risk,
)


def test_create_writes_all_sheets_and_headers(backend):
    workbook = AnalysisWorkbook.create(backend, 'Analysis')
    assert workbook.sheet_titles == list(SHEET_ORDER)
    for name in SHEET_ORDER:
        assert backend.sheet_values(workbook.spreadsheet_id, name)[0] == HEADERS[name]


def test_open_adds_sheets_missing_from_an_existing_workbook(backend):
    file = backend.add_spreadsheet('Analysis', {WORKBOOKS: [HEADERS[WORKBOOKS]]})
    workbook = AnalysisWorkbook.open(backend, file.file_id)
    assert workbook.sheet_titles == list(SHEET_ORDER)
    assert backend.sheet_values(file.file_id, DOMAIN_ANALYSIS)[0] == HEADERS[DOMAIN_ANALYSIS]


def test_open_rejects_a_workbook_with_conflicting_headers(backend):
    file = backend.add_spreadsheet('Wrong', {WORKBOOKS: [['Name', 'Owner']]})
    with pytest.raises(SchemaMismatch, match='Workbooks'):
        AnalysisWorkbook.open(backend, file.file_id)


def test_open_is_idempotent(backend):
    created = AnalysisWorkbook.create(backend, 'Analysis')
    reopened = AnalysisWorkbook.open(backend, created.spreadsheet_id)
    assert reopened.sheet_titles == list(SHEET_ORDER)
    assert backend.sheet_values(created.spreadsheet_id, WORKBOOKS) == [HEADERS[WORKBOOKS]]


def test_record_workbook_upserts_by_url(backend, analysis):
    analysis.record_workbook('https://sheets/1', 'Q1 Metrics')
    analysis.record_workbook('https://sheets/2', 'Q2 Metrics')
    analysis.record_workbook('https://sheets/1', 'Q1 Metrics (renamed)')

    rows = backend.sheet_values(analysis.spreadsheet_id, WORKBOOKS)[1:]
    assert rows == [
        ['https://sheets/1', 'Q1 Metrics (renamed)'],
        ['https://sheets/2', 'Q2 Metrics'],
    ]


def test_record_references_skips_duplicates(analysis):
    references = [
        DomainReference('https://link#gid=0&range=A2', 'smithlab.io', '2026-08-04'),
        DomainReference('https://link#gid=0&range=A3', 'smithlab.io', '2026-08-04'),
    ]
    assert len(analysis.record_references(references)) == 2
    assert analysis.record_references(references) == []
    assert analysis.reference_counts() == {'smithlab.io': 2}


def test_multiple_references_per_domain_are_kept(backend, analysis):
    analysis.record_references(
        [
            DomainReference('ref-a', 'smithlab.io', '2026-08-04'),
            DomainReference('ref-b', 'smithlab.io', '2026-08-04'),
            DomainReference('ref-c', 'harvard.edu', '2026-08-04'),
        ]
    )
    rows = backend.sheet_values(analysis.spreadsheet_id, DOMAIN_REFERENCES)[1:]
    assert len(rows) == 3
    assert analysis.reference_counts() == {'smithlab.io': 2, 'harvard.edu': 1}
    assert analysis.sample_references()['smithlab.io'] == ['ref-a', 'ref-b']


def test_ensure_analysis_rows_creates_one_row_per_domain(backend, analysis):
    added = analysis.ensure_analysis_rows(['smithlab.io', 'harvard.edu', 'SmithLab.io'])
    assert added == ['smithlab.io', 'harvard.edu']

    assert analysis.ensure_analysis_rows(['harvard.edu']) == []
    rows = backend.sheet_values(analysis.spreadsheet_id, DOMAIN_ANALYSIS)[1:]
    assert [row[0] for row in rows] == ['smithlab.io', 'harvard.edu']


def test_scan_leaves_anonymized_domain_blank_until_analysis(backend, analysis):
    analysis.ensure_analysis_rows(['smithlab.io'])
    row = backend.sheet_values(analysis.spreadsheet_id, DOMAIN_ANALYSIS)[1]
    assert row == ['smithlab.io', '', '', '']
    assert analysis.pending_domains()[0].domain == 'smithlab.io'


def test_store_analysis_assigns_a_token_to_high_risk_only(backend, analysis):
    analysis.ensure_analysis_rows(['smithlab.io', 'pluralistic.net', 'broadinstitute.org'])
    analysis.store_analysis(
        [
            ('smithlab.io', 'High', 'Personal lab domain', None),
            ('pluralistic.net', 'Medium', 'Single-author blog', None),
            ('broadinstitute.org', 'Low', 'Broad Institute', None),
        ],
        random.Random(7),
    )

    rows = {
        row[0]: row for row in backend.sheet_values(analysis.spreadsheet_id, DOMAIN_ANALYSIS)[1:]
    }
    assert rows['smithlab.io'][1] == 'High'
    assert rows['smithlab.io'][3].startswith('anon')
    assert rows['pluralistic.net'][3] == ''
    assert rows['broadinstitute.org'][3] == ''
    assert analysis.pending_domains() == []


def test_store_analysis_honours_an_explicit_anonymize_flag(analysis):
    analysis.ensure_analysis_rows(['tinylab.org', 'famous.com'])
    analysis.store_analysis(
        [
            ('tinylab.org', 'Medium', 'Two-person lab', True),
            ('famous.com', 'High', 'Named person, but already public', False),
        ],
        random.Random(3),
    )
    mapping = analysis.anonymized_mapping()
    assert 'tinylab.org' in mapping
    assert 'famous.com' not in mapping


def test_store_analysis_retains_an_existing_token(analysis):
    analysis.ensure_analysis_rows(['smithlab.io'])
    analysis.store_analysis([('smithlab.io', 'High', 'first pass', None)], random.Random(1))
    first = analysis.anonymized_mapping()['smithlab.io']

    analysis.store_analysis([('smithlab.io', 'Medium', 'second pass', None)], random.Random(99))
    assert analysis.anonymized_mapping()['smithlab.io'] == first
    assert analysis.analysis_by_domain()['smithlab.io'].explanation == 'second pass'


def test_store_analysis_updates_in_place_without_adding_rows(backend, analysis):
    analysis.ensure_analysis_rows(['a.org', 'b.org'])
    analysis.store_analysis([('b.org', 'Low', 'known org', None)], random.Random(1))
    analysis.store_analysis([('b.org', 'Low', 'known org, reconfirmed', None)], random.Random(1))

    rows = backend.sheet_values(analysis.spreadsheet_id, DOMAIN_ANALYSIS)[1:]
    assert [row[0] for row in rows] == ['a.org', 'b.org']
    assert rows[1][2] == 'known org, reconfirmed'


def test_store_analysis_creates_a_row_for_an_unscanned_domain(backend, analysis):
    analysis.store_analysis([('surprise.io', 'High', 'not from a scan', None)], random.Random(1))
    rows = backend.sheet_values(analysis.spreadsheet_id, DOMAIN_ANALYSIS)[1:]
    assert rows[0][0] == 'surprise.io'


def test_store_analysis_does_not_reuse_a_token(analysis):
    domains = [f'lab{index}.org' for index in range(50)]
    analysis.ensure_analysis_rows(domains)
    analysis.store_analysis(
        [(domain, 'High', 'personal lab', None) for domain in domains], random.Random(11)
    )
    tokens = list(analysis.anonymized_mapping().values())
    assert len(tokens) == len(set(tokens)) == 50


def test_store_analysis_rejects_a_risk_outside_the_taxonomy(analysis):
    with pytest.raises(ValueError, match='Critical'):
        analysis.store_analysis([('x.org', 'Critical', 'nope', None)], random.Random(1))


@pytest.mark.parametrize(('given', 'expected'), [('high', 'High'), ('  LOW ', 'Low')])
def test_normalize_risk(given, expected):
    assert normalize_risk(given) == expected


def test_record_redactions_appends(backend, analysis):
    analysis.record_redactions(
        [
            RedactionRecord('src', 'dst', 'ref-a', 'smithlab.io', 'anon0001', '2026-08-04'),
            RedactionRecord('src', 'dst', 'ref-b', 'smithlab.io', 'anon0001', '2026-08-04'),
        ]
    )
    rows = backend.sheet_values(analysis.spreadsheet_id, REDACTIONS)[1:]
    assert len(rows) == 2
    assert rows[0] == ['2026-08-04', 'src', 'dst', 'ref-a', 'smithlab.io', 'anon0001']
