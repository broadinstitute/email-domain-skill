"""The MCP tool surface, driven the way the analysis skill drives it."""

import asyncio
from pathlib import Path

import pytest

from email_domain_scrubber import server, xlsx
from email_domain_scrubber.errors import InvalidRisk, InvalidWorkbookReference, UnanalyzedDomains
from email_domain_scrubber.staging import ANALYSIS_WORKBOOK_ENV
from email_domain_scrubber.workbook import (
    DOMAIN_ANALYSIS,
    HEADERS,
    REDACTIONS,
    WORKBOOKS,
    AnalysisWorkbook,
)

from .fakes import fake_fetch, write_xlsx

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


def _edit(book, domain, *, risk=None, alias=None):
    """Hand-edit one row of the analysis workbook, the way the user would in Excel."""
    analysis = AnalysisWorkbook.open(Path(book))
    rows = [
        [
            row.domain,
            risk if risk is not None and row.domain == domain else row.risk,
            row.explanation,
            alias if alias is not None and row.domain == domain else row.anonymized_domain,
        ]
        for row in analysis.analysis_rows()
    ]
    xlsx.rewrite(Path(book), {DOMAIN_ANALYSIS: [HEADERS[DOMAIN_ANALYSIS], *rows]})


def _set_risk(book, domain, risk):
    _edit(book, domain, risk=risk)


def _set_alias(book, domain, alias):
    _edit(book, domain, alias=alias)


# -- registration ------------------------------------------------------------------------------
def test_all_tools_are_registered():
    """Guards against a tool being added without its @mcp.tool() decorator."""
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == {
        'scan_workbook',
        'list_domains_for_analysis',
        'research_domains',
        'store_domain_analysis',
        'plan_redaction',
        'apply_redaction',
    }


def test_apply_redaction_takes_no_upload_target():
    """Nothing is published anywhere, so the tool exposes no destination parameter."""
    tools = asyncio.run(server.mcp.list_tools())
    apply_tool = next(tool for tool in tools if tool.name == 'apply_redaction')

    assert set(apply_tool.input_schema['properties']) == {'workbook', 'analysis_workbook'}


def test_no_tool_asks_the_caller_to_write_a_cell():
    """The skill must have no way to write to the report, so no tool hands it cell writes."""
    tools = asyncio.run(server.mcp.list_tools())
    names = {tool.name for tool in tools}

    assert 'write_blocks' not in str(tools)
    assert not names & {'finish_redaction', 'write_data_to_excel'}


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
def test_scan_list_store_plan_apply(metrics, book):
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
    assert plan.aliases_minted == {}

    applied = server.apply_redaction(metrics, book)
    assert applied.cells_changed == 2
    assert applied.remaining_domains == ['broadinstitute.org']
    assert applied.redactions_recorded == 2
    assert applied.redacted_workbook_title == 'Q1 Metrics (anonymized).xlsx'
    assert applied.source_workbook_path == metrics
    assert xlsx.read_rows(applied.redacted_workbook_path, 'Users') == [
        ['User', 'Email'],
        ['Alice', f'alice@{alias}'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', f'carol@{alias}'],
    ]


def test_apply_redacts_a_whole_column(book, tmp_path):
    rows = [['Email'], *[[f'user{index}@lab.io'] for index in range(30)]]
    path = str(write_xlsx(tmp_path / 'Big.xlsx', {'Users': rows}))
    server.scan_workbook(path, book)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='lab.io', risk='High', explanation='a lab')], book
    )

    applied = server.apply_redaction(path, book)

    assert applied.cells_changed == 30
    assert applied.remaining_domains == []


def test_apply_keeps_the_domains_that_were_left_as_is(book, tmp_path):
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

    applied = server.apply_redaction(path, book)

    assert applied.cells_changed == 5
    assert applied.remaining_domains == ['broadinstitute.org']


