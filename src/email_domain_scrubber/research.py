"""Domain research — the OSINT that a risk verdict rests on.

The analysis skill judges; it does not search. Every lookup lives here, so the evidence behind a
verdict is fixed by code rather than by whatever query the model happened to think of, and two
runs over the same domain consult the same sources.

Two sources, both free and unauthenticated:

* **RDAP** — the structured successor to WHOIS. Queried through `rdap.org`, which redirects to
  the authoritative registry for the TLD.

  Be realistic about what this yields. Since GDPR, gTLD registries publish the registrar and the
  registration date but redact the registrant entirely, so for a `.com` or `.org` the name is
  usually simply absent — which is the norm, not a finding. Some ccTLDs still publish a
  registrant, and where one appears it is strong evidence. The registration date and a consumer
  registrar are weak evidence at best.
* **Europe PMC** — the scientific literature, PubMed and bioRxiv/medRxiv preprints together,
  searched full text. This is the source that usually decides a hard case: a domain appearing in
  the affiliations or correspondence addresses of one author's papers and nobody else's points at
  one person, while thousands of hits across hundreds of first authors point at an institution.

Deliberately absent, and reported as such in `not_searched`: general web search, which needs a
paid API key this server does not have, and any request to the domain's own host, which would
mean contacting a user's server to profile them. Naming the gaps in the result keeps a verdict
from implying evidence that was never gathered.

Every lookup degrades rather than raising: a source that times out, 404s, or returns something
unparseable comes back with a status and a `detail` saying why. `rdap.org` times out often enough
that this matters, and plenty of ccTLDs are not in its bootstrap at all. A domain no source can
speak to is a real finding — it is what "unresolved, classify conservatively" is made of — not an
error, and not a reason to fall back to guessing from the shape of the name.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = 'email-domain-scrubber (+https://github.com/broadinstitute/email-domain-skill)'
# Long enough for a slow registry redirect, short enough that a queue of unresponsive TLDs does
# not outlast the caller. `rdap.org` is the one that stalls.
TIMEOUT_SECONDS = 12.0

RDAP_URL = 'https://rdap.org/domain/'
EUROPE_PMC_URL = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'

MAX_HITS = 10
MAX_WORKERS = 4

FOUND = 'found'
NOT_FOUND = 'not_found'
UNAVAILABLE = 'unavailable'

NOT_SEARCHED = (
    'General web search — no search provider is configured for this server.',
    "The domain's own website — never contacted, by design.",
    'GitHub and other code registries.',
    'ORCID (registrant emails there are almost never public anyway).',
)

# Substrings that mark a registrant field as a shield rather than a name. Deliberately narrow:
# 'private' is left out because plenty of real organizations are called Private Something.
_PRIVACY_MARKERS = (
    'redact',
    'privacy',
    'withheld',
    'not disclosed',
    'data protected',
    'gdpr',
    'proxy',
    'anonymi',
    'statutory masking',
    'identity protect',
    'domain protection',
)

Fetch = Callable[[str], bytes]


class FetchError(Exception):
    """A lookup could not be completed. Internal to this module: callers get a status instead."""


def http_get(url: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """GET a URL, turning every failure mode into one `FetchError`."""
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoints, built above
        url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as error:
        raise FetchError(f'HTTP {error.code} {error.reason}') from error
    except urllib.error.URLError as error:
        raise FetchError(f'unreachable: {error.reason}') from error
    except TimeoutError as error:
        raise FetchError(f'timed out after {timeout:g}s') from error


# -- registration ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Registration:
    """What the registry says about who owns a domain."""

    status: str
    registrant_name: str = ''
    registrant_organization: str = ''
    registrant_kind: str = ''
    privacy_shielded: bool = False
    registered_on: str = ''
    registrar: str = ''
    detail: str = ''


def is_privacy_shielded(*values: str) -> bool:
    """Whether a registrant field is a shield rather than a name."""
    joined = ' '.join(values).lower()
    return any(marker in joined for marker in _PRIVACY_MARKERS)


def lookup_registration(domain: str, fetch: Fetch | None = None) -> Registration:
    """Query RDAP for one domain.

    A 404 means the registry has no record — for an in-use email domain that usually means the
    TLD's registry does not answer RDAP, not that the domain is unregistered, so it is reported
    as `not_found` with the distinction spelled out rather than as evidence of anything.
    """
    fetch = fetch or http_get
    url = RDAP_URL + urllib.parse.quote(domain.strip().lower())
    try:
        payload = json.loads(fetch(url))
    except FetchError as error:
        detail = str(error)
        if 'HTTP 404' in detail:
            return Registration(
                status=NOT_FOUND,
                detail='No RDAP record. Either the domain is unregistered or the registry for '
                'this TLD does not serve RDAP — this does not distinguish the two.',
            )
        return Registration(status=UNAVAILABLE, detail=f'RDAP lookup failed: {detail}')
    except (ValueError, TypeError) as error:
        return Registration(status=UNAVAILABLE, detail=f'RDAP returned unparseable JSON: {error}')

    if not isinstance(payload, dict):
        return Registration(status=UNAVAILABLE, detail='RDAP response was not an object.')
    return parse_registration(payload)


def parse_registration(payload: dict[str, Any]) -> Registration:
    """Pull registrant, registrar, and registration date out of an RDAP domain object."""
    registrant = _vcard(_entity(payload, 'registrant'))
    name = registrant.get('fn', '')
    organization = registrant.get('org', '')
    return Registration(
        status=FOUND,
        registrant_name=name,
        registrant_organization=organization,
        registrant_kind=registrant.get('kind', ''),
        privacy_shielded=is_privacy_shielded(name, organization),
        registered_on=_event_date(payload, 'registration'),
        registrar=_vcard(_entity(payload, 'registrar')).get('fn', ''),
        detail='' if (name or organization) else 'Registry published no registrant name or org.',
    )


def _entity(payload: dict[str, Any], role: str) -> dict[str, Any]:
    """The first entity holding `role`, searched depth-first.

    Nesting matters: registries commonly publish the registrant as an entity *of* the registrar
    entity rather than at the top level, and a top-level-only search would miss it.
    """
    entities = payload.get('entities')
    if not isinstance(entities, list):
        return {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        roles = entity.get('roles')
        if isinstance(roles, list) and role in roles:
            return entity
    for entity in entities:
        if isinstance(entity, dict):
            nested = _entity(entity, role)
            if nested:
                return nested
    return {}


def _vcard(entity: dict[str, Any]) -> dict[str, str]:
    """An RDAP entity's jCard flattened to `{property: text}`.

    jCard is `['vcard', [[name, params, type, value], ...]]`, and a value can itself be an array
    of components (a structured `org` or `n`), so components are joined rather than stringified.
    """
    array = entity.get('vcardArray')
    if not isinstance(array, list) or len(array) < 2 or not isinstance(array[1], list):
        return {}
    fields: dict[str, str] = {}
    for entry in array[1]:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        name = str(entry[0]).lower()
        text = _flatten(entry[3])
        if text and name not in fields:
            fields[name] = text
    return fields


def _flatten(value: Any) -> str:
    if isinstance(value, list):
        return ' '.join(part for part in (_flatten(item) for item in value) if part).strip()
    if isinstance(value, dict) or value is None:
        return ''
    return str(value).strip()


def _event_date(payload: dict[str, Any], action: str) -> str:
    events = payload.get('events')
    if not isinstance(events, list):
        return ''
    for event in events:
        if isinstance(event, dict) and event.get('eventAction') == action:
            return str(event.get('eventDate', ''))[:10]
    return ''


# -- literature --------------------------------------------------------------------------------
@dataclass(frozen=True)
class LiteratureHit:
    """One publication whose text mentions the domain."""

    title: str = ''
    authors: str = ''
    first_author: str = ''
    year: str = ''
    journal: str = ''
    doi: str = ''
    is_preprint: bool = False


@dataclass(frozen=True)
class Literature:
    """What the scientific literature says about a domain."""

    status: str
    hit_count: int = 0
    hits: list[LiteratureHit] = field(default_factory=list)
    distinct_first_authors: list[str] = field(default_factory=list)
    detail: str = ''


def search_literature(domain: str, fetch: Fetch | None = None, limit: int = MAX_HITS) -> Literature:
    """Full-text search Europe PMC for the domain as a quoted phrase.

    `distinct_first_authors` is the signal worth reading: one first author across every hit points
    at a single-principal domain, while many point at an institution.
    """
    fetch = fetch or http_get
    query = urllib.parse.urlencode(
        {
            'query': f'"{domain.strip().lower()}"',
            'format': 'json',
            'resultType': 'lite',
            'pageSize': str(limit),
        }
    )
    try:
        payload = json.loads(fetch(f'{EUROPE_PMC_URL}?{query}'))
    except FetchError as error:
        return Literature(status=UNAVAILABLE, detail=f'Europe PMC search failed: {error}')
    except (ValueError, TypeError) as error:
        return Literature(
            status=UNAVAILABLE, detail=f'Europe PMC returned unparseable JSON: {error}'
        )

    if not isinstance(payload, dict):
        return Literature(status=UNAVAILABLE, detail='Europe PMC response was not an object.')
    return parse_literature(payload, limit)


def parse_literature(payload: dict[str, Any], limit: int = MAX_HITS) -> Literature:
    results = payload.get('resultList', {})
    records = results.get('result', []) if isinstance(results, dict) else []
    hits = [_hit(record) for record in records[:limit] if isinstance(record, dict)]
    authors = list(dict.fromkeys(hit.first_author for hit in hits if hit.first_author))
    try:
        count = int(payload.get('hitCount', 0))
    except (TypeError, ValueError):
        count = len(hits)
    return Literature(
        status=FOUND if count else NOT_FOUND,
        hit_count=count,
        hits=hits,
        distinct_first_authors=authors,
    )


def _hit(record: dict[str, Any]) -> LiteratureHit:
    authors = str(record.get('authorString', '')).strip()
    return LiteratureHit(
        title=str(record.get('title', '')).strip(),
        authors=authors,
        first_author=first_author(authors),
        year=str(record.get('pubYear', '')).strip(),
        journal=str(record.get('journalTitle') or record.get('bookOrReportDetails') or '').strip(),
        doi=str(record.get('doi', '')).strip(),
        is_preprint=str(record.get('source', '')).upper() == 'PPR',
    )


def first_author(author_string: str) -> str:
    """The first name out of a Europe PMC `authorString` such as `Smith J, Doe A.`."""
    head = author_string.split(',')[0].strip().rstrip('.')
    return head


# -- one domain, both sources ------------------------------------------------------------------
@dataclass(frozen=True)
class DomainEvidence:
    """Everything this server can find out about one domain."""

    domain: str
    registration: Registration
    literature: Literature
    not_searched: list[str] = field(default_factory=lambda: list(NOT_SEARCHED))

    @property
    def resolved(self) -> bool:
        """Whether any source said something substantive about the domain."""
        named = bool(self.registration.registrant_name or self.registration.registrant_organization)
        return named or self.literature.hit_count > 0


def research_domain(domain: str, fetch: Fetch | None = None) -> DomainEvidence:
    return DomainEvidence(
        domain=domain.strip().lower(),
        registration=lookup_registration(domain, fetch),
        literature=search_literature(domain, fetch),
    )


def research_domains(domains: list[str], fetch: Fetch | None = None) -> list[DomainEvidence]:
    """Research several domains, in the order given.

    Concurrent because each domain costs two round trips and a queue of thirty would otherwise
    outlast the caller's patience; the worker count is small enough to stay polite to two free
    public APIs.
    """
    wanted = list(dict.fromkeys(domain.strip().lower() for domain in domains if domain.strip()))
    if not wanted:
        return []
    if len(wanted) == 1:
        return [research_domain(wanted[0], fetch)]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(wanted))) as pool:
        return list(pool.map(lambda domain: research_domain(domain, fetch), wanted))


__all__ = [
    'NOT_SEARCHED',
    'DomainEvidence',
    'FetchError',
    'Literature',
    'LiteratureHit',
    'Registration',
    'first_author',
    'http_get',
    'is_privacy_shielded',
    'lookup_registration',
    'parse_literature',
    'parse_registration',
    'research_domain',
    'research_domains',
    'search_literature',
]
