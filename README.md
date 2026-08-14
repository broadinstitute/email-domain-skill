# Email Domain Scrubber

A skill and an MCP server for handling email domain names in platform usage metric reports.

Quarterly usage metrics for a scientific research platform are full of user email domains. Most
are harmless (`broadinstitute.org`, `nih.gov`, `gmail.com`), but some name a person — a personal
lab domain, a single-researcher consultancy, a `username.github.io`. Those need to be anonymized
before the report is shared, and the decision needs an audit trail.

The split is deliberate:

- **The skill** does the judgement: reading the evidence, assigning a risk level, explaining why,
  and getting your approval.
- **This MCP server** does everything factual and everything that writes: reading the workbook,
  finding the domains, researching them, recording them, minting aliases, rewriting a copy, and
  proving it happened.

The server never decides that something is risky, and the skill never runs a search of its own,
invents an alias, or edits a spreadsheet. Everything both of them know lives in one local Excel
file, the **domain analysis workbook** — whose `AnonymizedDomain` column *is* the redaction plan,
so you can edit it and have the edit take effect.

Reports are local Excel `.xlsx` files. Everything runs on your machine: no report is uploaded
anywhere, and the file you point at is never modified. Google Sheets and CSV are out of scope —
convert them to `.xlsx` first (File > Download > Microsoft Excel).

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Claude Code
- Nothing else: no accounts and no credentials.

Your report never leaves your machine. The server does reach the network, but only to look up
**domain names** — RDAP for registration and Europe PMC for the literature, both free and
unauthenticated. Nothing else from the workbook is ever sent anywhere, and no email address is.

## Install

This repo is its own Claude Code plugin marketplace. Installing the plugin registers the skill and
the MCP server for **every** project and session, not just this directory:

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
claude plugin details email-domain-scrubber   # expect: 1 skill, 1 MCP server
claude mcp list                               # expect: plugin:email-domain-scrubber:* connected
```

The server appears as `plugin:email-domain-scrubber:email-domain-scrubber`. No Microsoft Excel
installation is needed anywhere.

`.mcp.json` at the repo root registers the same server for work inside this directory without the
plugin. With the plugin also enabled here it loads twice under different names — harmless, but
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
2. **Researches** each domain it has not seen before — registration and published literature — and
   assigns a risk level with a written justification.
3. **Presents a table** of `Domain | Risk | Explanation | Action` and waits for your approval. Say
   which verdicts you disagree with and it revises them.
4. **Hands you the analysis workbook** and waits while you read it. This is the step that decides
   what happens: edit the sheet and the redaction follows your edit. See
   [Reviewing the analysis](#reviewing-the-analysis).
5. **Shows you the changes** it intends to make — how many cells, with examples — and waits again.
6. **Writes the redacted copy** as `<name> (anonymized).xlsx` in the work directory, re-reads it,
   and refuses to certify it if any domain that should have been replaced is still there.

You end up with the path to a redacted copy and an audit trail in the analysis workbook. Your
original is untouched, nothing is uploaded, and sharing the copy is yours to do.

Domains it has already analyzed in a previous quarter keep their existing verdict and alias, so a
follow-up report only asks you about what is new.

## MCP tools

| Tool | What it does |
| --- | --- |
| `scan_workbook` | Reads every cell of the `.xlsx`, records each domain occurrence with a locator, and opens a blank analysis row per unique domain. |
| `list_domains_for_analysis` | The work queue: domains with no verdict yet, plus how often and where each was seen. |
| `research_domains` | Looks each domain up in RDAP and Europe PMC and returns the evidence, including which sources were *not* consulted. The only research in the workflow. |
| `store_domain_analysis` | Writes approved `Risk`/`Explanation`, minting an alias for each domain to anonymize. |
| `plan_redaction` | Previews what will change, from the report and the analysis workbook. Creates no copy and touches no report. |
| `apply_redaction` | Copies the report, writes the anonymized cells, reads the copy back to verify, and logs every change. |

## Workflow

```text
scan_workbook ──▶ list_domains_for_analysis ──▶ research_domains ──▶ (judge, approve)
                                                                            │
       ┌────────────────────────────────────────────────────────────────────┘
       ▼
store_domain_analysis ──▶ you review and edit the analysis workbook ──▶ plan_redaction
                                                                            │
       ┌────────────────────────────────────────────────────────────────────┘
       ▼
