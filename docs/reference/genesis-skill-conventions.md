# Genesis Skill Conventions

**Date:** 2026-03-11
**Status:** Active — applies to all skill creation and modification
**Source:** Anthropic's official skill-building guide + Genesis-specific requirements
**Location:** Genesis internal skills live in `src/genesis/skills/<skill-name>/`.
Claude Code native skills live in `.claude/skills/<skill-name>/` (project-scoped)
or `~/.claude/skills/<skill-name>/` (user-scoped). Both follow the same
conventions; CC-native skills omit the `consumer`, `phase`, `skill_type`
Genesis-specific frontmatter fields

---

## Skill Structure

Every skill is a directory containing a required `SKILL.md` and optional
resource subdirectories:

```
skill-name/
├── SKILL.md              (required — metadata + instructions)
├── references/            (optional — detailed docs, loaded as needed)
├── scripts/               (optional — executable utilities)
├── assets/                (optional — templates, files used in output)
└── examples/              (optional — working code examples)
```

### When to Use Each Directory

| Directory | Purpose | When to Include |
|-----------|---------|----------------|
| `references/` | Documentation Claude reads while working | Schemas, detailed guides, patterns, source hierarchies |
| `scripts/` | Executable code for deterministic tasks | Same code rewritten repeatedly, or reliability-critical operations |
| `assets/` | Files used in output (not read into context) | Templates, images, boilerplate |
| `examples/` | Working examples users can copy/adapt | Complex workflows, config samples |

**Rule:** If SKILL.md exceeds ~2,000 words, move detailed content to
`references/`. Keep SKILL.md lean — it loads into context every time the
skill triggers.

---

## YAML Frontmatter (Required)

```yaml
---
name: skill-name                    # Required — kebab-case
description: >                      # Required — third-person with trigger phrases
  This skill should be used when the user asks to "specific phrase 1",
  "specific phrase 2", "specific phrase 3", or when Genesis [internal
  trigger condition]. Also triggered by "[additional phrases]".
consumer: cc_foreground             # Genesis-specific — who uses this skill
phase: 8                            # Genesis-specific — which build phase
skill_type: workflow                # Genesis-specific — uplift | workflow | hybrid
---
```

### Required Fields

**`name`** — Kebab-case identifier. Must match the directory name.

**`description`** — The most important field. Determines when the skill triggers.

**Description rules:**
- **Third-person format:** "This skill should be used when..." — NOT "Use this
  skill when..." or "Load when..."
- **Include specific trigger phrases** in quotes: "write a LinkedIn post",
  "optimize my profile", "research this company"
- **Include both user triggers AND Genesis internal triggers** where applicable:
  "...or when Genesis proactively generates post ideas during surplus compute"
- **Be concrete:** List exact phrases a user would say. Vague descriptions
  ("provides LinkedIn guidance") cause missed triggers.

**Good example:**
```yaml
description: >
  This skill should be used when the user asks to "write a LinkedIn post",
  "draft a post about", "help me post on LinkedIn", "create LinkedIn content",
  or when Genesis proactively generates post ideas during surplus compute.
  Also triggered by content calendar execution or when the user shares a topic
  they want to write about.
```

**Bad examples:**
```yaml
description: Provides guidance for LinkedIn content.        # Vague, no triggers
description: Use this skill for LinkedIn posts.              # Wrong person
description: Load when user needs LinkedIn help.             # Not third person
```

### Genesis-Specific Fields

These fields are NOT part of Anthropic's standard but are used by Genesis's
skill evolution system (Phase 7):

**`consumer`** — Who uses this skill:
- `cc_foreground` — User invokes directly in conversation
- `cc_background_task` — Genesis uses in autonomous task sessions
- `cc_background_research` — Genesis uses for research/evaluation
- `cc_background_surplus` — Genesis uses during surplus compute
- Multiple consumers: `cc_foreground, cc_background_surplus`

**`phase`** — Which build phase introduced this skill. For tracking lineage.

**`skill_type`** — How the skill extends Genesis:
- `uplift` — Extends what Genesis CAN do (new capability)
- `workflow` — Describes HOW to do what Genesis already can
- `hybrid` — Both (e.g., new capability with a specific workflow)

Matters for skill evolution: uplift failures suggest capability gaps (need
new tools); workflow failures suggest bad instructions (need prompt rewrites).

