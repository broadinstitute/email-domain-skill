"""Domain research: RDAP and Europe PMC parsing, and how each source fails.

No test here opens a socket — `conftest` blocks that outright, and every lookup takes an injected
fetcher. What is being tested is the reading of real-shaped payloads and, just as much, what comes
back when a source says nothing useful: an unresolved domain is a finding the skill has to be able
to act on.
"""

import json

import pytest

from email_domain_scrubber import research
from email_domain_scrubber.research import (
    FetchError,
    first_author,
    is_privacy_shielded,
    lookup_registration,
    parse_literature,
    parse_registration,
    research_domain,
    research_domains,
    search_literature,
)

from .fakes import fake_fetch


def vcard(*fields):
    return ['vcard', [['version', {}, 'text', '4.0'], *fields]]


PERSONAL = {
    'entities': [
        {
            'roles': ['registrant'],
            'vcardArray': vcard(
                ['fn', {}, 'text', 'Jane Smith'], ['kind', {}, 'text', 'individual']
            ),
        },
        {'roles': ['registrar'], 'vcardArray': vcard(['fn', {}, 'text', 'Namecheap, Inc.'])},
    ],
    'events': [
        {'eventAction': 'registration', 'eventDate': '2014-03-02T00:00:00Z'},
        {'eventAction': 'expiration', 'eventDate': '2027-03-02T00:00:00Z'},
    ],
}

INSTITUTIONAL = {
    'entities': [
        {
            'roles': ['registrant'],
            'vcardArray': vcard(
                ['fn', {}, 'text', 'Broad Institute'],
                ['org', {}, 'text', ['Broad Institute', 'IT']],
                ['kind', {}, 'text', 'org'],
            ),
        }
    ]
}


# -- registration ------------------------------------------------------------------------------
def test_a_named_individual_registrant_is_read_out():
    found = parse_registration(PERSONAL)

    assert found.status == 'found'
    assert found.registrant_name == 'Jane Smith'
    assert found.registrant_kind == 'individual'
    assert found.registrar == 'Namecheap, Inc.'
    assert found.registered_on == '2014-03-02'
    assert not found.privacy_shielded


def test_a_structured_org_value_is_joined_rather_than_stringified():
    found = parse_registration(INSTITUTIONAL)

    assert found.registrant_organization == 'Broad Institute IT'
    assert found.registrant_kind == 'org'


def test_a_registrant_nested_under_the_registrar_is_still_found():
    """Registries commonly publish it that way, and a top-level-only search would miss it."""
    payload = {
        'entities': [
            {
                'roles': ['registrar'],
                'entities': [
                    {'roles': ['registrant'], 'vcardArray': vcard(['fn', {}, 'text', 'A Person'])}
                ],
            }
        ]
    }

    assert parse_registration(payload).registrant_name == 'A Person'


def test_a_registry_that_published_no_registrant_says_so():
    found = parse_registration({'entities': [{'roles': ['registrar']}]})

    assert found.status == 'found'
    assert found.registrant_name == ''
    assert found.detail


@pytest.mark.parametrize(
    'value',
    [
        'REDACTED FOR PRIVACY',
        'Domains By Proxy, LLC',
        'Withheld for Privacy ehf',
        'GDPR Masked',
        'Statutory Masking Enabled',
    ],
)
def test_shield_registrants_are_flagged(value):
    assert is_privacy_shielded(value)


@pytest.mark.parametrize('value', ['Jane Smith', 'Broad Institute', 'Private University Trust'])
def test_real_names_and_orgs_are_not_flagged_as_shields(value):
    assert not is_privacy_shielded(value)


def test_a_missing_rdap_record_is_reported_without_being_read_as_evidence():
    found = lookup_registration('lab.example', fake_fetch({'rdap.org': FetchError('HTTP 404')}))

    assert found.status == 'not_found'
    assert 'does not serve RDAP' in found.detail


def test_an_rdap_outage_is_reported_as_unavailable():
    found = lookup_registration('lab.io', fake_fetch({'rdap.org': FetchError('unreachable')}))

    assert found.status == 'unavailable'
    assert 'unreachable' in found.detail


def test_unparseable_rdap_does_not_raise():
    found = lookup_registration('lab.io', fake_fetch({'rdap.org': 'not json at all'}))

    assert found.status == 'unavailable'


def test_rdap_that_is_json_but_not_an_object_does_not_raise():
    found = lookup_registration('lab.io', fake_fetch({'rdap.org': '["nope"]'}))

    assert found.status == 'unavailable'


def test_the_queried_domain_is_normalized():
    seen = []

    def fetch(url):
        seen.append(url)
        return json.dumps(PERSONAL).encode()

    lookup_registration('  SmithLab.IO ', fetch)

    assert seen == [research.RDAP_URL + 'smithlab.io']


# -- literature --------------------------------------------------------------------------------
def payload(*results, hit_count=None):
    return json.dumps(
        {
            'hitCount': len(results) if hit_count is None else hit_count,
            'resultList': {'result': list(results)},
        }
    )


