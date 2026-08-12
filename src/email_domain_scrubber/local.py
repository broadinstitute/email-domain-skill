"""Local `.xlsx` workbooks — the default and, for now, only working source.

Nothing here touches Google. `drive.py` is intact and still wired up, but the connector path is
known broken (the rclone remote's scopes do not satisfy it) and is left to be finished later, so a
reference is treated as a local path unless it clearly names something remote.

A local workbook is read in place and never modified. The staged copy that redaction writes to
still lands under the work directory rather than beside the original, so nothing unexpected
appears next to the user's file.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .drive import XLSX_MIME, FileInfo
from .errors import InvalidWorkbookReference, UnsupportedWorkbook

#: `scheme://` at the start of a reference, which means it is not a filesystem path.
_REMOTE_SCHEME = re.compile(r'^[a-z][a-z0-9+.-]*://', re.IGNORECASE)


def is_local_reference(value: str) -> bool:
    """True if `value` names a file on this machine rather than a Drive file.

    Deliberately conservative about what counts as a path: a bare Drive file id is an unbroken
    run of id characters, so requiring a separator, a `.xlsx` suffix, or a leading `~`/`.` keeps
    the two apart without guessing.
    """
    reference = (value or '').strip()
    if not reference:
        return False
    if reference.lower().startswith('file://'):
        return True
    if _REMOTE_SCHEME.match(reference):
        return False
    return (
        reference.lower().endswith('.xlsx')
        or reference.startswith(('~', '.', '/'))
        or '/' in reference
    )


def resolve(value: str) -> Path:
    """The absolute path a local reference names, checked for existence and format."""
    reference = (value or '').strip()
    if reference.lower().startswith('file://'):
        from urllib.parse import unquote, urlparse

        reference = unquote(urlparse(reference).path)

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
    """A `file://` URL, used wherever a Drive workbook would contribute its Drive link."""
    return path.as_uri()


def info(path: Path) -> FileInfo:
    """`FileInfo` for a local file.

    `file_id` carries the absolute path. It is what keys the staging directory and what the
    audit trail resolves back to, and for a local file the path is the durable identifier.
    """
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return FileInfo(
        file_id=str(path), name=path.name, mime_type=XLSX_MIME, modified_time=modified.isoformat()
    )