---

## Writing Style

### Imperative / Infinitive Form

Write all instructions using verb-first imperative form. Not second person.

**Correct:**
```markdown
Read the voice profile before writing.
Generate 3-5 hook options using different patterns.
Verify each option against the banned patterns list.
```

**Incorrect:**
```markdown
You should read the voice profile before writing.
You need to generate 3-5 hook options.
You can verify each option against the list.
```

### Objective, Instructional Language

Focus on what to do, not who should do it:

**Correct:**
```markdown
Parse the input to determine the post type.
Check for anti-slop violations before outputting.
```

**Incorrect:**
```markdown
Claude should parse the input to determine the post type.
The user might want to check for violations.
```

### Third Person in Description Only

The description (frontmatter) uses third person. The body uses imperative.
These are different conventions for different purposes:
- Description tells the system WHEN to load the skill
- Body tells the executor HOW to do the work

---

## Size Guidelines

### SKILL.md Body

| Ideal | Acceptable | Too Large |
|-------|-----------|-----------|
| 1,500-2,000 words | Up to 3,000 words | 3,000+ words — split into references/ |

The body loads into context every time the skill triggers. Oversized skills
waste context window on every invocation.

### References Files

No hard limit — they load on demand. But each file should be focused on one
topic. Prefer multiple smaller files over one massive reference doc.

Recommended: 1,000-5,000 words per reference file.

If a reference file exceeds ~10,000 words, include grep search patterns in
SKILL.md so the executor can search it instead of reading it fully.

### Description

Keep the description under ~100 words. It's always in context (part of the
skill metadata that's permanently loaded). Concise but specific.

---

## Progressive Disclosure

Skills use a three-level loading system:

```
Level 1: Metadata (name + description)     — Always in context (~100 words)
Level 2: SKILL.md body                      — Loaded when skill triggers (<5k words)
Level 3: references/, scripts/, assets/     — Loaded as needed (unlimited)
```

**Design for this hierarchy:**
- If information determines WHETHER to use the skill → description (Level 1)
- If information is needed EVERY TIME the skill runs → SKILL.md body (Level 2)
- If information is needed SOMETIMES → references/ (Level 3)
- If code should be EXECUTED not read → scripts/ (Level 3, even more efficient)

---

## Cross-Skill References

Skills can reference other skills. Use relative paths:

```markdown
## References

- `../linkedin-post-writer/references/voice-profile.md` — Voice and anti-AI rules
- `../lead-generation/SKILL.md` — Broader prospecting pipeline
```

Shared resources (like voice profiles used by multiple skills) live in the
skill that owns them and are referenced by relative path from other skills.
Do NOT duplicate shared resources across skill directories.

---

## Known Procedures Section

Skills may include a `## Known Procedures` section containing battle-tested
approaches learned from past interactions. This section is automatically
maintained by the procedure promotion pipeline when procedures reach L2 tier.

Format:
```markdown
## Known Procedures

These are learned procedures relevant to this skill's domain. Follow them
unless you have a specific reason to deviate.

### <task_type> (confidence: N%, N successes)
1. Step one
2. Step two
**Known failure modes:** description
```

Procedures in this section should be followed BEFORE inventing new approaches.
They represent empirically validated knowledge. The promotion pipeline
(`src/genesis/learning/procedural/promoter.py`) syncs L2-tier procedures
here automatically.

---

## Canonical Workflow Patterns

Every skill follows one or more workflow patterns. Identifying the pattern
up front clarifies the skill's structure and makes it easier to review,
refine, and compose with other skills.

### 1. Sequential Workflow Orchestration

Execute steps in a fixed order where each step's output feeds the next.
The canonical shape is Think, Plan, Execute, Verify. Use when the task has
a natural pipeline structure and earlier steps must complete before later
ones can begin.

**When to use:** Tasks with clear phases — writing, building, deploying,
migrating. Any skill where skipping a step produces garbage.

**Genesis example:** The `linkedin-post-writer` skill follows a strict
sequence: load voice profile, generate hooks, draft body, apply anti-slop
checks, format output. Each step depends on the previous.

### 2. Multi-MCP Coordination

Orchestrate tools from multiple MCP servers within a single skill execution.
The skill acts as a conductor, pulling data from one server, processing it,
and pushing results to another. Use when the task requires information or
capabilities that span Genesis's server boundaries.

