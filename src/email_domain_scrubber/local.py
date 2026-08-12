"""Local `.xlsx` workbooks — the one and only source of metrics reports.

Where a report came from is not this server's business. Fetch it however you like — a browser, a
sync client, a cloud-storage plugin — and pass the path to the file on disk.

A workbook is read in place and never modified. The anonymized copy that redaction writes lands
under the work directory rather than beside the original, so nothing unexpected appears next to
the user's file.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .errors import InvalidWorkbookReference, UnsupportedWorkbook

#: `scheme://` at the start of a reference, which means it is not a filesystem path.
_REMOTE_SCHEME = re.compile(r'^[a-z][a-z0-9+.-]*://', re.IGNORECASE)


def resolve(value: str) -> Path:
    """The absolute path a reference names, checked for existence and format.

    A `file://` URL is unwrapped; any other scheme is refused outright. A remote URL would
    otherwise fall through to `No such file`, which does not tell the caller what to do about it.
    """
    reference = (value or '').strip()
    if not reference:
        raise InvalidWorkbookReference('No workbook path was provided.')

    if reference.lower().startswith('file://'):
        reference = unquote(urlparse(reference).path)
    elif _REMOTE_SCHEME.match(reference):
        raise InvalidWorkbookReference(
            f'{reference!r} is a URL, not a path. This server reads workbooks from disk: '
            'download the file and pass the path it landed at.'
        )

    path = Path(reference).expanduser()
    try:
        path = path.resolve()
    except OSError as error:  # pragma: no cover - unresolvable paths are rare
        raise InvalidWorkbookReference(f'{reference!r} could not be resolved: {error}') from error

    if not path.exists():
        raise InvalidWorkbookReference(f'No such file: {path}')
    if not path.is_file():
        raise InvalidWorkbookReference(f'{path} is not a file.')
    if path.suffix.lower() != '.xlsx':
        raise UnsupportedWorkbook(
            f'{path.name!r} is not an Excel workbook. This server handles .xlsx files only — '
            'convert Google Sheets and CSV files first (File > Download > Microsoft Excel).'
        )
    return path


def url(path: Path) -> str:
    """A `file://` URL for `path`. The base of every cell locator."""
    return path.as_uri()


def cell_reference(source_url: str, sheet_title: str, a1: str) -> str:
    """A locator for one cell of a workbook.

    A `file://` URL cannot deep-link a cell, so the URL names the file and the fragment names the
    cell for a human reading the audit trail.
    """
    return f'{source_url}#{sheet_title}!{a1}'
