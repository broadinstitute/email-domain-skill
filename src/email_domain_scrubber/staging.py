"""The local working directory.

Everything this server produces lands here rather than beside the user's report, so nothing
unexpected appears next to the file they pointed at.

Layout under the work directory:

    <workdir>/analysis.xlsx        the analysis record, unless overridden
    <workdir>/<key>/<name> (anonymized).xlsx

`<key>` is the source workbook's absolute path, flattened by `safe_name`, which keeps copies of
two same-named reports from different folders apart.
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
    """A file name or path reduced to something safe to write to disk."""
    cleaned = _UNSAFE.sub('_', name).strip(' .') or 'workbook'
    return cleaned[:120]


def _stem(name: str) -> str:
    cleaned = safe_name(name)
    return cleaned[:-5] if cleaned.lower().endswith('.xlsx') else cleaned


class Staging:
    """Anonymized copies, kept one directory per source workbook."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or workdir()

    @property
    def root(self) -> Path:
        return self._root

    def directory(self, key: str) -> Path:
        path = self._root / safe_name(key)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def anonymized_path(self, key: str, name: str) -> Path:
        """`<name> (anonymized).xlsx`, or `… 2`, `… 3` if that already exists.

        Never overwrites: an earlier anonymized copy may already have been shared, and silently
        reusing its path would make a second redaction look like a no-op.
        """
        directory = self.directory(key)
        base = f'{_stem(name)}{ANONYMIZED_SUFFIX}'
        candidate = directory / f'{base}.xlsx'
        if not candidate.exists():
            return candidate
        for suffix in range(2, 100):
            candidate = directory / f'{base} {suffix}.xlsx'
            if not candidate.exists():
                return candidate
        raise RuntimeError(f'Could not find an unused name based on {base!r} after 99 tries.')
