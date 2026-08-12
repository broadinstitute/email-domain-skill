"""Test helpers.

Workbooks are real `.xlsx` files built with openpyxl, so the tests exercise the same reader and
writer that production uses. The only thing faked is the *other* MCP server: `Recorder` stands in
for the Excel server that performs the redaction writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from email_domain_scrubber import xlsx


def write_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> Path:
    """Build a real `.xlsx` on disk. The fixture workbook for most tests."""
    xlsx.create(path, sheets)
    return path


@dataclass
class Recorder:
    """Captures what the Excel MCP server would have been asked to do.

    Redaction writes are performed by an external server in production. Tests apply the same
    blocks with openpyxl, which is what that server does internally, and keep the calls so a
    test can assert on their shape.
    """

    calls: list[tuple[str, str, str, list[list[str]]]] = field(default_factory=list)

    def apply(self, path: Path, blocks: list) -> None:
        """Stand in for `write_data_to_excel`, one call per block."""
        from openpyxl import load_workbook
        from openpyxl.utils.cell import coordinate_to_tuple

        for block in blocks:
            self.calls.append((str(path), block.sheet, block.start_cell, block.values))
            row, column = coordinate_to_tuple(block.start_cell)
            workbook = load_workbook(path)
            try:
                worksheet = workbook[block.sheet]
                for row_offset, values in enumerate(block.values):
                    for column_offset, value in enumerate(values):
                        worksheet.cell(
                            row=row + row_offset, column=column + column_offset, value=value
                        )
                workbook.save(path)
            finally:
                workbook.close()
