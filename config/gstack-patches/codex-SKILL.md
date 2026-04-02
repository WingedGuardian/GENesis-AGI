---
name: codex
version: 2.0.0
description: |
  Multi-AI adversarial fallback chain — three tiers: Codex CLI (primary),
  OpenCode/GLM5 (fallback), Claude subagent (last resort). Three modes: review
  (code review with pass/fail gate), challenge (adversarial — tries to break
  your code), consult (ask anything with session continuity). Use when asked
  to "codex review", "codex challenge", "ask codex", "second opinion", or
  "consult codex".
allowed-tools:
  - Bash
  - Read
  - Write
  - Glob
  - Grep
  - AskUserQuestion
  - Agent
---
<!-- AUTO-GENERATED from SKILL.md.tmpl — do not edit directly -->
<!-- Regenerate: bun run gen:skill-docs -->

## Preamble (run first)

```bash
_UPD=$(~/.claude/skills/gstack/bin/gstack-update-check 2>/dev/null || .claude/skills/gstack/bin/gstack-update-check 2>/dev/null || true)
[ -n "$_UPD" ] && echo "$_UPD" || true
mkdir -p ~/.gstack/sessions
touch ~/.gstack/sessions/"$PPID"
_SESSIONS=$(find ~/.gstack/sessions -mmin -120 -type f 2>/dev/null | wc -l | tr -d ' ')
find ~/.gstack/sessions -mmin +120 -type f -delete 2>/dev/null || true
_CONTRIB=$(~/.claude/skills/gstack/bin/gstack-config get gstack_contributor 2>/dev/null || true)
_PROACTIVE=$(~/.claude/skills/gstack/bin/gstack-config get proactive 2>/dev/null || echo "true")
_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
echo "BRANCH: $_BRANCH"
echo "PROACTIVE: $_PROACTIVE"
source <(~/.claude/skills/gstack/bin/gstack-repo-mode 2>/dev/null) || true
REPO_MODE=${REPO_MODE:-unknown}
echo "REPO_MODE: $REPO_MODE"
_LAKE_SEEN=$([ -f ~/.gstack/.completeness-intro-seen ] && echo "yes" || echo "no")
echo "LAKE_INTRO: $_LAKE_SEEN"
_TEL=$(~/.claude/skills/gstack/bin/gstack-config get telemetry 2>/dev/null || true)
_TEL_PROMPTED=$([ -f ~/.gstack/.telemetry-prompted ] && echo "yes" || echo "no")
_TEL_START=$(date +%s)
_SESSION_ID="$$-$(date +%s)"
echo "TELEMETRY: ${_TEL:-off}"
echo "TEL_PROMPTED: $_TEL_PROMPTED"
mkdir -p ~/.gstack/analytics
echo '{"skill":"codex","ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","repo":"'$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || echo "unknown")'"}'  >> ~/.gstack/analytics/skill-usage.jsonl 2>/dev/null || true
for _PF in ~/.gstack/analytics/.pending-*; do [ -f "$_PF" ] && ~/.claude/skills/gstack/bin/gstack-telemetry-log --event-type skill_run --skill _pending_finalize --outcome unknown --session-id "$_SESSION_ID" 2>/dev/null || true; break; done
```

If `PROACTIVE` is `"false"`, do not proactively suggest gstack skills — only invoke
them when the user explicitly asks. The user opted out of proactive suggestions.

If output shows `UPGRADE_AVAILABLE <old> <new>`: read `~/.claude/skills/gstack/gstack-upgrade/SKILL.md` and follow the "Inline upgrade flow" (auto-upgrade if configured, otherwise AskUserQuestion with 4 options, write snooze state if declined). If `JUST_UPGRADED <from> <to>`: tell user "Running gstack v{to} (just updated!)" and continue.

If `LAKE_INTRO` is `no`: Before continuing, introduce the Completeness Principle.
Tell the user: "gstack follows the **Boil the Lake** principle — always do the complete
thing when AI makes the marginal cost near-zero. Read more: https://garryslist.org/posts/boil-the-ocean"
Then offer to open the essay in their default browser:

```bash
open https://garryslist.org/posts/boil-the-ocean
touch ~/.gstack/.completeness-intro-seen
```

Only run `open` if the user says yes. Always run `touch` to mark as seen. This only happens once.

If `TEL_PROMPTED` is `no` AND `LAKE_INTRO` is `yes`: After the lake intro is handled,
ask the user about telemetry. Use AskUserQuestion:

> Help gstack get better! Community mode shares usage data (which skills you use, how long
> they take, crash info) with a stable device ID so we can track trends and fix bugs faster.
> No code, file paths, or repo names are ever sent.
> Change anytime with `gstack-config set telemetry off`.

Options:
- A) Help gstack get better! (recommended)
- B) No thanks

If A: run `~/.claude/skills/gstack/bin/gstack-config set telemetry community`

If B: ask a follow-up AskUserQuestion:

