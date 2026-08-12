"""Entry point.

With no arguments this runs the MCP server over stdio, which is how the `.mcp.json`
registration launches it. `check-auth` is the one subcommand meant for a human: it proves the
Drive MCP connector is reachable with the configured credentials before an MCP client is in the
picture, and names the fix if it is not.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .errors import ScrubberError


async def _check() -> tuple[str, list[str], int]:
    """Prove the connector both answers and *works*.

    Listing tools is not enough: the endpoint answers `tools/list` even when the Drive MCP API is
    disabled for the OAuth client's project, so a check that stopped there would report a false
    green and leave the real failure for the first scan. A harmless read is the only proof.
    """
    from .auth import credential_source
    from .drive import XLSX_MIME, DriveMcpClient

    client = DriveMcpClient()
    tools = await client.list_tools()
    found = await client.search(f"mimeType = '{XLSX_MIME}'", page_size=1)
    return credential_source(), tools, len(found)


def check_auth() -> int:
    """Report which credentials are in use and whether the Drive MCP connector accepts them."""
    from .auth import login_hint
    from .staging import analysis_workbook_path, workdir

    try:
        source, tools, found = asyncio.run(_check())
    except ScrubberError as exc:
        print(f'Google Drive MCP access is NOT working.\n\n{exc}', file=sys.stderr)
        return 1

    print(f'Credential source:  {source}')
    print(f'Drive MCP endpoint: OK, {len(tools)} tools: {", ".join(tools)}')
    print(f'Test search:        OK, {found} .xlsx file(s) visible')
    print(f'Work directory:     {workdir()}')
    print(f'Analysis workbook:  {analysis_workbook_path()}')
    print(
        '\nThe server acts as the signed-in user, so it can only reach files that account can '
        f'already open.\nTo sign in as someone else: {login_hint()}'
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='email-domain-scrubber',
        description='MCP server for email domain risk analysis in usage metric reports.',
    )
    subcommands = parser.add_subparsers(dest='command')
    subcommands.add_parser('serve', help='Run the MCP server over stdio (the default).')
    subcommands.add_parser(
        'check-auth', help='Verify the Google Drive MCP connector is reachable and authorized.'
    )
    arguments = parser.parse_args(argv)

    if arguments.command == 'check-auth':
        return check_auth()

    from .server import main as serve

    serve()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
