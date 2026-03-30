---
name: lead-generation
description: Prospect discovery, enrichment, scoring, and reporting against an Ideal Customer Profile
consumer: cc_background_task
phase: 7
skill_type: uplift
---

# Lead Generation

## Purpose

Discover prospects matching an Ideal Customer Profile (ICP), enrich with
publicly available data, score on a 0-100 rubric, deduplicate against known
leads, and produce structured reports. Pairs naturally with the OSINT skill
for deep enrichment on high-scoring leads.

## When to Use

- User defines a target market, role, or company profile to prospect.
- A scheduled lead generation cycle triggers.
- An OSINT investigation surfaces a company worth prospecting.
- Strategic reflection identifies a market opportunity to explore.

## Pipeline

### Phase 1: ICP Construction

Build the Ideal Customer Profile from user requirements:

```yaml
icp:
  industry: <target industry or industries>
  role: <decision-maker titles (e.g., CTO, VP Engineering, Head of AI)>
  company_size: <startup(1-50) | smb(50-500) | enterprise(500+) | any>
  geography: <region or country focus>
  growth_signals:
    - <what indicates a good prospect (hiring, funding, product launch)>
  tech_stack: <relevant technologies they should use>
  exclusions:
    - <companies or categories to skip>
```

### Phase 2: Discovery Queries

Generate 5-10 search queries combining ICP dimensions:

- `"[industry]" "[role]" hiring` — active demand signal
- `"[industry]" companies "series A" OR "series B" OR "series C"` — funded companies
- `"top [industry] startups" [year]` — curated lists
- `site:crunchbase.com "[industry]" "[geography]"` — structured data
- `"[industry]" "[role]" interview OR podcast` — visible decision-makers
- `"[industry]" companies "[tech_stack]"` — technology fit
- `"[industry]" "fastest growing" OR "Inc 5000" OR "emerging"` — growth signals

Target: discover 2-3x the desired lead count to allow for filtering.

### Phase 3: Enrichment

Three tiers based on configured depth:

**Basic** (from discovery):
- Person name and title
- Company name
- Source URL

**Standard** (add web research):
- Company website → employee count, industry, product description
- `site:stackshare.io "[company]"` OR `site:builtwith.com` → tech stack
- Job board signals (what roles are they hiring for?)
- Recent news (funding, launches, partnerships)

**Deep** (add targeted investigation):
- Funding history (Crunchbase, press releases)
- Company news (last 6 months)
- Social profiles (public LinkedIn via `site:linkedin.com`, Twitter/X)
- Competitive positioning
- Consider triggering OSINT skill for high-value targets

### Phase 4: Deduplication

Before scoring, deduplicate against known leads:

**Normalization rules:**
- Company: strip legal suffixes (Inc, LLC, Ltd, Corp, Co, GmbH, AG, SA),
  lowercase, remove "The" prefix
- Person: lowercase, remove middle names, handle common nicknames
  (Bob=Robert, Mike=Michael, Bill=William, Jim=James)

**Match criteria (any = duplicate):**
- Exact normalized company name + person name
- Fuzzy match (Levenshtein distance < 2)
- Domain match (same company website)

### Phase 5: Scoring

Score each lead 0-100 on this rubric:

| Category | Max Points | Breakdown |
|----------|-----------|-----------|
| **ICP Match** | 30 | Industry match +10, Company size +5, Geography +5, Role/title match +10 |
| **Growth Signals** | 20 | Recent funding +8, Actively hiring +6, Product launch +3, Press coverage +3 |
| **Enrichment Quality** | 20 | Email pattern found +5, LinkedIn found +5, Full company data +5, Tech stack known +5 |
| **Recency** | 15 | Active this month +15, This quarter +10, This year +5 |
| **Accessibility** | 15 | Direct contact info +15, Company contact page +10, Social only +5 |

**Score grades:**
- **A (80-100):** Hot lead — high ICP match, strong signals, accessible
- **B (60-79):** Warm lead — good match, some gaps
- **C (40-59):** Cool lead — partial match, needs more enrichment
- **D (0-39):** Cold lead — weak match, archive but don't pursue

### Phase 6: Report Generation

```markdown
# Lead Report: [ICP Description]

**Date:** YYYY-MM-DD
**Leads discovered:** N (after dedup)
**Grade distribution:** A: N, B: N, C: N, D: N

## Hot Leads (A-Grade)

| # | Name | Title | Company | Score | Key Signal |
|---|------|-------|---------|-------|-----------|

## Warm Leads (B-Grade)

| # | Name | Title | Company | Score | Key Signal |
|---|------|-------|---------|-------|-----------|

## Summary
- Total new leads: N
- Duplicates filtered: N
- Top industries represented: ...
- Common growth signals: ...

## Recommended Next Steps
- <which leads to prioritize>
- <what enrichment to run next>
- <ICP refinements based on findings>
```

### Phase 7: State Persistence

- Store leads as observations via MemoryStore
- Tag with ICP profile for future cycle dedup
- Record discovery metadata for report generation

## LinkedIn Approach

**Compliance-safe pattern:**
- Use `"[name]" "[company]" site:linkedin.com` via search engine
- Only access publicly visible profile information
- Do NOT use LinkedIn API for scraping
- Do NOT bypass login walls
- Do NOT send unsolicited connection requests or messages
- LinkedIn Organization Messaging API (OAuth2) is available for messaging
  IF the user has proper API credentials and authorization

**Email pattern discovery (reference only):**
- `firstname@domain`
- `firstname.lastname@domain`
- `f.lastname@domain`
- `firstname.l@domain`
- Never send unsolicited emails. Pattern discovery is for user reference.

## Output Format

```yaml
report_id: <LEAD-YYYY-MM-DD-NNN>
date: <YYYY-MM-DD>
icp_summary: <one-line ICP description>
leads_discovered: <count>
duplicates_filtered: <count>
leads:
  - name: <person name>
    title: <job title>
    company: <company name>
    score: <0-100>
    grade: <A | B | C | D>
    icp_match:
      industry: <match | partial | no>
      size: <match | partial | no>
      geography: <match | partial | no>
      role: <match | partial | no>
    growth_signals:
      - <signal description>
    enrichment:
      linkedin: <URL or null>
      website: <URL or null>
      tech_stack: [<technologies>]
      recent_funding: <description or null>
    source_urls:
      - <where this lead was found>
```

## References

- `src/genesis/skills/osint/SKILL.md` — Deep enrichment for high-value leads
- `src/genesis/skills/research/SKILL.md` — General research methodology
- `src/genesis/memory/` — MemoryStore for lead persistence