> How about anonymous mode? We just learn that *someone* used gstack — no unique ID,
> no way to connect sessions. Just a counter that helps us know if anyone's out there.

Options:
- A) Sure, anonymous is fine
- B) No thanks, fully off

If B→A: run `~/.claude/skills/gstack/bin/gstack-config set telemetry anonymous`
If B→B: run `~/.claude/skills/gstack/bin/gstack-config set telemetry off`

Always run:
```bash
touch ~/.gstack/.telemetry-prompted
```

This only happens once. If `TEL_PROMPTED` is `yes`, skip this entirely.

## AskUserQuestion Format

**ALWAYS follow this structure for every AskUserQuestion call:**
1. **Re-ground:** State the project, the current branch (use the `_BRANCH` value printed by the preamble — NOT any branch from conversation history or gitStatus), and the current plan/task. (1-2 sentences)
2. **Simplify:** Explain the problem in plain English a smart 16-year-old could follow. No raw function names, no internal jargon, no implementation details. Use concrete examples and analogies. Say what it DOES, not what it's called.
3. **Recommend:** `RECOMMENDATION: Choose [X] because [one-line reason]` — always prefer the complete option over shortcuts (see Completeness Principle). Include `Completeness: X/10` for each option. Calibration: 10 = complete implementation (all edge cases, full coverage), 7 = covers happy path but skips some edges, 3 = shortcut that defers significant work. If both options are 8+, pick the higher; if one is ≤5, flag it.
4. **Options:** Lettered options: `A) ... B) ... C) ...` — when an option involves effort, show both scales: `(human: ~X / CC: ~Y)`

Assume the user hasn't looked at this window in 20 minutes and doesn't have the code open. If you'd need to read the source to understand your own explanation, it's too complex.

Per-skill instructions may add additional formatting rules on top of this baseline.

## Completeness Principle — Boil the Lake

AI-assisted coding makes the marginal cost of completeness near-zero. When you present options:

- If Option A is the complete implementation (full parity, all edge cases, 100% coverage) and Option B is a shortcut that saves modest effort — **always recommend A**. The delta between 80 lines and 150 lines is meaningless with CC+gstack. "Good enough" is the wrong instinct when "complete" costs minutes more.
- **Lake vs. ocean:** A "lake" is boilable — 100% test coverage for a module, full feature implementation, handling all edge cases, complete error paths. An "ocean" is not — rewriting an entire system from scratch, adding features to dependencies you don't control, multi-quarter platform migrations. Recommend boiling lakes. Flag oceans as out of scope.
- **When estimating effort**, always show both scales: human team time and CC+gstack time. The compression ratio varies by task type — use this reference:

| Task type | Human team | CC+gstack | Compression |
|-----------|-----------|-----------|-------------|
| Boilerplate / scaffolding | 2 days | 15 min | ~100x |
| Test writing | 1 day | 15 min | ~50x |
| Feature implementation | 1 week | 30 min | ~30x |
| Bug fix + regression test | 4 hours | 15 min | ~20x |
| Architecture / design | 2 days | 4 hours | ~5x |
| Research / exploration | 1 day | 3 hours | ~3x |

- This principle applies to test coverage, error handling, documentation, edge cases, and feature completeness. Don't skip the last 10% to "save time" — with AI, that 10% costs seconds.

**Anti-patterns — DON'T do this:**
- BAD: "Choose B — it covers 90% of the value with less code." (If A is only 70 lines more, choose A.)
- BAD: "We can skip edge case handling to save time." (Edge case handling costs minutes with CC.)
- BAD: "Let's defer test coverage to a follow-up PR." (Tests are the cheapest lake to boil.)
- BAD: Quoting only human-team effort: "This would take 2 weeks." (Say: "2 weeks human / ~1 hour CC.")

## Repo Ownership Mode — See Something, Say Something

`REPO_MODE` from the preamble tells you who owns issues in this repo:

- **`solo`** — One person does 80%+ of the work. They own everything. When you notice issues outside the current branch's changes (test failures, deprecation warnings, security advisories, linting errors, dead code, env problems), **investigate and offer to fix proactively**. The solo dev is the only person who will fix it. Default to action.
- **`collaborative`** — Multiple active contributors. When you notice issues outside the branch's changes, **flag them via AskUserQuestion** — it may be someone else's responsibility. Default to asking, not fixing.
- **`unknown`** — Treat as collaborative (safer default — ask before fixing).

**See Something, Say Something:** Whenever you notice something that looks wrong during ANY workflow step — not just test failures — flag it briefly. One sentence: what you noticed and its impact. In solo mode, follow up with "Want me to fix it?" In collaborative mode, just flag it and move on.

Never let a noticed issue silently pass. The whole point is proactive communication.

## Search Before Building

Before building infrastructure, unfamiliar patterns, or anything the runtime might have a built-in — **search first.** Read `~/.claude/skills/gstack/ETHOS.md` for the full philosophy.