apply_redaction ──▶ redacted .xlsx
```

## Reviewing the analysis

After the skill records its verdicts it stops and gives you the analysis workbook. The
`DomainAnalysis` sheet is the redaction plan — there is no separate plan — and two rules govern it:

- **A domain with an `AnonymizedDomain` is replaced,** whatever its `Risk`.
- **A domain whose `Risk` reads `High` is given an `AnonymizedDomain`** if it has none.

Which means:

| To do this | Edit this |
| --- | --- |
| Anonymize something left alone | Set `Risk` to `High` |
| Spare something marked High | Clear `AnonymizedDomain` **and** lower `Risk` — both |
| Correct the reasoning | Edit `Explanation`; it is the audit trail |

Clearing an alias while the row still reads `High` is undone when the plan is computed. That is
deliberate: a row saying `High` with nothing to replace it with is more likely a half-finished edit
than a decision, and this is the direction that fails safe. Leave `Domain` alone, and don't delete
rows for domains that are in the report — redaction refuses to run while any domain in it is
unanalyzed.

## Domain analysis workbook

One local `.xlsx`, four sheets. It is the memory shared by the skill and the server, and the
record you would show an auditor. Being a plain file, it can live in a git repo alongside the
reports it describes.

| Sheet | Columns | Contents |
| --- | --- | --- |
| `Workbooks` | `Path`, `Title` | Every metrics workbook scanned. |
| `DomainReferences` | `DateExtracted`, `Reference`, `Domain` | One row per cell a domain was found in. Many rows per domain. |
| `DomainAnalysis` | `Domain`, `Risk`, `Explanation`, `AnonymizedDomain` | Exactly one row per unique domain. |
| `Redactions` | `DateRedacted`, `SourcePath`, `RedactedPath`, `Reference`, `Domain`, `AnonymizedDomain`, `Before`, `After` | One row per cell rewritten, with what it held before and after. |

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

**Redaction creates; it never edits in place.** `apply_redaction` byte-copies the workbook to
`<name> (anonymized).xlsx` in the work directory — not beside your original, so nothing unexpected
appears next to the file you pointed at — and rewrites the copy. If the name is taken the next copy
becomes `<name> (anonymized) 2`, so an earlier copy you may already have shared is never overwritten
and re-redacting is never a silent no-op.

**The skill has no way to write to a report.** Not a rule it follows — a capability it lacks. No
tool hands it cell writes, so a judgement cannot become an edit without passing through the analysis
workbook, where you can see it.

**`apply_redaction` re-reads the file before recording anything.** Producing the right values proves
nothing about whether they reached the disk. If any domain that should have been replaced is still
present, it raises and logs nothing, rather than certifying a half-redacted report.

**Research lives in the server, not the skill.** A verdict has to rest on evidence that is the same
from one run to the next, which a model composing its own queries cannot promise. So the sources are
fixed in code, and every result names the ones that were *not* consulted — general web search among
them — so an explanation can't imply evidence nobody gathered. The skill may still reason from what
it knows about a well-known institution; it just has to say so.

**Only the cells in the plan are written.** Writes go cell by cell rather than by rectangle, so a
kept address sitting between two redacted ones is left exactly as the byte copy found it. Since
cells are read with cached values, rewriting one would replace a formula with its result.

**Redaction refuses to run while any domain is unanalyzed.** Nothing unreviewed can reach a shared
report. Domains analyzed as *not* needing anonymization are left in place on purpose, and are
reported back as `domains_left_as_is` so the outcome is explicit.

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

- **Redaction loses charts, pivot tables, and images.** The copy is byte-identical until it is
  written, and that round-trips through openpyxl, which does not preserve them. Cell values,
  formulas elsewhere in the workbook, and most formatting survive.
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
- **RDAP rarely names a registrant any more.** Since GDPR, `.com`/`.org`/`.net` registries publish
  the registrar and the registration date but redact the registrant, so for most domains that
  lookup returns nothing about *who* — and its silence means nothing either way. Some ccTLDs still
  publish one. `rdap.org` also times out and does not cover every ccTLD.
- **There is no general web search.** It needs a paid API key the server does not have, so a
  one-person site with no publications and a redacted registration may come back unresolved. The
  skill is told to classify those conservatively and flag them to you rather than guess.
- **The domain's own website is never fetched,** deliberately: profiling a user by contacting their
  host is not something this tool does on your behalf.

## Development

```bash
uv sync
uv run pytest          # real .xlsx files, no network at all
uv run ruff check .
uv run ruff format --check .
```

See `AGENTS.md` for the architecture and the analysis workbook schema.
