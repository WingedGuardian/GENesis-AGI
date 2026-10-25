---
name: genesis-development
description: >
  This skill should be used when developing, debugging, refactoring, or
  building Genesis itself — tasks like "fix this in Genesis", "add a new
  MCP tool", "wire up the runtime", "Genesis won't start", "create a
  worktree", "debug the bridge", or "add a capability". Applies to any
  task modifying files under src/, .claude/, or tests/. Do NOT load for
  Genesis-as-tool work ("summarize this", "write a LinkedIn post",
  "research X") or general questions unrelated to Genesis internals.
keywords: [genesis, worktree]
consumer: cc_foreground
phase: 10
skill_type: workflow
---

## Load Gate

Before reading any reference, confirm the task is Genesis-*development*,
not Genesis-*as-tool*. If uncertain, ask the user: "Are we modifying
Genesis itself, or using Genesis for something else?"

## Which session are you? Load that skill now

This is the shared core. Each session type has its own skill: load it with the
Skill tool, or Read its file if this text reached you by injection.

- **Spec**, shaping a change before anyone plans it: `spec-session`
  (`.claude/skills/spec-session/SKILL.md`).
- **Build**, one item from Ready to its open PR, including rework from a
  send-back spec: `build-session` (`.claude/skills/build-session/SKILL.md`).
- **Close**, driving open PRs to merge and fixing their review findings:
  `closing-session` (`.claude/skills/closing-session/SKILL.md`).
- **Validate**, post-merge verification, deploy and E2E: `validating-merges`
  (`.claude/skills/validating-merges/SKILL.md`).

Decide from the owner's words, the task's shape, or the dispatcher's prompt;
none fits (debugging the runtime, a one-off fix)? Load `build-session`. Two
types (spec, then build) load both, in order. Build and close sessions Read
`.claude/skills/genesis-development/references/commit-path.md` before their
first commit.

## On-Load Mindset

Internalize these immediately when this skill fires — they shape how to
work from the start, not just what to check before commit.

### PR readiness and mode

**Finish the work and clear blockers to entering review BEFORE opening a PR.**
Complete the implementation, required local review, and verification required
before publication. If unfinished work or an unresolved prerequisite prevents
normal review from proceeding, keep the change local, resolve the blocker, and
then open its PR. A draft PR is not a place to park incomplete or blocked work.

**A session opens every PR in regular, non-draft mode.** Never create a PR with
`--draft`. The exception is the runtime's own automated openers, which open
drafts by design: the executor's build lane (`autonomy/executor/pr_open.py`) and
the contribution opener (`contribution/pr_opener.py`).

**Moving an open PR to draft** is for a blocker to review that appeared AFTER it
opened (a prerequisite found missing, a design question raised in review); it
needs no explicit user request. It is never a way to hold work that was already
unfinished when the PR opened.

**A PR in draft returns to regular once the blocker that put it there is
resolved.** That holds for drafts opened before this rule. A draft from one of
the automated openers is different: it has no blocker to clear, neither opener
ever marks a PR ready, and the closing session counts and lists drafts without
closing them, so marking it ready is the owner's decision, which the closing
session's draft list is there to surface. Whoever resolves a blocker marks the
PR ready (`gh pr ready <N>`) and requests review (`@codex review`), because
marking a draft ready is not a step to rely on for triggering review. A draft
is never abandoned in that state.

