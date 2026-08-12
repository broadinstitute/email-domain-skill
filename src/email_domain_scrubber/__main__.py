"""Entry point.

With no arguments this runs the MCP server over stdio, which is how the `.mcp.json`
registration launches it.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='email-domain-scrubber',
        description='MCP server for email domain risk analysis in usage metric reports.',
    )
    subcommands = parser.add_subparsers(dest='command')
    subcommands.add_parser('serve', help='Run the MCP server over stdio (the default).')
    parser.parse_args(argv)

    from .server import main as serve

    serve()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
