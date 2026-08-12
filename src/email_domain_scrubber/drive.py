"""Google Drive access, entirely through Google's Drive MCP connector.

The connector at `https://drivemcp.googleapis.com/mcp/v1` replaces what used to be a
hand-written wrapper over the Drive v3 and Sheets v4 REST APIs. This module is an MCP *client*
of it, which keeps file bytes out of the model's context: `download_file_content` hands back
base64, and a 1 MB workbook would be some 350k tokens if it travelled through the conversation.

The connector's surface is deliberately small and read-mostly — `search_files`,
`get_file_metadata`, `get_file_permissions`, `list_recent_files`, `read_file_content`,
`download_file_content`, `create_file`, `copy_file`. Notably there is **no update, delete, or
move**, which is why redaction always creates a new file and why the analysis workbook is kept
locally rather than in Drive.

Behaviours confirmed against the live endpoint, none of which the documentation states:

* Errors arrive as HTTP 403 with a *valid* JSON-RPC body, so the body must be read on non-2xx
  rather than treated as a transport failure.
* A failed tool call is reported as ``is_error`` with the message in text content, not as a
  JSON-RPC error — and sometimes as bare prose with no error flag at all, so any non-JSON
  response has to be treated as a failure message.
* File objects use Drive's *v2*-style field names, not the v3 names the REST API uses: a file's
  name is ``title`` and its parent is a single ``parentId``, not a ``parents`` array.
* ``download_file_content`` returns the base64 under ``content``, while ``create_file`` *accepts*
  it as ``base64Content``. The two are not symmetric.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from .auth import TokenSource
from .errors import (
    AccessDenied,
    DriveMcpApiDisabled,
    DriveMcpError,
    InvalidWorkbookReference,
    MissingScopes,
    WorkbookNotFound,
)

ENDPOINT = 'https://drivemcp.googleapis.com/mcp/v1'

#: The only workbook format this server handles. Google Sheets and CSV are out of scope.
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

_DRIVE_URL_ID = re.compile(r'(?:/file/d/|/spreadsheets/d/|[?&]id=)([A-Za-z0-9_-]+)')
_BARE_ID = re.compile(r'^[A-Za-z0-9_-]{20,}$')


def parse_file_id(url_or_id: str) -> str:
    """Extract a Drive file id from a Drive URL, or accept a bare id."""
    value = (url_or_id or '').strip()
    if not value:
        raise InvalidWorkbookReference('No workbook URL or id was provided.')
    match = _DRIVE_URL_ID.search(value)
    if match:
        return match.group(1)
    if _BARE_ID.match(value):
        return value
    raise InvalidWorkbookReference(
        f'{value!r} is not a Google Drive URL or file id. Expected something like '
        'https://drive.google.com/file/d/<id>/view'
    )


def file_url(file_id: str) -> str:
    return f'https://drive.google.com/file/d/{file_id}/view'


def cell_reference(file_id_or_url: str, sheet_title: str, a1: str) -> str:
    """A locator for one cell of a workbook.

    A Google Sheet could be deep-linked to a selected cell with `#gid=…&range=…`. An `.xlsx` has
    no such link, so the URL opens the file and the fragment names the cell for a human reading
    the audit trail.

    Accepts either a Drive file id or a full URL, so a local workbook's `file://` URL produces
    the same shape of locator as a Drive one.
    """
    base = file_id_or_url if '://' in file_id_or_url else file_url(file_id_or_url)
    return f'{base}#{sheet_title}!{a1}'


@dataclass(frozen=True)
class FileInfo:
    """A Drive file as the connector describes it.

    Field names are those the live connector actually returns, which differ from the Drive REST
    API's: the file's name arrives as ``title``, and its parent as a single ``parentId`` rather
    than a ``parents`` array.
    """

    file_id: str
    name: str
    mime_type: str
    modified_time: str = ''
    parent_id: str = ''

    @property
    def is_xlsx(self) -> bool:
        return self.mime_type == XLSX_MIME or self.name.lower().endswith('.xlsx')


class Drive(Protocol):
    """The Drive surface this package depends on — four operations, all via the connector."""

    async def get_metadata(self, file_id: str) -> FileInfo: ...

    async def download(self, file_id: str) -> bytes: ...

    async def create(
        self, name: str, content: bytes, *, mime_type: str = XLSX_MIME, parent_id: str | None = None
    ) -> FileInfo: ...

    async def search(self, query: str, *, page_size: int = 10) -> list[FileInfo]: ...


def _first_text(content: Any) -> str:
    """The text payload of an MCP tool result, whichever shape the SDK hands back."""
    for block in content or []:
        text = getattr(block, 'text', None)
        if text is None and isinstance(block, dict):
            text = block.get('text')
        if text:
            return str(text)
    return ''


def _classify(tool: str, detail: str) -> Exception:
    """Turn a connector failure into the most actionable error we can name."""
    lowered = detail.lower()
    if 'drive mcp api has not been used' in lowered or 'drivemcp.googleapis.com' in lowered:
        return DriveMcpApiDisabled(detail)
    if 'scope' in lowered and ('insufficient' in lowered or 'required' in lowered):
        return MissingScopes(detail)
    if 'not found' in lowered or 'notfound' in lowered or '404' in lowered:
        return WorkbookNotFound(
            f'Google could not find the file: {detail}. Check the id, and that the file is '
            'shared with the signed-in account.'
        )
    if 'permission' in lowered or 'forbidden' in lowered or 'denied' in lowered:
        return AccessDenied(
            f'Google denied access: {detail}. The server acts as the signed-in user, so make '
            'sure that account can open the file — and, to upload, the destination folder.'
        )
    return DriveMcpError(tool, detail)


class DriveMcpClient:
    """`Drive` backed by the live connector.

    A session is opened per call rather than held open. The connector identifies itself as a
    `StatelessServer` and returns no session id, so there is nothing to keep alive, and a
    long-lived MCP server that cached a session would have to handle its own reconnection.
    """

    def __init__(self, endpoint: str = ENDPOINT, tokens: TokenSource | None = None) -> None:
        self._endpoint = endpoint
        self._tokens = tokens or TokenSource()

    @asynccontextmanager
    async def _session(self, timeout: float) -> AsyncIterator[Any]:
        """A connected MCP client, authorized as the signed-in user."""
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        token = await self._tokens.token()
        async with httpx2.AsyncClient(
            timeout=timeout, headers={'Authorization': f'Bearer {token}'}
        ) as http:
            # Pass the transport unentered: Client enters it, and entering it here would hand
            # Client a plain stream tuple instead of the context manager it expects.
            transport = streamable_http_client(self._endpoint, http_client=http)
            async with Client(transport, raise_exceptions=True) as client:
                yield client

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._session(timeout=120) as client:
            result = await client.call_tool(tool, arguments)

        text = _first_text(result.content)
        # snake_case: `isError`/`structuredContent` are the wire aliases, and reading those off
        # the model silently yields None — so an error result would sail through as a success.
        if result.is_error:
            raise _classify(tool, text or 'no detail given')
        if isinstance(result.structured_content, dict):
            return result.structured_content
        # A successful call always answers with JSON. The connector has been seen to report a
        # refusal as plain prose *without* setting isError, so unparseable text is a failure
        # message, not a malformed success — classify it rather than complaining about the shape.
        payload = _parse_json(text, tool)
        if payload is None:
            raise _classify(tool, text or 'empty response')
        return payload

    async def get_metadata(self, file_id: str) -> FileInfo:
        payload = await self._call('get_file_metadata', {'fileId': file_id})
        return _file_info(payload, fallback_id=file_id)

    async def download(self, file_id: str) -> bytes:
        payload = await self._call('download_file_content', {'fileId': file_id})
        # `content` is what the connector returns, confirmed live. Note the asymmetry with
        # `create_file`, whose *input* field is `base64Content` and whose `content` is deprecated.
        encoded = _pick(payload, 'content', 'base64Content', 'data', 'fileContent')
        if not encoded:
            raise DriveMcpError('download_file_content', f'no content returned for {file_id}')
        try:
            return base64.b64decode(encoded, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise DriveMcpError(
                'download_file_content', f'content for {file_id} was not valid base64: {exc}'
            ) from exc

    async def create(
        self, name: str, content: bytes, *, mime_type: str = XLSX_MIME, parent_id: str | None = None
    ) -> FileInfo:
        arguments: dict[str, Any] = {
            'title': name,
            'base64Content': base64.b64encode(content).decode('ascii'),
            'contentMimeType': mime_type,
            # Without this an uploaded .xlsx is silently converted to a Google Sheet, and this
            # server's whole contract is that the published artefact is an Excel workbook.
            'disableConversionToGoogleType': True,
        }
        if parent_id:
            arguments['parentId'] = parent_id
        return _file_info(await self._call('create_file', arguments), fallback_id='')

    async def search(self, query: str, *, page_size: int = 10) -> list[FileInfo]:
        payload = await self._call('search_files', {'query': query, 'pageSize': page_size})
        files = payload.get('files') or payload.get('results') or []
        return [_file_info(entry, fallback_id='') for entry in files if isinstance(entry, dict)]

    async def list_tools(self) -> list[str]:
        """Tool names the connector advertises. Used by `check-auth` to prove reachability."""
        async with self._session(timeout=60) as client:
            listed = await client.list_tools()
        return sorted(tool.name for tool in listed.tools)


def _parse_json(text: str, tool: str) -> dict[str, Any] | None:
    """The response as a mapping, or `None` if it is not JSON at all."""
    import json

    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else {'value': parsed}


def _pick(payload: dict[str, Any], *keys: str) -> str:
    """First non-empty string among `keys`, at the top level or under a `file` wrapper."""
    for source in (payload, payload.get('file') or {}):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return ''


def _file_info(payload: dict[str, Any], *, fallback_id: str) -> FileInfo:
    """Read a File object. `title` and `parentId` are what the connector really sends."""
    return FileInfo(
        file_id=_pick(payload, 'id', 'fileId') or fallback_id,
        name=_pick(payload, 'title', 'name'),
        mime_type=_pick(payload, 'mimeType', 'contentMimeType'),
        modified_time=_pick(payload, 'modifiedTime', 'modifiedAt'),
        parent_id=_pick(payload, 'parentId', 'parent'),
    )