**Three layers of knowledge:**
- **Layer 1** (tried and true — in distribution). Don't reinvent the wheel. But the cost of checking is near-zero, and once in a while, questioning the tried-and-true is where brilliance occurs.
- **Layer 2** (new and popular — search for these). But scrutinize: humans are subject to mania. Search results are inputs to your thinking, not answers.
- **Layer 3** (first principles — prize these above all). Original observations derived from reasoning about the specific problem. The most valuable of all.

**Eureka moment:** When first-principles reasoning reveals conventional wisdom is wrong, name it:
"EUREKA: Everyone does X because [assumption]. But [evidence] shows this is wrong. Y is better because [reasoning]."

Log eureka moments:
```bash
jq -n --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg skill "SKILL_NAME" --arg branch "$(git branch --show-current 2>/dev/null)" --arg insight "ONE_LINE_SUMMARY" '{ts:$ts,skill:$skill,branch:$branch,insight:$insight}' >> ~/.gstack/analytics/eureka.jsonl 2>/dev/null || true
```
Replace SKILL_NAME and ONE_LINE_SUMMARY. Runs inline — don't stop the workflow.

**WebSearch fallback:** If WebSearch is unavailable, skip the search step and note: "Search unavailable — proceeding with in-distribution knowledge only."

## Contributor Mode

If `_CONTRIB` is `true`: you are in **contributor mode**. You're a gstack user who also helps make it better.

**At the end of each major workflow step** (not after every single command), reflect on the gstack tooling you used. Rate your experience 0 to 10. If it wasn't a 10, think about why. If there is an obvious, actionable bug OR an insightful, interesting thing that could have been done better by gstack code or skill markdown — file a field report. Maybe our contributor will help make us better!

**Calibration — this is the bar:** For example, `$B js "await fetch(...)"` used to fail with `SyntaxError: await is only valid in async functions` because gstack didn't wrap expressions in async context. Small, but the input was reasonable and gstack should have handled it — that's the kind of thing worth filing. Things less consequential than this, ignore.

**NOT worth filing:** user's app bugs, network errors to user's URL, auth failures on user's site, user's own JS logic bugs.

**To file:** write `~/.gstack/contributor-logs/{slug}.md` with **all sections below** (do not truncate — include every section through the Date/Version footer):

```
# {Title}

Hey gstack team — ran into this while using /{skill-name}:

**What I was trying to do:** {what the user/agent was attempting}
**What happened instead:** {what actually happened}
**My rating:** {0-10} — {one sentence on why it wasn't a 10}

## Steps to reproduce
1. {step}

## Raw output
```
{paste the actual error or unexpected output here}
```

## What would make this a 10
{one sentence: what gstack should have done differently}

**Date:** {YYYY-MM-DD} | **Version:** {gstack version} | **Skill:** /{skill}
```

Slug: lowercase, hyphens, max 60 chars (e.g. `browse-js-no-await`). Skip if file already exists. Max 3 reports per session. File inline and continue — don't stop the workflow. Tell user: "Filed gstack field report: {title}"

## Completion Status Protocol

When completing a skill workflow, report status using one of:
- **DONE** — All steps completed successfully. Evidence provided for each claim.
- **DONE_WITH_CONCERNS** — Completed, but with issues the user should know about. List each concern.
- **BLOCKED** — Cannot proceed. State what is blocking and what was tried.
- **NEEDS_CONTEXT** — Missing information required to continue. State exactly what you need.

### Escalation

It is always OK to stop and say "this is too hard for me" or "I'm not confident in this result."

Bad work is worse than no work. You will not be penalized for escalating.
- If you have attempted a task 3 times without success, STOP and escalate.
- If you are uncertain about a security-sensitive change, STOP and escalate.
- If the scope of work exceeds what you can verify, STOP and escalate.

Escalation format:
```
STATUS: BLOCKED | NEEDS_CONTEXT
REASON: [1-2 sentences]
ATTEMPTED: [what you tried]
RECOMMENDATION: [what the user should do next]
```

## Telemetry (run last)

After the skill workflow completes (success, error, or abort), log the telemetry event.
Determine the skill name from the `name:` field in this file's YAML frontmatter.
Determine the outcome from the workflow result (success if completed normally, error
if it failed, abort if the user interrupted).

**PLAN MODE EXCEPTION — ALWAYS RUN:** This command writes telemetry to
`~/.gstack/analytics/` (user config directory, not project files). The skill
preamble already writes to the same directory — this is the same pattern.
Skipping this command loses session duration and outcome data.

Run this bash:

```bash
_TEL_END=$(date +%s)
_TEL_DUR=$(( _TEL_END - _TEL_START ))
rm -f ~/.gstack/analytics/.pending-"$_SESSION_ID" 2>/dev/null || true
~/.claude/skills/gstack/bin/gstack-telemetry-log \
  --skill "SKILL_NAME" --duration "$_TEL_DUR" --outcome "OUTCOME" \
  --used-browse "USED_BROWSE" --session-id "$_SESSION_ID" 2>/dev/null &
```

