"""Test helpers.

Workbooks are real `.xlsx` files built with openpyxl, so the tests exercise the same reader and
writer that production uses. Nothing about the spreadsheet layer is faked.

The one thing faked is the network: `fake_fetch` stands in for `research.http_get`, so the
research tests are as offline as the rest of the suite.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from email_domain_scrubber import xlsx
from email_domain_scrubber.research import FetchError


def write_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> Path:
    """Build a real `.xlsx` on disk. The fixture workbook for most tests."""
    xlsx.create(path, sheets)
    return path


def fake_fetch(responses: dict[str, str | Exception]) -> Callable[[str], bytes]:
    """A `research.Fetch` that answers by URL substring.

    Keyed on a substring rather than the whole URL so a test can say 'the RDAP call' or 'the
    Europe PMC call' without restating query strings. An unmatched URL raises, which is what an
    unreachable source looks like to the code under test.
    """

    def fetch(url: str) -> bytes:
        for marker, response in responses.items():
            if marker in url:
                if isinstance(response, Exception):
                    raise response
                return response.encode()
        raise FetchError(f'no canned response for {url}')

    return fetch