**When to use:** Cross-cutting tasks — research that needs memory context,
outreach that needs recon findings, evaluation that needs both web data and
stored knowledge.

**Genesis example:** The `prospect-researcher` skill coordinates across
memory (`memory_recall` for prior interactions), recon (`recon_findings`
for company intelligence), and web search for fresh data, then synthesizes
a unified prospect brief.

### 3. Context-Aware Tool Routing

Inspect the input or current state and choose different tool paths based on
what's found. The skill contains branching logic — not a fixed pipeline but
a decision tree that adapts to context. Use when the same trigger can lead
to meaningfully different execution paths.

**When to use:** Skills that handle diverse inputs — evaluation of different
resource types, research at different depth levels, content for different
channels.

**Genesis example:** The `evaluate` skill inspects the resource type (paper,
tool, framework, API) and routes to different analysis depths and criteria
sets. A 2-page blog post gets a different evaluation workflow than a 50-page
research paper.

### 4. Iterative Refinement (Quality Gate Loop)

Execute the primary task, then run an AI quality check against a threshold.
If the output falls short, generate improvement feedback and re-execute.
Repeat until the threshold is met or a maximum iteration count is reached.
This is the standard building block for autonomous output quality. See the
dedicated "Quality Gate Pattern" section below for the canonical
implementation.

**When to use:** Any skill producing user-facing or downstream-consumed
output where quality variance is high — content drafting, code generation,
research synthesis, report generation.

**Genesis example:** The `linkedin-hook-writer` generates multiple hook
options, scores them against engagement criteria, and regenerates weak
options with specific improvement feedback until they meet the quality bar.

### 5. Domain-Specific Knowledge Injection

Inject specialized knowledge, terminology, constraints, or decision
frameworks that the base LLM lacks. The skill's value is not in its workflow
but in the domain context it provides. Use when generic LLM output is
noticeably worse than domain-expert output for the task.

**When to use:** Tasks requiring specialized knowledge — industry jargon,
company-specific conventions, regulatory constraints, project-specific
architecture rules.

**Genesis example:** The `triage-calibration` skill injects Genesis's
autonomy level definitions, escalation thresholds, and historical triage
outcomes so that triage decisions align with the project's specific
governance model rather than generic LLM reasoning.

---

**Rule:** Every Genesis skill should explicitly identify which pattern(s) it
follows in its frontmatter or SKILL.md. Add a `## Workflow Pattern` section
near the top of the body naming the pattern(s) and any deviations. This
makes the skill's structure immediately legible to reviewers and to the
skill evolution system.

---

## Skill Categories

Skills fall into three categories based on their role in the system. This
categorization makes skill interdependencies explicit and guides
prioritization during skill development.

### Context Skills

Build and maintain shared state that other skills consume. Context skills
do not produce user-facing output directly — they make OTHER skills better
by providing richer, more accurate context.

**Characteristics:** Run early or in the background. Output is state, not
artifacts. Failure degrades quality of downstream skills silently.

**Genesis examples:**
- Session context injection (cognitive state, active tasks, user model)
- `linkedin-post-writer/references/voice-profile.md` — voice context that
  multiple LinkedIn skills reference

### Execution Skills

Perform the actual productive work — research, write, deploy, analyze,
build. These are the skills users invoke directly or that Genesis dispatches
during autonomous task execution.

**Characteristics:** Produce artifacts (content, reports, code, decisions).
Quality is directly measurable. Most skills fall here.

**Genesis examples:**
- `evaluate` — analyze and score resources
- `research` — deep investigation of topics
- `prospect-researcher` — build prospect intelligence briefs
- `linkedin-post-writer` — draft LinkedIn content
- `osint` — open-source intelligence gathering

### Meta Skills

Manage the system itself — learning, feedback, calibration, skill
refinement, quality assessment. Meta skills improve Context and Execution
skills over time.

**Characteristics:** Output is system improvement, not user-facing
artifacts. Operate on the skill system rather than through it.

**Genesis examples:**
- `retrospective` — extract lessons from past sessions
- `triage-calibration` — calibrate autonomy decision thresholds
- `forecasting` — predict outcomes to improve future planning

---

