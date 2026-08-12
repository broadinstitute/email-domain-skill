"""Planning a redaction, grouping it into write blocks, and verifying it landed."""

import random

import pytest

from email_domain_scrubber import xlsx
from email_domain_scrubber.errors import UnanalyzedDomains
from email_domain_scrubber.redact import (
    CellEdit,
    coalesce,
    create_copy,
    plan_redaction,
    record,
    verify,
)
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


def test_nothing_to_change_produces_no_blocks(analysis, staged, hits):
    analysis.store_analysis(
        [
            ('smithlab.io', 'Low', 'actually an org', None),
            ('broadinstitute.org', 'Low', 'Broad', None),
        ],
        random.Random(1),
    )

    plan = plan_redaction(staged, hits, analysis)

    assert plan.edits == []
    assert plan.blocks == []
    assert sorted(plan.left_as_is) == ['broadinstitute.org', 'smithlab.io']


# -- block coalescing --------------------------------------------------------------------------
def edit(sheet, row, column, after='x'):
    return CellEdit(sheet, f'c{row}', row, column, 'before', after, ('d.io',))


def test_consecutive_rows_in_one_column_become_a_single_block():
    blocks = coalesce([edit('S', 2, 2, 'a'), edit('S', 3, 2, 'b'), edit('S', 4, 2, 'c')])

    assert len(blocks) == 1
    assert (blocks[0].sheet, blocks[0].start_cell) == ('S', 'B2')
    assert blocks[0].values == [['a'], ['b'], ['c']]


def test_a_row_gap_starts_a_new_block():
    """A block must never span a gap, or it would clobber the untouched cell in between."""
    blocks = coalesce([edit('S', 2, 2, 'a'), edit('S', 4, 2, 'c')])

    assert [(block.start_cell, block.values) for block in blocks] == [
        ('B2', [['a']]),
        ('B4', [['c']]),
    ]


def test_separate_columns_become_separate_blocks():
    blocks = coalesce([edit('S', 2, 2, 'a'), edit('S', 2, 3, 'b')])

    assert sorted(block.start_cell for block in blocks) == ['B2', 'C2']


def test_separate_sheets_become_separate_blocks():
    blocks = coalesce([edit('One', 2, 2, 'a'), edit('Two', 2, 2, 'b')])

    assert sorted(block.sheet for block in blocks) == ['One', 'Two']


def test_blocks_are_produced_in_a_deterministic_order():
    unordered = [edit('B', 5, 1), edit('A', 3, 2), edit('A', 2, 2), edit('A', 9, 1)]

    first = [(b.sheet, b.start_cell) for b in coalesce(unordered)]
    second = [(b.sheet, b.start_cell) for b in coalesce(list(reversed(unordered)))]

    assert first == second == [('A', 'A9'), ('A', 'B2'), ('B', 'A5')]


def test_columns_past_z_use_the_right_letters():
    assert coalesce([edit('S', 1, 27, 'a')])[0].start_cell == 'AA1'


def test_a_scan_shaped_plan_makes_one_block_per_run(analysis, tmp_path):
    """The common case: a contiguous column of addresses collapses to a single write call."""
    rows = [['Email'], *[[f'user{index}@lab.io'] for index in range(20)]]
    path = write_xlsx(tmp_path / 'Big.xlsx', {'Users': rows})
    staged = open_workbook(str(path))
    analysis.store_analysis([('lab.io', 'High', 'a lab', None)], random.Random(1))

    plan = plan_redaction(staged, scan_path(staged.path, staged.url), analysis)

    assert plan.cells_to_change == 20
    assert len(plan.blocks) == 1
    assert plan.blocks[0].start_cell == 'A2'


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
def test_applying_the_blocks_anonymizes_the_copy(analysis, staged, hits, staging, excel):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    alias = plan.mapped_domains['smithlab.io']
    copy = create_copy(staged, staging)

    excel.apply(copy, plan.blocks)

    assert xlsx.read_rows(copy, 'Users') == [
        ['User', 'Email'],
        ['Alice', f'alice@{alias}'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', f'carol@{alias}'],
    ]


def test_verify_is_silent_once_every_block_is_applied(analysis, staged, hits, staging, excel):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)
    excel.apply(copy, plan.blocks)

    assert verify(copy, plan.mapped_domains) == []


def test_verify_catches_an_unapplied_plan(analysis, staged, hits, staging):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)

    assert verify(copy, plan.mapped_domains) == ['smithlab.io']


def test_verify_catches_a_partially_applied_plan(analysis, staging, tmp_path, excel):
    path = write_xlsx(tmp_path / 'Two.xlsx', {'S': [['a@one.org'], [''], ['b@two.org']]})
    staged = open_workbook(str(path))
    analysis.store_analysis(
        [('one.org', 'High', 'person one', None), ('two.org', 'High', 'person two', None)],
        random.Random(4),
    )
    plan = plan_redaction(staged, scan_path(staged.path, staged.url), analysis)
    copy = create_copy(staged, staging)

    assert len(plan.blocks) == 2
    excel.apply(copy, plan.blocks[:1])

    assert verify(copy, plan.mapped_domains) == ['two.org']


def test_a_domain_left_as_is_stays_in_the_copy(analysis, staged, hits, staging, excel):
    analyze_all(analysis)
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)
    excel.apply(copy, plan.blocks)

    remaining = {hit.domain for hit in scan_path(copy, 'copy-id')}
    assert remaining == {'broadinstitute.org'}


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
