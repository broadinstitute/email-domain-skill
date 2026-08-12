"""Email Domain Scrubber — privacy risk analysis and anonymization of email domains.

The MCP server lives in `server`. `drive` reaches Google Drive through Google's Drive MCP
connector, `staging` and `xlsx` handle local Excel files, and `scan`, `workbook`, `redact`,
`domains` and `anonymize` hold the logic the server exposes — all independently importable and
testable.
"""

__all__ = ['__version__']

__version__ = '0.1.0'
