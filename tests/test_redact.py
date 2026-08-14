"""Planning a redaction, writing it into the copy, and verifying it landed."""

import random

import pytest

from email_domain_scrubber import xlsx
from email_domain_scrubber.errors import RedactionNotApplied, UnanalyzedDomains
from email_domain_scrubber.redact import apply, create_copy, plan_redaction, record, redact, verify
from email_domain_scrubber.scan import open_workbook, scan_path
from email_domain_scrubber.workbook import REDACTIONS

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


@pytest.fixture
def staged(metrics):
    return open_workbook(str(metrics))


@pytest.fixture
def hits(staged):
    return scan_path(staged.path, staged.url)


def analyze_all(analysis):
    analysis.store_analysis(
        [
            ('smithlab.io', 'High', 'Personal lab domain', None),
            ('broadinstitute.org', 'Low', 'Broad Institute', None),
        ],
        random.Random(5),
    )


# -- planning ----------------------------------------------------------------------------------
def test_plan_refuses_while_a_domain_is_unanalyzed(analysis, staged, hits):
    analysis.ensure_analysis_rows(['smithlab.io', 'broadinstitute.org'])
    analysis.store_analysis([('smithlab.io', 'High', 'personal lab', None)], random.Random(1))

    with pytest.raises(UnanalyzedDomains) as caught:
        plan_redaction(staged, hits, analysis)
    assert caught.value.domains == ['broadinstitute.org']


def test_plan_targets_only_domains_with_an_alias(analysis, staged, hits):
    analyze_all(analysis)

    plan = plan_redaction(staged, hits, analysis)

    assert list(plan.mapped_domains) == ['smithlab.io']
    assert plan.left_as_is == ['broadinstitute.org']
    assert [edit.a1 for edit in plan.edits] == ['B2', 'B4']
    assert plan.cells_to_change == 2


def test_the_alias_column_alone_decides_what_is_replaced(analysis, staged, hits):
    """A High row whose alias was cleared is left in; a Low row given one is replaced."""
    analysis.store_analysis(
        [
            ('smithlab.io', 'High', 'personal lab', False),
            ('broadinstitute.org', 'Low', 'Broad', True),
        ],
        random.Random(3),
    )

    plan = plan_redaction(staged, hits, analysis)

    assert list(plan.mapped_domains) == ['broadinstitute.org']
    assert plan.left_as_is == ['smithlab.io']


def test_planning_touches_no_files(analysis, staged, hits, staging):
    analyze_all(analysis)
    before = sorted(path.name for path in staging.root.rglob('*'))
    source = staged.path.read_bytes()

    plan_redaction(staged, hits, analysis)

    assert sorted(path.name for path in staging.root.rglob('*')) == before
    assert staged.path.read_bytes() == source


def test_a_cell_holding_two_domains_is_edited_once(analysis, tmp_path):
    path = write_xlsx(tmp_path / 'M.xlsx', {'S': [['a@one.org and b@two.org']]})
    staged = open_workbook(str(path))
    hits = scan_path(staged.path, staged.url)
    analysis.store_analysis(
        [('one.org', 'High', 'person one', None), ('two.org', 'High', 'person two', None)],
        random.Random(2),
    )

    plan = plan_redaction(staged, hits, analysis)

    assert len(plan.edits) == 1
    assert plan.edits[0].domains == ('one.org', 'two.org')
    one, two = plan.mapped_domains['one.org'], plan.mapped_domains['two.org']
    assert plan.edits[0].after == f'a@{one} and b@{two}'


def test_nothing_to_change_produces_no_edits(analysis, staged, hits):
    analysis.store_analysis(
        [
            ('smithlab.io', 'Low', 'actually an org', None),
            ('broadinstitute.org', 'Low', 'Broad', None),
        ],
        random.Random(1),
    )

    plan = plan_redaction(staged, hits, analysis)

    assert plan.edits == []
    assert sorted(plan.left_as_is) == ['broadinstitute.org', 'smithlab.io']


def test_edits_are_produced_in_a_deterministic_order(analysis, tmp_path):
    rows = [[f'u{index}@lab.io', f'v{index}@lab.io'] for index in range(5)]
    path = write_xlsx(tmp_path / 'Grid.xlsx', {'B': rows, 'A': rows})
    staged = open_workbook(str(path))
    analysis.store_analysis([('lab.io', 'High', 'a lab', None)], random.Random(1))

    plan = plan_redaction(staged, scan_path(staged.path, staged.url), analysis)

    assert [(edit.sheet_title, edit.a1) for edit in plan.edits][:3] == [
        ('A', 'A1'),
        ('A', 'A2'),
        ('A', 'A3'),
    ]


# -- the copy ----------------------------------------------------------------------------------
def test_create_copy_leaves_the_staged_source_untouched(staged, staging):
    original = staged.path.read_bytes()

    copy = create_copy(staged, staging)

    assert copy != staged.path
    assert copy.read_bytes() == original
    assert staged.path.read_bytes() == original


def test_the_copy_is_named_after_the_source(staged, staging):
    assert create_copy(staged, staging).name == 'Q1 Metrics (anonymized).xlsx'


def test_a_second_copy_does_not_overwrite_the_first(staged, staging):
    first = create_copy(staged, staging)
    second = create_copy(staged, staging)

    assert first.name == 'Q1 Metrics (anonymized).xlsx'
    assert second.name == 'Q1 Metrics (anonymized) 2.xlsx'
    assert first.exists() and second.exists()


