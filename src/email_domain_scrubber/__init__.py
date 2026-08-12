"""Email Domain Scrubber — privacy risk analysis and anonymization of email domains.

The MCP server lives in `server`. `local`, `staging` and `xlsx` handle files on disk, and `scan`,
`workbook`, `redact`, `domains` and `anonymize` hold the logic the server exposes — all
independently importable and testable.
"""

__all__ = ['__version__']

__version__ = '0.1.0'