Context skills feed Execution skills. Meta skills improve both. When
planning a new skill, first ask: "Is this building context, doing work, or
improving the system?" The answer determines where it fits, what it depends
on, and how to measure its effectiveness.

---

## Quality Gate Pattern

Standard building block for any skill that needs to ensure output quality
without human review. Use this whenever a skill produces content, code,
analysis, or any artifact where quality variance is high enough to warrant
automated checking.

### Canonical Implementation

```
1. Execute primary task → produce initial output
2. AI quality check (LLM-as-judge or deterministic metrics)
3. Score against threshold (default: 0.8 on 0-1 scale)
4. If below threshold AND iterations < max (default: 3):
   a. Generate specific improvement feedback (what failed, why, how to fix)
   b. Re-execute with original input + improvement feedback
   c. Go to step 2
5. If above threshold OR max iterations reached:
   a. Accept result
   b. Log final score and iteration count
```

### Configuration Defaults

| Parameter | Default | Override When |
|-----------|---------|-------------|
| Quality threshold | 0.8 | Lower for drafts (0.6), raise for published content (0.9) |
| Max iterations | 3 | Lower for latency-sensitive tasks (1-2), raise for high-stakes output (5) |
| Judge model | Same as executor | Use a different model for independent judgment when stakes are high |

### Key Design Rules

**Improvement feedback must be specific.** "Make it better" is useless.
"The opening lacks a concrete hook — add a specific statistic or
counterintuitive claim in the first sentence" gives the executor something
actionable. The quality check step should produce structured feedback: what
scored low, why, and a concrete suggestion.

**Log every iteration.** Record the score, feedback, and iteration count.
This data feeds the skill evolution system — skills with consistently low
first-pass scores need better instructions, not more iterations.

**Fail gracefully at max iterations.** Accept the best output produced,
log that the threshold was not met, and flag for human review if the skill
supports it. Never silently drop output because it did not meet the bar.

**Separate judge criteria from execution instructions.** The quality check
should evaluate against explicit criteria (a rubric), not vague
impressions. Define the rubric in the skill's `references/` directory so
it can be refined independently of the execution instructions.

### When to Apply

Use the quality gate pattern for:
- Content drafting (posts, reports, summaries)
- Code generation (snippets, scripts, configurations)
- Research synthesis (briefs, evaluations, recommendations)
- Any output consumed by downstream skills or users without further review

Skip it for:
- Information retrieval (either found or not — no quality gradient)
- Simple transformations (formatting, extraction — deterministic correctness)
- Interactive tasks where the user provides real-time feedback

---

## Output Format

Every skill should define its expected output format. This enables:
- Downstream skills to parse the output
- Genesis to process results programmatically
- Users to know what to expect

Use YAML or markdown templates in the skill. Include all fields even if
some are optional (mark them as such).

---

## Skill Lifecycle

Skills are learning artifacts, not static configuration. Phase 7's skill
evolution system tracks effectiveness and proposes refinements:

1. **Creation** — Skill is written following these conventions
2. **Usage** — Skill is invoked, sessions are tagged with `skill_tags`
3. **Measurement** — SkillEffectivenessAnalyzer computes per-skill metrics
4. **Refinement** — SkillRefiner proposes improvements based on data
5. **Evolution** — Changes applied (auto for MINOR, staged for MODERATE+)
6. **Quarantine** — Skills with declining success rates are quarantined

**Implication for skill authors:** Write skills knowing they will evolve.
Prefer modular structure (sections that can be independently refined) over
monolithic instructions.

---

## Validation Checklist

Before finalizing any skill:

**Structure:**
- [ ] SKILL.md exists with valid YAML frontmatter
- [ ] `name` and `description` fields present
- [ ] `name` matches directory name (kebab-case)
- [ ] Genesis fields present: `consumer`, `phase`, `skill_type`
- [ ] All referenced files exist (no broken references)
- [ ] Only directories that are needed are created

**Description quality:**
- [ ] Uses third person ("This skill should be used when...")
- [ ] Includes specific trigger phrases in quotes
- [ ] Lists both user and Genesis internal triggers (if dual-use)
- [ ] Under ~100 words

**Body quality:**
- [ ] Uses imperative/infinitive form throughout
- [ ] No second person ("you should", "you need to")
- [ ] Under 3,000 words (ideally 1,500-2,000)
- [ ] Detailed content moved to `references/`
- [ ] References resources by relative path

