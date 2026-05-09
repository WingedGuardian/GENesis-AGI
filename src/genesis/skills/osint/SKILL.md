---
name: osint
description: OSINT investigation — discover, track, and report on people, companies, and technologies
consumer: cc_background_task
phase: 7
skill_type: uplift
---

# OSINT Investigation

## Purpose

Conduct open-source intelligence gathering on specific targets — people,
companies, technologies, or markets. Discover publicly available information,
track changes over time, assess source reliability, and produce structured
intelligence reports.

This is NOT the awareness loop (which monitors Genesis's own systems). This
is outward-facing investigation — finding information about external entities.

## When to Use

- User requests research on a person, company, or competitive entity.
- Lead generation identifies a prospect needing deeper enrichment.
- Strategic reflection flags a competitor or technology to monitor.
- A scheduled monitoring task triggers a collection cycle.
- An inbox item references an entity worth investigating.

## Investigation Pipeline

### Phase 1: Target Initialization

Define the target clearly:
- **Type:** person | company | technology | market | competitor
- **Identity:** name, aliases, known associations
- **Scope:** what specifically to find (general profile, funding history,
  team composition, technology stack, competitive positioning)
- **Depth:** surface (headlines only) | deep (full articles + sources) |
  exhaustive (multi-hop research across connected entities)

### Phase 2: Query Construction

Build 10-20 search queries tailored to target type:

**Person:**
- `"[name]" [company]` — basic association
- `"[name]" site:linkedin.com` — public LinkedIn profile
- `"[name]" [industry] interview OR podcast OR keynote`
- `"[name]" [company] announcement OR appointed OR promoted`
- `"[name]" github OR gitlab` — technical contributions

**Company:**
- `"[company]" funding OR "series A" OR "series B" OR acquisition`
- `"[company]" hiring OR careers OR "we're hiring"`
- `site:crunchbase.com "[company]"` — Crunchbase profile
- `site:stackshare.io "[company]"` OR `site:builtwith.com "[company]"` — tech stack
- `"[company]" review OR glassdoor` — employee sentiment
- `"[company]" revenue OR valuation OR growth`

**Technology:**
- `"[technology]" benchmark OR comparison OR vs`
- `"[technology]" production OR deployment OR "in production"`
- `"[technology]" github stars OR contributors OR releases`
- `"[technology]" adoption OR migration OR "switched to"`

### Phase 3: Collection Sweep

For each query:
1. Search (Tinyfish / web search)
2. Fetch top 3-5 results
3. Extract structured data: names, dates, numbers, relationships
4. Tag each data point with: source URL, timestamp, confidence, relevance

**Source quality heuristics:**
- Official sources (filings, gov data, press releases) = Very High
- Institutional (Reuters, AP, established publications) = High
- Professional (industry pubs, analyst reports) = Medium-High
- Community (forums, social media, reviews) = Medium
- Anonymous/unverified = Low

### Phase 4: Entity Extraction

For each data point, extract entities and relationships:

**Entity types:** Person, Organization, Product, Event, Financial, Technology, Location

**Relationship types:** works_at, founded, invested_in, competes_with,
partnered_with, launched, acquired, uses_technology, located_in, reports_to

Store as structured records:
```yaml
entity:
  name: <canonical name>
  type: <entity type>
  attributes:
    title: <if person>
    industry: <if company>
    founded: <if company>
    headcount: <if known>
  sources:
    - url: <source URL>
      reliability: <very_high | high | medium_high | medium | low>
      date_accessed: <YYYY-MM-DD>
  first_seen: <YYYY-MM-DD>
  confidence: high | medium | low
```

### Phase 5: Change Detection (for ongoing monitoring)

If this is a follow-up cycle on a previously investigated target:
1. Compare current findings against previous snapshot
2. Classify changes:
   - **CRITICAL** (immediate attention): Leadership change, acquisition,
     major funding (>$10M), product discontinuation, legal action, security breach
   - **IMPORTANT** (include in next report): New product launch, partnership,
     hiring surge (>5 roles), pricing change, competitor move
   - **MINOR** (note in report): Blog post, minor update, conference appearance
3. Flag critical changes for immediate surfacing

### Phase 6: Report Generation

Produce a structured intelligence report:

```markdown
# OSINT Report: [Target Name]

**Date:** YYYY-MM-DD
**Depth:** surface | deep | exhaustive
**Sources consulted:** N

## Summary
<3-5 sentence overview of key findings>

## Entity Profile
<structured profile data>

## Key Findings
1. <finding with source>
2. <finding with source>

## Changes Since Last Report (if applicable)
- [CRITICAL] <change>
- [IMPORTANT] <change>

## Relationships
<entity → relationship → entity map>

## Source Quality
| Source | Reliability | Data Points |
|--------|------------|-------------|

## Confidence Assessment
<overall confidence in findings, gaps identified>

## Recommended Follow-Up
- <what to investigate next>
- <what to monitor>
```

### Phase 7: State Persistence

- Store entity data as observations via MemoryStore
- Update existing observations if entity already tracked
- Record investigation metadata for future cycles

## Source Evaluation Checklist

Before trusting any data point, check:
- [ ] **Recency** — Is this from the last 12 months?
- [ ] **Primary vs Secondary** — Is this the original source?
- [ ] **Corroboration** — Can a second independent source confirm?
- [ ] **Bias** — Does the source have an incentive to distort?
- [ ] **Specificity** — Are claims specific and verifiable?
- [ ] **Track record** — Has this source been reliable before?

If 3+ checks fail, downgrade confidence to "low."

## Compliance Rules

- Only use publicly available information
- Do NOT attempt to bypass login walls, paywalls, or CAPTCHAs
- Do NOT scrape behind authentication barriers
- LinkedIn discovery uses `site:linkedin.com` via search engines only
- Respect robots.txt and rate limits
- Label all speculation as speculation

## Output Format

```yaml
investigation_id: <OSINT-YYYY-MM-DD-NNN>
target: <name>
target_type: <person | company | technology | market | competitor>
date: <YYYY-MM-DD>
depth: <surface | deep | exhaustive>
sources_consulted: <count>
entities_extracted: <count>
key_findings:
  - finding: <description>
    confidence: high | medium | low
    sources:
      - <URL>
    significance: critical | important | minor
changes_detected:
  - change: <description>
    significance: critical | important | minor
recommended_actions:
  - <next step>
monitoring_schedule: <none | weekly | daily>
```

## References

- `src/genesis/skills/research/SKILL.md` — General research methodology
- `src/genesis/memory/` — MemoryStore for persistence
- `docs/reference/gemini-routing.md` — For video content during investigation
