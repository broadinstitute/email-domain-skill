# Email Domain Scrubber

A skill and an MCP server for handling email domain names in platform usage metric reports.

Quarterly usage metrics for a scientific research platform are full of user email domains. Most
are harmless (`broadinstitute.org`, `nih.gov`, `gmail.com`), but some name a person — a personal
lab domain, a single-researcher consultancy, a `username.github.io`. Those need to be anonymized
before the report is published, and the decision needs an audit trail.

The split is deliberate:

- **The skill** does the judgement: researching each domain, assigning a risk level, explaining
  why, and getting your approval.
- **This MCP server** does everything deterministic and auditable: fetching the workbook, finding
  the domains, recording them, minting aliases, planning the rewrite, and proving it happened.
- **The Excel MCP server** applies the rewrite to a copy of the workbook.
- **Google's Drive MCP connector** is the only route to Drive. There is no Google API code here.

The server never decides that something is risky, and the skill never invents an alias or edits
a report. Everything both of them know lives in one local Excel file, the **domain analysis
workbook**.

Reports are Excel `.xlsx` files in Google Drive. Google Sheets and CSV are out of scope.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

### Enable the Drive MCP API

The connector is billed to the Google Cloud project that owns your OAuth client, and **that
project must have `drivemcp.googleapis.com` enabled** — having the plain Drive API enabled is not
enough and says nothing about this one. Enable both:

```bash
gcloud services enable drive.googleapis.com drivemcp.googleapis.com --project=<project>
```

If you skip this, every call fails with Google's own message naming the project and a console
link. `check-auth` below reports it directly.

### Authenticate

Credentials come from an existing rclone Google Drive remote. rclone already holds a Drive OAuth
client and refresh token, and its full `drive` scope covers both scopes the connector asks for
(`drive.readonly` and `drive.file`). The config is only ever read; refreshed access tokens stay
in memory and are never written back. The server acts as the signed-in user, so it can only reach
files that user can already open.

```bash
export EMAIL_DOMAIN_RCLONE_REMOTE=aso    # a remote with `type = drive` and `scope = drive`
```

The remote needs its own `client_id`/`client_secret` — with rclone's built-in OAuth client the
secret is not in the config and cannot be used. Set `RCLONE_CONFIG` if your config is not at
`~/.config/rclone/rclone.conf`.

If the rclone remote's client belongs to a project where you cannot enable the Drive MCP API, use
a client from a project where you can, keeping rclone's refresh token:

```bash
export EMAIL_DOMAIN_OAUTH_CLIENT_ID=...
export EMAIL_DOMAIN_OAUTH_CLIENT_SECRET=...
```

Check it end to end:

```bash
uv run email-domain-scrubber check-auth
```

### Register the servers

`.mcp.json` in this repo registers both for work inside this directory. For use elsewhere:

```bash
claude mcp add email-domain-scrubber -- uv --directory /path/to/email-domain-skill run email-domain-scrubber
claude mcp add excel -- uvx excel-mcp-server stdio
```

