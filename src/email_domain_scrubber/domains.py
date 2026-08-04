"""Finding email domain names in spreadsheet cell text.

Two kinds of match are recognised, with deliberately different strictness:

* Addresses (``alice@smithlab.io``) — the ``@`` is strong evidence, so any casing and any
  alphabetic TLD is accepted.
* Bare domains (a ``Domain`` column holding ``smithlab.io``) — no such evidence, so the match
  must be entirely lowercase and end in a recognised TLD. Metric reports are full of dotted
  tokens that are not domains (``Total.Count``, ``report.csv``, ``1.2.3``, ``Fig.2A``), and
  these two rules reject them without needing a full public-suffix list.
"""

import re

_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?'
_HOSTNAME = rf'{_LABEL}(?:\.{_LABEL})+'

_ADDRESS = re.compile(rf'[A-Za-z0-9._%+-]+@(?P<domain>{_HOSTNAME})')

# Not preceded by an address-ish character (so we never re-match the domain half of an
# address, or the tail of a longer dotted token) and not followed by one either.
_BARE = re.compile(rf'(?<![A-Za-z0-9@._%+-])(?P<domain>{_HOSTNAME})(?![A-Za-z0-9@-])')

# Trailing labels that look like TLDs but are file extensions.
_NOT_TLDS = frozenset({
    'bam', 'bed', 'bigwig', 'cram', 'csv', 'doc', 'docx', 'exe', 'fasta', 'fastq', 'gff', 'gtf',
    'gz', 'htm', 'html', 'ipynb', 'jpeg', 'jpg', 'json', 'log', 'md', 'pdf', 'png', 'ppt', 'pptx',
    'py', 'rds', 'rmd', 'sam', 'sh', 'so', 'sql', 'svg', 'tar', 'tsv', 'txt', 'vcf', 'xls', 'xlsx',
    'xml', 'yaml', 'yml', 'zip',
})  # fmt: skip

# Common gTLDs seen in research-platform usage metrics. Any two-letter ccTLD is also accepted,
# which covers .uk/.de/.jp as well as .ac.uk-style suffixes.
_GTLDS = frozenset({
    'academy', 'agency', 'ai', 'app', 'bio', 'biz', 'cloud', 'co', 'com', 'dev', 'digital', 'edu',
    'email', 'eu', 'foundation', 'gov', 'group', 'health', 'info', 'institute', 'int', 'io', 'lab',
    'life', 'link', 'me', 'mil', 'net', 'ninja', 'online', 'org', 'page', 'pro', 'pub', 'research',
    'science', 'site', 'software', 'solutions', 'space', 'store', 'tech', 'university', 'us',
    'website', 'world', 'xyz',
})  # fmt: skip


def _plausible_tld(tld: str) -> bool:
    lowered = tld.lower()
    if not lowered.isalpha() or lowered in _NOT_TLDS:
        return False
    return len(lowered) == 2 or lowered in _GTLDS


def normalize(domain: str) -> str:
    """Fold a raw match to the canonical form stored in the workbook."""
    cleaned = domain.strip().strip('.').lower()
    return cleaned.removeprefix('www.')


def _accept_address(domain: str) -> bool:
    tld = domain.rpartition('.')[2]
    return tld.isalpha() and len(tld) >= 2 and tld.lower() not in _NOT_TLDS


def extract_domains(text: str) -> list[str]:
    """Return the normalized email domains in `text`, in order of first appearance."""
    if not text or '.' not in text:
        return []

    found: dict[str, None] = {}
    for match in _ADDRESS.finditer(text):
        domain = match.group('domain')
        if _accept_address(domain):
            found.setdefault(normalize(domain), None)

    for match in _BARE.finditer(text):
        domain = match.group('domain')
        if domain != domain.lower():
            continue
        if _plausible_tld(domain.rpartition('.')[2]):
            found.setdefault(normalize(domain), None)

    return list(found)


def redaction_pattern(domain: str) -> re.Pattern[str]:
    """Match `domain` as a whole hostname, not as a suffix of a longer one.

    ``smithlab.io`` must not match inside ``mail.smithlab.io`` — that subdomain is extracted and
    analyzed as a domain in its own right, and replacing only its tail would corrupt it.
    """
    return re.compile(rf'(?<![A-Za-z0-9.-]){re.escape(domain)}(?![A-Za-z0-9-])', re.IGNORECASE)


def apply_redactions(text: str, mapping: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute anonymized tokens into `text`.

    Returns the rewritten text and the domains actually replaced. Longer domains are replaced
    first so that a mapping for both ``smithlab.io`` and ``mail.smithlab.io`` behaves.
    """
    replaced: list[str] = []
    result = text
    for domain in sorted(mapping, key=len, reverse=True):
        pattern = redaction_pattern(domain)
        result, count = pattern.subn(mapping[domain], result)
        if count:
            replaced.append(domain)
    return result, replaced