CI and external review that run after opening, and verification explicitly
required after merge, remain their normal gates; waiting for those is not a
reason to use draft mode. Distinguish a blocker to entering review from a check
that review or merge will subsequently require. Open review findings, and a
dependency on another open PR named in the body, are of the second kind. A PR
sent BACK for rework or for an architecture decision is of the first kind,
because review stops until the rework lands (closing-session, "Sending a PR
back").

### Measure, Do Not Choose

**If your reason for picking a command, a flag, a value or a procedure is a claim
about how something OUTSIDE this repo behaves — git, the shell, the harness, a
provider API — then "which of these is right?" is an experiment you have not run,
not a judgement you are entitled to make.** Run it first. Ship what the run says.

The tripwire is mechanical, so it cannot be reasoned around: you are about to write
an instruction someone will execute, and your justification is a sentence about
another program's semantics that you did not observe. Reading the manual feels like
evidence. It is not.

Three clauses, each bought by a defect:

1. **Enumerate the space; do not pick cases from it.** List the AXES that
   independently change the behaviour, sweep the cross product, score every
   candidate on every cell. The cases you would think of are the cases you already
   believe in, which is exactly why they pass.
2. **Pre-register the predicate, the decision rule, and what you will do if
   nothing passes.** That last clause is what stops the least-bad option being
   rationalised into the right one.
3. **Control the instrument.** An ORACLE arm that must score 100% and a NO-OP arm
   that must fail. If the oracle is not perfect, every number in the run is void —
   including the flattering ones. Treat a surprising result as a suspicion about
   the harness before it is a finding about the code.

MEASURED instance: a hook note telling a reader how to undo a destructive git
operation was wrong FOUR times, each version reasoned out and each refuted by a
state nobody had constructed. The fifth was swept rather than chosen — 320 states x
6 procedures with an oracle and a no-op. Of the 240 states where the reader's HEAD
had not moved, the winner scored 240/240; the best alternative managed 80, and the
procedure that had actually SHIPPED managed 56. The sweep also surfaced a boundary
no reasoning had: that same winner scores 0/80 once the reader has committed, where
a different command gets 72. Twenty minutes of compute against three review rounds
of guessing.

Full method, including how to split a space where the question stops being
well-posed: `references/high-stakes-verification.md` section 9.

**And where you cannot measure, READ THE AUTHORITATIVE SOURCE — never your own
recollection of it.** Measurement is the top of the ladder; this is the rung
below, and the one most often skipped because recall feels like knowledge. For
any claim about how something OUTSIDE this repo behaves — a flag's semantics, an
exit code's meaning, an API's contract, a shell builtin's edge case — the order
is MEASURE > the authoritative document (the vendor's own reference, the man
page, the spec) > everything else. Pretraining recall is not a source. It is a
hypothesis phrased confidently, and it is wrong most often exactly where a
flag's NAME supports the assumption everyone makes about it.

READ, from an external audit of a sibling toolkit (2026-09): its CI documented
`uv --frozen` as the check that fails when the lockfile has drifted from the
project file. It is not. Per the vendor's own reference — uv docs, "Locking and
syncing", consulted 2026-09-15 — `--frozen` uses the lockfile as the source of
truth *instead of* checking whether it is up to date, while `--locked` is the
flag that "requires that the lockfile is up-to-date" and errors when it is not.
(That citation is this passage obeying its own rule: an earlier draft asserted
the same thing from recall and cited nothing, which an adversarial review
caught.) The claim was plausible, the flag name invites the misreading, and
nothing in that pipeline could ever have contradicted it — the verification
everyone believed was running simply was not, and a green run said so every
time.

The habit that prevents it costs one lookup: when you write an instruction, a
CI step, or a code comment that names an external tool's flag, open that tool's
documentation in the same minute, and cite what you consulted so the next reader
re-checks rather than re-derives. "I am fairly sure that flag means X" is the
sentence to catch yourself in.

### Due diligence before building (standing owner rule, 2026-10-04)

**Get it right the first time. On everything that could change the plan, the
bar is beyond reasonable doubt BEFORE building, not after review finds it.** A
PR that comes out half-built because half its premises were never checked is
the failure this rule exists to stop. The checklist, which every development
plan presented for approval answers (task-executor plans from `/task` keep their
own `TASK_INTAKE.md` section contract instead):

1. **Every free read is done.** A free read is anything this session can settle
   read-only, or by a probe that changes nothing beyond scratch state, using
   tools it already holds. It is owed whenever its answer could change a decision. That covers: the code and every caller the change touches,
   its `CURRENT.md` entry, recent commits and PRs in the area, overlapping open
   PRs and issues, existing implementations outside the repo (public repos,
   grep.app, and the Claude Code mods catalogue for anything that runs inside
   a CC session), peer sessions, code intelligence, an API or renderer probe,
   and a corpus measurement. "I did not verify X, but it should be fine" is not
   an answer for an X like that: verify it, or name it as a residual with the
   reason no read or probe settles it.
2. **Premises measured, then red-teamed.** Try to disprove the root cause and the
   change's consequences, and name the alternatives weighed, simplest first.
3. **The plan body states:** what, why and how; confidence per item with what
   would disprove it; what was verified and what was NOT; alternatives; the
   red-team of the root cause and consequences; regression markers; and the
   test plan (targeted tests midway and at the end, CI for the full suite, a
   functional check of the built thing, "Verify BEFORE the PR" (build-session), and the
   `E2E:` line for post-merge verification).
4. **A revised plan opens with a "Changed in this revision" section** right
   after its title (YAML frontmatter stays on line 1, and the title stays the
   first heading): what changed, and why.
5. **Ask the owner about intent, priorities and trade-offs. Decide measured
   technical edge cases yourself** and state the call; the owner overrides at
   approval. Design principles and standing axioms, approval and sovereignty
   gates, irreversible or outward-facing actions, and anything user-visible
   still go to the owner.

A question is material when its answer could change the plan; stakes set how
deep to go on each one, never whether it is asked. On the material ones, this
bar is higher than CLAUDE.md's investigate-below-90% floor. Detail, the
evidence behind the rule, and how it fits the rules it ties together:
`references/due-diligence.md`.

### Architecture Review

Every FINALIZED plan gets exactly ONE `genesis-architect` review — premise
check, scope drift, architecture — before it is presented for approval.
The agent's Step 0.5/0.6 take the plan file as input; hand it the path.
Revisions made in answer to that review do not re-trigger it; the one
exception is a part whose design the plan deferred (below), which gets its own
review once that design is written. A plan too small to write down needs none. `/plan-ceo-review` and `/office-hours` are
optional extras, if installed. Its findings are claims, handled under
CLAUDE.md's "Verify agent output": re-derive them, then fold the confirmed ones
into the plan BEFORE presenting it.

**A plan part that defers its own design is not finalized.** "Read X first, then
design it" is a placeholder, and a review of the plan reviews the placeholder.
Before coding that part, write its design, and give the part its own architect
review. For anything that persists state another program reads, the design
itself carries the writer-state × reader table: every state the writer can
leave (complete, skipped, partial, failed midway, killed, a leftover from an
earlier run) against every reader, with what each reader does in each cell.
Hand that table to the architect and ask it to hunt for the missing cells.
(genesis-architect Step 0.7 covers the lifecycle of state inside a guard or
gate only, so it does not do this for you.)
(Origin: PR #2853 — the plan reviewed once, its backup part was a placeholder,
and ten of 25 external findings over five rounds were cells of the
backup/restore state table nobody had written down.)

### The Bash tool's own timeout

**The Bash TOOL's own timeout is separate from the Timeout Policy (build-session) — default 120000ms, HARD CEILING
600000ms (10 min).** An inner `timeout N …` INSIDE the command does NOT extend it:
the tool wrapper SIGTERMs the whole call at its own `timeout` param (default
120000ms → exit 143). To allow longer, set that `timeout` PARAMETER explicitly —
**but you cannot exceed 600000ms.** A larger value does not buy more time; the
call still dies at 10 minutes (MEASURED 2026-08-27: `timeout: 1600000` was killed
at exactly `10m 0s`). Treat "raise the timeout" as a fix that tops out, not one
that scales.

For long or unbounded work generally, use **`run_in_background: true`** —
harness-tracked, notifies on completion, and it does NOT inherit the 120s default
(MEASURED 2026-09-16: a 400s background task completed clean, exit 0). Prefer it
over a foreground timeout or `nohup … &` (detached but untracked → no completion
signal, so you end up hand-polling anyway).

Deploys are the exception: detach them, never `run_in_background`
(`.claude/skills/validating-merges/SKILL.md`, "Deploys: detach `update.sh`
from the session").

### Size defaults are never about secrecy

The truncation default (build-session, "Select, don't amputate") is about SIZE, never about secrecy, and the two must not be
confused. If a value may carry a credential, token, or personal data, the
unbounded default does NOT apply — for UNAUTHORIZED egress and for storage you
cannot vouch for: there, fail closed, omit wholesale with a marker as the "omit
explicitly" rule (build-session, "Select, don't amputate") says, and keep only non-sensitive metadata. Losing diagnostic
prose is recoverable; leaking a token is not. The scope qualifier is
load-bearing and an earlier revision dropped it while restructuring — read
unconditionally, "omit wholesale" would delete values existing features exist
to hold: the References store deliberately RETAINS credentials, and its own
dashboard tab masks the value behind an explicit reveal
(`dashboard/routes/references.py`; verified 2026-09-04). That is a statement
about ONE surface, not a security guarantee — other readers of the same store
return raw bodies (`reference_lookup`, the knowledge routes), which is worth
knowing precisely because a rule reader might otherwise lean on the reveal
gate as if it covered them. An authorized, gated store holding a secret is the
feature working, not a leak.

**Then ask WHO WROTE IT as well as where it is going — two independent axes,
and each governs a different decision.** Destination governs DISCLOSURE: what
may cross a trust boundary is decided by where it lands, whoever wrote it — a
user-authored secret bound for an external channel still gets scanned — and
quarantined when a SUPPORTED pattern matches. Say that precisely, because the
scanner is a high-confidence pattern gate by its own declaration, not a general
secret filter: content carrying a secret outside its pattern set passes as
safe, so nothing downstream may lean on that gate as proof of cleanliness
(`security/output_scanner.py`; verified 2026-09-05). Authorship is a RISK INPUT to sanitization, and the treatment
itself is chosen by the CONSUMING SINK: the same stranger-authored email
fields are HTML-escaped and truncated where the sink renders raw HTML
(`outreach/engagement.py` `_sanitize_ping_field`, whose docstring names both
the threat and the destination; verified 2026-09-04) and stored UNESCAPED
where the sink is a parameterised database column (`db/crud/email_threads.py`
`record_reply`; verified 2026-09-04). Unescaped is not unbounded, and an
earlier draft conflated them: what reaches that column is `body_preview`,
already cut to 500 characters upstream (`mail/reply_poller.py`,
`parsed.body[:500]`) — a silent mid-value cut of exactly the kind "Select, don't amputate" (build-session)
exists to surface, sitting inside the example chosen to demonstrate the
opposite. The escaping point stands; the "canonical, unbounded" flourish did
not survive following its own pointer. Escaping at
ingestion would corrupt the stored copy; passing raw markup to a live
renderer hands a stranger the user's most trusted channel. Maximally
authorized destination, maximally defensive treatment AT THE RENDERER — any
rule that reads "it's going somewhere trusted, so pass it whole" deletes that
defence, and any rule that reads "a stranger wrote it, so escape it
everywhere" corrupts the stored original.

**And an authorized destination does not mean untreated.** The rule is narrow:
do not strip the USER'S OWN content on its way to the user. It is not a licence
to stop scrubbing on owner-facing surfaces, and this repo does not (each
verified 2026-09-04): it rewrites username-bearing paths out of what the owner's
own dashboard renders (`dashboard/routes/backup.py` `_scrub_reason`), gates
credential values behind an explicit reveal rather than showing them (the
References tab — that one surface, per the caveat above), applies the safe
mechanical rewrite to the draft the user reviews (`content/egress.py`
`should_gate`, `category == "content"` — non-fixable tells are FLAGGED, not
removed, so the copy they approve has been treated, not certified clean), and
in at least one subsystem deliberately keeps captured text out of its own
private store entirely (`attention/types.py`: "never stored";
`db/crud/attention.py`: "value-free … NO text"). "Into a private store" is
emphatically not a blanket exemption; some stores are built specifically to
never receive the value.

### Code Intelligence

CLAUDE.md "Code Intelligence" picks the tool by the question. The decision
matrix and freshness model: `.claude/docs/code-intelligence.md`; syntax, the
`CodeRelation` edge model and Cypher examples: `.claude/docs/code-intelligence-guide.md`.

### Anti-Rationalization

These are excuses sessions use to skip discipline. If you catch yourself
thinking any of these, STOP — you are rationalizing a shortcut.

| Rationalization | Why it's wrong |
|---|---|
| "This is just a simple fix, no tests needed" | Simple fixes break complex systems. The Qdrant regression was a "simple fix." Write the test. |
| "I'll clean this up in the next commit" | Next commit never comes in autonomous sessions. Do it now, or file it — Genesis-repo work is a GitHub issue, not a local row. |
| "This file is too large to read fully" | Read the relevant section. Partial reads lead to partial understanding and wrong fixes. |
| "The linter is happy, ship it" | Linters catch syntax, not logic. Clean lint with broken behavior is worse than a warning with correct behavior. |
| "This change is low-risk, no impact analysis needed" | Your confidence is based on what you know; checking callers reveals what you don't. Serena `find_referencing_symbols` is live — run it. For multi-hop blast radius from the main checkout, run `scripts/lib/code_intel_index.sh "$PWD" gitnexus fast`, then `impact`; in a linked worktree, use Serena for live branch truth. |
| "I can skip the worktree, I'll be quick" | Concurrent session safety exists because "quick" commits have destroyed work before. Always worktree. |
| "The error is transient, retry will fix it" | Diagnose first. Retrying a misdiagnosed error wastes tokens and masks root causes. |
| "I'll add the follow-up later" | Records not created in-session are lost. File it now while context is fresh — Genesis-repo work as a GitHub issue, user-owned work as a follow-up. |
| "It's a separate PR, so it's for another session" | A separate PR never means a separate session. In-scope work waits only on one of the three grounds in CLAUDE.md "Where deferred work goes" (blocked on an unmet precondition, gated on time or data, big enough to derail the session) or the user's direction (#1721). |
| "I don't need a skill for this" | If a skill exists, use it. The using-superpowers Red Flags table exists for this exact rationalization. |
| "This review round is the same class, it doesn't really count" | The budget decides, not you: a head that drew findings is a round whatever its class. Read the count from `review_budget.py`, and commit the round's reflection before its fixes. (Internal same-model reviews are never rounds.) |
| "The user already said proceed, so I can keep looping" | The round and fix-attempt caps CONSUME standing approval. Going past the lane's terminal round (or fix #4) on an old instruction is a violation, not obedience. |
| "I'll just add the field the reviewer flagged" | A spec's required-set is closed — derive and enforce ALL of it at once, with one test locking the whole set, or the next round finds the next missing field (see build-session, Debugging Discipline: spec required-sets). |

### The review chain: a different model is the gate

- **A different model is the real correctness gate; Codex is the default.** A Claude
  reviewer shares this model's blind spots, so it clears the LOCAL depth gate but is not
  the cross-model gate. When the GitHub Codex reviewer is unavailable AND the install
  has an approved alternative external reviewer — a DIFFERENT model, with explicit
  per-use user approval every time — run it non-interactively over the diff with an
  adversarial mandate. Which reviewer that is (if any) is install-local and belongs in
  user-level config, not here.
  **Unavailable is established by ASKING**: comment `@codex review`, wait, and read the
  reply. An explicit usage-limits comment is the STRONGEST evidence available — but it
  is not proof, and treating it as proof is how an alternate-reviewer approval gets
  spent on a Codex that was never down. The two channels are INDEPENDENT: a
  usage-limits ISSUE comment can sit there while a later trigger delivers real INLINE
  findings (MEASURED on #1484 — see the Pre-Merge Gate in closing-session). So: re-trigger,
  let time pass, and check the INLINE endpoint before concluding unavailable.
  A second shape carries the same weight and the same caveat: an explicit
  REVIEW-FAILURE reply that REPRODUCES. The bot answering `Codex Review: Something went
  wrong` names its own cause, and a cause you can DISPROVE locally is the strongest
  reading available — MEASURED 2026-09-09 on two PRs, twice each 43 minutes apart,
  `Provided git ref <sha> does not exist` while `git ls-remote origin refs/heads/<branch>`
  returned that exact sha, and two other PRs on the same account reviewed normally in the
  same window. Reproduces + refuted locally + not account-wide = a defect on their side,
  not a quota and not something a further retry fixes. One such reply is an incident, not
  yet evidence: retry once, let time pass, and check the INLINE endpoint first, exactly
  as the usage-limits case demands.
  Everything weaker is not evidence at all — silence is not, and neither is
  `--check-pr` reporting no review, which says the same thing whether the reviewer is
  down or was simply never triggered at this head. Nor is a
  `codex exec` quota error: that is a separate surface on separate quota.
  Scope what you hand it exactly as `.claude/commands/deep-review.md` §1 specifies.
  **Verify it saw a diff at all**: a clean verdict that does not demonstrate WHAT it
  reviewed is void, and a false clean from the cross-model gate is worse than no review.
  Do not merge on a same-model-only review.

### Private data never reaches the public repo

- **Private-data scan before every push (public repo).** Grep the ENTIRE diff
  (`git diff origin/main...HEAD`) for private/identifying data — real names,
  company/product names, emails, IPs, private career/project specifics, verbatim
  user messages. Check ALL surfaces, not just prose: **source comments,
  docstrings, and test fixtures/data** are the easy misses. Use a synthetic
  stand-in in tests, never the real private artifact. (2026-07-01: a verbatim
  private DM leaked via a test docstring + a code comment after the commit
  message and PR body were already clean.)

- **No sensitive data in commits** — voice data, research profiles, IPs,
  and secrets must never enter the repo. User data lives in overlays
  outside the repo (e.g., `~/.claude/skills/*/`, `~/.genesis/`).

## Reference Router

Read references ONLY when relevant to the specific task. Do NOT load all
references on every trigger.

| When you need... | Read... |
|---|---|
| Subsystem purpose/maturity/do-not-touch (judgment layer) | `docs/architecture/CURRENT.md` |
| Codebase structure, package map, gotchas, debugging | `references/codebase-map.md` |
| Package/module/symbol navigation (progressive drill) | `codebase_navigate` MCP tool (L0→L1→L2) |
| venv, DB paths, Qdrant, Ollama, network, commands | `references/environment.md` |
| Worktree rules, concurrent sessions, branch naming | `references/worktrees.md` |
| tracked_task, exc_info, os.killpg, logging patterns | `references/observability.md` |
| V3 state, build order, GROUNDWORK, architecture docs | `references/architecture.md` |
| Phase 6 contribution pipeline, sanitizer | `references/contribution.md` |
| Pending work, active incidents, subsystem status | `references/build-state.md` |
| Auditing/deep-reviewing AI-generated code (failure taxonomy, audit passes) | `references/ai-code-audit.md` |
| Writing or revising a multi-session plan document | `references/plan-docs.md` |
| What a plan owes before building: free reads, red-team, body fields, ask vs decide | `references/due-diligence.md` |
| Pre-release review, bug hunt, guard/gate change — verification method | `references/high-stakes-verification.md` |
| Choosing a command/value/procedure by reasoning about an external tool | same, section 9 |
| Auditing the CLAIMS a session wrote (wrap-up, or before any permanent record) | same, section 11 |
| Which code tool to use (CBM vs Serena vs GitNexus vs Grep) | `.claude/docs/code-intelligence.md` |
| Committing: review_state sequence, sigils, Pre-Commit Gate, depth gate, verify-RED | `references/commit-path.md` (mandatory before a commit) |
| SYSTEMIC vs PARTIAL guard failure, and the worked example | `references/guard-failure-regimes.md` |
| What each `gh` listing caps at before you pass a flag | `references/gh-listing-caps.md` |
| A blocked call, `secrets.env`, ask keys | `references/ask-keys-and-sigils.md` |
| Merge-gate findings: Devin scoring, `auth`, outside-diff, doc paths | `references/merge-gate-findings.md` |
| A PR whose diff touches the enforcement-hook surface (gate-fix lane) | `references/gate-fix-lane.md` |
| Release review: scope, pre-existing defects a release recruits for | `references/release-review.md` |
| Which session type you are | the router at the top of this file |

**Freshness rule:** On first read of `codebase-map.md` in a session,
verify structural claims against current code. If a package status or
gotcha has changed, flag to user before acting on stale assumptions.
`docs/architecture/CURRENT.md` carries per-entry `verified:` stamps
enforced by `scripts/check_subsystem_map.py` (CI `subsystem-map-check`) —
after changing a subsystem's capabilities, update its entry and stamp.