# -- the source is never touched ---------------------------------------------------------------
def test_the_original_is_never_modified(metrics, book):
    before = Path(metrics).read_bytes()
    server.scan_workbook(metrics, book)
    analyze_all(book)
    server.plan_redaction(metrics, book)
    server.apply_redaction(metrics, book)

    assert Path(metrics).read_bytes() == before
    assert xlsx.read_rows(metrics, 'Users')[1] == ['Alice', 'alice@smithlab.io']


def test_the_redacted_copy_lands_in_the_work_directory(metrics, book, staging):
    server.scan_workbook(metrics, book)
    analyze_all(book)

    applied = server.apply_redaction(metrics, book)

    redacted = Path(applied.redacted_workbook_path)
    assert staging.root in redacted.parents
    assert redacted.parent != Path(metrics).parent


def test_planning_creates_no_copy_and_changes_nothing(metrics, book, staging):
    server.scan_workbook(metrics, book)
    analyze_all(book)
    before = sorted(path.name for path in staging.root.rglob('*'))

    plan = server.plan_redaction(metrics, book)

    assert plan.cells_to_change == 2
    assert sorted(path.name for path in staging.root.rglob('*')) == before


# -- the user's edits to the analysis workbook -------------------------------------------------
def test_the_user_spares_a_domain_by_clearing_the_alias_and_lowering_the_risk(metrics, book):
    """The documented override at review time: both cells, so a stray edit cannot expose anyone."""
    server.scan_workbook(metrics, book)
    analyze_all(book)
    _edit(book, 'smithlab.io', risk='Low', alias='')

    applied = server.apply_redaction(metrics, book)

    assert applied.cells_changed == 0
    assert sorted(applied.remaining_domains) == ['broadinstitute.org', 'smithlab.io']


def test_clearing_the_alias_alone_does_not_expose_a_high_risk_domain(metrics, book):
    """A half-finished edit must fail safe: the row still reads High, so it is still redacted."""
    server.scan_workbook(metrics, book)
    analyze_all(book)
    _set_alias(book, 'smithlab.io', '')

    plan = server.plan_redaction(metrics, book)

    assert list(plan.aliases_minted) == ['smithlab.io']
    assert plan.cells_to_change == 2


def test_a_risk_the_user_edited_up_to_high_gets_an_alias_and_takes_effect(metrics, book):
    server.scan_workbook(metrics, book)
    server.store_domain_analysis(
        [
            server.AnalysisInput(domain='smithlab.io', risk='Low', explanation='looked like org'),
            server.AnalysisInput(domain='broadinstitute.org', risk='Low', explanation='Broad'),
        ],
        book,
    )
    _set_risk(book, 'smithlab.io', 'High')

    plan = server.plan_redaction(metrics, book)

    assert list(plan.aliases_minted) == ['smithlab.io']
    assert plan.cells_to_change == 2

    applied = server.apply_redaction(metrics, book)
    assert applied.cells_changed == 2
    assert applied.remaining_domains == ['broadinstitute.org']


def test_a_risk_the_user_made_nonsense_of_is_refused(metrics, book):
    server.scan_workbook(metrics, book)
    analyze_all(book)
    _set_risk(book, 'smithlab.io', 'Severe')

    with pytest.raises(InvalidRisk, match='smithlab.io'):
        server.plan_redaction(metrics, book)


# -- refusals ----------------------------------------------------------------------------------
def test_plan_refuses_before_analysis_is_complete(metrics, book):
    server.scan_workbook(metrics, book)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )

    with pytest.raises(UnanalyzedDomains, match='broadinstitute.org'):
        server.plan_redaction(metrics, book)


def test_apply_refuses_before_analysis_is_complete(metrics, book):
    """The refusal guards apply too, not just the preview the user was shown."""
    server.scan_workbook(metrics, book)
    server.store_domain_analysis(
        [server.AnalysisInput(domain='smithlab.io', risk='High', explanation='lab')], book
    )

    with pytest.raises(UnanalyzedDomains, match='broadinstitute.org'):
        server.apply_redaction(metrics, book)


