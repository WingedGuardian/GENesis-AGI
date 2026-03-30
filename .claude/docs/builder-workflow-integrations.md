# Genesis Builder — Claude Code Workflow Integrations

> These are improvements to the Claude Code workflow used to BUILD Genesis.
> They are tools, commands, and practices for the developer and the Claude Code
> instances working on the Genesis codebase — NOT features of Genesis itself.
>
> Implement these in the Genesis repo's `.claude/` directory and CLAUDE.md.

---

## Implement Immediately

### 1. `/audit-changes` Custom Command — Structured Pre-Commit Self-Audit

**Source:** Cole Medin's "One Command Makes Coding Agents Find All Their
Mistakes" + existing pre-commit verification rule

**What it does:** Before committing, forces Claude Code to systematically
audit its own recent changes against the original request.

**Implementation:** `.claude/commands/audit-changes.md`

```markdown
Review all changes in the current working tree (staged and unstaged).

For EACH modified file:
1. What was the original request that motivated this change?
2. Does the change match the request EXACTLY, or does it include extras?
3. What assumptions did you make that were not explicitly stated?
4. What edge cases exist that are not handled?
5. Does the code match the patterns in CLAUDE.md and the architecture docs?

Then holistically:
6. Are there files that SHOULD have been changed but were not?
7. Do any changes contradict each other?
8. Would a fresh reviewer find anything surprising?

Output a structured report. If any issues found, fix them before proceeding.
If no issues found, state "AUDIT CLEAN" explicitly.
```

### 2. `/drift` Custom Command — Plan vs Reality Tracker

**Source:** Vin's `/drift` command (Obsidian + Claude Code suite)

**What it does:** Compares build plan against actual codebase state.

**Implementation:** `.claude/commands/drift.md`

```markdown
Read the Genesis build phases document:
  docs/architecture/genesis-v3-build-phases.md

Read the git log for the last 60 days:
  git log --oneline --since="60 days ago"

Read the current directory structure.

For each build phase:
1. What does the plan say should be implemented?
2. What evidence exists in the codebase that it IS implemented?
3. What is missing?
4. What exists that is NOT in any plan?

Output a structured drift report:
- IMPLEMENTED: [phase] — [component] — [evidence: file/commit]
- MISSING: [phase] — [component] — [no evidence found]
- UNPLANNED: [component] — [exists but not in any phase]
- DRIFT SCORE: X% of planned items implemented
```

### 3. CLAUDE.md Progressive Disclosure Restructuring

**Source:** Cole Medin's three-layer progressive disclosure pattern

**What it does:** Restructures Genesis CLAUDE.md from monolithic to thin root
with topic files loaded on demand.

**Target structure:**
```
CLAUDE.md                          # <80 lines: identity, critical rules, pointers
.claude/
  docs/
    architecture-overview.md       # 3-layer architecture, MCP servers
    reflection-engine.md           # Depth levels, meta-prompting, capabilities
    task-execution.md              # Sub-agents, quality gates, governance
    memory-system.md               # memory-mcp schema, consolidation, tagging
    build-phase-current.md         # ONLY the phase currently being built
    testing-patterns.md            # Test conventions, verification approaches
  commands/
    audit-changes.md               # See above
    drift.md                       # See above
    challenge.md                   # See below
  skills/                          # Future: phase-specific build skills
```

Root CLAUDE.md contains ONLY: project identity (3 lines), repo structure,
branch discipline, 5 most critical coding rules, pointers to topic files.

### 4. Ralph Wiggum for Mechanical Build Phases

**Source:** Ralph Wiggum plugin

**When to use:** Phases with programmatic verification (tests, lint, schema).
Phase 0 (data foundation), Phase 1 (heartbeat), Phase 2 (awareness loop).

**When NOT to use:** Architecture decisions, integration judgment, identity
files, governance rules.

**PROMPT.md template:**
```markdown
# Phase [N]: [Name]

## Context
Read: docs/architecture/genesis-v3-build-phases.md (Phase [N] section)
Read: docs/architecture/genesis-v3-autonomous-behavior-design.md ([section])

## Requirements
[Numbered list from build-phases.md]

## Verification
- [ ] Code exists and is syntactically valid
- [ ] Unit tests written and passing
- [ ] Integration test passing
- [ ] No lint errors
- [ ] CLAUDE.md updated if new patterns introduced

## Completion
When ALL checks pass: <promise>PHASE_[N]_COMPLETE</promise>
If blocked after 10 iterations: <promise>PHASE_[N]_BLOCKED</promise>
Document: what is blocking, what was attempted, suggested approach.
```

**Cost note:** 30-iteration loop can cost $50-100+. Set --max-iterations
conservatively. Use cheaper models for mechanical work.

### 5. `/challenge` Custom Command — Design Doc Pressure Testing

**Source:** Vin's `/challenge` command

**Implementation:** `.claude/commands/challenge.md`

```markdown
Read the specified design doc section.

Identify:
1. The 3 weakest assumptions
2. What breaks first in production under load?
3. Most likely edge case discovered during implementation?
4. What dependency change would invalidate this design?
5. Strongest argument AGAINST implementing this?

Rate each: Likelihood (H/M/L), Impact (H/M/L), Mitigation cost (trivial/moderate/expensive).

Output as structured risk assessment.
```

### 6. Branch Retrospectives Before Deletion

**Source:** GEA paper

**Practice:** Before `git branch -D <branch>`, append to
`genesis-knowledge-transfer/branch-retrospectives.md`:

```markdown
## [branch-name] — [date] — [ABANDONED/SUPERSEDED]
**Tried:** [approach]
**Failed because:** [reason]
**Lesson:** [what to do differently]
```

---

## Implement When Genesis Has Code

### 7. Doc-to-Code Consistency Enforcement (Ralph Loop)

Ralph PROMPT.md that reads design docs, reads implementation, lists
discrepancies, fixes until zero remain.

### 8. Session Start Ritual

Start of every build session: `/drift` → read branch-retrospectives →
read current phase → check git status. Could become a SessionStart hook.

### 9. Tool Invocation Pattern Logging

Add to session handoffs: which tools/commands were most effective for the
type of work done. Template:

```markdown
## Effective Patterns This Session
- [pattern]: [when it helped]
- [tool/command]: [why it was better than alternatives]
```

---

## Not Recommended

| Item | Why Not |
|------|---------|
| Kane AI | Wrong domain — no browser E2E testing needed |
| Google Antigravity | Competing framework, no transferable patterns |
| Full GEA population evolution | Requires multiple instances |

---

## Sources

- [Ralph Wiggum Plugin](https://github.com/anthropics/claude-code/tree/main/plugins/ralph-wiggum)
- [Cole Medin Second Brain Skills](https://github.com/coleam00/second-brain-skills)
- [Cole Medin Context-Driven Dev](https://gitnation.com/contents/advanced-claude-code-techniques-agentic-engineering-with-context-driven-development-3256)
- [Vin Obsidian Workflows](https://ccforeveryone.com/mini-lessons/vin-obsidian-workflows)
- [Brad Bonanno Setup](https://okhlopkov.com/second-brain-obsidian-claude-code/)
- [GEA Paper](https://venturebeat.com/ai/new-agent-framework-matches-human-engineered-ai-systems/)
