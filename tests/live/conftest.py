"""Fixtures for the tests that talk to the real Google Drive MCP connector.

These need working credentials — see the setup section of the README. The fixtures here exist to
make running them against a real account safe:

* every scratch file is named with a unique per-run prefix, and
* teardown removes everything matching that prefix, not just the ids a test remembered.

The second point matters because the code under test creates files of its own — an
`(anonymized)` copy of a metrics workbook — and a test that fails halfway never gets to register
them. Sweeping by name leaves nothing behind either way.

Cleanup cannot go through the connector, which offers no delete, trash, or update — so teardown
deletes by file id through the Drive REST API, using the same token. Deleting by id rather than
by name matters: `create_file` chooses its own parent when none is given, and has been seen to
put files in a shared drive root, where a name-based sweep of My Drive would never find them.
This is the one place the Drive REST API appears, and only to undo what the tests did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from email_domain_scrubber import xlsx
from email_domain_scrubber.auth import TokenSource
from email_domain_scrubber.drive import DriveMcpClient, FileInfo
from email_domain_scrubber.errors import ScrubberError

DRIVE_FILES = 'https://www.googleapis.com/drive/v3/files'

#: Every scratch file starts with this, so the sweep can find them and a human scanning their
#: Drive can tell at a glance what these are.
PREFIX = 'zz-scrubber-test'


@pytest.fixture(scope='session')
def live_drive() -> DriveMcpClient:
    """Prove the connector answers before any test writes to Drive; say how to fix it if not."""
    import asyncio

    client = DriveMcpClient()
    try:
        tools = asyncio.run(client.list_tools())
    except ScrubberError as exc:
        pytest.fail(
            f'Live tests need a reachable Drive MCP connector.\n\n{exc}\n\n'
            'Check it with:  uv run email-domain-scrubber check-auth',
            pytrace=False,
        )
    assert 'create_file' in tools, f'connector did not advertise create_file, only {tools}'
    return client


@dataclass
class Scratch:
    """Creates uniquely named files in My Drive and removes everything it caused."""

    drive: DriveMcpClient
    prefix: str
    tmp_path: Path
    created: list[FileInfo] = field(default_factory=list)

    def name(self, label: str) -> str:
        return f'{self.prefix} {label}.xlsx'

    async def upload(self, label: str, sheets: dict[str, list[list[str]]]) -> FileInfo:
        """Put a real `.xlsx` in Drive, as a user uploading a metrics report would."""
        local = self.tmp_path / f'{label}.xlsx'
        xlsx.create(local, sheets)
        info = await self.drive.create(self.name(label), local.read_bytes())
        self.created.append(info)
        return info

    def sweep(self) -> None:
        """Delete every file this run created, by id."""
        import asyncio

        leftover = asyncio.run(self._delete_all())
        if leftover:
            print(
                f'\nWARNING: could not delete {len(leftover)} live test file(s):\n'
                + '\n'.join(f'  {name} ({reason})' for name, reason in leftover)
                + f'\n  Remove them by searching Drive for: {self.prefix}'
            )

    async def _delete_all(self) -> list[tuple[str, str]]:
        import httpx2

        token = await TokenSource().token()
        failures: list[tuple[str, str]] = []
        async with httpx2.AsyncClient(
            timeout=60, headers={'Authorization': f'Bearer {token}'}
        ) as http:
            for info in self.created:
                if not info.file_id:
                    continue
                try:
                    response = await http.delete(
                        f'{DRIVE_FILES}/{info.file_id}', params={'supportsAllDrives': 'true'}
                    )
                except Exception as exc:  # noqa: BLE001 - never mask the test's own failure
                    failures.append((info.name, str(exc)))
                    continue
                if response.status_code not in (200, 204, 404):
                    failures.append((info.name, f'HTTP {response.status_code}'))
        return failures


@pytest.fixture
def scratch(live_drive: DriveMcpClient, tmp_path: Path):
    workspace = Scratch(
        drive=live_drive, prefix=f'{PREFIX}-{uuid.uuid4().hex[:8]}', tmp_path=tmp_path
    )
    try:
        yield workspace
    finally:
        workspace.sweep()
