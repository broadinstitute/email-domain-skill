"""Turning Drive MCP connector faults into errors that say what to do.

The connector reports a failed tool call as `result.isError` with the detail in text content,
not as a JSON-RPC error, so `DriveMcpClient` has to classify that text. These tests pin the
classification, including the wording Google actually returns for the most likely first-run
failure.
"""

import asyncio

import pytest

from email_domain_scrubber.drive import (
    XLSX_MIME,
    DriveMcpClient,
    FileInfo,
    _classify,
    _file_info,
    cell_reference,
    file_url,
    parse_file_id,
)
from email_domain_scrubber.errors import (
    AccessDenied,
    DriveMcpApiDisabled,
    DriveMcpError,
    InvalidWorkbookReference,
    MissingScopes,
    WorkbookNotFound,
)

#: Verbatim from the live endpoint, with the Drive MCP API disabled on the client's project.
API_DISABLED = (
    'Drive MCP API has not been used in project 497778860653 before or it is disabled. Enable '
    'it by visiting https://console.developers.google.com/apis/api/drivemcp.googleapis.com/'
    'overview?project=497778860653 then retry.'
)


# -- classification ----------------------------------------------------------------------------
def test_the_disabled_api_message_is_recognised():
    error = _classify('search_files', API_DISABLED)

    assert isinstance(error, DriveMcpApiDisabled)
    # Google names the project and the console URL, so both must survive.
    assert '497778860653' in str(error)
    assert 'drivemcp.googleapis.com' in str(error)
    assert 'EMAIL_DOMAIN_OAUTH_CLIENT_ID' in str(error)


def test_a_missing_scope_is_recognised():
    assert isinstance(
        _classify('create_file', 'Insufficient scope for this request'), MissingScopes
    )


def test_a_missing_file_is_recognised():
    error = _classify('get_file_metadata', 'File not found: abc123')

    assert isinstance(error, WorkbookNotFound)
    assert 'shared with the signed-in account' in str(error)


def test_a_permission_failure_is_recognised():
    error = _classify('create_file', 'The user does not have permission to write here')

    assert isinstance(error, AccessDenied)
    assert 'acts as the signed-in user' in str(error)


def test_anything_else_names_the_tool_that_failed():
    error = _classify('download_file_content', 'something strange happened')

    assert isinstance(error, DriveMcpError)
    assert error.tool == 'download_file_content'
    assert 'something strange happened' in str(error)


# -- references --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    'reference',
    [
        'https://drive.google.com/file/d/1AbC_dEf-23456789012345/view',
        'https://drive.google.com/file/d/1AbC_dEf-23456789012345/view?usp=sharing',
        'https://drive.google.com/open?id=1AbC_dEf-23456789012345',
        'https://docs.google.com/spreadsheets/d/1AbC_dEf-23456789012345/edit',
        '1AbC_dEf-23456789012345',
    ],
)
def test_file_ids_are_parsed_from_every_shape_of_reference(reference):
    assert parse_file_id(reference) == '1AbC_dEf-23456789012345'


@pytest.mark.parametrize('reference', ['', '   ', 'Q1 metrics', 'not/a/url', 'short'])
def test_unparseable_references_are_rejected(reference):
    with pytest.raises(InvalidWorkbookReference):
        parse_file_id(reference)


def test_a_cell_reference_opens_the_file_and_names_the_cell():
    assert cell_reference('abc', 'Users', 'B2') == (
        'https://drive.google.com/file/d/abc/view#Users!B2'
    )


def test_file_url_is_a_drive_link():
    assert file_url('abc') == 'https://drive.google.com/file/d/abc/view'


# -- payload shapes ----------------------------------------------------------------------------
def test_metadata_is_read_from_the_documented_field_names():
    info = _file_info(
        {
            'id': 'abc',
            'name': 'Q1.xlsx',
            'mimeType': XLSX_MIME,
            'modifiedTime': '2026-01-01T00:00:00Z',
            'parents': ['folder1'],
        },
        fallback_id='',
    )

    assert info == FileInfo('abc', 'Q1.xlsx', XLSX_MIME, '2026-01-01T00:00:00Z', ('folder1',))
    assert info.is_xlsx


def test_metadata_is_read_through_a_file_wrapper():
    """create_file has been seen to answer with the File object nested under `file`."""
    info = _file_info({'file': {'id': 'abc', 'title': 'Q1.xlsx'}}, fallback_id='')

    assert (info.file_id, info.name) == ('abc', 'Q1.xlsx')


def test_a_response_without_an_id_falls_back_to_the_requested_one():
    assert _file_info({'name': 'Q1.xlsx'}, fallback_id='asked-for').file_id == 'asked-for'


def test_an_xlsx_is_recognised_by_name_when_the_mime_type_is_vague():
    assert _file_info({'id': 'a', 'name': 'Q1.XLSX'}, fallback_id='').is_xlsx
    assert not _file_info({'id': 'a', 'name': 'Q1.csv'}, fallback_id='').is_xlsx


# -- the client's own error paths ---------------------------------------------------------------
class StubClient(DriveMcpClient):
    """A client whose transport is replaced by a canned tool result."""

    def __init__(self, payload):
        super().__init__()
        self._payload = payload

    async def _call(self, tool, arguments):
        return self._payload


def test_a_download_with_no_content_is_an_error():
    with pytest.raises(DriveMcpError, match='no content returned'):
        asyncio.run(StubClient({}).download('abc'))


def test_a_download_of_invalid_base64_is_an_error():
    with pytest.raises(DriveMcpError, match='not valid base64'):
        asyncio.run(StubClient({'base64Content': '!!!not base64!!!'}).download('abc'))


def test_a_download_decodes_base64_content():
    assert asyncio.run(StubClient({'base64Content': 'aGVsbG8='}).download('abc')) == b'hello'


def test_a_download_accepts_the_deprecated_content_field():
    assert asyncio.run(StubClient({'content': 'aGVsbG8='}).download('abc')) == b'hello'
