import pytest

from email_domain_scrubber.domains import apply_redactions, extract_domains, normalize


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('alice@smithlab.io', ['smithlab.io']),
        ('Contact: Bob Jones <bob.jones+lab@harvard.edu>', ['harvard.edu']),
        ('ALICE@Broad.ORG', ['broad.org']),
        ('user@username.github.io', ['username.github.io']),
        ('broadinstitute.org', ['broadinstitute.org']),
        ('gmail.com, yahoo.com', ['gmail.com', 'yahoo.com']),
        ('mailto:pi@ox.ac.uk', ['ox.ac.uk']),
        ('https://foo.harvard.edu/reports', ['foo.harvard.edu']),
        ('www.broadinstitute.org', ['broadinstitute.org']),
    ],
)
def test_extracts_domains(text, expected):
    assert extract_domains(text) == expected


@pytest.mark.parametrize(
    'text',
    [
        '',
        'no domains here',
        'Total.Count',  # mixed case dotted label, not a domain
        'Sum.of.Users',
        'quarterly_report.csv',
        'metrics.xlsx',
        'analysis.ipynb',
        '1.2.3',
        '192.168.0.1',
        'e.g. a note',
        'i.e. another note',
        '3.14',
    ],
)
def test_rejects_non_domains(text):
    assert extract_domains(text) == []


def test_dedupes_within_a_cell_preserving_order():
    text = 'a@smithlab.io; b@smithlab.io; c@harvard.edu'
    assert extract_domains(text) == ['smithlab.io', 'harvard.edu']


def test_address_domain_is_not_also_reported_as_a_bare_domain():
    assert extract_domains('alice@smithlab.io') == ['smithlab.io']


def test_address_accepts_tlds_not_on_the_bare_domain_allow_list():
    # The @ is evidence enough; a bare `lab.example` would be rejected.
    assert extract_domains('pi@lab.newtld') == ['lab.newtld']
    assert extract_domains('lab.newtld') == []


def test_subdomain_is_reported_in_full():
    assert extract_domains('mail.smithlab.io') == ['mail.smithlab.io']


def test_normalize():
    assert normalize('  WWW.Harvard.EDU. ') == 'harvard.edu'


def test_redaction_replaces_only_the_mapped_domain():
    text = 'alice@smithlab.io and bob@harvard.edu'
    after, replaced = apply_redactions(text, {'smithlab.io': 'anon0001'})
    assert after == 'alice@anon0001 and bob@harvard.edu'
    assert replaced == ['smithlab.io']


def test_redaction_does_not_match_a_suffix_of_a_longer_hostname():
    after, replaced = apply_redactions('mail.smithlab.io', {'smithlab.io': 'anon0001'})
    assert after == 'mail.smithlab.io'
    assert replaced == []


def test_redaction_prefers_the_longer_domain_when_both_are_mapped():
    mapping = {'smithlab.io': 'anon0001', 'mail.smithlab.io': 'anon0002'}
    after, replaced = apply_redactions('mail.smithlab.io / smithlab.io', mapping)
    assert after == 'anon0002 / anon0001'
    assert set(replaced) == {'smithlab.io', 'mail.smithlab.io'}


def test_redaction_is_case_insensitive():
    after, _ = apply_redactions('Alice@SmithLab.IO', {'smithlab.io': 'anon0001'})
    assert after == 'Alice@anon0001'


def test_redaction_reports_nothing_when_no_domain_is_present():
    after, replaced = apply_redactions('total users', {'smithlab.io': 'anon0001'})
    assert after == 'total users'
    assert replaced == []
