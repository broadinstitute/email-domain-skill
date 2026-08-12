"""Fixtures for the tests that talk to real Google Sheets and Drive.

These need a signed-in user, and signing in is interactive — see `tests/live/README.md`. The
fixtures here exist to make that safe to run against a real account:

* every scratch file is named with a unique per-test prefix, and
* teardown trashes everything matching that prefix, not just the ids a test remembered.

The second point matters because the code under test creates files of its own — a converted
`(Sheets)` copy of an upload, an `(anonymized)` copy of a metrics workbook — and a test that
fails halfway never gets to register them. Sweeping by name leaves nothing behind either way.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from email_domain_scrubber import auth
from email_domain_scrubber.errors import ScrubberError
from email_domain_scrubber.sheets import FileInfo, SpreadsheetInfo, spreadsheet_url

#: Every scratch file starts with this, so the sweep can find them and a human scanning their
#: Drive can tell at a glance what these are.
PREFIX = 'zz-scrubber-test'


@pytest.fixture(scope='session')
def live_access() -> auth.AccessCheck:
    """Prove credentials work before any test writes to Drive, and say how to fix them if not."""
    try:
        return auth.verify_access()
    except ScrubberError as exc:
        pytest.fail(
            f'Live tests need working Google credentials.\n\n{exc}\n\n'
            f'After signing in, check it with:  uv run email-domain-scrubber check-auth',
            pytrace=False,
        )


@pytest.fixture(scope='session')
def drive(live_access: auth.AccessCheck) -> Any:
    from googleapiclient.discovery import build

    return build('drive', 'v3', credentials=auth.credentials(), cache_discovery=False)


@pytest.fixture(scope='session')
def live_backend(live_access: auth.AccessCheck) -> Any:
    return auth.google_backend()


@dataclass
class Scratch:
    """Creates uniquely named files in My Drive and cleans up everything it caused."""

    backend: Any
    drive: Any
    prefix: str
    created: list[str] = field(default_factory=list)

    def name(self, label: str) -> str:
        return f'{self.prefix} {label}'

    def spreadsheet(self, label: str, sheets: dict[str, list[list[str]]]) -> SpreadsheetInfo:
        info = self.backend.create_spreadsheet(self.name(label), list(sheets))
        self.created.append(info.spreadsheet_id)
        writes = {f"'{title}'!A1": rows for title, rows in sheets.items() if rows}
        if writes:
            self.backend.write_ranges(info.spreadsheet_id, writes)
        return info

    def upload(self, label: str, content: bytes, mime_type: str) -> FileInfo:
        """Put a non-native file (CSV, XLSX) in Drive, as a user uploading a report would."""
        from googleapiclient.http import MediaInMemoryUpload

        payload = (
            self.drive.files()
            .create(
                body={'name': self.name(label)},
                media_body=MediaInMemoryUpload(content, mimetype=mime_type),
                fields='id, name, mimeType, parents',
                supportsAllDrives=True,
            )
            .execute()
        )
        self.created.append(payload['id'])
        return FileInfo(
            file_id=payload['id'],
            name=payload.get('name', ''),
            mime_type=payload.get('mimeType', ''),
            parents=tuple(payload.get('parents', ())),
        )

    def url(self, spreadsheet_id: str) -> str:
        return spreadsheet_url(spreadsheet_id)

    def sweep(self) -> list[str]:
        """Trash every file whose name starts with this run's prefix."""
        found = (
            self.drive.files()
            .list(
                q=f"name contains '{self.prefix}' and trashed = false",
                fields='files(id, name)',
                pageSize=100,
                corpora='allDrives',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get('files', [])
        )
        trashed = []
        for file in found:
            try:
                self.drive.files().update(
                    fileId=file['id'], body={'trashed': True}, supportsAllDrives=True
                ).execute()
                trashed.append(file['name'])
            except Exception as exc:  # noqa: BLE001 - report, never mask the test's own failure
                print(f'WARNING: could not trash {file["name"]} ({file["id"]}): {exc}')
        return trashed


@pytest.fixture
def scratch(live_backend: Any, drive: Any) -> Any:
    workspace = Scratch(backend=live_backend, drive=drive, prefix=f'{PREFIX}-{uuid.uuid4().hex[:8]}')
    try:
        yield workspace
    finally:
        workspace.sweep()