def test_apply_refuses_a_workbook_that_does_not_exist(book, tmp_path):
    with pytest.raises(InvalidWorkbookReference):
        server.apply_redaction(str(tmp_path / 'nope.xlsx'), book)


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
def test_apply_records_the_redacted_file(metrics, book):
    server.scan_workbook(metrics, book)
    analyze_all(book)

    applied = server.apply_redaction(metrics, book)

    rows = xlsx.read_rows(book, REDACTIONS)[1:]
    assert len(rows) == 2
    assert {row[1] for row in rows} == {metrics}
    assert {row[2] for row in rows} == {applied.redacted_workbook_path}
    # The Reference locator stays a file:// URL with the cell in the fragment.
    redacted_uri = Path(applied.redacted_workbook_path).as_uri()
    assert {row[3] for row in rows} == {f'{redacted_uri}#Users!B2', f'{redacted_uri}#Users!B4'}


def test_apply_records_what_each_cell_held_before_and_after(metrics, book):
    server.scan_workbook(metrics, book)
    analyze_all(book)

    applied = server.apply_redaction(metrics, book)

    alias = applied.domains_anonymized['smithlab.io']
    rows = xlsx.read_rows(book, REDACTIONS)[1:]
    assert [(row[6], row[7]) for row in rows] == [
        ('alice@smithlab.io', f'alice@{alias}'),
        ('carol@smithlab.io', f'carol@{alias}'),
    ]


# -- research ----------------------------------------------------------------------------------
RDAP_PERSONAL = """
{"entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [
    ["version", {}, "text", "4.0"],
    ["fn", {}, "text", "Jane Smith"],
    ["kind", {}, "text", "individual"]]]}],
 "events": [{"eventAction": "registration", "eventDate": "2014-03-02T00:00:00Z"}]}
"""

PMC_ONE_AUTHOR = """
{"hitCount": 2, "resultList": {"result": [
    {"title": "A method", "authorString": "Smith J, Doe A.", "pubYear": "2021",
     "journalTitle": "J Methods", "doi": "10.1/a", "source": "MED"},
    {"title": "Another method", "authorString": "Smith J.", "pubYear": "2023",
     "journalTitle": "bioRxiv", "doi": "10.1/b", "source": "PPR"}]}}
"""


def test_research_returns_registration_and_literature_evidence(monkeypatch):
    monkeypatch.setattr(
        'email_domain_scrubber.research.http_get',
        fake_fetch({'rdap.org': RDAP_PERSONAL, 'europepmc': PMC_ONE_AUTHOR}),
    )

    found = server.research_domains(['smithlab.io'])[0]

    assert found.domain == 'smithlab.io'
    assert found.registration.registrant_name == 'Jane Smith'
    assert found.registration.registrant_kind == 'individual'
    assert found.registration.registered_on == '2014-03-02'
    assert found.literature.hit_count == 2
    assert found.literature.distinct_first_authors == ['Smith J']
    assert found.literature.hits[1].is_preprint
    assert found.resolved


def test_research_names_the_sources_it_did_not_consult(monkeypatch):
    """An explanation must not imply evidence nobody gathered."""
    monkeypatch.setattr(
        'email_domain_scrubber.research.http_get',
        fake_fetch({'rdap.org': RDAP_PERSONAL, 'europepmc': PMC_ONE_AUTHOR}),
    )

    found = server.research_domains(['smithlab.io'])[0]

    assert any('web search' in note.lower() for note in found.not_searched)


def test_research_reports_an_unresolvable_domain_rather_than_failing(monkeypatch):
    monkeypatch.setattr(
        'email_domain_scrubber.research.http_get', fake_fetch({'nothing-matches': '{}'})
    )

    found = server.research_domains(['ghost.example'])[0]

    assert not found.resolved
    assert found.registration.status == 'unavailable'
    assert found.literature.status == 'unavailable'
    assert found.registration.detail
