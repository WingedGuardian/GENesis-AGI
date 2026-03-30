---
name: prospect-researcher
description: >
  This skill should be used when the user asks to "research this company",
  "look into this person", "find the best angle for reaching out to",
  "who should I contact at [company]", "what does [company] care about",
  or when preparing outreach to a specific target. Also triggered by
  "help me prepare for an interview with [company]" or "I want to apply
  to [company]". Combines lead-generation intelligence with LinkedIn-specific
  approach planning.
consumer: cc_foreground, cc_background_task
phase: 8
skill_type: uplift
---

# Prospect Researcher

## Purpose

Research a target company or person deeply enough to craft outreach that
demonstrates genuine understanding — not generic "I saw your profile and
thought we'd be a great fit." Produce actionable intelligence: what they
care about, what problems they face, what angle gives the user the best
chance of a meaningful response.

## Relationship to Other Skills

This skill does the RESEARCH. Other skills use its output:
- `linkedin-dm-outreach` — Uses findings to write personalized messages
- `lead-generation` — May trigger this for deep enrichment on hot leads
- `linkedin-post-writer` — May inform commentary posts about the target's space
- `osint` — Can be invoked for deeper investigation on high-value targets

## Research Process

### Phase 1: Target Identification

Clarify who and what to research:

```yaml
target:
  type: <company | person | both>
  company_name: <if known>
  person_name: <if known>
  person_role: <if known or "find the right person">
  goal: <job_search | client_outreach | networking | partnership | interview_prep>
  user_offering: |
    <what the user brings to the table — skills, experience, services>
```

If the user says "find the right person at [company]," determine the right
contact based on the user's goal:
- Job search → hiring manager for the relevant team, not HR
- Client outreach → decision-maker for the budget area
- Partnership → someone with strategic authority
- Interview prep → the interviewer(s) if known, otherwise the team lead

### Phase 2: Company Intelligence

Gather structured information about the target company:

**Public sources (search in this order):**
1. Company website — about page, careers, blog, press releases
2. `site:linkedin.com/company/[name]` — company page, employee count, posts
3. Recent news — funding, launches, acquisitions, leadership changes
4. Job postings — what roles are they hiring for? (reveals priorities)
5. Tech stack — `site:stackshare.com`, `site:builtwith.com`, job posting requirements
6. Competitors — who they compete with, how they differentiate
7. Industry reports or analyst coverage if available

**Synthesize into:**
```yaml
company_intel:
  summary: <what the company does, in plain language>
  size: <employee count range>
  stage: <startup | growth | enterprise>
  recent_news:
    - <significant recent events>
  current_priorities: |
    <what they appear to be focused on, based on hiring/news/content>
  tech_stack:
    - <confirmed technologies>
  challenges: |
    <inferred pain points based on hiring patterns, industry context>
  culture_signals: |
    <what their content and careers page reveal about values/culture>
```

### Phase 3: Person Intelligence

If researching a specific person:

**Public sources:**
1. `"[name]" "[company]" site:linkedin.com` — profile summary, experience
2. Their LinkedIn posts and articles — what they write about, what they care about
3. Conference talks, podcasts, interviews — `"[name]" (talk OR podcast OR interview)`
4. Published articles or blog posts
5. GitHub or technical contributions if relevant

**Synthesize into:**
```yaml
person_intel:
  name: <full name>
  role: <current title>
  tenure: <how long at current company>
  background: |
    <career trajectory — where they came from, pattern of roles>
  interests: |
    <what they post/talk about, what they care about professionally>
  communication_style: |
    <how they write — formal/casual, technical/business, verbose/concise>
  connection_points:
    - <shared interests, experiences, connections with the user>
  recent_activity: |
    <their most recent posts or public activity>
```

### Phase 4: Angle Analysis

The most valuable output. Based on company intel, person intel, and the
user's goal, identify the best approach:

```yaml
approach_analysis:
  primary_angle: |
    <the single strongest reason this person should respond to the user>
  supporting_angles:
    - <backup angle if primary doesn't resonate>
    - <additional angle>
  connection_points:
    - <specific shared elements — technologies, industries, challenges>
  timing_factors: |
    <why reaching out NOW is relevant — recent news, hiring, etc.>
  what_to_avoid: |
    <approaches that would likely backfire with this person>
  recommended_channel: <linkedin_dm | email | mutual_introduction | event>
  recommended_message_type: <connection_request | inmail | first_message>
  confidence: <high | medium | low>
  confidence_reasoning: |
    <why we're confident or not in this approach>
```

### Phase 5: Report

Combine all findings into a structured report. If Genesis is running this
proactively (background task), stage the report for user review.

## Compliance

- Use only publicly available information
- Do not scrape LinkedIn profiles (use search engine results)
- Do not bypass login walls or use API access without authorization
- Do not store personal data beyond what's needed for the outreach
- Flag if research reveals the person appears to be unreachable or
  not interested in being contacted (no public activity, minimal profile)

## Output Format

```markdown
# Prospect Research: [Target Name/Company]

**Date:** YYYY-MM-DD
**Goal:** [user's stated goal]
**Confidence:** [high | medium | low]

## Company Overview
[company_intel summary]

## Contact: [Person Name]
[person_intel summary]

## Recommended Approach
**Primary angle:** [primary_angle]
**Channel:** [recommended_channel]
**Timing:** [timing_factors]

## What to Avoid
[what_to_avoid]

## Raw Intelligence
[Full yaml blocks for reference]

## Suggested Next Steps
- [Concrete action items — e.g., "Draft connection request using X angle"]
- [Follow-up research if needed]
```

## References

- `references/research-sources.md` — Detailed source hierarchy and search patterns
- `../linkedin-dm-outreach/SKILL.md` — Uses findings for message drafting
- `../lead-generation/SKILL.md` — Broader prospecting pipeline
- `../osint/SKILL.md` — Deep investigation capability