[`excel-mcp-server`](https://github.com/haris-musa/excel-mcp-server) needs no Microsoft Excel
installation. Leave `EXCEL_FILES_PATH` unset so it accepts the absolute paths this server hands
it.

Install the skill globally (optional — it is already active in this repo):

```bash
ln -s "$PWD/.claude/skills/email-domain-risk-analysis" ~/.claude/skills/
```

### Optional settings

```bash
# Where workbooks are staged and the analysis record is kept.
export EMAIL_DOMAIN_WORKDIR=~/.cache/email-domain-scrubber

# The analysis workbook, if you want it somewhere else (e.g. in a git repo).
export EMAIL_DOMAIN_ANALYSIS_WORKBOOK=~/metrics/email-domain-analysis.xlsx
```

## Usage

Point Claude at a metrics workbook:

> Analyze the email domains in
> `https://drive.google.com/file/d/1abc.../view` and anonymize the risky ones.

The skill will scan the workbook, research each new domain, present a table of risk verdicts for
your approval, show you what the redaction will change, then produce and publish the anonymized
copy.

## MCP tools

| Tool | What it does |
| --- | --- |
| `scan_workbook` | Downloads the `.xlsx` from Drive, reads every cell, records each domain occurrence with a locator, and opens a blank analysis row per unique domain. |
| `list_domains_for_analysis` | The work queue: domains with no verdict yet, plus how often and where each was seen. |
| `store_domain_analysis` | Writes approved `Risk`/`Explanation`, minting an alias for each domain to anonymize. |
| `plan_redaction` | Copies the workbook locally and returns the `write_blocks` that anonymize it. Publishes nothing. |
| `finish_redaction` | Verifies the copy was actually rewritten, uploads it to Drive, and records every change. |

Between the last two, the Excel MCP server applies each block with `write_data_to_excel`.

## Workflow

```text
scan_workbook ──▶ list_domains_for_analysis ──▶ (research, approve) ──▶ store_domain_analysis
                                                                                  │
       ┌──────────────────────────────────────────────────────────────────────────┘
       ▼
plan_redaction ──▶ excel: write_data_to_excel × N blocks ──▶ finish_redaction ──▶ Drive
```

## Domain analysis workbook

One local `.xlsx`, four sheets. It is the memory shared by the skill and the server, and the
record you would show an auditor. Being a plain file, it can live in a git repo alongside the
reports it describes.

| Sheet | Columns | Contents |
| --- | --- | --- |
| `Workbooks` | `URL`, `Title` | Every metrics workbook scanned. |
| `DomainReferences` | `DateExtracted`, `Reference`, `Domain` | One row per cell a domain was found in. Many rows per domain. |
| `DomainAnalysis` | `Domain`, `Risk`, `Explanation`, `AnonymizedDomain` | Exactly one row per unique domain. |
| `Redactions` | `DateRedacted`, `SourceURL`, `RedactedURL`, `Reference`, `Domain`, `AnonymizedDomain` | One row per cell actually rewritten. |

`Redactions` is what satisfies "keep separate records of anonymizations": `DomainAnalysis` holds
the mapping and `DomainReferences` holds the source locations, but neither records what was
published, where, and when.

`DomainAnalysis` example:

```csv
Domain,Risk,Explanation,AnonymizedDomain
stephenwolfram.com,High,Stephen Wolfram of Wolfram Research,anon3746
pluralistic.net,Medium,Daily link blog of Cory Doctorow,
broadinstitute.org,Low,Broad Institute,
```

## Design decisions

**This server is the Drive connector's client, not Claude.** `download_file_content` returns
base64, so a workbook routed through the conversation would cost roughly 350k tokens per
megabyte, twice. Calling the connector from inside the server keeps bytes out of context
entirely, and workbook size stops mattering.

**The analysis workbook is local because the connector cannot update a file.** Its eight tools
are `search_files`, `get_file_metadata`, `get_file_permissions`, `list_recent_files`,
`read_file_content`, `download_file_content`, `create_file`, and `copy_file` — read-mostly, with
no update, delete, or move. A Drive-hosted record could only ever be rewritten as a new file per
edit.

**Redaction creates; it never edits in place.** Neither the Drive original nor the staged local
copy of it is touched. `plan_redaction` byte-copies the staged workbook to
`<name> (anonymized).xlsx`, the Excel MCP server rewrites that, and `finish_redaction` uploads it
as a new Drive file. If the local name is taken the next copy becomes `<name> (anonymized) 2` —
an already-published copy is never overwritten, and re-redacting is never a silent no-op.

**`finish_redaction` re-reads the file before publishing.** An external server performs the
writes now, so a produced plan no longer implies applied edits. If any domain that should have
been replaced is still present, it refuses to upload rather than publishing a half-redacted
report.

**Redaction refuses to run while any domain is unanalyzed.** Nothing unreviewed can reach a
published report. Domains analyzed as *not* needing anonymization are left in place on purpose,
and are reported back as `domains_left_as_is` so the outcome is explicit.

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

**Downloads are cached against `modifiedTime`.** Re-scanning a workbook is a normal thing to do
and should not mean re-downloading it. Absent a `modifiedTime` freshness cannot be proven, so the
file is re-fetched rather than assumed current.

**Bare domains are matched more strictly than addresses.** In `alice@smithlab.io` the `@` is
strong evidence, so any casing and any alphabetic TLD is accepted. A bare `smithlab.io` in a
`Domain` column has no such evidence, so it must be entirely lowercase and end in a recognised
TLD. Metric reports are full of dotted tokens that are not domains — `Total.Count`,
`report.csv`, `1.2.3`, `Fig.2A` — and those two rules reject them without a public-suffix list.

## Limitations

- **Redaction loses charts, pivot tables, and images.** The copy is byte-identical until the
  Excel MCP server opens it, and that round-trips through openpyxl, which does not preserve them.
  Cell values, formulas elsewhere in the workbook, and most formatting survive.
- **`Reference` is not a cell deep link.** A Google Sheet could be linked with `#gid=…&range=…`;
  an `.xlsx` in Drive cannot. The format is
  `https://drive.google.com/file/d/<id>/view#<Sheet>!<A1>` — it opens the file, and the fragment
  names the cell for a human.
- **Only `.xlsx` is supported.** Convert Google Sheets and CSV files first (File > Download >
  Microsoft Excel, then upload).
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
uv run pytest          # unit tests: a fake Drive connector, real .xlsx files
uv run pytest --live   # also hits the real connector; creates and removes scratch Drive files
uv run ruff check .
uv run ruff format --check .
```

Live teardown goes through rclone, since the connector cannot delete. Files are named
`zz-scrubber-test-*`; if cleanup cannot run it says so and names the prefix to search for.
