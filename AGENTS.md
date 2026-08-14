# Email Domain Scrubber

Skill and MCP server for use on platform usage metric reports that contain email domain names

Reports are local Microsoft Excel (`.xlsx`) files. Google Sheets and CSV are out of scope.

**Everything is local.** The server reads a workbook from disk and writes its output under the
work directory. It has no notion of where a report came from: fetch it however you like — a
browser, a sync client, a storage plugin — and pass the path to the file on disk. `local.py`
resolves that reference and refuses anything that is a URL rather than a path.

## Architecture

Two parts, each with one job:

- **The analysis skill** judges risk and talks to the user. It runs no research of its own and
  writes to no spreadsheet.
- **The `email-domain-scrubber` MCP server** (this repo) does everything factual and everything
  that writes: reading workbooks, finding domains, researching them, recording verdicts, minting
  aliases, rewriting the redacted copy, verifying it, and logging what changed.

The split is the point. Research lives in the server so that the evidence behind a verdict is
fixed by code rather than by whatever query a model thought of, and two runs over the same domain
see the same sources. Writing lives in the server so that there is no path by which a judgement
becomes a cell edit without passing through the analysis workbook.

Workbook bytes never pass through the model's context: the server reads the file itself, and only
domains, locators, and counts travel back through a tool result.

Skill to perform risk analysis of each email domain:

- use the MCP server to extract a list of domains to analyze
- use the MCP server to research each domain; craft no queries of its own
- analyze email domain for the privacy risk of associating the domain to a specific person
- explain analysis and justify recommendations to anonymize
- obtain user approval for the analysis and recommendations
- use the MCP server to store approved analysis
- have the user review, and optionally edit, the analysis workbook
- use the MCP server to plan and then apply the redaction

MCP server to research and anonymize domains and create a structured analysis and anonymization
report:

- scan reports for email domain names (the analysis skill uses this to list domains to analyze)
- research a domain's registration (RDAP) and appearances in the scientific literature (Europe PMC)
- store analysis results in a separate domain analysis workbook (the analysis skill calls this to report its results)
- write the anonymized copy of the report, driven solely by the analysis workbook
- verify the writes landed and keep separate records of anonymizations

### Research sources

Both free, unauthenticated, and named in every result so an explanation can be honest about them:

- **RDAP** via `rdap.org`. Post-GDPR, gTLD registries redact the registrant, so a name is usually
  absent for `.com`/`.org`/`.net` and its absence means nothing; some ccTLDs still publish one.
  `rdap.org` also times out and does not cover every ccTLD, so `unavailable` is a normal outcome.
- **Europe PMC**, full-text over PubMed and bioRxiv/medRxiv. The deciding source in most hard
  cases: one recurring first author points at a person, many point at an institution.

Deliberately not wired, and reported as `not_searched`: general web search (needs a paid API key),
fetching the domain's own site (contacting a user's host to profile them), GitHub, and ORCID.
Adding a source means adding it here, not letting the skill improvise one.

## Domain Analysis Workbook Schema

This workbook is the memory of the scrubber skill and MCP server. It is a local `.xlsx` file, so
it can live in a git repo alongside the reports it describes.

### *Workbooks* Sheet

Columns:

- `Path` workbook scanned for email domains
- `Title` - workbook title

There can be multiple workbook rows in this sheet

### DomainReferences Sheet

Columns:

- `DateExtracted`, the date the input workbook was scanned and the domain extracted from it
- `Reference`, a locator for the cell containing the domain, of the form
  `file:///path/to/report.xlsx#<Sheet>!<A1>`. A `file://` URL cannot deep-link a cell of an
  `.xlsx`, so the URL names the file and the fragment names the cell.
- `Domain`, the non-anonymized domain name found within the referenced cell

There can be multiple `Reference`s for each unique `Domain`

### DomainAnalysis Sheet

Columns:

- `Domain` email domain name
- `Risk` result of risk analysis for `Domain` (see Risk Taxonomy)
- `Explanation` result of risk analysis for `Domain` (to be provided by the skill doing the analysis)
- `AnonymizedDomain` substituted for high-risk `Domain`s in published reports; may be left blank if risk is not high

There should be only one row for each unique `Domain`.
The MCP generates `AnonymizedDomain` for each new `Domain` and retains any pre-existing mapping.
The analysis skill determines the `Risk` and the need to anonymize; the MCP handles persistence of this workbook.

**This sheet is the redaction plan.** There is no separate plan: `AnonymizedDomain` is exactly the
set of substitutions `apply_redaction` will make, and nothing else decides them. That is what makes
the user's review step real — they edit this sheet, and the edit takes effect. Two rules, reconciled
at plan time:

- A domain with an `AnonymizedDomain` is replaced, whatever its `Risk`.
- A domain whose `Risk` reads `High` is given an `AnonymizedDomain` if it has none.

So sparing a domain marked High takes both edits: clear the alias *and* lower the risk. Clearing
the alias alone is undone, deliberately — a row reading `High` with nothing to replace it with is
more likely a half-finished edit than a decision, and this is the direction that fails safe. A
`Risk` outside the taxonomy is refused, naming the row. An alias is never reissued or changed, so
mappings stay valid across quarters.

Example:

```csv
Domain,Risk,Explanation,AnonymizedDomain
stephenwolfram.com,High,Stephen Wolfram of Wolfram Research,anon3746
pluralistic.net,Medium,Daily link blog of Cory Doctorow,
broadinstitute.org,Low,Broad Institute,
```

### Redactions Sheet

Columns:

- `DateRedacted`, the date the anonymized copy was written
- `SourcePath`, the metrics workbook that was redacted
- `RedactedPath`, the anonymized copy in the work directory
- `Reference`, a locator for the rewritten cell *in the copy*
- `Domain`, the domain that was replaced
- `AnonymizedDomain`, what replaced it
- `Before`, the cell's full text before the rewrite
- `After`, the cell's full text after it

The applied log, written by `apply_redaction` only after it has read the copy back and confirmed
the writes landed. One row per rewritten cell per domain replaced — a cell holding two anonymized
domains yields two rows sharing the same `Before` and `After`, which keeps `Domain` a single value
that joins against `DomainAnalysis`.

This is what satisfies "keep separate records of anonymizations": `DomainAnalysis` holds the
mapping and `DomainReferences` holds the source locations, but neither records what was produced,
where, and when.

## Skill: Privacy Risk Analysis of Email Domains

You are an expert Privacy Compliance & OSINT Analyst for scientific computing platforms. The user will give you a list of email domain names from quarterly usage metrics of a scientific research platform. Your goal is to evaluate the email domain names as to the risk of the name itself disclosing the identity of an individual. Consider only the privacy risk from the domain name itself.

### Risk Taxonomy

1. High risk
   - Personal name or pseudonym: (e.g., johnsmith.org, smithlab.io, dr-doe-research.com).
   - Personal GitHub / Developer Hosting: (e.g., username.github.io, username.netlify.app).

2. Medium risk
   - Single-Principal Academic/Consulting Entities: Domains owned by a single researcher or consultant
   - Small Independent Labs / Niche Startups: Domains associated with fewer than 3 identifiable individuals
   - Niche Departmental / Personal Academic Subdomains: Highly specific subdomains that resolve directly to a single erver.

3. Low risk
   - Major Academic Institutions: (e.g., harvard.edu, broadinstitute.org, ox.ac.uk).
   - Government & Non-Profit Research Bodies: (e.g., nih.gov, embl.org, ebi.ac.uk).
   - Standard Commercial & Corporate Entities: (e.g., pfizer.com, illumina.com, aws.com).
   - Widely used freemail & consumer email providers: (e.g., gmail.com, yahoo.com, proton.me, comcast.net, icloud.com).

### Unrelated Out-of-scope risks

Do **not** consider unrelated risks such as domain reputation, email provider used, or the security and trustworthiness of users of the domain.

**Email usernames are out of scope.** Only the domain is judged. `alice@smithlab.io` and
`j.smith@smithlab.io` are one domain with one verdict, and that `j.smith` echoes `smithlab` is not
evidence of anything. Usernames must not appear in an `Explanation`. Redaction replaces the domain
and leaves the local part in place, by design.

### Risk Analysis

Call `research_domains` for the domains in the queue, in one batch. Run no searches of your own —
no web search, no fetch, no asking the user to look something up. What the server returns is the
evidence; see *Research sources* above for what each one is worth and what its absence means.

Your own knowledge of a domain is legitimate evidence and should be used, especially where RDAP is
redacted. Say when a verdict rests on recall rather than a lookup, and never attribute a claim to a
source that did not make it.

Where the evidence is exhausted and the domain is still unidentified, classify conservatively
(Medium rather than Low) and flag it to the user as unresolved. Do not infer a verdict from the
shape of the name.

### Output

Present the analysis, get the user's approval, then call the MCP server to store the Risk and
Explanation for each domain. Then hand the user the analysis workbook to review and edit before
calling `plan_redaction` and `apply_redaction`. The skill writes nothing to any spreadsheet at any
point.