Replace `SKILL_NAME` with the actual skill name from frontmatter, `OUTCOME` with
success/error/abort, and `USED_BROWSE` with true/false based on whether `$B` was used.
If you cannot determine the outcome, use "unknown". This runs in the background and
never blocks the user.

## Step 0: Detect base branch

Determine which branch this PR targets. Use the result as "the base branch" in all subsequent steps.

1. Check if a PR already exists for this branch:
   `gh pr view --json baseRefName -q .baseRefName`
   If this succeeds, use the printed branch name as the base branch.

2. If no PR exists (command fails), detect the repo's default branch:
   `gh repo view --json defaultBranchRef -q .defaultBranchRef.name`

3. If both commands fail, fall back to `main`.

Print the detected base branch name. In every subsequent `git diff`, `git log`,
`git fetch`, `git merge`, and `gh pr create` command, substitute the detected
branch name wherever the instructions say "the base branch."

---

# /codex — Multi-AI Second Opinion (with fallback chain)

You are running the `/codex` skill. This wraps multiple external AI tools to get an
independent, brutally honest second opinion from a different AI system.

**Fallback chain:** Codex CLI → OpenCode (GLM5 via Zen) → Claude subagent (Opus 4.6).
Each tool is tried in order; on failure, the next is attempted automatically.

The primary persona is the "200 IQ autistic developer" — direct, terse, technically
precise, challenges assumptions, catches things you might miss. Present output
faithfully, not summarized.

---

## Step 0: Check available tools

```bash
CODEX_BIN=$(which codex 2>/dev/null || echo "")
OPENCODE_BIN=$(which opencode 2>/dev/null || echo "")
[ -n "$CODEX_BIN" ] && echo "CODEX: FOUND" || echo "CODEX: NOT_FOUND"
[ -n "$OPENCODE_BIN" ] && echo "OPENCODE: FOUND" || echo "OPENCODE: NOT_FOUND"
echo "CLAUDE_SUBAGENT: ALWAYS_AVAILABLE"
```

Note which tools are available. **Do NOT stop here** — Claude subagent is always
available, so the skill can always run even if both CLIs are missing.

If both CLIs are missing, note: "Codex and OpenCode CLIs not found. Falling back to
Claude subagent. Install Codex: `npm install -g @openai/codex`"

---

## Fallback Chain Rules

**The chain:** Codex → OpenCode (GLM5) → Claude subagent

**A tool invocation has FAILED if ANY of these are true:**
- Binary not found (from Step 0)
- Auth/credits error: stderr or JSON output contains "auth", "login", "unauthorized",
  "Insufficient balance", "API key", "forbidden", "401", or "CreditsError"
- Timeout: Bash call exceeds 5 minutes (300000ms)
- Empty response: stdout is empty or whitespace-only after parsing
- Non-zero exit code (catch-all)

**Behavior:**
- Fallback is **automatic and silent** — do not ask the user before falling back
- On success at any tier, **stop** — do not continue down the chain
- Always **attribute output** to the tool that produced it (see Attribution section)
- After completion, print a **chain summary** showing what happened at each tier

**OpenCode model:** Use `opencode-go/glm-5` with `--variant max`. If GLM5 fails due to
credits, fall through to Claude subagent (Opus 4.6 is stronger than free-tier models).

---

## Step 1: Detect mode

Parse the user's input to determine which mode to run:

