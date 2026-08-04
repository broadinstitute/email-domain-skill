"""The domain analysis workbook — the shared memory of the skill and this server.

Sheets, per AGENTS.md:

* ``Workbooks``        — every metrics workbook that has been scanned
* ``DomainReferences`` — one row per cell a domain was found in (many rows per domain)
* ``DomainAnalysis``   — exactly one row per unique domain, holding the skill's verdict
* ``Redactions``       — one row per cell actually rewritten, satisfying the requirement to keep
  separate records of anonymizations (the mapping and the source locations live in the two
  sheets above; this records what was published, where, and when)

The skill owns ``Risk`` and ``Explanation``. This module owns ``AnonymizedDomain``: it mints a
token when analysis says a domain needs one and never changes a token that already exists, so
tokens stay stable across quarters.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import anonymize
from .errors import SchemaMismatch
from .sheets import SheetsBackend, SpreadsheetInfo, quote_sheet_title, spreadsheet_url

WORKBOOKS = 'Workbooks'
DOMAIN_REFERENCES = 'DomainReferences'
DOMAIN_ANALYSIS = 'DomainAnalysis'
REDACTIONS = 'Redactions'

HEADERS: dict[str, list[str]] = {
    WORKBOOKS: ['URL', 'Title'],
    DOMAIN_REFERENCES: ['DateExtracted', 'Reference', 'Domain'],
    DOMAIN_ANALYSIS: ['Domain', 'Risk', 'Explanation', 'AnonymizedDomain'],
    REDACTIONS: [
        'DateRedacted',
        'SourceURL',
        'RedactedURL',
        'Reference',
        'Domain',
        'AnonymizedDomain',
    ],
}

SHEET_ORDER = (WORKBOOKS, DOMAIN_REFERENCES, DOMAIN_ANALYSIS, REDACTIONS)

RISKS = ('High', 'Medium', 'Low')
_RISK_BY_LOWER = {risk.lower(): risk for risk in RISKS}

DEFAULT_TITLE = 'Email Domain Analysis'


def normalize_risk(value: str) -> str:
    """Canonicalize a risk label, rejecting anything outside the taxonomy."""
    try:
        return _RISK_BY_LOWER[(value or '').strip().lower()]
    except KeyError:
        raise ValueError(
            f'{value!r} is not a valid Risk. Expected one of: {", ".join(RISKS)}'
        ) from None


def today() -> str:
    return datetime.now(UTC).date().isoformat()


@dataclass(frozen=True)
class DomainReference:
    """A single cell that a domain was found in."""

    reference: str
    domain: str
    date_extracted: str = field(default_factory=today)


@dataclass
class AnalysisRow:
    domain: str
    risk: str = ''
    explanation: str = ''
    anonymized_domain: str = ''
    row_number: int = 0  # 1-based sheet row; 0 until written

    @property
    def analyzed(self) -> bool:
        return bool(self.risk)


@dataclass(frozen=True)
class RedactionRecord:
    source_url: str
    redacted_url: str
    reference: str
    domain: str
    anonymized_domain: str
    date_redacted: str = field(default_factory=today)


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) and row[index] is not None else ''


class AnalysisWorkbook:
    """Read/write access to one analysis workbook, with the sheet contents cached in memory."""

    def __init__(self, backend: SheetsBackend, info: SpreadsheetInfo) -> None:
        self._backend = backend
        self._info = info
        self._analysis: list[AnalysisRow] | None = None
        self._reference_keys: list[tuple[str, str]] | None = None
        self._workbook_urls: dict[str, int] | None = None

    # -- construction --------------------------------------------------------------------
    @classmethod
    def open(cls, backend: SheetsBackend, spreadsheet_id: str) -> AnalysisWorkbook:
        workbook = cls(backend, backend.get_spreadsheet(spreadsheet_id))
        workbook.ensure_schema()
        return workbook

    @classmethod
    def create(
        cls, backend: SheetsBackend, title: str = DEFAULT_TITLE, folder_id: str | None = None
    ) -> AnalysisWorkbook:
        """Create the workbook, optionally moving it into a Drive folder or shared drive.

        The Sheets API can only create in the user's My Drive, so placing it elsewhere is a
        second step.
        """
        info = backend.create_spreadsheet(title, list(SHEET_ORDER))
        if folder_id:
            backend.move_to_folder(info.spreadsheet_id, folder_id)
        workbook = cls(backend, info)
        workbook.ensure_schema()
        return workbook

    @property
    def spreadsheet_id(self) -> str:
        return self._info.spreadsheet_id

    @property
    def title(self) -> str:
        return self._info.title

    @property
    def url(self) -> str:
        return spreadsheet_url(self.spreadsheet_id)

    @property
    def sheet_titles(self) -> list[str]:
        return [sheet.title for sheet in self._info.sheets]

    def ensure_schema(self) -> None:
        """Create any missing sheet, add missing headers, and reject mismatched ones."""
        missing = [name for name in SHEET_ORDER if self._info.sheet(name) is None]
        if missing:
            self._info = self._backend.add_sheets(self.spreadsheet_id, missing)

        blocks = {
            block.sheet_title: block.values
            for block in self._backend.read_sheets(self.spreadsheet_id, list(SHEET_ORDER))
        }
        header_writes: dict[str, list[list[str]]] = {}
        for name in SHEET_ORDER:
            expected = HEADERS[name]
            rows = blocks.get(name) or []
            actual = [cell.strip() for cell in (rows[0] if rows else [])]
            if not actual:
                header_writes[f'{quote_sheet_title(name)}!A1'] = [expected]
            elif actual[: len(expected)] != expected:
                raise SchemaMismatch(
                    f'Sheet {name!r} in {self.url} has headers {actual!r}, expected '
                    f'{expected!r}. Point at a different workbook or fix the header row.'
                )
        if header_writes:
            self._backend.write_ranges(self.spreadsheet_id, header_writes)
        self._invalidate()

    def _invalidate(self) -> None:
        self._analysis = None
        self._reference_keys = None
        self._workbook_urls = None

    # -- reads ---------------------------------------------------------------------------
    def _read(self, sheet_title: str) -> list[list[str]]:
        blocks = self._backend.read_sheets(self.spreadsheet_id, [sheet_title])
        rows = blocks[0].values if blocks else []
        return rows[1:] if rows else []

    def analysis_rows(self) -> list[AnalysisRow]:
        if self._analysis is None:
            self._analysis = [
                AnalysisRow(
                    domain=_cell(row, 0).lower(),
                    risk=_cell(row, 1),
                    explanation=_cell(row, 2),
                    anonymized_domain=_cell(row, 3),
                    row_number=offset + 2,
                )
                for offset, row in enumerate(self._read(DOMAIN_ANALYSIS))
                if _cell(row, 0)
            ]
        return self._analysis

    def analysis_by_domain(self) -> dict[str, AnalysisRow]:
        """Latest row wins if the sheet was hand-edited to contain duplicates."""
        return {row.domain: row for row in self.analysis_rows()}

    def pending_domains(self) -> list[AnalysisRow]:
        return [row for row in self.analysis_rows() if not row.analyzed]

    def anonymized_mapping(self) -> dict[str, str]:
        """Domain -> token, for domains that analysis decided to anonymize."""
        return {
            row.domain: row.anonymized_domain
            for row in self.analysis_rows()
            if row.anonymized_domain
        }

    def reference_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, domain in self._reference_keys_ordered():
            counts[domain] = counts.get(domain, 0) + 1
        return counts

    def sample_references(self, limit: int = 3) -> dict[str, list[str]]:
        """Domain -> its first few cell links, in the order they were recorded."""
        samples: dict[str, list[str]] = {}
        for reference, domain in self._reference_keys_ordered():
            bucket = samples.setdefault(domain, [])
            if len(bucket) < limit:
                bucket.append(reference)
        return samples

    def _reference_keys_ordered(self) -> list[tuple[str, str]]:
        """`(Reference, Domain)` in sheet order.

        Ordered, not a set: these back the example links shown to the analyst, and set iteration
        order varies between runs, which would make the same domain's examples change each call.
        """
        if self._reference_keys is None:
            self._reference_keys = [
                (_cell(row, 1), _cell(row, 2).lower())
                for row in self._read(DOMAIN_REFERENCES)
                if _cell(row, 2)
            ]
        return self._reference_keys

    def scanned_workbooks(self) -> dict[str, int]:
        """Workbook URL -> 1-based row number in the Workbooks sheet."""
        if self._workbook_urls is None:
            self._workbook_urls = {
                _cell(row, 0): offset + 2
                for offset, row in enumerate(self._read(WORKBOOKS))
                if _cell(row, 0)
            }
        return self._workbook_urls

    # -- writes --------------------------------------------------------------------------
    def record_workbook(self, url: str, title: str) -> None:
        """Upsert a scanned workbook by URL, keeping its title current."""
        existing = self.scanned_workbooks()
        if url in existing:
            row_number = existing[url]
            self._backend.write_ranges(
                self.spreadsheet_id,
                {f'{quote_sheet_title(WORKBOOKS)}!A{row_number}': [[url, title]]},
            )
        else:
            self._backend.append_rows(self.spreadsheet_id, WORKBOOKS, [[url, title]])
        self._workbook_urls = None

    def record_references(self, references: list[DomainReference]) -> list[DomainReference]:
        """Append references not already recorded. Returns the ones appended."""
        recorded = self._reference_keys_ordered()
        seen = set(recorded)
        fresh: list[DomainReference] = []
        for reference in references:
            key = (reference.reference, reference.domain.lower())
            if key not in seen:
                seen.add(key)
                recorded.append(key)
                fresh.append(reference)
        if fresh:
            self._backend.append_rows(
                self.spreadsheet_id,
                DOMAIN_REFERENCES,
                [[ref.date_extracted, ref.reference, ref.domain] for ref in fresh],
            )
        return fresh

    def ensure_analysis_rows(self, domains: list[str]) -> list[str]:
        """Create a blank DomainAnalysis row for each unseen domain. Returns those added."""
        known = self.analysis_by_domain()
        rows = self.analysis_rows()
        next_row = max((row.row_number for row in rows), default=1) + 1
        added: list[str] = []
        for domain in dict.fromkeys(d.lower() for d in domains):
            if domain in known:
                continue
            record = AnalysisRow(domain=domain, row_number=next_row)
            known[domain] = record
            rows.append(record)
            added.append(domain)
            next_row += 1
        if added:
            self._backend.append_rows(
                self.spreadsheet_id, DOMAIN_ANALYSIS, [[domain, '', '', ''] for domain in added]
            )
        return added

    def store_analysis(
        self, entries: list[tuple[str, str, str, bool | None]], rng: random.Random | None = None
    ) -> list[AnalysisRow]:
        """Write Risk/Explanation for each `(domain, risk, explanation, anonymize)` entry.

        `anonymize=None` means "anonymize iff the risk is High". A token is minted only when one
        is needed and none exists; an existing token is never replaced or removed, so a domain
        keeps the same alias even if a later analysis downgrades its risk.
        """
        rows = self.analysis_rows()
        by_domain = {row.domain: row for row in rows}
        taken = {row.anonymized_domain for row in rows if row.anonymized_domain}
        next_row = max((row.row_number for row in rows), default=1) + 1

        updated: list[AnalysisRow] = []
        appended: list[AnalysisRow] = []
        for raw_domain, raw_risk, explanation, anonymize_flag in entries:
            domain = raw_domain.strip().lower()
            risk = normalize_risk(raw_risk)
            record = by_domain.get(domain)
            if record is None:
                record = AnalysisRow(domain=domain, row_number=next_row)
                by_domain[domain] = record
                rows.append(record)
                appended.append(record)
                next_row += 1
            else:
                updated.append(record)

            record.risk = risk
            record.explanation = (explanation or '').strip()

            wants_token = risk == 'High' if anonymize_flag is None else anonymize_flag
            if wants_token and not record.anonymized_domain:
                token = anonymize.generate_token(taken, rng)
                taken.add(token)
                record.anonymized_domain = token

        if appended:
            self._backend.append_rows(
                self.spreadsheet_id, DOMAIN_ANALYSIS, [self._analysis_values(r) for r in appended]
            )
        if updated:
            self._backend.write_ranges(
                self.spreadsheet_id,
                {
                    f'{quote_sheet_title(DOMAIN_ANALYSIS)}!A{r.row_number}': [
                        self._analysis_values(r)
                    ]
                    for r in updated
                },
            )
        return updated + appended

    @staticmethod
    def _analysis_values(row: AnalysisRow) -> list[str]:
        return [row.domain, row.risk, row.explanation, row.anonymized_domain]

    def record_redactions(self, records: list[RedactionRecord]) -> None:
        if not records:
            return
        self._backend.append_rows(
            self.spreadsheet_id,
            REDACTIONS,
            [
                [
                    record.date_redacted,
                    record.source_url,
                    record.redacted_url,
                    record.reference,
                    record.domain,
                    record.anonymized_domain,
                ]
                for record in records
            ],
        )
