# Email Domain Scrubber

A skill and an MCP server for handling email domain names in platform usage metric reports.

Quarterly usage metrics for a scientific research platform are full of user email domains. Most
are harmless (`broadinstitute.org`, `nih.gov`, `gmail.com`), but some name a person — a personal
lab domain, a single-researcher consultancy, a `username.github.io`. Those need to be anonymized
before the report is shared, and the decision needs an audit trail.

The split is deliberate:

- **The skill** does the judgement: researching each domain, assigning a risk level, explaining
  why, and getting your approval.
- **This MCP server** does everything deterministic and auditable: reading the workbook, finding
  the domains, recording them, minting aliases, planning the rewrite, and proving it happened.
- **The Excel MCP server** applies the rewrite to a copy of the workbook.

The server never decides that something is risky, and the skill never invents an alias or edits
a report. Everything both of them know lives in one local Excel file, the **domain analysis
workbook**.

Reports are local Excel `.xlsx` files. Everything runs on your machine: no report is uploaded
anywhere, and the file you point at is never modified. Google Sheets and CSV are out of scope —
convert them to `.xlsx` first (File > Download > Microsoft Excel).

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Claude Code
- Nothing else: no accounts and no credentials.

Neither server touches the network — your report stays on your machine. The skill's research step
does search the web, but for the domain names themselves, not for anything else in the workbook.

## Install

This repo is its own Claude Code plugin marketplace. Installing the plugin registers the skill and
both MCP servers for **every** project and session, not just this directory:

```bash
claude plugin marketplace add broadinstitute/email-domain-skill
claude plugin install email-domain-scrubber@broadinstitute-email-domain
```

That is the whole install. `uv` builds the server's environment from the lockfile the first time
it starts, so the first call may take a few seconds longer than the rest.

To work against a local clone instead, point the marketplace at the working tree. The plugin then
runs the code in place, so your edits take effect on the next `/reload-plugins`:

```bash
claude plugin marketplace add /path/to/email-domain-skill
claude plugin install email-domain-scrubber@broadinstitute-email-domain
```

Either way, `${CLAUDE_PLUGIN_ROOT}` resolves the path in `uv run --project`, so there is nothing to
hand-edit. It is `--project` rather than `--directory` on purpose: `--directory` would change the
server's working directory to the plugin, and a relative workbook path would then resolve against
the wrong place.

### Check the install

```bash
claude plugin details email-domain-scrubber   # expect: 1 skill, 2 MCP servers
claude mcp list                               # expect: both plugin:email-domain-scrubber:* connected
```

