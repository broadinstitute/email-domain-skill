"""Entry point.

With no arguments this runs the MCP server over stdio, which is how the `.mcp.json`
registration launches it. `check-auth` is the one subcommand meant for a human: it proves the
Google credentials work before an MCP client is in the picture, and names the login to run if
they do not.
"""

from __future__ import annotations

import argparse
import sys

from .errors import ScrubberError


def check_auth() -> int:
    """Report which credentials are in use and whether Google accepts them."""
    from .auth import login_hint, verify_access

    try:
        check = verify_access()
    except ScrubberError as exc:
        print(f'Google access is NOT working.\n\n{exc}', file=sys.stderr)
        return 1

    print(f'Credential source: {check.source}')
    print(f'Signed in as:      {check.account}')
    print('Drive API:         OK')
    print('Sheets API:        OK')
    print(
        '\nThe server acts as this user, so it can only reach workbooks that account can '
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
        'check-auth', help='Verify Google Sheets and Drive access and report who is signed in.'
    )
    arguments = parser.parse_args(argv)

    if arguments.command == 'check-auth':
        return check_auth()

    from .server import main as serve

    serve()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
