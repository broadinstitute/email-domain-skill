"""The local analysis workbook: schema, upserts, and alias stability."""

import random

import pytest

from email_domain_scrubber import xlsx
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


def test_open_creates_the_workbook_with_headers(tmp_path):
    path = tmp_path / 'analysis.xlsx'
    workbook = AnalysisWorkbook.open(path)

    assert path.exists()
    assert workbook.sheet_titles == list(SHEET_ORDER)
    for name in SHEET_ORDER:
        assert xlsx.read_rows(path, name) == [HEADERS[name]]


def test_open_is_idempotent_and_preserves_data(tmp_path):
    path = tmp_path / 'analysis.xlsx'
    first = AnalysisWorkbook.open(path)
    first.store_analysis([('lab.io', 'High', 'a lab', None)], random.Random(1))

    second = AnalysisWorkbook.open(path)

    assert [row.domain for row in second.analysis_rows()] == ['lab.io']
    assert second.sheet_titles == list(SHEET_ORDER)


def test_open_adds_a_missing_sheet_to_an_existing_workbook(tmp_path):
    path = tmp_path / 'analysis.xlsx'
    xlsx.create(path, {WORKBOOKS: [HEADERS[WORKBOOKS]]})

    workbook = AnalysisWorkbook.open(path)

    assert set(workbook.sheet_titles) >= set(SHEET_ORDER)
    assert xlsx.read_rows(path, REDACTIONS) == [HEADERS[REDACTIONS]]


def test_mismatched_headers_are_rejected(tmp_path):
    path = tmp_path / 'analysis.xlsx'
    xlsx.create(path, {name: [HEADERS[name]] for name in SHEET_ORDER})
    xlsx.rewrite(path, {DOMAIN_ANALYSIS: [['Something', 'Else']]})

    with pytest.raises(SchemaMismatch, match=DOMAIN_ANALYSIS):
        AnalysisWorkbook.open(path)


def test_record_workbook_upserts_by_url(analysis):
    analysis.record_workbook('https://drive.google.com/file/d/abc/view', 'Q1')
    analysis.record_workbook('https://drive.google.com/file/d/abc/view', 'Q1 renamed')
    analysis.record_workbook('https://drive.google.com/file/d/def/view', 'Q2')

    assert analysis.scanned_workbooks() == {
        'https://drive.google.com/file/d/abc/view': 'Q1 renamed',
        'https://drive.google.com/file/d/def/view': 'Q2',
    }


def test_references_are_appended_once(analysis):
    first = [
        DomainReference('ref-a', 'lab.io', '2026-01-01'),
        DomainReference('ref-b', 'lab.io', '2026-01-01'),
    ]
    assert len(analysis.record_references(first)) == 2

    again = analysis.record_references([*first, DomainReference('ref-c', 'lab.io', '2026-01-02')])

    assert [ref.reference for ref in again] == ['ref-c']
    assert analysis.reference_counts() == {'lab.io': 3}


def test_reference_domains_are_matched_case_insensitively(analysis):
    analysis.record_references([DomainReference('ref-a', 'Lab.IO', '2026-01-01')])
    assert analysis.record_references([DomainReference('ref-a', 'lab.io', '2026-01-01')]) == []


def test_sample_references_are_stable_and_capped(analysis):
    analysis.record_references(
        [DomainReference(f'ref-{index}', 'lab.io', '2026-01-01') for index in range(5)]
    )

    samples = analysis.sample_references()['lab.io']
    assert samples == ['ref-0', 'ref-1', 'ref-2']
    assert analysis.sample_references()['lab.io'] == samples


def test_ensure_analysis_rows_adds_only_unseen_domains(analysis):
    assert analysis.ensure_analysis_rows(['a.org', 'b.org', 'a.org']) == ['a.org', 'b.org']
    assert analysis.ensure_analysis_rows(['b.org', 'c.org']) == ['c.org']
    assert [row.domain for row in analysis.analysis_rows()] == ['a.org', 'b.org', 'c.org']


