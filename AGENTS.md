# Email Domain Scrubber

Skill and MCP server for use on platform usage metric reports that contain email domain names

Skill to perform risk analysis of each email domain:

- use the MCP server to extract a list of domains to analysis
- analyze email domain for the privacy risk of associating the domain to a specific person
- explain analysis and justify recommendations to anonymize
- obtain user approval for the analysis and recommendations
- use the MCP server to store approved analysis and perform approved redactions

MCP server to anonymize domains and create a structured analysis and anonymization report:

- scan reports for email domain names (the analysis skill uses this to list domains to analyze)
- store analysis results in a separate domain analysis workbook (the analysis skill calls this to report its results)
- anonymize email domain names in the metrics reports
- keep separate records of anonymizations

## Domain Analysis Workbook Schema

This workbook is the memory of the scrubber skill and MCP server.

### *Workbooks* Sheet

Columns:

- `URL` workbook scanned for email domains
- `Title` - workbook title

There can be multiple workbook rows in this sheet

### DomainReferences Sheet

Columns:

- `DateExtracted`, the date the input workbook was scanned and the domain extracted from it
- `Reference`, a direct Google Sheets link to the spreadsheet cell containing the domain
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

Example:

```csv
Domain,Risk,Explanation,AnonymizedDomain
stephenwolfram.com,High,Stephen Wolfram of Wolfram Research,anon3746
pluralistic.net,Medium,Daily link blog of Cory Doctorow,
broadinstitute.org,Low,Broad Institute,
```

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

### Risk Analysis

When evaluating ambiguous or custom domains, simulate or execute the following search steps:

1. General Web Search:
   - Query: "domain.com" OR site:domain.com
   - Look for: Personal portfolios, single-person blogs, or individual contact footers.

2. Scholarly & Scientific Databases:
   - Google Scholar / PubMed: Search "domain.com" in author affiliation lines or correspondence emails.
   - ORCID Search: Query ORCID registries for emails ending in "@domain.com".
   - bioRxiv / medRxiv: Check preprints for corresponding author emails matching the domain.

3. Code & Infrastructure Registries:
   - GitHub / GitLab: Search site:github.com "domain.com" to identify single-user repositories or personal sites.

4. WHOIS / RDAP & Web Archives:
   - Check if the domain registrant name matches a personal name or privacy-shielded individual rather than an organization.

### Output

Call the MCP server to store the Risk and Explanation for each domain.