**Progressive disclosure:**
- [ ] Core workflow in SKILL.md
- [ ] Detailed docs in `references/`
- [ ] Working examples in `examples/` (if applicable)
- [ ] Utility scripts in `scripts/` (if applicable)

---

## Common Mistakes

| Mistake | Why It's Bad | Fix |
|---------|-------------|-----|
| Vague description | Skill won't trigger | Add specific trigger phrases in quotes |
| Second-person writing | Inconsistent, not Anthropic convention | Rewrite in imperative form |
| Everything in SKILL.md | Bloats context on every trigger | Move details to `references/` |
| Duplicated resources | Update drift, wasted tokens | Shared resources live in one skill, others reference it |
| No output format | Downstream consumers can't parse results | Define explicit output template |
| Missing Genesis fields | Skill evolution system can't track it | Add `consumer`, `phase`, `skill_type` |
| No in-skill examples | Abstract instructions get misinterpreted | Add 1-2 input→output demos in SKILL.md body |
| No negative boundaries | Skill hijacks unrelated requests | Add "Do NOT use for [list]" to description |

---

## Failure Mode Taxonomy

When a skill misbehaves, diagnose which failure mode it exhibits. Each
mode has a specific root cause and fix — don't guess, match the symptom.

| Mode | Symptom | Diagnosis | Fix |
|------|---------|-----------|-----|
| **Silent** | Never fires on matching requests | Description too weak, missing trigger phrases | Add 5-7+ explicit trigger phrases. Be embarrassingly explicit. |
| **Hijacker** | Fires on unrelated requests | Description too broad, no negative boundaries | Add "Do NOT use for [X, Y, Z]" to description. |
| **Drifter** | Fires correctly but output varies | Instructions are ambiguous, multiple interpretations | Replace vague rules with testable ones. "Handle appropriately" → "If X, then Y." |
| **Fragile** | Works on clean input, breaks on edge cases | Missing edge-case handling | Add "If [condition], then [specific action]" for each failure. |
| **Overachiever** | Adds unsolicited content, extra sections | Instructions say what to do but not what NOT to do | Add explicit scope constraints: "Output ONLY [format]. Do NOT add [list]." |

Use this taxonomy both when authoring new skills (prevent each mode by
design) and when the skill evolution pipeline flags a declining skill
(diagnose which mode is causing the decline, then apply the matching fix).

---

## Testing Protocol

Before marking any skill as finalized, run all five tests. This is a
required gate alongside the Validation Checklist above.

### Test 1: Happy Path

Run the skill with clean, complete, ideal input. Does it produce the
expected output in the expected format? If not, refine the workflow
instructions.

### Test 2: Minimal Input

Run the skill with the absolute minimum information a user might provide.
Does it ask for what it needs? Does it handle missing information
gracefully without inventing data?

### Test 3: Edge Case

Run the skill with unusual inputs — contradictory requirements, extremely
short or long inputs, unexpected formats. Does it handle them with
specific instructions, or fall back to generic behavior?

### Test 4: Negative Test

Try to trigger the skill with a request that SHOULD NOT activate it. Does
it correctly stay silent? If it fires, the description needs tighter
negative boundaries (Silent/Hijacker fix).

### Test 5: Repeat Test

Run the same input through the skill three times. Is the output
consistent in structure and quality? If it varies significantly, the
instructions are ambiguous somewhere (Drifter fix).

Fix every failure. Update the SKILL.md. Test again. All five tests must
pass before the skill is considered production-ready.

---

## In-Skill Examples

Include at least one happy-path example and one edge-case example directly
in SKILL.md body, not only in `examples/`. A concrete input→output demo
is worth fifty lines of abstract instruction and is cheap at Level 2
(loaded on every trigger).

Use `examples/` directory for longer, rarer scenarios that don't need to
load on every invocation.

Examples count toward the SKILL.md word budget (1,500-2,000 target) but
are high-ROI tokens — prioritize them over additional abstract rules when
approaching the budget limit.

**Format:**

```markdown
### Fire Example
**Input:** "specific user request"
**Action:** What the skill does, which references it reads

### Don't-Fire Example
**Input:** "request that looks similar but shouldn't trigger"
**Reason:** Why this doesn't match, what should happen instead
```
