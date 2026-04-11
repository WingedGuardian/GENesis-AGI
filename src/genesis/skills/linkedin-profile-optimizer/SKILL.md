---
name: linkedin-profile-optimizer
description: >
  This skill should be used when the user asks to "optimize my LinkedIn profile",
  "update my LinkedIn headline", "rewrite my LinkedIn summary", "improve my
  LinkedIn about section", or when Genesis identifies that the user's profile
  doesn't align with their current goals or target audience.
consumer: cc_foreground
phase: 8
skill_type: workflow
---

# LinkedIn Profile Optimizer

## Purpose

Analyze and rewrite LinkedIn profile sections to clearly communicate the user's
value to their target audience — whether that's employers, clients, or
professional network. Avoid generic "results-driven professional" language.
Sound human. Sound like the user.

## Voice Loading

Before writing any profile content, load the user's voice via voice-master's
overlay resolution:

1. Read `../voice-master/SKILL.md` and follow its **User Calibration
   Overlay** section to load the user's exemplars and voice-dimensions from
   the out-of-repo overlay (or template fallback with warning if no overlay).
2. This skill's medium is `professional`. Select professional-medium
   exemplars from whatever voice-master loads.
3. Read `../voice-master/references/anti-slop.md` — apply the **Universal**
   and **Professional / LinkedIn** sections.

If no overlay is present, voice-master falls back to the generic template
and warns — note this in your output.

Profile copy follows the same anti-slop rules as posts. A profile that reads
like a ChatGPT template is worse than an outdated one.

## Profile Sections

### Headline (220 characters max)

The most visible element. Appears in search, comments, connection requests.

**Formula:** `[What you do] | [For whom or in what domain] | [Differentiator]`

**Rules:**
- No "passionate about" or "driven by"
- Concrete role or capability, not aspirational fluff
- Include keywords recruiters/prospects actually search for
- Test: would this make someone click on the profile?

### About / Summary (2,600 characters max)

First-person narrative. Not a resume summary — a conversation with the reader.

**Structure:**
- Open with what you actually do and why it matters (2-3 sentences)
- Middle: specific experience, achievements, or expertise areas with enough
  detail to be credible (not a bullet list of buzzwords)
- Mention what you're looking for or interested in — gives people a reason
  to reach out
- End with how to get in touch or what kind of conversations you welcome

**Rules:**
- Write in first person ("I build..." not "[Name] is a...")
- Include at least 2 specific technical skills or tools
- Include at least 1 quantified achievement
- No "seasoned professional with N years of experience"
- Keep paragraphs short (3-4 sentences max)

### Experience Entries

Each role should communicate impact, not just responsibilities.

**Formula:** `[What you did] → [What changed because of it] → [How you did it]`

**Rules:**
- Lead with outcomes, not duties
- Quantify where possible (team size, scale, savings, uptime)
- Name technologies and methodologies specifically
- 3-5 bullet points per role, not 15
- Recent roles get more detail than old ones

### Skills Section

Prioritize skills that:
- Align with target roles or client needs
- Are searchable (LinkedIn's algorithm uses these for recommendations)
- Are specific ("Terraform" > "Infrastructure as Code" > "Cloud Computing")

### Featured Section

Curate 3-5 items maximum:
- Best-performing LinkedIn posts
- Published articles or talks
- Project showcases or case studies
- Portfolio links

## Optimization Process

1. **Gather current state** — Ask the user to paste their current profile
   sections, or provide their LinkedIn URL for reference.

2. **Clarify goals** — What is the profile optimized FOR? Job search?
   Client acquisition? Professional networking? Thought leadership? The
   rewrite differs significantly based on the answer.

3. **Identify target audience** — Who should find this profile compelling?
   Recruiters in cloud engineering? CTOs evaluating consultants? Peers in
   the same field?

4. **Rewrite sections** — Apply voice profile, anti-AI rules, and the
   structure guidance above. Provide before/after for each section.

5. **Keyword check** — Ensure high-value search terms appear naturally in
   headline, about, and experience sections. Never keyword-stuff.

## Output Format

```yaml
target_goal: <job search | client acquisition | networking | thought leadership>
target_audience: <who this profile should attract>
sections_optimized:
  - section: <headline | about | experience | skills | featured>
    before: |
      <original text if provided>
    after: |
      <optimized text>
    rationale: |
      <why these changes were made>
```

## References

- `../voice-master/SKILL.md` — Voice authority; follow its User Calibration Overlay section to load professional exemplars from the user overlay
- `../voice-master/references/anti-slop.md` — Universal + Professional/LinkedIn anti-slop rules
