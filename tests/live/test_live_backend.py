"""End-to-end against the real Google Drive MCP connector.

Run with `uv run pytest --live`. Everything here creates files in the signed-in user's My Drive
and cleans up afterwards; see `tests/live/conftest.py`.

These cover the parts an in-memory fake cannot vouch for: that the connector accepts our token,
that `create_file` keeps an `.xlsx` an `.xlsx` instead of converting it to a Google Sheet, and
that a workbook survives the upload/download round trip byte for byte.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from email_domain_scrubber import xlsx
from email_domain_scrubber.drive import XLSX_MIME, parse_file_id
from email_domain_scrubber.errors import UnsupportedWorkbook
from email_domain_scrubber.redact import create_copy, plan_redaction, record, verify
from email_domain_scrubber.scan import scan_path, stage_workbook
from email_domain_scrubber.staging import Staging
from email_domain_scrubber.workbook import REDACTIONS, AnalysisWorkbook

pytestmark = pytest.mark.live

USERS = {
    'Users': [
        ['User', 'Email'],
        ['Alice', 'alice@smithlab.io'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', 'carol@smithlab.io'],
    ]
}


def test_the_connector_advertises_the_documented_tools(live_drive):
    tools = asyncio.run(live_drive.list_tools())

    assert set(tools) == {
        'copy_file',
        'create_file',
        'download_file_content',
        'get_file_metadata',
        'get_file_permissions',
        'list_recent_files',
        'read_file_content',
        'search_files',
    }


def test_an_uploaded_xlsx_stays_an_xlsx(scratch, live_drive):
    """Without disableConversionToGoogleType Drive would silently make this a Google Sheet."""
    uploaded = asyncio.run(scratch.upload('stays-excel', USERS))

    metadata = asyncio.run(live_drive.get_metadata(uploaded.file_id))

    assert metadata.mime_type == XLSX_MIME
    assert metadata.is_xlsx


def test_a_workbook_survives_the_round_trip(scratch, live_drive, tmp_path):
    uploaded = asyncio.run(scratch.upload('round-trip', USERS))

    downloaded = asyncio.run(live_drive.download(uploaded.file_id))

    path = tmp_path / 'returned.xlsx'
    path.write_bytes(downloaded)
    assert xlsx.read_rows(path, 'Users') == USERS['Users']


def test_staging_reports_metadata_and_caches_the_download(scratch, live_drive, tmp_path):
    uploaded = asyncio.run(scratch.upload('staging', USERS))
    staging = Staging(tmp_path / 'work')

    first = asyncio.run(stage_workbook(live_drive, staging, uploaded.file_id))
    second = asyncio.run(stage_workbook(live_drive, staging, uploaded.file_id))

    assert first.downloaded
    assert first.info.modified_time, 'connector must report modifiedTime for caching to work'
    assert not second.downloaded


def test_a_google_sheet_is_refused(scratch, live_drive, tmp_path):
    """Uploaded with conversion left on, so Drive stores it as a native Sheet."""
    local = tmp_path / 'native.xlsx'
    xlsx.create(local, USERS)
    sheet = asyncio.run(
        live_drive.create(
            f'{scratch.prefix} native-sheet.xlsx', local.read_bytes(), mime_type=XLSX_MIME
        )
    )
    scratch.created.append(sheet)

    # The upload above disables conversion, so force the check with a Google-native mime type.
    doc = asyncio.run(
        live_drive.create(
            f'{scratch.prefix} native-doc',
            b'not a workbook',
            mime_type='application/vnd.google-apps.document',
        )
    )
    scratch.created.append(doc)

    with pytest.raises(UnsupportedWorkbook):
        asyncio.run(stage_workbook(live_drive, Staging(tmp_path / 'work'), doc.file_id))


def test_the_whole_workflow_against_real_drive(scratch, live_drive, tmp_path):
    uploaded = asyncio.run(scratch.upload('workflow', USERS))
    staging = Staging(tmp_path / 'work')
    analysis = AnalysisWorkbook.open(tmp_path / 'analysis.xlsx')

    staged = asyncio.run(stage_workbook(live_drive, staging, uploaded.file_id))
    hits = scan_path(staged.path, staged.info.file_id)
    assert {hit.domain for hit in hits} == {'smithlab.io', 'broadinstitute.org'}

    analysis.record_workbook(staged.url, staged.info.name)
    analysis.ensure_analysis_rows([hit.domain for hit in hits])
    analysis.store_analysis(
        [
            ('smithlab.io', 'High', 'Personal lab domain', None),
            ('broadinstitute.org', 'Low', 'Broad Institute', None),
        ],
        random.Random(5),
    )

    plan = plan_redaction(staged, hits, analysis)
    alias = plan.mapped_domains['smithlab.io']
    copy = create_copy(staged, staging)

    # Stands in for the Excel MCP server, which applies exactly these blocks.
    from tests.fakes import Recorder

    Recorder().apply(copy, plan.blocks)
    assert verify(copy, plan.mapped_domains) == []

    published = asyncio.run(live_drive.create(copy.name, copy.read_bytes()))
    scratch.created.append(published)
    record(plan, f'https://drive.google.com/file/d/{published.file_id}/view', analysis)

    # Read the published file back out of Drive rather than trusting the local copy.
    returned = tmp_path / 'published.xlsx'
    returned.write_bytes(asyncio.run(live_drive.download(published.file_id)))
    assert xlsx.read_rows(returned, 'Users') == [
        ['User', 'Email'],
        ['Alice', f'alice@{alias}'],
        ['Bob', 'bob@broadinstitute.org'],
        ['Carol', f'carol@{alias}'],
    ]
    assert {hit.domain for hit in scan_path(returned, published.file_id)} == {'broadinstitute.org'}

    rows = xlsx.read_rows(analysis.path, REDACTIONS)[1:]
    assert len(rows) == 2
    assert all(parse_file_id(row[2]) == published.file_id for row in rows)


def test_the_drive_original_is_untouched_by_the_whole_flow(scratch, live_drive, tmp_path):
    uploaded = asyncio.run(scratch.upload('untouched', USERS))
    before = asyncio.run(live_drive.download(uploaded.file_id))

    staging = Staging(tmp_path / 'work')
    analysis = AnalysisWorkbook.open(tmp_path / 'analysis.xlsx')
    staged = asyncio.run(stage_workbook(live_drive, staging, uploaded.file_id))
    hits = scan_path(staged.path, staged.info.file_id)
    analysis.store_analysis(
        [
            ('smithlab.io', 'High', 'Personal lab domain', None),
            ('broadinstitute.org', 'Low', 'Broad Institute', None),
        ],
        random.Random(5),
    )
    plan = plan_redaction(staged, hits, analysis)
    copy = create_copy(staged, staging)
    from tests.fakes import Recorder

    Recorder().apply(copy, plan.blocks)

    assert asyncio.run(live_drive.download(uploaded.file_id)) == before
