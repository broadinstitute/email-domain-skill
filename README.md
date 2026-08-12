# Email Domain Scrubber

A skill and an MCP server for handling email domain names in platform usage metric reports.

Quarterly usage metrics for a scientific research platform are full of user email domains. Most
are harmless (`broadinstitute.org`, `nih.gov`, `gmail.com`), but some name a person — a personal
lab domain, a single-researcher consultancy, a `username.github.io`. Those need to be anonymized
before the report is published, and the decision needs an audit trail.

The split is deliberate:

- **The skill** does the judgement: researching each domain, assigning a risk level, explaining
  why, and getting your approval.
- **The MCP server** does everything deterministic and auditable: finding the domains, recording
  them, minting aliases, rewriting cells, and keeping the record of what it changed.

The server never decides that something is risky, and the skill never invents an alias or edits
a report. Everything both of them know lives in one Google Sheet, the **domain analysis
workbook**.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Then authenticate with an existing rclone Google Drive remote. rclone already holds a Drive OAuth
client and refresh token, and its full `drive` scope covers the Sheets API too. The config is
only ever read; refreshed access tokens stay in memory and are never written back. The server
acts as the signed-in user, so it can only reach workbooks that user can already open.

```bash
export EMAIL_DOMAIN_RCLONE_REMOTE=aso    # a remote with `type = drive` and `scope = drive`
```

The remote needs its own `client_id`/`client_secret` — with rclone's built-in OAuth client the
secret is not in the config and cannot be used. Set `RCLONE_CONFIG` if your config is not at
`~/.config/rclone/rclone.conf`. Check it end to end with:

```bash
uv run email-domain-scrubber check-auth
```

Register the server. `.mcp.json` in this repo already does it for work inside this directory; for
use elsewhere:

```bash
claude mcp add email-domain-scrubber -- uv --directory /path/to/email-domain-skill run email-domain-scrubber
```

Install the skill globally (optional — it is already active in this repo):

```bash
ln -s "$PWD/.claude/skills/email-domain-risk-analysis" ~/.claude/skills/
```

Optionally set the analysis workbook once so you can omit it from every call:

```bash
export EMAIL_DOMAIN_ANALYSIS_WORKBOOK='https://docs.google.com/spreadsheets/d/<id>/edit'
```

## Usage

Point Claude at a metrics workbook:

> Analyze the email domains in
> `https://docs.google.com/spreadsheets/d/1abc.../edit` and anonymize the risky ones.

The skill will scan the workbook, research each new domain, present a table of risk verdicts for
your approval, store the approved analysis, then show you a dry run of the redaction before
producing the anonymized copy.

## MCP tools

| Tool | What it does |
| --- | --- |
| `create_analysis_workbook` | Creates an empty analysis workbook with the four sheets and headers. Once per project. Takes an optional `folder_id` to put it on a shared drive. |
| `scan_workbook` | Reads every cell of a metrics workbook, records each domain occurrence with a link to its cell, and opens a blank analysis row per unique domain. |
| `list_domains_for_analysis` | The work queue: domains with no verdict yet, plus how often and where each was seen. |
| `store_domain_analysis` | Writes approved `Risk`/`Explanation`, minting an alias for each domain to anonymize. |
| `redact_workbook` | Copies the workbook and replaces domains with their aliases in the copy. `dry_run=true` reports what would change. |

## Domain analysis workbook

One Google Sheet, four sheets. It is the memory shared by the skill and the server, and the
record you would show an auditor.

| Sheet | Columns | Contents |
| --- | --- | --- |
| `Workbooks` | `URL`, `Title` | Every metrics workbook scanned. |
| `DomainReferences` | `DateExtracted`, `Reference`, `Domain` | One row per cell a domain was found in. `Reference` is a link that opens the workbook with that cell selected. Many rows per domain. |
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

**Aliases are minted at analysis time, not scan time.** A domain gets an `AnonymizedDomain` when
analysis says it needs one — so Medium and Low rows stay blank, as in the example above. Tokens
are random `anonNNNN` rather than sequential (which would leak discovery order and domain count)
and are not derived from the domain (which would make the mapping recoverable by hashing a
guessed domain list). Once assigned, an alias is never changed or removed, so a domain keeps the
same alias across quarters even if a later analysis revises its risk.

**Redaction copies; it never edits in place.** The source workbook is untouched. Redaction
Drive-copies it to `<title> (anonymized)` and rewrites the copy. If that name is taken, the next
copy becomes `<title> (anonymized) 2` — an already-published copy is never overwritten, and
re-redacting an already-redacted sheet is never a silent no-op.

**Redaction refuses to run while any domain is unanalyzed.** Nothing unreviewed can reach a
published report through this tool. Domains analyzed as *not* needing anonymization are left in
place on purpose, and are reported back as `domains_left_as_is` so the outcome is explicit.

**Shared drives are searched explicitly.** Drive file lookups pass `corpora=allDrives`. Without
it the search covers only My Drive, so a workbook on a shared drive reads as absent — which would
re-convert the same upload on every scan and let a second redaction reuse an already-published
`(anonymized)` name. The Sheets API can only create files in My Drive, so
`create_analysis_workbook` takes a `folder_id` and reparents afterwards.

**XLSX and CSV uploads are converted first.** The Sheets API only reads native Google Sheets, and
only native sheets have cell links. A Drive upload is converted once to `<name> (Sheets)` beside
the original and that conversion is reused on later scans, so the `Reference` links resolve.

**Bare domains are matched more strictly than addresses.** In `alice@smithlab.io` the `@` is
strong evidence, so any casing and any alphabetic TLD is accepted. A bare `smithlab.io` in a
`Domain` column has no such evidence, so it must be entirely lowercase and end in a recognised
TLD. Metric reports are full of dotted tokens that are not domains — `Total.Count`,
`report.csv`, `1.2.3`, `Fig.2A` — and those two rules reject them without a public-suffix list.

## Limitations

- **Local parts are not scrubbed.** `alice@smithlab.io` becomes `alice@anon3746`. The scope here
  is domain names; if your reports contain full addresses, the local part remains a separate
  identifier that this tool does not touch.
- **A bare-domain TLD allow-list will miss novel TLDs.** A bare `lab.example` is not recognised
  (`pi@lab.example` is). Add to `_GTLDS` in `src/email_domain_scrubber/domains.py` if needed.
- **Formulas are read as their computed values.** A cell whose formula produces a domain is
  recorded and rewritten as a literal string in the copy, replacing the formula.
- **The alias space is 10,000 tokens.** Exhausting it raises rather than reusing a token.

## Development

```bash
uv run pytest          # tests run entirely against an in-memory fake of Sheets and Drive
uv run ruff check .
uv run ruff format --check .
```
