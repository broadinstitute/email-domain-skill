"""The domain analysis workbook — the shared memory of the skill and this server.

Sheets, per AGENTS.md:

* ``Workbooks``        — every metrics workbook that has been scanned
* ``DomainReferences`` — one row per cell a domain was found in (many rows per domain)
* ``DomainAnalysis``   — exactly one row per unique domain, holding the skill's verdict
* ``Redactions``       — one row per cell rewritten, with what it held before and after,
  satisfying the requirement to keep separate records of anonymizations (the mapping and the
  source locations live in the two sheets above; this records what was produced, where, and when)

The skill owns ``Risk`` and ``Explanation``. This module owns ``AnonymizedDomain``: it mints a
token when analysis says a domain needs one and never changes a token that already exists, so
tokens stay stable across quarters.

``DomainAnalysis`` is also the redaction plan — ``AnonymizedDomain`` is exactly the set of
substitutions redaction will make, and nothing else decides them. That is what lets the user edit
the sheet between analysis and redaction: clear a token and the domain survives, and
:meth:`AnalysisWorkbook.reconcile_aliases` mints one for a row hand-edited up to High so an edit
that should cause a redaction actually does.

It is a plain local `.xlsx`, so the whole sheet is loaded, mutated, and saved on every write —
no row-number tracking, no cache to invalidate. Being a plain file, it can also live in a git
repo alongside the reports it describes.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import anonymize, xlsx
from .errors import InvalidRisk, SchemaMismatch

WORKBOOKS = 'Workbooks'
DOMAIN_REFERENCES = 'DomainReferences'
DOMAIN_ANALYSIS = 'DomainAnalysis'
REDACTIONS = 'Redactions'

HEADERS: dict[str, list[str]] = {
    WORKBOOKS: ['Path', 'Title'],
    DOMAIN_REFERENCES: ['DateExtracted', 'Reference', 'Domain'],
    DOMAIN_ANALYSIS: ['Domain', 'Risk', 'Explanation', 'AnonymizedDomain'],
    REDACTIONS: [
        'DateRedacted',
        'SourcePath',
        'RedactedPath',
        'Reference',
        'Domain',
        'AnonymizedDomain',
        'Before',
        'After',
    ],
}

SHEET_ORDER = (WORKBOOKS, DOMAIN_REFERENCES, DOMAIN_ANALYSIS, REDACTIONS)

RISKS = ('High', 'Medium', 'Low')
_RISK_BY_LOWER = {risk.lower(): risk for risk in RISKS}


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

    @property
    def analyzed(self) -> bool:
        return bool(self.risk)


@dataclass(frozen=True)
class RedactionRecord:
    source_path: str
    redacted_path: str
    reference: str
    domain: str
    anonymized_domain: str
    before: str = ''
    after: str = ''
    date_redacted: str = field(default_factory=today)


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) and row[index] is not None else ''


class AnalysisWorkbook:
    """Read/write access to one local analysis workbook."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # -- construction --------------------------------------------------------------------
    @classmethod
    def open(cls, path: Path) -> AnalysisWorkbook:
        """Open the workbook, creating it if it does not exist yet.

        Creating on demand removes a setup step from the skill's workflow.
        """
        workbook = cls(path)
        if not path.exists():
            xlsx.create(path, {name: [list(HEADERS[name])] for name in SHEET_ORDER})
        workbook.ensure_schema()
        return workbook

    @property
    def location(self) -> str:
        """How the record is referred to in tool output."""
        return str(self.path)

    @property
    def sheet_titles(self) -> list[str]:
        return xlsx.sheet_titles(self.path)

    def ensure_schema(self) -> None:
        """Add any missing sheet or header row, and reject headers that disagree."""
        present = set(self.sheet_titles)
        writes: dict[str, list[list[str]]] = {}
        for name in SHEET_ORDER:
            expected = HEADERS[name]
            rows = self._rows(name) if name in present else []
            actual = [cell.strip() for cell in (rows[0] if rows else [])]
            if not actual:
                writes[name] = [list(expected), *rows[1:]]
            elif actual[: len(expected)] != expected:
                raise SchemaMismatch(
                    f'Sheet {name!r} in {self.path} has headers {actual!r}, expected '
                    f'{expected!r}. Point at a different workbook or fix the header row.'
                )
        if writes:
            xlsx.rewrite(self.path, writes)

    # -- reads ---------------------------------------------------------------------------
    def _rows(self, sheet_title: str) -> list[list[str]]:
        return xlsx.read_rows(self.path, sheet_title)

    def _data_rows(self, sheet_title: str) -> list[list[str]]:
        """Rows below the header."""
        rows = self._rows(sheet_title)
        return rows[1:] if rows else []

    def analysis_rows(self) -> list[AnalysisRow]:
        return [
            AnalysisRow(
                domain=_cell(row, 0).lower(),
                risk=_cell(row, 1),
                explanation=_cell(row, 2),
                anonymized_domain=_cell(row, 3),
            )
            for row in self._data_rows(DOMAIN_ANALYSIS)
            if _cell(row, 0)
        ]

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
        for _, domain in self._reference_keys():
            counts[domain] = counts.get(domain, 0) + 1
        return counts

    def sample_references(self, limit: int = 3) -> dict[str, list[str]]:
        """Domain -> its first few cell locators, in the order they were recorded."""
        samples: dict[str, list[str]] = {}
        for reference, domain in self._reference_keys():
            bucket = samples.setdefault(domain, [])
            if len(bucket) < limit:
                bucket.append(reference)
        return samples

    def _reference_keys(self) -> list[tuple[str, str]]:
        """`(Reference, Domain)` in sheet order.

        Ordered, not a set: these back the example links shown to the analyst, and set iteration
        order varies between runs, which would make the same domain's examples change each call.
        """
        return [
            (_cell(row, 1), _cell(row, 2).lower())
            for row in self._data_rows(DOMAIN_REFERENCES)
            if _cell(row, 2)
        ]

    def scanned_workbooks(self) -> dict[str, str]:
        """Workbook path -> recorded title."""
        return {_cell(row, 0): _cell(row, 1) for row in self._data_rows(WORKBOOKS) if _cell(row, 0)}

    # -- writes --------------------------------------------------------------------------
    def _write(self, sheet_title: str, rows: list[list[str]]) -> None:
        xlsx.rewrite(self.path, {sheet_title: [list(HEADERS[sheet_title]), *rows]})

    def _append(self, sheet_title: str, rows: list[list[str]]) -> None:
        if rows:
            self._write(sheet_title, [*self._data_rows(sheet_title), *rows])

    def record_workbook(self, path: str, title: str) -> None:
        """Upsert a scanned workbook by path, keeping its title current."""
        existing = self.scanned_workbooks()
        existing[path] = title
        self._write(WORKBOOKS, [[key, value] for key, value in existing.items()])

    def record_references(self, references: list[DomainReference]) -> list[DomainReference]:
        """Append references not already recorded. Returns the ones appended."""
        seen = set(self._reference_keys())
        fresh: list[DomainReference] = []
        for reference in references:
            key = (reference.reference, reference.domain.lower())
            if key not in seen:
                seen.add(key)
                fresh.append(reference)
        self._append(
            DOMAIN_REFERENCES, [[ref.date_extracted, ref.reference, ref.domain] for ref in fresh]
        )
        return fresh

    def ensure_analysis_rows(self, domains: list[str]) -> list[str]:
        """Create a blank DomainAnalysis row for each unseen domain. Returns those added."""
        known = self.analysis_by_domain()
        added = [
            domain for domain in dict.fromkeys(d.lower() for d in domains) if domain not in known
        ]
        self._append(DOMAIN_ANALYSIS, [[domain, '', '', ''] for domain in added])
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

        stored: list[AnalysisRow] = []
        for raw_domain, raw_risk, explanation, anonymize_flag in entries:
            domain = raw_domain.strip().lower()
            risk = normalize_risk(raw_risk)
            record = by_domain.get(domain)
            if record is None:
                record = AnalysisRow(domain=domain)
                by_domain[domain] = record
                rows.append(record)

            record.risk = risk
            record.explanation = (explanation or '').strip()

            wants_token = risk == 'High' if anonymize_flag is None else anonymize_flag
            if wants_token and not record.anonymized_domain:
                token = anonymize.generate_token(taken, rng)
                taken.add(token)
                record.anonymized_domain = token
            stored.append(record)

        self._write(DOMAIN_ANALYSIS, [self._analysis_values(row) for row in rows])
        return stored

    @staticmethod
    def _analysis_values(row: AnalysisRow) -> list[str]:
        return [row.domain, row.risk, row.explanation, row.anonymized_domain]

    def reconcile_aliases(
        self, domains: list[str] | None = None, rng: random.Random | None = None
    ) -> dict[str, str]:
        """Make the sheet's own contents consistent, and return the aliases that had to be minted.

        Called at redaction time, when the sheet may have been hand-edited since the skill wrote
        it. Two repairs, both confined to `domains` when given:

        * A row whose Risk reads High but has no alias gets one. Without this, editing a Risk up
          to High would look like it took effect and silently change nothing, since redaction
          reads the alias column and nothing else.
        * A Risk typed in another casing is written back canonicalized.

        So the two columns together say: **an alias means the domain is replaced, and High means
        it gets an alias.** Sparing a domain the skill marked High therefore takes both edits —
        clear the alias *and* lower the Risk. Clearing the alias alone is undone here, on the
        grounds that a row reading `High` with nothing to replace it with is more likely a
        half-finished edit than a decision.

        An alias is never removed and never changed: aliases are stable across quarters, and
        re-minting one would break a mapping already published in an earlier report.
        """
        rows = self.analysis_rows()
        scope = {domain.lower() for domain in domains} if domains is not None else None
        taken = {row.anonymized_domain for row in rows if row.anonymized_domain}

        minted: dict[str, str] = {}
        changed = False
        for row in rows:
            if not row.analyzed or (scope is not None and row.domain not in scope):
                continue
            try:
                risk = normalize_risk(row.risk)
            except ValueError as error:
                raise InvalidRisk(row.domain, row.risk) from error
            if risk != row.risk:
                row.risk = risk
                changed = True
            if risk == 'High' and not row.anonymized_domain:
                token = anonymize.generate_token(taken, rng)
                taken.add(token)
                row.anonymized_domain = token
                minted[row.domain] = token
                changed = True

        if changed:
            self._write(DOMAIN_ANALYSIS, [self._analysis_values(row) for row in rows])
        return minted

    def record_redactions(self, records: list[RedactionRecord]) -> None:
        self._append(
            REDACTIONS,
            [
                [
                    record.date_redacted,
                    record.source_path,
                    record.redacted_path,
                    record.reference,
                    record.domain,
                    record.anonymized_domain,
                    record.before,
                    record.after,
                ]
                for record in records
            ],
        )