# -- applying and verifying --------------------------------------------------------------------
def test_applying_the_plan_anonymizes_the_copy(analysis, staged, hits, staging):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    alias = plan.mapped_domains['smithlab.io']
    copy = create_copy(staged, staging)

    assert apply(plan, copy) == 2

    assert xlsx.read_rows(copy, 'Users') == [
        ['User', 'Email'],
        ['Alice', f'alice@{alias}'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', f'carol@{alias}'],
    ]


def test_applying_touches_no_cell_outside_the_plan(analysis, staging, tmp_path):
    """A kept address between two redacted ones must survive byte for byte."""
    sheets = {'S': [['a@lab.io'], ['keep@broadinstitute.org'], ['b@lab.io']]}
    path = write_xlsx(tmp_path / 'Mixed.xlsx', sheets)
    staged = open_workbook(str(path))
    analysis.store_analysis(
        [('lab.io', 'High', 'a lab', None), ('broadinstitute.org', 'Low', 'Broad', None)],
        random.Random(1),
    )
    plan = plan_redaction(staged, scan_path(staged.path, staged.url), analysis)
    copy = create_copy(staged, staging)

    assert apply(plan, copy) == 2
    assert xlsx.read_rows(copy, 'S')[1] == ['keep@broadinstitute.org']


def test_applying_nothing_writes_nothing(analysis, staged, hits, staging):
    analysis.store_analysis(
        [('smithlab.io', 'Low', 'an org', None), ('broadinstitute.org', 'Low', 'Broad', None)],
        random.Random(1),
    )
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)
    before = copy.read_bytes()

    assert apply(plan, copy) == 0
    assert copy.read_bytes() == before


def test_verify_is_silent_once_the_plan_is_applied(analysis, staged, hits, staging):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)
    apply(plan, copy)

    assert verify(copy, plan.mapped_domains) == []


def test_verify_catches_an_unapplied_plan(analysis, staged, hits, staging):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)

    assert verify(copy, plan.mapped_domains) == ['smithlab.io']


def test_a_domain_left_as_is_stays_in_the_copy(analysis, staged, hits, staging):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)
    apply(plan, copy)

    remaining = {hit.domain for hit in scan_path(copy, 'copy-id')}
    assert remaining == {'broadinstitute.org'}


# -- the whole redaction -----------------------------------------------------------------------
def test_redact_copies_writes_verifies_and_records(analysis, staged, hits, staging):
    analyze_all(analysis)

    plan, copy, records = redact(staged, hits, analysis, staging)

    alias = plan.mapped_domains['smithlab.io']
    assert copy.name == 'Q1 Metrics (anonymized).xlsx'
    assert xlsx.read_rows(copy, 'Users')[1] == ['Alice', f'alice@{alias}']
    assert len(records) == 2
    assert verify(copy, plan.mapped_domains) == []


def test_redact_leaves_the_source_alone(analysis, staged, hits, staging):
    analyze_all(analysis)
    before = staged.path.read_bytes()

    redact(staged, hits, analysis, staging)

    assert staged.path.read_bytes() == before


def test_redact_refuses_and_records_nothing_when_a_write_does_not_land(
    analysis, staged, hits, staging, monkeypatch
):
    """The audit trail must never assert a redaction that did not reach the disk."""
    analyze_all(analysis)
    monkeypatch.setattr('email_domain_scrubber.redact.apply', lambda plan, copy: 0)

    with pytest.raises(RedactionNotApplied, match='smithlab.io'):
        redact(staged, hits, analysis, staging)

    assert xlsx.read_rows(analysis.path, REDACTIONS)[1:] == []


# -- the audit record --------------------------------------------------------------------------
def test_record_writes_one_row_per_cell_and_domain(analysis, staged, hits, tmp_path):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = write_xlsx(tmp_path / 'Q1 Metrics (anonymized).xlsx', USERS)

    records = record(plan, copy, analysis)

    assert len(records) == 2
    rows = xlsx.read_rows(analysis.path, REDACTIONS)[1:]
    assert len(rows) == 2
    assert {row[1] for row in rows} == {str(staged.path)}
    assert {row[2] for row in rows} == {str(copy)}
    assert {row[4] for row in rows} == {'smithlab.io'}
    assert {row[5] for row in rows} == {plan.mapped_domains['smithlab.io']}


def test_record_keeps_the_before_and_after_of_each_cell(analysis, staged, hits, tmp_path):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    alias = plan.mapped_domains['smithlab.io']

    record(plan, tmp_path / 'copy.xlsx', analysis)

    rows = xlsx.read_rows(analysis.path, REDACTIONS)[1:]
    assert [(row[6], row[7]) for row in rows] == [
        ('alice@smithlab.io', f'alice@{alias}'),
        ('carol@smithlab.io', f'carol@{alias}'),
    ]


def test_recorded_references_point_at_the_redacted_copy(analysis, staged, hits, tmp_path):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = write_xlsx(tmp_path / 'Q1 Metrics (anonymized).xlsx', USERS)

    records = record(plan, copy, analysis)

    assert [rec.reference for rec in records] == [
        f'{copy.as_uri()}#Users!B2',
        f'{copy.as_uri()}#Users!B4',
    ]


def test_a_two_domain_cell_records_both_replacements(analysis, tmp_path):
    path = write_xlsx(tmp_path / 'M.xlsx', {'S': [['a@one.org and b@two.org']]})
    staged = open_workbook(str(path))
    analysis.store_analysis(
        [('one.org', 'High', 'person one', None), ('two.org', 'High', 'person two', None)],
        random.Random(2),
    )
    plan = plan_redaction(staged, scan_path(staged.path, staged.url), analysis)

    records = record(plan, tmp_path / 'copy.xlsx', analysis)

    assert {rec.domain for rec in records} == {'one.org', 'two.org'}
    # One cell, so both rows share its before and after.
    assert len({(rec.before, rec.after) for rec in records}) == 1