def article(authors, source='MED', title='A paper'):
    return {'title': title, 'authorString': authors, 'pubYear': '2021', 'source': source}


def test_one_recurring_first_author_is_surfaced():
    found = parse_literature(json.loads(payload(article('Smith J, Doe A.'), article('Smith J.'))))

    assert found.status == 'found'
    assert found.hit_count == 2
    assert found.distinct_first_authors == ['Smith J']


def test_many_first_authors_are_all_reported():
    found = parse_literature(
        json.loads(payload(article('Smith J, Doe A.'), article('Nguyen T, Ali R.')))
    )

    assert found.distinct_first_authors == ['Smith J', 'Nguyen T']


def test_a_preprint_is_marked_as_one():
    found = parse_literature(json.loads(payload(article('Smith J.', source='PPR'))))

    assert found.hits[0].is_preprint


def test_no_hits_is_a_status_not_an_error():
    found = parse_literature(json.loads(payload()))

    assert found.status == 'not_found'
    assert found.hit_count == 0
    assert found.hits == []


def test_hits_are_capped_but_the_total_is_kept():
    """The count is the signal; ten examples are enough to read."""
    many = [article(f'Author{index} A.') for index in range(30)]
    found = parse_literature(json.loads(payload(*many, hit_count=400)), limit=10)

    assert found.hit_count == 400
    assert len(found.hits) == 10


def test_a_nonsense_hit_count_falls_back_to_what_was_returned():
    raw = json.loads(payload(article('Smith J.')))
    raw['hitCount'] = 'lots'

    assert parse_literature(raw).hit_count == 1


def test_a_europe_pmc_outage_is_reported_as_unavailable():
    found = search_literature('lab.io', fake_fetch({'europepmc': FetchError('timed out')}))

    assert found.status == 'unavailable'
    assert 'timed out' in found.detail


def test_the_domain_is_searched_as_a_quoted_phrase():
    seen = []

    def fetch(url):
        seen.append(url)
        return payload().encode()

    search_literature('smithlab.io', fetch)

    assert '%22smithlab.io%22' in seen[0]


@pytest.mark.parametrize(
    ('author_string', 'expected'),
    [
        ('Smith J, Doe A.', 'Smith J'),
        ('Smith J.', 'Smith J'),
        ('', ''),
        ('van der Berg H, Ali R.', 'van der Berg H'),
    ],
)
def test_first_author_is_taken_off_the_front(author_string, expected):
    assert first_author(author_string) == expected


# -- both sources together ---------------------------------------------------------------------
def test_evidence_from_both_sources_makes_a_domain_resolved():
    found = research_domain(
        'smithlab.io',
        fake_fetch({'rdap.org': json.dumps(PERSONAL), 'europepmc': payload(article('Smith J.'))}),
    )

    assert found.resolved
    assert found.registration.registrant_name == 'Jane Smith'
    assert found.literature.hit_count == 1


def test_a_domain_no_source_can_speak_to_is_unresolved():
    found = research_domain('ghost.example', fake_fetch({}))

    assert not found.resolved
    assert found.registration.status == 'unavailable'
    assert found.literature.status == 'unavailable'


def test_a_shielded_registrant_with_no_papers_is_still_unresolved():
    """A privacy shield is not an identification, so it must not read as one."""
    shielded = {
        'entities': [
            {
                'roles': ['registrant'],
                'vcardArray': vcard(['fn', {}, 'text', 'REDACTED FOR PRIVACY']),
            }
        ]
    }
    found = research_domain(
        'lab.io', fake_fetch({'rdap.org': json.dumps(shielded), 'europepmc': payload()})
    )

    assert found.registration.privacy_shielded
    assert found.resolved is True  # A shield string is still something the registry published.
    assert found.literature.hit_count == 0


def test_every_result_names_the_sources_that_were_not_consulted():
    found = research_domain('lab.io', fake_fetch({}))

    assert any('web search' in note.lower() for note in found.not_searched)
    assert any('github' in note.lower() for note in found.not_searched)


# -- batching ----------------------------------------------------------------------------------
def test_domains_come_back_deduplicated_and_lowercased():
    found = research_domains(
        ['Lab.IO', 'lab.io', 'other.org'],
        fake_fetch({'rdap.org': json.dumps(PERSONAL), 'europepmc': payload()}),
    )

    assert [item.domain for item in found] == ['lab.io', 'other.org']


def test_an_empty_batch_makes_no_requests():
    assert research_domains(['', '  '], fake_fetch({})) == []


def test_one_slow_source_does_not_lose_the_other_domains():
    """Each domain's sources fail independently, so a batch is never all-or-nothing."""
    responses = {'rdap.org/domain/good.org': json.dumps(PERSONAL), 'europepmc': payload()}
    found = research_domains(['bad.example', 'good.org'], fake_fetch(responses))

    by_domain = {item.domain: item for item in found}
    assert by_domain['bad.example'].registration.status == 'unavailable'
    assert by_domain['good.org'].registration.registrant_name == 'Jane Smith'
