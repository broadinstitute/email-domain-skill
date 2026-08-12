"""The local working directory.

Workbooks are downloaded from Drive to disk and worked on there. That is what lets the Excel
MCP server touch them at all — in stdio mode it requires an absolute local path — and it is
what keeps workbook bytes out of the model's context.

Layout under the work directory:

    <workdir>/analysis.xlsx          the analysis record, unless overridden
    <workdir>/<file-id>/<name>.xlsx  a downloaded metrics workbook
    <workdir>/<file-id>/<name> (anonymized).xlsx

Downloads are cached: a file is re-fetched only when Drive reports a `modifiedTime` newer than
the marker written beside it. Re-scanning a workbook is a normal thing to do, and it should not
mean re-downloading it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

WORKDIR_ENV = 'EMAIL_DOMAIN_WORKDIR'
ANALYSIS_WORKBOOK_ENV = 'EMAIL_DOMAIN_ANALYSIS_WORKBOOK'

DEFAULT_ANALYSIS_NAME = 'analysis.xlsx'
ANONYMIZED_SUFFIX = ' (anonymized)'

_UNSAFE = re.compile(r'[^A-Za-z0-9 ._()-]+')


def workdir() -> Path:
    """The work directory, created if absent."""
    override = os.environ.get(WORKDIR_ENV, '').strip()
    root = (
        Path(override).expanduser()
        if override
        else Path.home() / '.cache' / 'email-domain-scrubber'
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def analysis_workbook_path(override: str | None = None) -> Path:
    """Where the analysis record lives: the argument, the environment, or the default."""
    reference = (override or os.environ.get(ANALYSIS_WORKBOOK_ENV) or '').strip()
    if reference:
        return Path(reference).expanduser()
    return workdir() / DEFAULT_ANALYSIS_NAME


def safe_name(name: str) -> str:
    """A Drive file name reduced to something safe to write to disk."""
    cleaned = _UNSAFE.sub('_', name).strip(' .') or 'workbook'
    return cleaned[:120]


def _stem(name: str) -> str:
    cleaned = safe_name(name)
    return cleaned[:-5] if cleaned.lower().endswith('.xlsx') else cleaned


class Staging:
    """Local copies of Drive workbooks, keyed by Drive file id."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or workdir()

    @property
    def root(self) -> Path:
        return self._root

    def directory(self, file_id: str) -> Path:
        path = self._root / safe_name(file_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def workbook_path(self, file_id: str, name: str) -> Path:
        return self.directory(file_id) / f'{_stem(name)}.xlsx'

    def anonymized_path(self, file_id: str, name: str) -> Path:
        """`<name> (anonymized).xlsx`, or `… 2`, `… 3` if that already exists.

        Never overwrites: an earlier anonymized copy may already have been published, and
        silently reusing its path would make a second redaction look like a no-op.
        """
        directory = self.directory(file_id)
        base = f'{_stem(name)}{ANONYMIZED_SUFFIX}'
        candidate = directory / f'{base}.xlsx'
        if not candidate.exists():
            return candidate
        for suffix in range(2, 100):
            candidate = directory / f'{base} {suffix}.xlsx'
            if not candidate.exists():
                return candidate
        raise RuntimeError(f'Could not find an unused name based on {base!r} after 99 tries.')

    def is_current(self, path: Path, modified_time: str) -> bool:
        """True if `path` was downloaded at or after Drive's `modified_time`."""
        marker = path.with_suffix('.modified')
        if not (path.exists() and marker.exists()):
            return False
        # Absent a modifiedTime from Drive we cannot prove freshness, so re-download.
        return bool(modified_time) and marker.read_text().strip() == modified_time

    def write(self, path: Path, content: bytes, modified_time: str = '') -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.with_suffix('.modified').write_text(modified_time)
        return path