The two servers appear as `plugin:email-domain-scrubber:email-domain-scrubber` and
`plugin:email-domain-scrubber:excel`.
[`excel-mcp-server`](https://github.com/haris-musa/excel-mcp-server) needs no Microsoft Excel
installation. Leave `EXCEL_FILES_PATH` unset so it accepts the absolute paths this server hands it.

`.mcp.json` at the repo root registers the same two servers for work inside this directory without
the plugin. With the plugin also enabled here they load twice under different names — harmless, but
disable one if the duplicate tool lists get in the way.

### Optional settings

```bash
# Where the redacted copy is written and the analysis record is kept.
export EMAIL_DOMAIN_WORKDIR=~/.cache/email-domain-scrubber

# The analysis workbook, if you want it somewhere else (e.g. in a git repo).
export EMAIL_DOMAIN_ANALYSIS_WORKBOOK=~/metrics/email-domain-analysis.xlsx
```

Both default under `$HOME` rather than the current directory, so the analysis record is one durable
file no matter which project you invoke the skill from.

## Usage

Point Claude at an `.xlsx` on disk. A relative or absolute path both work, as does `~`:

> Analyze the email domains in `~/Downloads/Q1 Metrics.xlsx` and anonymize the risky ones.

The skill takes it from there:

1. **Scans** the workbook in place, recording every domain occurrence and the cell it came from.
2. **Researches** each domain it has not seen before, and assigns a risk level with a written
   justification.
3. **Presents a table** of `Domain | Risk | Explanation | Action` and waits for your approval. Say
   which verdicts you disagree with and it revises them.
4. **Shows you the changes** it intends to make — how many cells, with examples — and waits again.
5. **Writes the redacted copy** as `<name> (anonymized).xlsx` in the work directory, then re-reads
   it and refuses to finish if any domain that should have been replaced is still there.

You end up with the path to a redacted copy and an audit trail in the analysis workbook. Your
original is untouched, nothing is uploaded, and sharing the copy is yours to do.

Domains it has already analyzed in a previous quarter keep their existing verdict and alias, so a
follow-up report only asks you about what is new.

## MCP tools

| Tool | What it does |
| --- | --- |
| `scan_workbook` | Reads every cell of the `.xlsx`, records each domain occurrence with a locator, and opens a blank analysis row per unique domain. |
| `list_domains_for_analysis` | The work queue: domains with no verdict yet, plus how often and where each was seen. |
| `store_domain_analysis` | Writes approved `Risk`/`Explanation`, minting an alias for each domain to anonymize. |
| `plan_redaction` | Copies the workbook into the work directory and returns the `write_blocks` that anonymize it. Changes nothing. |
| `finish_redaction` | Verifies the copy was actually rewritten and records every change. |

Between the last two, the Excel MCP server applies each block with `write_data_to_excel`.

## Workflow

```text
scan_workbook ──▶ list_domains_for_analysis ──▶ (research, approve) ──▶ store_domain_analysis
                                                                                  │
       ┌──────────────────────────────────────────────────────────────────────────┘
       ▼
plan_redaction ──▶ excel: write_data_to_excel × N blocks ──▶ finish_redaction ──▶ redacted .xlsx
```

## Domain analysis workbook

One local `.xlsx`, four sheets. It is the memory shared by the skill and the server, and the
record you would show an auditor. Being a plain file, it can live in a git repo alongside the
reports it describes.

| Sheet | Columns | Contents |
| --- | --- | --- |
| `Workbooks` | `Path`, `Title` | Every metrics workbook scanned. |
| `DomainReferences` | `DateExtracted`, `Reference`, `Domain` | One row per cell a domain was found in. Many rows per domain. |
| `DomainAnalysis` | `Domain`, `Risk`, `Explanation`, `AnonymizedDomain` | Exactly one row per unique domain. |
| `Redactions` | `DateRedacted`, `SourcePath`, `RedactedPath`, `Reference`, `Domain`, `AnonymizedDomain` | One row per cell actually rewritten. |

`Redactions` is what satisfies "keep separate records of anonymizations": `DomainAnalysis` holds
the mapping and `DomainReferences` holds the source locations, but neither records what was
produced, where, and when.

`DomainAnalysis` example:

```csv
Domain,Risk,Explanation,AnonymizedDomain
stephenwolfram.com,High,Stephen Wolfram of Wolfram Research,anon3746
pluralistic.net,Medium,Daily link blog of Cory Doctorow,
broadinstitute.org,Low,Broad Institute,
```

## Design decisions

**Workbook bytes never enter the conversation.** The server reads the file from disk itself. A
megabyte of `.xlsx` routed through the model as base64 would cost roughly 350k tokens, twice, so
reading it inside the server means workbook size stops mattering.

**The source workbook is read in place and never modified.** There is nothing to download and
nothing to cache, and copying it first would only create a second file to keep straight.

**Redaction creates; it never edits in place.** `plan_redaction` byte-copies the workbook to
`<name> (anonymized).xlsx` in the work directory — not beside your original, so nothing unexpected
appears next to the file you pointed at. The Excel MCP server rewrites the copy. If the name is
taken the next copy becomes `<name> (anonymized) 2`, so an earlier copy you may already have shared
is never overwritten and re-redacting is never a silent no-op.

**`finish_redaction` re-reads the file before recording anything.** An external server performs
the writes, so a produced plan does not prove the edits landed. If any domain that should have been
replaced is still present, it refuses rather than certifying a half-redacted report.

**Redaction refuses to run while any domain is unanalyzed.** Nothing unreviewed can reach a shared
report. Domains analyzed as *not* needing anonymization are left in place on purpose, and are
reported back as `domains_left_as_is` so the outcome is explicit.

**Write blocks never span a gap.** Edits are grouped into runs of consecutive rows in one column,
so a contiguous column of addresses becomes a single `write_data_to_excel` call. Gaps are not
bridged even where the intervening value is known, because that would rewrite cells nobody
approved changing — and since cells are read with cached values, writing one back would replace a
formula with its result. The cost is bounded by the number of cells being redacted, which for
this workload is a small minority of any email column.

**Aliases are minted at analysis time, not scan time.** A domain gets an `AnonymizedDomain` when
analysis says it needs one — so Medium and Low rows stay blank, as in the example above. Tokens
are random `anonNNNN` rather than sequential (which would leak discovery order and domain count)
and are not derived from the domain (which would make the mapping recoverable by hashing a
guessed domain list). Once assigned, an alias is never changed or removed, so a domain keeps the
same alias across quarters even if a later analysis revises its risk.

**Bare domains are matched more strictly than addresses.** In `alice@smithlab.io` the `@` is
strong evidence, so any casing and any alphabetic TLD is accepted. A bare `smithlab.io` in a
`Domain` column has no such evidence, so it must be entirely lowercase and end in a recognised
TLD. Metric reports are full of dotted tokens that are not domains — `Total.Count`,
`report.csv`, `1.2.3`, `Fig.2A` — and those two rules reject them without a public-suffix list.

## Limitations

- **Redaction loses charts, pivot tables, and images.** The copy is byte-identical until the
  Excel MCP server opens it, and that round-trips through openpyxl, which does not preserve them.
  Cell values, formulas elsewhere in the workbook, and most formatting survive.
- **`Reference` is not a cell deep link.** It is a `file://` URL with the sheet and cell in the
  fragment — `file:///path/to/report.xlsx#<Sheet>!<A1>` — which identifies the cell for a human
  reading the audit trail but will not open a spreadsheet at it.
- **Only `.xlsx` is supported.** Convert Google Sheets and CSV files first (File > Download >
  Microsoft Excel).
- **Formulas are read as their cached values,** and a rewritten cell becomes a literal string. A
  workbook written by a tool that does not compute formulas has no cached value, and such a cell
  reads as empty.
- **Local parts are not scrubbed.** `alice@smithlab.io` becomes `alice@anon3746`. The scope here
  is domain names; if your reports contain full addresses, the local part remains a separate
  identifier that this tool does not touch.
- **A bare-domain TLD allow-list will miss novel TLDs.** A bare `lab.example` is not recognised
  (`pi@lab.example` is). Add to `_GTLDS` in `src/email_domain_scrubber/domains.py` if needed.
- **The alias space is 10,000 tokens.** Exhausting it raises rather than reusing a token.

## Development

```bash
uv sync
uv run pytest          # real .xlsx files, no network at all
uv run ruff check .
uv run ruff format --check .
```

See `AGENTS.md` for the architecture and the analysis workbook schema.