def test_pending_domains_are_those_without_a_risk(analysis):
    analysis.ensure_analysis_rows(['a.org', 'b.org'])
    analysis.store_analysis([('a.org', 'Low', 'known org', None)], random.Random(1))

    assert [row.domain for row in analysis.pending_domains()] == ['b.org']


def test_high_risk_gets_an_alias_and_low_risk_does_not(analysis):
    stored = analysis.store_analysis(
        [('lab.io', 'High', 'a person', None), ('broad.org', 'Low', 'an org', None)],
        random.Random(7),
    )

    by_domain = {row.domain: row for row in stored}
    assert by_domain['lab.io'].anonymized_domain.startswith('anon')
    assert by_domain['broad.org'].anonymized_domain == ''


def test_an_explicit_anonymize_flag_overrides_the_risk_default(analysis):
    stored = analysis.store_analysis(
        [('low.org', 'Low', 'asked for anyway', True), ('high.io', 'High', 'not this time', False)],
        random.Random(3),
    )

    by_domain = {row.domain: row for row in stored}
    assert by_domain['low.org'].anonymized_domain.startswith('anon')
    assert by_domain['high.io'].anonymized_domain == ''


def test_an_alias_survives_a_later_downgrade(analysis):
    first = analysis.store_analysis([('lab.io', 'High', 'a person', None)], random.Random(1))
    alias = first[0].anonymized_domain

    second = analysis.store_analysis([('lab.io', 'Low', 'actually an org', None)], random.Random(2))

    assert second[0].risk == 'Low'
    assert second[0].anonymized_domain == alias
    assert analysis.anonymized_mapping() == {'lab.io': alias}


def test_restoring_updates_in_place_rather_than_appending(analysis):
    analysis.store_analysis([('lab.io', 'High', 'first pass', None)], random.Random(1))
    analysis.store_analysis([('lab.io', 'High', 'second pass', None)], random.Random(1))

    rows = analysis.analysis_rows()
    assert len(rows) == 1
    assert rows[0].explanation == 'second pass'


def test_store_analysis_fills_a_row_opened_by_scanning(analysis):
    analysis.ensure_analysis_rows(['lab.io'])
    analysis.store_analysis([('LAB.IO', 'High', 'a person', None)], random.Random(1))

    rows = analysis.analysis_rows()
    assert len(rows) == 1
    assert rows[0].domain == 'lab.io'


def test_aliases_are_unique_across_domains(analysis):
    stored = analysis.store_analysis(
        [(f'lab{index}.io', 'High', 'a person', None) for index in range(25)], random.Random(11)
    )
    aliases = [row.anonymized_domain for row in stored]
    assert len(set(aliases)) == 25


def test_an_invalid_risk_is_rejected(analysis):
    with pytest.raises(ValueError, match='Critical'):
        analysis.store_analysis([('lab.io', 'Critical', 'nope', None)], random.Random(1))


def test_normalize_risk_canonicalizes_case():
    assert normalize_risk(' high ') == 'High'
    assert normalize_risk('LOW') == 'Low'


def test_record_redactions_appends_rows(analysis):
    analysis.record_redactions(
        [
            RedactionRecord('src', 'dst', 'ref-1', 'lab.io', 'anon0001', '2026-02-01'),
            RedactionRecord('src', 'dst', 'ref-2', 'lab.io', 'anon0001', '2026-02-01'),
        ]
    )

    rows = xlsx.read_rows(analysis.path, REDACTIONS)[1:]
    assert len(rows) == 2
    assert rows[0] == ['2026-02-01', 'src', 'dst', 'ref-1', 'lab.io', 'anon0001']


def test_writes_to_one_sheet_do_not_disturb_another(analysis):
    analysis.record_references([DomainReference('ref-a', 'lab.io', '2026-01-01')])
    analysis.store_analysis([('lab.io', 'High', 'a person', None)], random.Random(1))
    analysis.record_workbook('https://drive.google.com/file/d/abc/view', 'Q1')

    assert analysis.reference_counts() == {'lab.io': 1}
    assert len(analysis.analysis_rows()) == 1
    assert len(analysis.scanned_workbooks()) == 1
    assert xlsx.read_rows(analysis.path, DOMAIN_REFERENCES)[0] == HEADERS[DOMAIN_REFERENCES]
