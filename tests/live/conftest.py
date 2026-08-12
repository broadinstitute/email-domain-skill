"""Fixtures for the tests that talk to the real Google Drive MCP connector.

These need working credentials — see the setup section of the README. The fixtures here exist to
make running them against a real account safe:

* every scratch file is named with a unique per-run prefix, and
* teardown removes everything matching that prefix, not just the ids a test remembered.

The second point matters because the code under test creates files of its own — an
`(anonymized)` copy of a metrics workbook — and a test that fails halfway never gets to register
them. Sweeping by name leaves nothing behind either way.

Cleanup goes through **rclone**, not the connector: the Drive MCP connector can create and copy
files but offers no delete, trash, or update. rclone is already required as the credential
source, so this adds no new dependency — but if the binary is missing, teardown says exactly
what was left behind rather than failing silently.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from email_domain_scrubber import auth, xlsx
from email_domain_scrubber.drive import DriveMcpClient, FileInfo
from email_domain_scrubber.errors import ScrubberError

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
        """Delete every file whose name starts with this run's prefix."""
        remote = auth.configured_remote()
        if not shutil.which('rclone'):
            self._warn('rclone is not on PATH')
            return
        result = subprocess.run(
            ['rclone', 'delete', f'{remote}:', '--include', f'{self.prefix}*', '--drive-use-trash'],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            self._warn(f'rclone delete failed: {result.stderr.strip()}')

    def _warn(self, reason: str) -> None:
        print(
            f'\nWARNING: could not clean up live test files ({reason}).\n'
            f'  Remove them by searching Drive for: {self.prefix}'
        )


@pytest.fixture
def scratch(live_drive: DriveMcpClient, tmp_path: Path):
    workspace = Scratch(
        drive=live_drive, prefix=f'{PREFIX}-{uuid.uuid4().hex[:8]}', tmp_path=tmp_path
    )
    try:
        yield workspace
    finally:
        workspace.sweep()