1. `/codex review` or `/codex review <instructions>` — **Review mode** (Step 2A)
2. `/codex challenge` or `/codex challenge <focus>` — **Challenge mode** (Step 2B)
3. `/codex` with no arguments — **Auto-detect:**
   - Check for a diff (with fallback if origin isn't available):
     `git diff origin/<base> --stat 2>/dev/null | tail -1 || git diff <base> --stat 2>/dev/null | tail -1`
   - If a diff exists, use AskUserQuestion:
     ```
     Codex detected changes against the base branch. What should it do?
     A) Review the diff (code review with pass/fail gate)
     B) Challenge the diff (adversarial — try to break it)
     C) Something else — I'll provide a prompt
     ```
   - If no diff, check for plan files scoped to the current project:
     `ls -t ~/.claude/plans/*.md 2>/dev/null | xargs grep -l "$(basename $(pwd))" 2>/dev/null | head -1`
     If no project-scoped match, fall back to: `ls -t ~/.claude/plans/*.md 2>/dev/null | head -1`
     but warn the user: "Note: this plan may be from a different project."
   - If a plan file exists, offer to review it
   - Otherwise, ask: "What would you like to ask Codex?"
4. `/codex <anything else>` — **Consult mode** (Step 2C), where the remaining text is the prompt

---

## Step 2A: Review Mode

Run code review against the current branch diff. Try each tier in order until one succeeds.

### Tier 1 — Codex

Skip if `CODEX: NOT_FOUND` from Step 0.

1. Create temp files:
```bash
TMPERR=$(mktemp /tmp/codex-err-XXXXXX.txt)
```

2. Run the review (5-minute timeout):
```bash
codex review --base <base> -c 'model_reasoning_effort="medium"' --enable web_search_cached 2>"$TMPERR"
```

Use `timeout: 300000` on the Bash call. If the user provided custom instructions
(e.g., `/codex review focus on security`), pass them as the prompt argument:
```bash
codex review "focus on security" --base <base> -c 'model_reasoning_effort="medium"' --enable web_search_cached 2>"$TMPERR"
```

3. Check for failure (see Fallback Chain Rules). Read stderr:
```bash
cat "$TMPERR" 2>/dev/null
```

If failed, note the reason (e.g., "Codex failed: auth error") and proceed to Tier 2.
Clean up: `rm -f "$TMPERR"`

4. If succeeded — parse cost from stderr:
```bash
grep "tokens used" "$TMPERR" 2>/dev/null || echo "tokens: unknown"
```

5. Determine gate verdict: `[P1]` present → **FAIL**. No `[P1]` → **PASS**.

6. Present output, then skip to "Post-review" section below.

### Tier 2 — OpenCode

Skip if `OPENCODE: NOT_FOUND` from Step 0.

```bash
opencode run "Review the changes on this branch against <base>. Run git diff origin/<base> to see the full diff. Perform a thorough code review. For each finding, classify as [P1] (critical - must fix) or [P2] (minor - should fix). Be direct and terse. No compliments — just the problems." --model opencode-go/glm-5 --format json --variant max 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        t = obj.get('type','')
        if t == 'text':
            p = obj.get('part',{})
            text = p.get('text','')
            if text: print(text)
        elif t == 'error':
            err = obj.get('error',{})
            msg = err.get('data',{}).get('message','') or str(err)
            print(f'OPENCODE_ERROR: {msg}', file=sys.stderr)
        elif t == 'step_finish':
            p = obj.get('part',{})
            tokens = p.get('tokens',{})
            total = tokens.get('total',0)
            if total: print(f'\ntokens used: {total}')
    except: pass
"
```

Use `timeout: 300000`. If output contains `OPENCODE_ERROR` or is empty, note the
failure reason and proceed to Tier 3.

If succeeded — determine gate verdict same as Tier 1, present output, skip to "Post-review."

### Tier 3 — Claude subagent

Dispatch via the Agent tool:

```
subagent_type: "feature-dev:code-reviewer"
prompt: "Read the diff for this branch with `git diff origin/<base>`. Perform a thorough
code review. For each finding, classify as [P1] (critical - must fix before merge) or
[P2] (minor - should fix but not blocking). Check for: bugs, race conditions, security
issues, error handling gaps, performance problems, and API misuse. Be direct and terse.
No compliments — just the problems."
```

If the subagent also fails: report `STATUS: BLOCKED` with what was tried at each tier.

### Post-review (after any tier succeeds)

1. Present the output with attribution (see Attribution section).

2. **Cross-model comparison:** If `/review` (Claude's own review) was already run
   earlier in this conversation, compare the two sets of findings:

```
CROSS-MODEL ANALYSIS:
  Both found: [findings that overlap]
  Only <source> found: [findings unique to the tool that ran]
  Only Claude found: [findings unique to Claude's /review]
  Agreement rate: X% (N/M total unique findings overlap)
```

3. Persist the review result:
```bash
~/.claude/skills/gstack/bin/gstack-review-log '{"skill":"codex-review","timestamp":"TIMESTAMP","status":"STATUS","gate":"GATE","findings":N,"findings_fixed":N,"source":"SOURCE"}'
```

Substitute: TIMESTAMP (ISO 8601), STATUS ("clean" if PASS, "issues_found" if FAIL),
GATE ("pass" or "fail"), findings (count of [P1] + [P2] markers),
findings_fixed (count of findings that were addressed/fixed before shipping).
SOURCE: "codex", "opencode", or "claude-subagent".

4. Clean up temp files:
```bash
rm -f "$TMPERR"
```

5. Print the fallback chain summary.

## Plan File Review Report

After displaying the Review Readiness Dashboard in conversation output, also update the
**plan file** itself so review status is visible to anyone reading the plan.

### Detect the plan file

1. Check if there is an active plan file in this conversation (the host provides plan file
   paths in system messages — look for plan file references in the conversation context).
2. If not found, skip this section silently — not every review runs in plan mode.

### Generate the report

Read the review log output you already have from the Review Readiness Dashboard step above.
Parse each JSONL entry. Each skill logs different fields:

- **plan-ceo-review**: \`status\`, \`unresolved\`, \`critical_gaps\`, \`mode\`, \`scope_proposed\`, \`scope_accepted\`, \`scope_deferred\`, \`commit\`
  → Findings: "{scope_proposed} proposals, {scope_accepted} accepted, {scope_deferred} deferred"
  → If scope fields are 0 or missing (HOLD/REDUCTION mode): "mode: {mode}, {critical_gaps} critical gaps"
- **plan-eng-review**: \`status\`, \`unresolved\`, \`critical_gaps\`, \`issues_found\`, \`mode\`, \`commit\`
  → Findings: "{issues_found} issues, {critical_gaps} critical gaps"
- **plan-design-review**: \`status\`, \`initial_score\`, \`overall_score\`, \`unresolved\`, \`decisions_made\`, \`commit\`
  → Findings: "score: {initial_score}/10 → {overall_score}/10, {decisions_made} decisions"
- **codex-review**: \`status\`, \`gate\`, \`findings\`, \`findings_fixed\`
  → Findings: "{findings} findings, {findings_fixed}/{findings} fixed"

All fields needed for the Findings column are now present in the JSONL entries.
For the review you just completed, you may use richer details from your own Completion
Summary. For prior reviews, use the JSONL fields directly — they contain all required data.

Produce this markdown table:

\`\`\`markdown
## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | \`/plan-ceo-review\` | Scope & strategy | {runs} | {status} | {findings} |
| Codex Review | \`/codex review\` | Independent 2nd opinion | {runs} | {status} | {findings} |
| Eng Review | \`/plan-eng-review\` | Architecture & tests (required) | {runs} | {status} | {findings} |
| Design Review | \`/plan-design-review\` | UI/UX gaps | {runs} | {status} | {findings} |
\`\`\`

Below the table, add these lines (omit any that are empty/not applicable):

- **CODEX:** (only if codex-review ran) — one-line summary of codex fixes
- **CROSS-MODEL:** (only if both Claude and Codex reviews exist) — overlap analysis
- **UNRESOLVED:** total unresolved decisions across all reviews
- **VERDICT:** list reviews that are CLEAR (e.g., "CEO + ENG CLEARED — ready to implement").
  If Eng Review is not CLEAR and not skipped globally, append "eng review required".

### Write to the plan file

**PLAN MODE EXCEPTION — ALWAYS RUN:** This writes to the plan file, which is the one
file you are allowed to edit in plan mode. The plan file review report is part of the
plan's living status.

- Search the plan file for a \`## GSTACK REVIEW REPORT\` section **anywhere** in the file
  (not just at the end — content may have been added after it).
- If found, **replace it** entirely using the Edit tool. Match from \`## GSTACK REVIEW REPORT\`
  through either the next \`## \` heading or end of file, whichever comes first. This ensures
  content added after the report section is preserved, not eaten. If the Edit fails
  (e.g., concurrent edit changed the content), re-read the plan file and retry once.
- If no such section exists, **append it** to the end of the plan file.
- Always place it as the very last section in the plan file. If it was found mid-file,
  move it: delete the old location and append at the end.

---

## Step 2B: Challenge (Adversarial) Mode

Try to break your code — finding edge cases, race conditions, security holes,
and failure modes that a normal review would miss.

1. Construct the adversarial prompt. If the user provided a focus area
(e.g., `/codex challenge security`), include it:

Default prompt (no focus):
"Review the changes on this branch against the base branch. Run `git diff origin/<base>` to see the diff. Your job is to find ways this code will fail in production. Think like an attacker and a chaos engineer. Find edge cases, race conditions, security holes, resource leaks, failure modes, and silent data corruption paths. Be adversarial. Be thorough. No compliments — just the problems."

With focus (e.g., "security"):
"Review the changes on this branch against the base branch. Run `git diff origin/<base>` to see the diff. Focus specifically on SECURITY. Your job is to find every way an attacker could exploit this code. Think about injection vectors, auth bypasses, privilege escalation, data exposure, and timing attacks. Be adversarial."

2. Try each tier in order:

### Tier 1 — Codex

Skip if `CODEX: NOT_FOUND` from Step 0.

Run codex exec with `--dangerously-bypass-approvals-and-sandbox` and **JSONL output** (5-minute timeout):
```bash
codex exec "<prompt>" --dangerously-bypass-approvals-and-sandbox -c 'model_reasoning_effort="medium"' --enable web_search_cached --json 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        t = obj.get('type','')
        if t == 'item.completed' and 'item' in obj:
            item = obj['item']
            itype = item.get('type','')
            text = item.get('text','')
            if itype == 'reasoning' and text:
                print(f'[codex thinking] {text}')
                print()
            elif itype == 'agent_message' and text:
                print(text)
            elif itype == 'command_execution':
                cmd = item.get('command','')
                if cmd: print(f'[codex ran] {cmd}')
        elif t == 'turn.completed':
            usage = obj.get('usage',{})
            tokens = usage.get('input_tokens',0) + usage.get('output_tokens',0)
            if tokens: print(f'\ntokens used: {tokens}')
    except: pass
"
```

**Note:** `--dangerously-bypass-approvals-and-sandbox` gives Codex full filesystem
access. This is intentional for adversarial analysis — Codex needs to run git diff,
read files, and potentially run tests. The Genesis container is the sandbox boundary.

If failed (empty output, non-zero exit, auth error), proceed to Tier 2.

### Tier 2 — OpenCode

Skip if `OPENCODE: NOT_FOUND` from Step 0.

```bash
opencode run "<same adversarial prompt>" --model opencode-go/glm-5 --format json --variant max 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        t = obj.get('type','')
        if t == 'text':
            p = obj.get('part',{})
            text = p.get('text','')
            if text: print(text)
        elif t == 'error':
            err = obj.get('error',{})
            msg = err.get('data',{}).get('message','') or str(err)
            print(f'OPENCODE_ERROR: {msg}', file=sys.stderr)
        elif t == 'step_finish':
            p = obj.get('part',{})
            tokens = p.get('tokens',{})
            total = tokens.get('total',0)
            if total: print(f'\ntokens used: {total}')
    except: pass
"
```

If failed, proceed to Tier 3.

### Tier 3 — Claude subagent

Dispatch via the Agent tool:

```
subagent_type: "general-purpose"
prompt: "Read the diff for this branch with `git diff origin/<base>`. Your job is to find
ways this code will fail in production. Think like an attacker and a chaos engineer. Find
edge cases, race conditions, security holes, resource leaks, failure modes, and silent
data corruption paths. Be adversarial. Be thorough. No compliments — just the problems.
For each finding, classify as FIXABLE (can be addressed now) or INVESTIGATE (needs more
analysis). This is research only — do NOT modify any files."
```

If all three tiers fail: `STATUS: BLOCKED`.

3. Present the full output with attribution (see Attribution section).
4. Print the fallback chain summary.

---

## Step 2C: Consult Mode

Ask anything about the codebase. Supports session continuity for follow-ups.

1. **Check for existing session:**
```bash
cat .context/codex-session-id 2>/dev/null || echo "NO_SESSION"
```

If a session file exists (not `NO_SESSION`), parse the tool name from it.
Use AskUserQuestion:
```
You have an active conversation from earlier (via <tool>). Continue it or start fresh?
A) Continue the conversation (remembers the prior context)
B) Start a new conversation
```

2. Create temp files:
```bash
TMPERR=$(mktemp /tmp/codex-err-XXXXXX.txt)
```

3. **Plan review auto-detection:** If the user's prompt is about reviewing a plan,
or if plan files exist and the user said `/codex` with no arguments:
```bash
ls -t ~/.claude/plans/*.md 2>/dev/null | xargs grep -l "$(basename $(pwd))" 2>/dev/null | head -1
```
If no project-scoped match, fall back to `ls -t ~/.claude/plans/*.md 2>/dev/null | head -1`
but warn: "Note: this plan may be from a different project — verify before sending."
Read the plan file and prepend the persona to the user's prompt:
"You are a brutally honest technical reviewer. Review this plan for: logical gaps and
unstated assumptions, missing error handling or edge cases, overcomplexity (is there a
simpler approach?), feasibility risks (what could go wrong?), and missing dependencies
or sequencing issues. Be direct. Be terse. No compliments. Just the problems.

THE PLAN:
<plan content>"

4. Try each tier in order:

### Tier 1 — Codex

Skip if `CODEX: NOT_FOUND` from Step 0.

For a **new session:**
```bash
codex exec "<prompt>" -s read-only -c 'model_reasoning_effort="medium"' --enable web_search_cached --json 2>"$TMPERR" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        t = obj.get('type','')
        if t == 'thread.started':
            tid = obj.get('thread_id','')
            if tid: print(f'SESSION_ID:{tid}')
        elif t == 'item.completed' and 'item' in obj:
            item = obj['item']
            itype = item.get('type','')
            text = item.get('text','')
            if itype == 'reasoning' and text:
                print(f'[codex thinking] {text}')
                print()
            elif itype == 'agent_message' and text:
                print(text)
            elif itype == 'command_execution':
                cmd = item.get('command','')
                if cmd: print(f'[codex ran] {cmd}')
        elif t == 'turn.completed':
            usage = obj.get('usage',{})
            tokens = usage.get('input_tokens',0) + usage.get('output_tokens',0)
            if tokens: print(f'\ntokens used: {tokens}')
    except: pass
"
```

For a **resumed session** (user chose "Continue" and prior tool was codex):
```bash
codex exec resume <session-id> "<prompt>" -s read-only -c 'model_reasoning_effort="medium"' --enable web_search_cached --json 2>"$TMPERR" | python3 -c "
<same python streaming parser as above>
"
```

If failed, proceed to Tier 2.

### Tier 2 — OpenCode

Skip if `OPENCODE: NOT_FOUND` from Step 0.

```bash
opencode run "<prompt>" --model opencode-go/glm-5 --format json --variant max 2>/dev/null | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
        t = obj.get('type','')
        if t == 'text':
            p = obj.get('part',{})
            text = p.get('text','')
            if text: print(text)
        elif t == 'error':
            err = obj.get('error',{})
            msg = err.get('data',{}).get('message','') or str(err)
            print(f'OPENCODE_ERROR: {msg}', file=sys.stderr)
        elif t == 'step_finish':
            p = obj.get('part',{})
            sid = obj.get('sessionID','')
            if sid: print(f'SESSION_ID:{sid}')
            tokens = p.get('tokens',{})
            total = tokens.get('total',0)
            if total: print(f'\ntokens used: {total}')
    except: pass
"
```

For a **resumed session** (prior tool was opencode):
```bash
opencode run "<prompt>" --model opencode-go/glm-5 --format json --variant max --session <session-id> --continue 2>/dev/null | python3 -c "
<same OpenCode parser as above>
"
```

If failed, proceed to Tier 3.

### Tier 3 — Claude subagent

Dispatch via the Agent tool:

```
subagent_type: "general-purpose"
prompt: "<the user's prompt>"
```

Claude subagent has no session continuity across invocations.

5. Save session info for follow-ups:
```bash
mkdir -p .context
echo "TOOL:<tool-that-succeeded> SESSION_ID:<id-if-available>" > .context/codex-session-id
```

If the tool that succeeded was Claude subagent, write `TOOL:claude-subagent SESSION_ID:none`.

6. Present the full output with attribution (see Attribution section).

7. After presenting, note any points where the tool's analysis differs from your own
   understanding. If there is a disagreement, flag it:
   "Note: Claude Code disagrees on X because Y."

8. Clean up temp files:
```bash
rm -f "$TMPERR"
```

9. Print the fallback chain summary.

---

## Attribution

Always present output with clear attribution showing which tool produced the result.

**When primary tool (Codex) succeeds:**
```
CODEX SAYS (<mode>):
════════════════════════════════════════════════════════════
<full output, verbatim — do not truncate or summarize>
════════════════════════════════════════════════════════════
Source: Codex CLI | Tokens: N
```

**When fallback to OpenCode:**
```
OPENCODE SAYS (<mode> — codex <reason>):
════════════════════════════════════════════════════════════
<full output, verbatim>
════════════════════════════════════════════════════════════
Source: OpenCode (glm-5 via Zen) | Codex failed: <reason>
```

**When fallback to Claude subagent:**
```
CLAUDE SUBAGENT SAYS (<mode> — last resort fallback):
════════════════════════════════════════════════════════════
<full output, verbatim>
════════════════════════════════════════════════════════════
Source: Claude subagent (Opus 4.6) | Codex failed: <reason> | OpenCode failed: <reason>
```

**Chain summary (always print at end):**
```
FALLBACK CHAIN: Codex (<status>) → OpenCode (<status>) → Claude subagent (<status>)
```

Where status is one of: SUCCESS, FAILED: <reason>, SKIPPED (binary not found), NOT_NEEDED.

---

## Model & Reasoning

**Codex model:** No model is hardcoded — codex uses whatever its current default is (the
frontier agentic coding model). If the user wants a specific model, pass `-m` through.

**OpenCode model:** `opencode-go/glm-5` with `--variant max` (maximum reasoning effort).

**Reasoning effort:** All Codex modes use `xhigh` — maximum reasoning power.

**Web search:** All codex commands use `--enable web_search_cached` so Codex can look up
docs and APIs during review. This is OpenAI's cached index — fast, no extra cost.

If the user specifies a model (e.g., `/codex review -m gpt-5.1-codex-max`
or `/codex challenge -m gpt-5.2`), pass the `-m` flag through to codex.

---

## Cost Estimation

**Codex:** Parse token count from stderr. Display as: `Tokens: N`
**OpenCode:** Parse from `step_finish` event's `tokens.total` field.
**Claude subagent:** No token count available. Display: `Tokens: (subagent — not tracked)`

If token count is not available, display: `Tokens: unknown`

---

## Error Handling

Errors at any tier trigger fallback to the next tier (see Fallback Chain Rules).
Only report `STATUS: BLOCKED` when ALL three tiers fail.

Common errors:
- **Auth error:** "Codex authentication failed. Run `codex login` to authenticate."
- **Credits error (OpenCode):** "OpenCode Zen insufficient balance. Falling back."
- **Timeout:** Tool timed out after 5 minutes.
- **Empty response:** Tool returned no response.
- **Session resume failure:** Delete the session file and start fresh.

---

## Important Rules

- **Challenge mode has full sandbox bypass.** Codex runs with
  `--dangerously-bypass-approvals-and-sandbox` in challenge mode only. Review and
  Consult modes remain read-only (`-s read-only` for Codex).
- **Present output verbatim.** Do not truncate, summarize, or editorialize output
  before showing it. Show it in full inside the attribution block.
- **Add synthesis after, not instead of.** Any Claude commentary comes after full output.
- **5-minute timeout** on all Bash calls (`timeout: 300000`).
- **Fallback is automatic.** Do not ask the user before falling back to the next tier.
- **Always attribute.** Every output block must clearly state which tool produced it.
- **No double-reviewing.** If the user already ran `/review`, this provides a second
  independent opinion. Do not re-run Claude Code's own review.
