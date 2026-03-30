# Research Sources — Prospect Researcher

## Source Priority Order

When researching a company or person, query sources in this order.
Stop when sufficient intelligence is gathered for the approach analysis.

### Tier 1: Direct Sources (Always Check)

| Source | What It Reveals | Search Pattern |
|--------|----------------|---------------|
| Company website | Mission, products, team, careers | Direct URL |
| LinkedIn company page | Size, industry, recent posts | `site:linkedin.com/company/[name]` |
| LinkedIn person profile | Role, background, interests | `"[name]" "[company]" site:linkedin.com` |
| Recent news | Funding, launches, pivots | `"[company]" (funding OR launch OR acquisition) [year]` |

### Tier 2: Enrichment Sources (Check for High-Value Targets)

| Source | What It Reveals | Search Pattern |
|--------|----------------|---------------|
| Job postings | Priorities, tech stack, growth areas | `"[company]" (hiring OR careers OR "job opening")` |
| Crunchbase / PitchBook | Funding history, investors, valuation | `site:crunchbase.com "[company]"` |
| StackShare / BuiltWith | Technology stack | `site:stackshare.io "[company]"` |
| Glassdoor | Culture, compensation, management | `site:glassdoor.com "[company]"` |
| GitHub | Open source activity, engineering culture | `site:github.com "[company]"` |

### Tier 3: Person-Specific Sources (When Targeting an Individual)

| Source | What It Reveals | Search Pattern |
|--------|----------------|---------------|
| LinkedIn posts | What they care about, thought leadership | Via LinkedIn search results |
| Conference talks | Expertise areas, communication style | `"[name]" (conference OR talk OR keynote OR panel)` |
| Podcasts | In-depth views, personality | `"[name]" (podcast OR interview)` |
| Published articles | Written perspective, depth of expertise | `"[name]" (article OR blog OR wrote)` |
| Twitter/X | Real-time interests, informal opinions | `"[name]" site:x.com OR site:twitter.com` |

### Tier 4: Competitive Context (For Client Outreach)

| Source | What It Reveals | Search Pattern |
|--------|----------------|---------------|
| Competitors | Market positioning, differentiation | `"[company]" (vs OR versus OR alternative OR competitor)` |
| Industry reports | Market trends, challenges | `"[industry]" (report OR trends OR challenges) [year]` |
| Customer reviews | What customers love/hate | `"[company]" (review OR case study)` |

## Search Query Patterns

### Finding the Right Person at a Company

When the user doesn't specify a contact:

```
"[company]" "[target role]" site:linkedin.com
"[company]" "VP Engineering" OR "CTO" OR "Head of" site:linkedin.com
"[company]" "[department]" "director" OR "manager" site:linkedin.com
```

### Finding Connection Points

```
"[person]" "[user's company or technology]"
"[person]" "[shared interest or skill]"
"[company]" "[user's technology expertise]"
```

### Timing Intelligence

```
"[company]" "[year]" (launch OR announce OR expand OR hire)
"[person]" (speaking OR presenting) "[year]"
```

## Quality Assessment

Rate the intelligence gathered on each dimension:

| Dimension | Strong | Weak |
|-----------|--------|------|
| Company clarity | Clear products, market, stage | Vague or conflicting info |
| Person specificity | Recent activity, known interests | Stale profile, no content |
| Connection points | Multiple genuine overlaps | Forced or generic connections |
| Timing relevance | Recent events create opening | No particular reason for "now" |
| Approach confidence | Clear angle with evidence | Best-guess based on limited data |

If quality is weak on 3+ dimensions, flag to the user that outreach may be
premature and suggest waiting for a better opening or gathering more data.
