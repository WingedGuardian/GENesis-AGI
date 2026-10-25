---
name: build-session
description: >
  This skill should be used when a session BUILDS one Genesis item: implementing
  a change in a worktree through to opening its PR, including rework from a
  send-back spec. Load `genesis-development` (the shared core) first. Do NOT load
  it for driving open PRs to merge (`closing-session`), post-merge verification
  (`validating-merges`), or specifying a change before it is planned
  (`spec-session`).
# MEASURED against the real scorer (scripts/hooks/skill_injection_hook.py) on
# the live catalog, 2026-10-21: 3/6 advertised build prompts fire this skill,
# and 2/10 control prompts do ("implement a grocery budget", "rework my
# resume"): both words are generic. The router in genesis-development, not this
# nudge, is what loads a type skill.
keywords: [implement, rework]
consumer: cc_foreground
phase: 10
skill_type: workflow
---

# Build session

**Load `genesis-development` (the shared core) first** if this session has not:
it holds the review-chain, evidence and private-data rules every session uses.

**Mandatory read before your first commit:**
`.claude/skills/genesis-development/references/commit-path.md`. It holds the
review_state sequence, the sigils, the Pre-Commit Gate, the depth gate and
verify-RED. Read it; it is not on-demand reference.

### Where your session ENDS — build sessions and closing sessions

**A session that writes code and a session that drives a PR to merge are
different SESSION TYPES, not two phases of one session's life** (user decision,
2026-09-02). Loading this skill makes you a **build session**.

- **A build session owns an item from Ready through opening its PR.** It runs
  the review the change EARNS — `/deep-review` for a substantial one, because
  that is what clears the commit gate's depth check; code-reviewer inline for a
  small focused fix (the Adaptive Review Protocol below is authoritative, and
  dispatching a full adversarial pass on a one-line change is not the bar) —
  and then it is DONE with that item. Where its effect can be observed here, a
  wrong run can be undone, and it is not on the never-run list, it is seen
  working BEFORE the PR opens ("Verify BEFORE the PR" below). It does not wait
  for Codex, and it
  carries no review loop — the obligations under "When to DRIVE a Merge" (closing-session)
  pass to the closing session along with the PR; they do not lapse.
- **A closing session owns the open-PR queue**, whichever session built each
  PR. Its unit of work is the queue, not the card. That is the
  **`closing-session`** skill; load it instead when the job is "get the open PRs
  merged".
- **A cloud coding agent (Devin) is a third kind of builder.** It works from the
  public repo in its own VM, with no access to an install: no running Genesis, no
  `~/.genesis/`, none of an install's hooks or local reviewers. Hand it only
  self-contained, spec-complete work whose tests run in CI, off the enforcement-hook
  surface, as a public issue. The `devin-ready` label hands it over and starts a
  paid session, so a session applies it only after the owner explicitly approves
  that issue in conversation. Once approved, the session applies the label itself;
  the owner does not apply it by hand.
  Its PRs join the same queue (closing-session, "Devin-built PRs").

The handoff between them is **the PR itself** — a durable artifact that survives
compaction and session death, so nothing has to be remembered across the
boundary. It also satisfies the `reviewer ≠ implementer, fresh context` contract
structurally rather than by discipline.

Why it matters here: fused, the first item's review loop consumes the whole
session — several compactions deep — while everything else the owner arrived
with goes untouched. **Finishing the PR is finishing the work.** Handing it to
the queue is not abandoning it.

Two consequences worth internalizing:

- **Throughput is a RATE, not a per-session virtue.** Closed/wk must exceed
  opened/wk or the queue grows without bound (Little's Law). Build sessions do
  NOT throttle themselves to protect it — that just relocates the queue upstream
  onto the human deciding what not to start. Closing capacity is the control
  variable. (`scripts/pr_flow_rate.py` measures the two rates; it lands with
  PR #1613, so check that it exists before reaching for it.)
- **Compaction policy follows the same seam.** Reset context at
  plan→implement. Implement→review is a session-TYPE boundary, not a compaction
  decision — you hand off, you do not compact and continue. And never reset
  mid-"fix the findings": that work needs the implementation context you would
  be throwing away.

### Wiring Discipline

Every new component needs at least one call site in the actual runtime
path. Apply this 4-level verification taxonomy:

1. **Exists** — file/function present. Proves nothing.
2. **Substantive** — tests pass, handles happy + error. No runtime proof.
3. **Wired** — live call site, import chain unbroken. Minimum for "done."
4. **Data-Flow Verified** — real data flows end-to-end. Required for
   critical paths.

Mark nothing "done" below Level 3.

### A new skill needs a measured trigger

**A new SKILL has its own version of "Measure, Do Not Choose" (genesis-development), and it is easy to miss.** Dropping a
`SKILL.md` into `.claude/skills/` gets it INDEXED automatically (the catalog
generator scans the directory — no registry to update), which looks like done.
It is Level 1. The nudge that actually surfaces it scores **only** whole-word
skill-NAME tokens and explicit frontmatter `keywords:` — description prose is
deliberately not scored, and name words on the hook's `_NAME_TOKEN_STOPLIST`
("genesis", "user", "plan", …) never score either — so a skill whose name is a
concept nobody types is indexed and silent. MEASURED 2026-09-02: `closing-session` scored **0.0** on
every one of its own trigger phrases ("work the open PR queue", "review and fix
the open PRs") until `keywords:` was declared; with them, 4/4 trigger phrases
fire and 3/3 unrelated prompts stay silent. Note the extractor drops tokens
shorter than 3 characters and does no stemming, so `pr` can never match and
`merge` will not match "merging" — declare the surface forms. Verify a new
skill by scoring it against the phrasings a user would really type, in BOTH
directions.

### Skill invocation points

Required steps at named moments. A skill marked "if installed" comes from an
optional plugin (gstack, superpowers); where it is absent, skip it — there is
no substitute checklist.

- **A test fails unexpectedly** → this skill's "Debugging Discipline"
  section first; plus `superpowers:systematic-debugging` or `/investigate`,
  if installed.
- **Before any done / fixed / passing claim** →
  `superpowers:verification-before-completion`, if installed.
- **An external tool or repo surfaces as a candidate** → the `evaluate`
  skill; on an ADOPT verdict, `integrate-module`; on an ADAPT verdict,
  reverse-engineer the reference before writing code (`evaluate`, Decision
  Protocol step 4).
- **About to build anything that runs inside a Claude Code session** — a hook
  or guard, status line, prompt or context injection, tool interception, a
  notification, an in-terminal pane → search Claude Code mods and plugins
  first (`evaluate` workflow step 2 names the sources per session context).
  From CC 2.1.287 mods can carry UI, tools, agents and model calls; below that
  version they need the early-access flag, so check the pin in
  `docs/reference/cc-compatibility.md`. Genesis has already shipped one
  (`plugins/genesis-web-override/`). A mod may AUGMENT an enforcement guard
  but never replace it: a mod that denied a tool call ran before the classic
  `PreToolUse` chain and suppressed it when measured (#2260).
- **A diff touches dashboard or other UI** → `/qa-only` or `/qa`, plus
  `/design-review`, if installed.

### Timeout Policy

The burden of proof is on you to justify why a timeout should exist.
Do not default to "add a timeout for safety." Instead:

1. **Identify the specific failure mode.** What hangs? Why? Is there
   evidence this actually happens, or is it speculative?
2. **Justify the specific value.** Why this number and not another?
   What legitimate work would be killed at a lower value?
3. **If you have no strong justification for a specific value, default
   to 2 hours (7200s).** This is the project floor — generous enough to
   never interfere with legitimate work while preventing permanent
   resource lockout from truly hung processes.
4. **Surface the request to the user** with the value, the failure mode,
   and the evidence. Never add a timeout as a "small improvement" or
   "defense in depth."

Timeouts on reflections, CC calls, cognitive paths, and long-thinking
work fight Genesis instead of helping it — they cap legitimate long
thinking and add speculative defense against rare hangs. The exception
is raw subprocess calls with no external watchdog (e.g., deterministic
executor steps), where a hung process blocks shared resources (executor
semaphore) with no other recovery mechanism.

### Verify Outcomes, Not Just Tests

`ruff check . && pytest -v` is the minimum bar, not the finish line.
After tests pass, verify the actual end-to-end outcome the change
delivers. Diff behavior between main and your changes when relevant.
For wiring changes: verify the init/bootstrap order passes the right
values at runtime, not just that parameters exist. For notification
changes: verify the notification actually arrives. Ask: "If the system
restarts right now, will this actually work?" If you can't answer yes
with evidence, you're not done.

**A check keyed on EXISTENCE can only confirm — it can never disconfirm.**
Before trusting a verification, ask what it would do if the thing were absent,
stale, or the wrong object. If the answer is "pass", it proved nothing. Three
shapes of this, all measured in one session 2026-09-08:

- **Waiting for a NEW artifact by testing that one exists.** A poll for "a
  scheduled-review marker is present" passed instantly against a marker from six
  days earlier. Key the wait on the artifact's IDENTITY — the head SHA it names —
  not on its presence.
- **Matching the first row instead of the right one.** "Is main green?" matched
  the first `push` run for that SHA, which was the CodeQL workflow (success),
  while the CI workflow for the same SHA had failed. Filter by the thing you
  actually mean (`--workflow ci.yml`), and prefer reading the JOB you care about
  over an aggregate conclusion.
- **Counting from a listing sorted by the wrong key.** `gh pr list --state
  merged --limit 40` sorts by CREATION, so PRs created earlier and merged today
  fall outside the window and vanish; the count read low and looked plausible.
  Use a query whose filter IS the property you are counting (`gh search prs
  --merged-at`), per CLAUDE.md's truncated-listing rule.

The failure is silent and self-confirming in all three: an under-read is
indistinguishable from a clean result, so nothing prompts a second look. The
tell is that the check passed FASTER or more easily than the work should have
allowed — a poll that succeeds on the first attempt for something that takes
40 seconds to produce has not observed that thing.

**Verify in the REAL runtime context, not a shell proxy.** "Works when I
run it" is not "works where it runs." Same code + same uid ≠ same context:
a long-running systemd service (genesis-server, guardian) differs from your
interactive shell in mount namespace, seccomp, `NoNewPrivileges`, dropped
caps, and — the one that bites — **ptrace/`/proc` access**. Anything that
reads `/proc/<other-pid>/{environ,mem,stat}`, another process's env, sockets,
or namespaced/hardened resources MUST be verified by hitting the **live
endpoint** (`curl` the real server) or running inside the real service — not
`python -c` in your shell. (Origin, 2026-08: the CC-slot stale-code badge
read `/proc/<pid>/environ`, which succeeds in a shell but returns EACCES
under the server's `ProtectSystem=strict` sandbox — so `enumerate_cc_slots()`
returned 0 in-server and **three** features shipped green but inert since
July; the module's docstring claim "same-uid reads succeed" was a shell-tested
falsehood. The fix routed around the ptrace-gated read entirely.)

**The config twin of that rule: a setting's EFFECT scope and its SOURCE scope are
different questions, and only the second one is obvious** (from #1605). Ask
"which sessions or processes does this file actually bind?", never "is this file
read?". Each process reads config from wherever it happens to sit, so a value
whose effect is machine-wide but whose source is per-session binds a minority and
does nothing for the rest, while looking correct everywhere you check. It is not
"ignored", which is why grepping for the key and finding it loaded proves
nothing: the file loads, for the sessions that read it. Before shipping any
config, name the set of processes the value must govern, then the set that will
actually load the file. If the second is smaller, the config is wrong however
well it works in your session. This is the Generalizability Gate's deploy-path
question one level down: how does this reach other SESSIONS? Origin: Claude
Code's auto-updater suppression and transcript retention were both set in the
repo's `.claude/settings.json`, which CC merges only for sessions launched at a
checkout's root (a session started in `src/` keeps the defaults), while the behaviour each controls spans the whole machine.

**And when the work is done, audit the CLAIMS as well as the outcome.**
Everything above distrusts a VERIFICATION; the companion failure is the
SENTENCES you wrote about the work — a number in a commit message, a docstring's
assertion, an in-thread reply saying a finding is fixed. A green suite says
nothing about any of them, and permanent record has no tone of voice, so the
next session reads a confident sentence as a measured one. Those surfaces are
ENUMERABLE, which makes the check mechanical rather than a mood.
`.claude/skills/genesis-development/references/high-stakes-verification.md` §11 carries the canonical surface list
and the walk; do not keep a second copy of that list here.

### Verify BEFORE the PR, when this install can show it (standing user rule, 2026-09-24)

You build on a long-lived install, so **when the change's effect can
be observed here AND a wrong run can be undone, see it work before you open the
PR.** Review then looks at a change that works, not at its first attempt to run.
Put what you ran and what you saw under the PR's `## Testing` section. The `E2E:`
line keeps its meaning: the check to run AFTER the merge. For a hook that is still
needed, because a pre-PR run tests the worktree copy, not the merged copy the
harness fires.

**Which copy actually ran is the whole question. Check it; never assume it.**
- A hook guard run as `python3 <worktree>/scripts/hooks/<guard>.py`, with a real
  payload on stdin, runs the branch's copy of that file. The harness, and the
  worktree's own `.claude/hooks/genesis-hook`, run MAIN's copy by default (see
  "The Gate Machinery" in commit-path.md). `GENESIS_HOOK_DEV_LOCAL=1` on that ONE launcher
  call switches it, and stderr announces the switch. Never set it session-wide or
  in `settings.json`: your own commits and pushes would then run under the gate
  you are still editing.
- Which `genesis` package a Python file imports depends on the file. MEASURED
  2026-09-25: from the worktree root, `import genesis` resolves to the MAIN tree
  through the editable install. But more than twenty scripts and hooks put their
  own tree's `src` on `sys.path`; run from a worktree they load the BRANCH's
  package, whose database path then resolves inside the worktree, where no
  database exists, while Qdrant is still the live one. Read the file's imports
  before running it, and print `genesis.__file__` when in doubt.
- So runtime code (`src/genesis/**`) is verified by tests, or by the options
  `.claude/skills/genesis-development/references/worktrees.md` lists for runtime checks, never by a live run,
  until there is a supported way to run a branch in the live runtime.
- Say in the PR which copy ran and what it touched.

**Only a run you can undo qualifies.** A script that deletes, prunes,
restores, deploys to the host, pushes, posts or sends a message, or writes to any
live store (the database, Qdrant, the graph engine) does NOT run before it
merges. A `--dry-run` flag is not an exception by itself: `restore.sh --dry-run`
still takes the live update and backup-restore locks, runs `git pull --rebase` on
the backup repository, and overwrites `~/.genesis/restore_status.json` on exit.
Run a dry-run only after reading that mode's code and finding that it writes
nothing. Do not improvise isolation: `GENESIS_HOME` moves neither
the database nor Qdrant, and many files hard-code `~/.genesis`. Tests are how such
a script is verified.

**Never run on the live install before it merges:** a branch's migration, privacy,
egress or credential handling, anything that loosens a gate other sessions rely
on, and anything whose verification needs days or a machine the owner does not
control. Test a migration in a pytest fixture, and patch EVERY store it touches on
the migration MODULE itself. The suite-wide fixture patches
`genesis.env.genesis_db_path`, but data migrations import that name directly
(`from genesis.env import genesis_db_path`), so do not rely on that fixture to
isolate a migration: patch the migration module's own names. A migration can
also reach live Qdrant and write under `~/.genesis`.
`d0008_reconcile_memory_cross_store`, for
example, deletes the Qdrant points it judges to be ghosts. Its test patches the
module's `genesis_db_path`, `get_client` and `_export_path`:
`tests/test_db/test_d0008_reconcile_memory_cross_store.py` is the pattern to
copy.

NEW behaviour on a risky surface (memory, graph, database) ships behind a
shadow flag, and the session never flips it: the owner does, after verifying.
Entity adjudication is the house example. `config/entity_adjudication.yaml` ships
it in `propose_only`, so a merge is recorded as a proposal. The owner reviews and
approves proposals and applies the approved ones (`entity_adjudication_approve`,
then `entity_adjudication_apply`) without changing the mode. Switching to `live`
is a separate decision: it applies only approved backlog rows, but from then on
it applies each new merge verdict with no approval step.

**Iterate with the owner.** When acceptance needs the owner's hands (a device, a
chat channel, another machine they use, or something only they can see), do not
open the PR on a guess. Tell them exactly what to try and what they should see,
ask through `AskUserQuestion`, fix what they report, and re-ask while it blocks.
Open the PR once they confirm it works. Waiting on the owner here is the design,
not a stall: no review round could have caught what they are about to see.
- **Do not dispatch work whose acceptance needs the owner's hands.** No
  dispatched path is known to hold a change for the owner's test today.
  - The autonomy executor pushes a completed code task's branch and opens a
    draft PR for a build-lane task that passes its scope gate. It then deletes the
    worktree and the local branch, so there is nothing left to test locally
    (#2421).
  - The executor's code steps have no MCP write except `observation_write`, so
    `follow_up_create` is not available to them (#2422).
  - A `direct_session_run` session's tools depend on its profile, and the
    profiles differ in what they allow. Read the chosen profile's definition
    (the built-ins in `src/genesis/cc/direct_session.py`, plus any install
    overlay in `genesis.cc.profile_overlay`) before assuming the session can commit
    code, keep a worktree, or record anything.
  If a dispatched session finds an owner test is owed anyway, it states what to
  try and what they should see in its final output. For an executor task that
  output is read through `task_detail`, and it reaches no notification. A
  follow-up row that a dispatched session creates (on a profile that allows it)
  lands on the hidden tabled lane, so its `reason` must say it awaits the owner's
  test in a foreground session.
- **The owner says they cannot test it this session, or the session ends
  before they confirm:** a `follow_up_create` row naming the owner test still
  owed and where the code is (branch, worktree path, head SHA). Never write the
  change up as verified.

### Acceptance Bar + Measured Rate — the primary methodology

**Use this as often as it applies. It is the default way to build anything here,
not a special-occasion technique.** Unit tests prove the code does what you wrote.
This proves the thing actually WORKS — with numbers and a denominator.

Two artifacts, both produced BEFORE shipping:

**1. The acceptance bar — replay the real defect.** Take the actual failure that
motivated the work and run it through the new thing. If it does not catch/fix
the case it exists for, it does not ship, however elegant it is and however
green the suite is. Reconstruct the real case (from git history, from the
transcript, from live data) rather than a stylised approximation — a synthetic
case can pass while the real shape does not.

**2. The measured rate — run it against real data and produce a number.**
A claim like "low false-positive rate" or "it should be fine in practice" is not
evidence. Run the thing over a real corpus — recent commits, live rows, real
traffic, historical transcripts — and report `k/N (x.x%)`. **A number without a
denominator is not a measurement.** Then look at the individual hits and say
honestly which are real signal and which are noise; a "false-positive rate" that
turns out to be mostly true positives is a fire rate, and saying so is part of
the result.

**For a Bash GUARD's predicate there is a corpus of this install's own recorded
`(command, cwd)` pairs, and `scripts/replay_guard_corpus.py` builds it — but
REPLAY IS CURRENTLY UNAVAILABLE.** Every guard is refused; `--guard <name>` and
`--all` exit 2. So the `blocked k/N` measurement this bullet wants has to be
taken another way for now — by hand, which is how it was always taken: MEASURED
2026-09-16, `git log origin/main --pretty='%s%n%b' | grep -icE "real commands"`
returns 23 against 1,656 merged-PR commits, of which 121 are `fix(hooks)` or
`feat(hooks)`. Roughly one hook PR in five already does this sweep by hand.

The permission was withdrawn rather than lost. It used to rest on a declared
claim that a guard performs no writes, spawns or network calls, re-derived by
walking its imports — and that claim cannot be established by reading: the
spelling set is open, and four review rounds each surfaced the next round's
miss, including three fail-opens inside the checks themselves. Issue #2036
restores replay the other way round, by running it where the effects are
impossible, so nothing has to be proved about the guard at all.

What the tool still does is check that each declaration's prose still points at
code that exists: `--list` resolves every `Cite(module, symbol, fragment)`
against the symbol's source and refuses on a mismatch. Comment stripping is
complete for PYTHON, which is tokenized; a non-Python file loses only full-line
comments, so a fragment cited from a shell file can still match from inside a
trailing one — and a citation with no symbol matches the whole file, unscoped.
`--list` publishes both limits. That is worth running
after editing a guard — six citations in this table went stale within five days,
one onto a comment about an unrelated timeout. It tells you nothing about what
the guard DOES; `--list` prints its own limits.

Two limits will travel with the number when replay returns: it is stamped
UNCLASSIFIED because the corpus contains dangerous commands too, so it is one
side of the tradeoff and needs the positive control the bullet above demands;
and there is **no way to print the blocked command lines**, because those are
real commands containing secrets passed in argv (issue #2007 tracks what a
correct version owes). The count and the verdict are what go public.

The measurement is a GATE, not a footnote. Decide the acceptable threshold
BEFORE measuring, and if the number misses it, tighten and re-measure rather
than shipping with a caveat. When you tighten, re-run the acceptance bar in the
same breath — a filter that improves the rate by breaking the thing you built it
for has made it worse, and only running both together catches that.

Worked example (2026-08-27/28, an orphaned-literal detector). The tool itself
was still on an unmerged branch when this was written, so treat the first two
bullets as a method illustration; the figures in the paragraph after them are
derived from this repository's own history and can be re-derived.
- Acceptance: replayed a real review defect; the detector named the exact
  sibling file. PASS.
- Measured, false-positive side: `1/151 real file-edits fired (0.7%)`. The one
  hit was an identifier rather than prose, so the filter was tightened to
  require interior whitespace, re-measured at `0/151 (0.0%)`, **and the
  acceptance replay was re-run to confirm the tightening had not blinded it.**

**That example then became a lesson against itself, which is why it is kept.**
Everything above measures FALSE POSITIVES, and `0/151` reads as though the
tightening were free. It was not. Measuring the other direction needs
INDEPENDENT ground truth rather than the tool's own criterion — otherwise you
grade the tool on its own definition of success. Here that meant mining history
for a literal removed from one file and then removed AGAIN from a second file in
a later commit: the repo itself recording that the first fix left a sibling.
Over 1,584 commits that yields **95 verified cases (6.0% of commits)** — and the
interior-whitespace filter that scored so well on precision **excludes 48 of
those 95 (51%) by construction.** The number that actually decided the design
was recall against a budget cap: **28/47 in-scope cases caught (60%)** with a
cap of 6 literals per edit, versus **47/47 (100%)** with the cap lifted — every
one of the 19 misses was that single cap, and lifting it recovered all of them.

The rule that generalises: **a rate measured on one side of a tradeoff is half a
measurement.** A precision number with no recall number cannot distinguish a
good filter from a blind one, and the side you did not measure is the side that
will be wrong. Decide which direction matters for the thing you are building,
and measure that one first.

This also catches a specific self-deception. A first prototype of that same
detector used a regex and reported "2 findings" while **silently skipping the
entire class it was built for** (the pattern excluded backslashes; every prompt
string ends in `\n`). The acceptance replay is what exposed it. A matcher that
finds nothing is indistinguishable from a matcher that looks at nothing —
only replaying a known-positive tells them apart.

**A null result is a LEAD, never a clearance — and it travels with its blind
spot attached or it does not travel.** The paragraph above is about an
instrument that looked at nothing. This is the harder case: the instrument
worked, the reading was clean, and the reading was still wrong. Three null
results from one session were wrong the same way, and every one was overturned
by CONSTRUCTION rather than by more sampling:

- *"An expansion in verb position always emits ≥2 words, so it self-corrupts any
  command it appears in."* Generalised from a handful of samples of ONE of the
  construct's spellings. Another spelling admits a degenerate case that emits
  exactly one word — so argv survives intact, and the parse resolves a verb bash
  never runs. Stated abstractly on purpose; see the note under the
  resource-defect bullet in Test-First Discipline.
- *"Zero over-block flips across 129,179 real commands."* The regression was
  CONSTRUCTIBLE, and a later worker constructed it: a `BLOCK → ALLOW` flip on
  an `rm -rf` of the production database's parent directory.
- *"Zero regressions, verified three ways."* All three ways were the same axis —
  see the resource-defect bullet under Test-First Discipline.

A corpus measures what has been TYPED. It says nothing about what is TYPEABLE.
For a SAFETY property the question is therefore never "did I observe a failure"
but "can one be CONSTRUCTED", and only the second question has an answer that
clears anything. **So when a null result is handed to anyone — a subagent, a
peer session, a PR body, the user — state the DENOMINATOR and the METHOD'S BLIND
SPOT in the same breath as the number, and say in words that it is not proof.**
Then name the recipient's job: the person best placed to construct the
counterexample is whoever is about to rely on the null result, and they will not
go looking unless you tell them the search is still open. A null passed on bare
is read as a clearance by everyone downstream, which is how one unproven
sentence becomes the premise of three later decisions.

**Measure the REMEDY, not only the finding** (#2255). A finding has to be
demonstrated; a fix is usually asserted, and review, tests and mutation all grade
the finding rather than the fix. So run the original exploit against the fixed
code, then run its ADJACENT spellings. A remedy that closes one arm of two
produces a diff that reads as the fix and changes nothing, which is worse than no
fix because it retires the finding. Twice in one day a recommended fix was
measured before shipping: one still gated, because two arms fired independently
and the finding named one; the other left the in-project spelling of the bypass
open. If the remedy cannot be measured, say so on the PR and file it rather than
ship it.

### Select, don't amputate — truncation is the absence of a decision

**Scope first, because bounding is often correct.** What makes something an
amputation is LOSS — the value cut here was the only copy. Nothing else. A
bounded PREVIEW of something stored intact elsewhere is a selection, and stays
one even if its handle is useless. Bounding against a hard external budget is
likewise correct: a hook's stdout cap, a context window, a column whose limit is
actually ENFORCED. That last qualifier is load-bearing here — this repo's SQLite
`TEXT` columns enforce no length at all, so "the database column" does not
excuse a cut; a self-imposed storage assumption is a decision to justify, not a
budget to obey. So is refusing an oversized value outright.

**And a SAFETY cap may be lossy — that is the one place cutting the only copy is
right.** Streaming is a TRANSPORT property and only-copy is a DURABILITY one;
check them separately, because a chunked read is often backed by a retained
source you could go back to. The case that earns the lossy cap is the source
that genuinely has no retained copy — a live subprocess pipe — where reading to
the end to avoid "truncating" is how a runaway command exhausts memory; this
repo bounds exactly that at a few MiB (`autonomy/executor/deterministic.py`
`_read_limited`, whose own comment names the `yes`-command threat; verified
2026-09-04). Losing the tail of a log beats losing the process. The obligation
there is not to keep the bytes, it is to be LOUD about the cut — say the output
was bounded and roughly by how much, so nobody reads a clipped log as a
complete one. That cited cap has since been fixed AT THE READ and is no longer
the silent-cut example it was: `_read_limited` drains the whole stream while
retaining only `limit` bytes and returns `(retained, total_size)`, so the caller
appends `... (truncated, N bytes total)` with N the TRUE drained total (PR
#1796; verified in `autonomy/executor/deterministic.py` 2026-09-08).
**It is still not a worked example of a loud cut, and the reason generalises:
a declaration has to survive the CONSUMERS, not just be emitted.** That marker
is appended at character 50,000 of the result field, and all six onward paths
head-slice it at 200-2000 characters — including the one that feeds the next
step's prompt — so the declaration is unreachable in every direction it
travels, and the slice that removes it declares nothing itself (MEASURED
2026-09-08). When you fix a silent cut, check the READERS of the field you just
made honest; otherwise you have moved the silence one layer out. A silent lossy
cap is still the defect; a declared one is a resource guard doing its job. This
section is about the remaining case.

**A handle that does not resolve is a separate defect, and do not conflate the
two** — that conflation is the mistake this section made about itself, twice.
Check the pointer, because a preview advertising a retrieval path that does not
exist teaches a lie; but when the full value survives somewhere, the fix is to
mend or drop the handle — removing the cap TO PREVENT DATA LOSS is fixing a
loss that never happened, and that misdiagnosis is how this rule causes the
damage it exists to stop. Whether the cap should exist at all is the separate
question the "what breaks if it is unbounded" test answers: a cap with no
external, safety, or measured compatibility justification may be removed once
that is established — for being unjustified, never for being an amputation.

**There, do not truncate.** Not strings, not lists, not context, not output.
Reaching for a character cap is a signal that a question was skipped, not
answered. Omitting is legitimate — it is a judgement about relevance. Truncating
is not: it is what happens when that judgement was never made, so the value gets
cut at a point that has nothing to do with meaning. A truncated value is
frequently worse than either alternative, because it still LOOKS complete, so
nobody checks it — at which point you may as well not have passed it at all.

Before bounding anything, answer: what is this value FOR, who reads it, why does
it need budgeting at all, and what actually breaks if it is unbounded? Solve
THAT. Usually the answer is "select less, whole" rather than "cut", and often the
bound turns out not to be load-bearing.

Three rules when a bound really is needed:

- **Bound by MEANING, not by one blanket number.** A closed set is validated
  against that set — a value outside it is INVALID, not "too long". A timestamp
  is a shape; half a timestamp is not a shorter timestamp. A blanket cap turns
  100,000 characters of foreign data into 300 characters of foreign data and
  calls it bounded.
  **This governs HOW you bound, never WHETHER you may.** Rejecting a structured
  value or a collection by size is correct and stays correct: an over-long id, an
  over-large batch or an implausibly large file is simply not a value we accept,
  and refusing it is a resource guard, not an amputation. What is forbidden is
  silently CUTTING them to fit. Only free text gets a bound it is expected to sit
  under; every other INBOUND value gets one it must not cross. Scope that to
  inbound on purpose: a READ that pages a large collection — the first n WHOLE
  elements plus a total and a truncation flag — is a selection with a
  denominator, this section's own preferred shape, and the source collection is
  valid precisely because it exceeds the page. Reject the oversized value you
  are asked to ACCEPT; paginate the oversized collection you are asked to LIST.
- **Derive the number from the right thing, and record how.** A SAFETY bound —
  memory exhaustion, an abuse ceiling, untrusted input — does not come from the
  corpus at all: historical traffic says nothing about adversarial input,
  concurrency, or the memory you actually have, and a cap chosen from observed
  values will be exactly the wrong size when it matters. Derive those from the
  protocol, the capacity and the threat model FIRST, then use `k/N` only to price
  what the bound rejects. It is the COMPATIBILITY bounds — how long is this field
  in practice, what does this cap cost real readers — that a corpus answers, and
  for those: measure the real population and report `k/N` per the Acceptance Bar,
  then name the corpus, the query and the date. Naming them is necessary and not
  sufficient: the query is only re-derivable if the rows are still there when the
  next reader runs it, so a table with a retention window yields an EPHEMERAL
  observation. Say which one you have. And what the bound COSTS is a SECOND claim
  needing its own denominator — "the cap discards the part worth keeping" is
  exactly the sentence that sounds measured because it followed a measurement.
- **Omit explicitly, with a constant-bounded marker** (`<omitted: 104,823
  chars>`) in preference to a mid-value cut. An honest gap beats a
  plausible-looking fragment. The bound-plus-loud-flag half of this is already
  the house pattern; the character-count marker is a proposal, so do not go
  looking for a precedent that is not there.
  **Declaration is a requirement on top of the loss rules, never a substitute
  for them.** Stating the rule as "never cut" overshoots: this repo cuts
  mid-value in several places on purpose and is right to — a resource guard on
  an unbounded stream, a preview rendered next to the full record, a display
  string trimmed before escaping so the cut cannot land mid-entity. Each of
  those is a cut the loss rules PERMIT (a safety cap, a pointer-backed
  selection, third-party display text) — which does not certify how each is
  reported today: the safety cap above still cuts silently, and PERMITTED is
  not DECLARED. The order matters: first the loss rules decide whether a cut may
  happen at all — announcing a sliced KEY does not un-merge the two identities
  it collapsed — and only then does declaration decide whether the permitted
  cut is honest. A silent permitted cut is still a defect; a loud forbidden cut
  is still forbidden.
  **When the total is not already known, say that instead of computing it.**
  Bounding a stream is the case — but first ask which KIND of bounded reader
  you have, because the bound is on RETENTION, not necessarily on reading. A
  drain-and-discard reader consumes to EOF anyway (a subprocess pipe must be
  drained or the child blocks) and can count what it discards in constant
  space, so it KNOWS the exact total and should say it. Only a stop-at-limit
  reader, which truly stops consuming, cannot know. There, state the quantity
  you KNOW — the cap — and mark the tail unknown:
  `<kept first 40,000 chars; rest of stream omitted>`. Do not write
  `<omitted: ≥40,000 chars>`: under this rule's own grammar that number
  describes the OMITTED content, and one character over the cap makes it a
  fabricated tail length — an invented number wearing the honest marker's
  clothes, which is the exact thing the marker exists to prevent.

One trap deserves naming, because it is what produced this rule: **a cap can
manufacture a correctness bug in the very data it was added to protect.**
Truncating an identifier used as a KEY merges two distinct identities into one,
and downstream code then attributes one subject's state to another. That is not
hypothetical — it shipped here. Two roster peers whose names shared a prefix
collapsed onto a single key, so one peer's success cleared the other peer's
recorded failure. A short DISPLAY handle is a different thing and is fine; the
rule is about the stored key, not the rendered one.

**A real need to truncate is a CONVERSATION to have, not a magic number to pick
alone.** If you catch yourself choosing 300 or 200 or 1000, stop and raise it.

**When there is nobody to raise it with** — an unattended background session —
the rule is NOT "never pick a number". It is **never pick one silently.** Bound
if you must, and put the reasoning beside the number, where a reader of the value
will see it. That includes the question that comes BEFORE the number — whether
this value needed bounding at all — because "is this big enough to matter?" is
the same judgement drawing on the same missing information, and skipping it is
precisely how the number gets invented. State both. A number with its reasoning
next to it can be argued with and corrected, which is all anyone needed from you.
What made the original defect dangerous was never that 300 existed — it was that
300 arrived silent, looking deliberate, and was then defended.

**One last thing, learned the hard way from this section itself.** Four review
rounds found defects in it, and every single one had the same shape: the cited
FACT was true, and the CONCLUSION drawn from it was false. A retrieval tool
really did lack an id lookup — and "therefore this is an amputation" was wrong,
because the value was stored whole elsewhere. A field really was written
unsliced — and "therefore it is a cheap place to record something" was wrong,
because reaching it aborted the whole run. Verifying that each cited fact is true
is the easy half, and it is not the half that fails. **State the inference as its
own claim, and check THAT.**

The sharpest instance is this section committing that error in the sentence next
to the one warning against it. A draft of the core's "never about secrecy" paragraph (in genesis-development, no longer two paragraphs away) cited a real file
that says, correctly, that a particular scrub applies to external audiences and
not to replies going to the user — and generalised it into "this repo leaves
owner-facing content untouched", which four other paths contradict. True fact,
false inference, two paragraphs from the rule forbidding exactly that. The
generalisation is the step to distrust, and it is seductive precisely when the
evidence under it is solid.

### A blocked compound command loses EVERYTHING in it

A PreToolUse block kills the **whole** Bash call, not the offending part — so a
guard firing on step 3 also silently discards steps 1 and 2, while the error
text talks only about step 3. This repo has many blocking guards
(`review_enforcement_commit`, `full_suite_guard`, `concurrent_test_guard`,
`git_push_guard`, the destructive/protected-path guards), so it is not rare.

**Never chain a state-changing step with a step that can be blocked.** Keep
`cd`, heredocs, file writes and restore-from-backup in their own invocation,
separate from test runs, commits, pushes, or anything a guard inspects.

**After any block, verify state before continuing.** Run `pwd`, and re-check the
file you believed you wrote. Do not assume the earlier half of the command ran.
Prefer `git -C <literal path>` over a persistent `cd`, so a lost `cd` cannot
silently redirect later commands. Do the same for scripts by spelling them
`$ROOT/scripts/…` (`ROOT="$(git rev-parse --show-toplevel)"`), which is what
`.claude/commands/deep-review.md` requires — a bare relative `scripts/…`
resolves against whatever the cwd drifted to.

**But the path is not what decides which worktree a script acts on. The PROCESS
CWD is.** Some scripts derive their target from the directory they run in —
`review_state.py`'s `evidence-path` and `mark` resolve the worktree via
`git rev-parse` with the inherited cwd, and never read `sys.argv[0]`. So run
those FROM the worktree they are about, in their own invocation, however you
spell the path. Get this wrong and review evidence is written under another
worktree's key, and the depth gate then blocks on a file you never wrote.

MEASURED 2026-08-28, with controls in both directions: from one cwd, the
relative, absolute and `$ROOT/`-prefixed spellings of `review_state.py` all
printed the SAME evidence path; the same absolute path run from three different
cwds printed three DIFFERENT ones. Path form: no effect. Cwd: decisive.

Measured cost in one session: four heredocs that never wrote, a
restore-from-backup that never ran (leaving a file deliberately regressed), and
a `cd` that never happened — so edits landed in the **wrong worktree** and had
to be reverted as cross-branch contamination. Guard false-positives make this
worse: `shell_parse` mis-parses backslash line-continuations and quoted heredoc
bodies, so legitimate commands get blocked too.

### Editing source in bypass/auto mode: reads via Bash, WRITES via Edit/Write

In bypass or auto permission mode the CC binary injects a meta message telling
the session to prefer Bash (`sed`, heredocs, scripts) over Read/Edit/Write —
and NOT only there: a third selector branch is a cohort assignment, so a
default-mode session can receive it too. It is an EFFICIENCY heuristic making no
correctness claim, which follows from how the message is phrased (a preference,
with its own escape hatch back to the dedicated tools) rather than from where it
is gated (provenance + full measurement:
`docs/reference/cc-compatibility.md` "Bypass/auto mode tells the agent to edit
via Bash"). For reads and search it is simply right — use `cat`/`sed -n`/`grep`/
`find` freely. For WRITES TO SOURCE it is not: a bare `sed -i` returns exit 0 on
an absent anchor (silent no-op), rewrites ALL matches of a non-unique one, and
silently hits the wrong line on a regex-metacharacter anchor where a literal was
meant — where Edit fails LOUDLY on each. A Bash write also fires none of the five
`Edit|Write` PostToolUse hooks (the repo's own post-edit verification plane) and
a heredoc sits in the blocked-compound blast radius above. MEASURED: 54.9% of
real edit anchors in this repo carry a character `sed` treats as special in BRE,
its default dialect (2,750 / 5,013; an ERE set scores 74.0% over the same
corpus, so quote the dialect or the number misleads) — the unsafe case is the
common one. Use Edit/Write for source; a script that does a literal replace AND
asserts its occurrence count is acceptable (it re-implements Edit's checks). This
is universal — the machinery is tracked, so every clone inherits it.

### Instance-Fix vs Class-Fix Gate

When a mechanism failed to write or propagate something (a memory, a
directive, a config row, a status flag), hand-writing the missing
artifact is a **data repair** — it mitigates ONE instance on ONE
install. It is never the fix. Before reporting anything as "fixed",
classify it:

- **Data repair** — you wrote the artifact the mechanism should have
  written. Label it "data repair" explicitly, and in the same session
  either fix the mechanism or get the user's explicit deferral (the mechanism
  fix recorded as an ISSUE; a local row only if the deferral itself needs
  tracking).
- **Class fix** — you changed the mechanism so the artifact is written
  correctly on every install, going forward, with a test proving it.

The test: "If a fresh install hits the same situation tomorrow, does my
change help them?" If the answer is no, you have repaired data, not
fixed anything. (Origin: 2026-07-17 — a stale-decision recurrence was
"fixed" with a hand-written memory + directive; the propagation
mechanism that failed to write them stayed broken.)

### Debugging Discipline (phase-gated)

Adapted from superpowers `systematic-debugging`. "Find root causes" is a value;
these are the GATES that make it enforceable:

- **Iron Law: no fix proposals until root-cause investigation completes.**
  Investigation (read the full error, reproduce, check recent changes, gather
  evidence) is a phase that FINISHES before any fix is proposed — never propose
  fixes in the same breath as the symptom. "It's probably X, let me fix that"
  = investigation skipped.
- **A root cause is VERIFIED TO CERTAINTY, or it is not a root cause** (from
  #1605). A mechanism you have not tested is a HYPOTHESIS, and stating it in the
  grammar of a diagnosis is worse than saying nothing: the fix is then designed
  against fiction and looks correct while the real cause keeps firing. Three
  rules make it enforceable:
  1. **Name the artifact that would falsify it, then go get that artifact.** A
     cause is verified when a specific query, file, timestamp or run comes back
     the way the cause predicts and could have come back otherwise. "Consistent
     with" is not verification; a wrong cause is usually consistent too.
  2. **A subagent's or reviewer's stated mechanism is a LEAD at its stated
     confidence**, and it does not become fact by being repeated: re-derive it
     per CLAUDE.md "Verify agent output".
  3. **Enumerate the population, not a sample, and report the outliers.** N
     instances explained by a mechanism is a claim about those N. If k of N do
     not fit, say "k of N unexplained" rather than generalising from the
     majority; the outlier is where the second bug lives.
  (Origin, 2026-09-02: an AI-attribution leak into the public repo was relayed as
  "dispatched sessions don't load project settings", a 70% hypothesis restated as
  cause. Measured: 10 of 11 leaking commits came from worktrees whose checkout
  PREDATED the setting, and 1 of 11 stayed unexplained. A fix aimed at the
  speculated cause would have shipped clean and changed nothing.)
- **Fix-attempt cap: 3 failed fixes → STOP and question the architecture.**
  The debugging twin of the review escalation cap, with the same mechanics
  (count attempts visibly; the cap consumes standing approval). Each failed fix
  revealing a new problem in a different place is not bad luck — it is the
  signature of a wrong architecture or a wrong problem statement. Do not
  attempt fix #4; bring the pattern to the user.
- **Date the code before classifying a red — a stale tree fabricates live
  blockers.** MEASURED 2026-09-02: the main worktree sat at ONE commit from
  09-01 13:40 to 09-02 19:06. A test run against it at ~18:05 failed
  reproducibly and was reported as a live repo-wide blocker; it had been fixed
  at 15:47 that day by a merged PR. The failure was real, reproducible, and
  describing state that no longer existed. Nothing warns you — `git log --all`
  and `git status` both work perfectly on a stale tree, and `git log --all`
  even SHOWS the fix, because the fetch is fine and only the checkout is old.
  Before calling any red live, FETCH AND COMPARE REFS —
  `git fetch origin main --quiet` then
  `git merge-base --is-ancestor origin/main HEAD`. Do NOT date the code
  (`git log -1 --format=%ad -- <file>`) or read the reflog: neither compares
  the checkout against current `origin/main`, and both mislead in BOTH
  directions — a current tree holding an unchanged old file looks stale, and a
  stale branch carrying one recent unrelated commit looks current. Dating it
  can therefore reproduce the exact false blocker this bullet exists to
  prevent.
  **The same trap applies to your TOOLS, which is easier to miss**: a worktree
  carries its own copy of `scripts/`, so a script run from an old branch is the
  OLD script. MEASURED the same day — `git_push_guard.py --check-pr 1611` from
  a days-old worktree reported `ci: pending` where the current copy reported
  `ci: green`, same PR, same minute. Run repo tooling from a tree at
  `origin/main`, not from whatever branch you happen to be on — and verify
  that with EQUALITY, not the ancestry test above:
  `[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]`. Ancestry is
  satisfied by any branch that merely CONTAINS main, including a PR branch
  that MODIFIES the tool — which is precisely the case where the verdict
  differs and the one you must not run.
  *"Verify against actual code" needs the companion "verify against actual
  CURRENT code."*
- **Boundary instrumentation for multi-component failures.** When the path
  crosses components (hook → server → engine; CI → build → deploy), don't
  reason about where it breaks — LOG entry/exit at each boundary, run ONCE,
  and let the evidence localize the failing component before investigating it.
  (The "starved vs broken" check — inputs before regression-hunting — is the
  special case of this.)
- **Read the reference implementation COMPLETELY before deriving from it.**
  When a change must mirror what another subsystem does (what routing consumes,
  what the runtime resolves, what a protocol expects), read that subsystem's
  path END-TO-END first and derive from its own code/loader — never
  incrementally guess-and-patch toward it. Incremental spec discovery is how a
  review loop runs 7 rounds. (Origin: PR #1281 — the onboarding floor's key
  list was wrong three times until it was derived from the router's own
  `load_config` + call-site chains.)
- **Spec required-sets: enforce the WHOLE set at once, and lock it with ONE
  test.** The spec-facing corollary of the rule above. When a change must make
  code satisfy a canonical spec's *required-set* — a prompt's required JSON
  fields, a validator's mandatory keys, an allow-list — read the spec's required
  list in full and enforce ALL of it in one move; then write a single test
  asserting the required block == the complete canonical set (not one assertion
  per field). Adding only the field a reviewer just flagged leaves the next one
  for the next round, and a per-field test goes green while the next missing
  field ships. (Origin: PR #1333 — `_SALVAGE_PROMPT`'s Required block was
  completed one field at a time across three Codex rounds — cognitive_state_update
  → confidence → observations — for what `REFLECTION_DEEP.md` declares as one
  closed set `{observations, confidence, cognitive_state_update}`.)
- **Condition-based waiting.** When a fix or test must wait for a state change,
  poll the CONDITION (with a bounded deadline), never sleep an arbitrary
  duration — arbitrary sleeps are flaky under load and slow everywhere else.
  This complements the Timeout Policy (which governs the values).

### Test-First Discipline, continued

The bug-first and verify-RED core is in
`.claude/skills/genesis-development/references/commit-path.md`.

- **A RED that comes back GREEN has AT LEAST six causes, and "the test is
  vacuous" is the LAST one to reach for.** In rough order of how often they
  actually occur:
  1. **The run never EXECUTED** — a guard refused it, a lock held it, the tool
     timed out — so there is no result at all. This is the CONFIDENT FALSE
     NEGATIVE: a sweep reporting "all mutations survived" is far more often a
     sweep that never ran. Make the runner ABORT when the test command emits no
     result line, and treat a SKIPPED/deselected line FOR THE TEST UNDER
     VERIFICATION the same way — it is a result line, and it still means
     nothing ran. Scope that check to the target: a suite carrying legitimate
     `skipif` tests emits SKIPPED lines on every healthy run, so a runner that
     aborts on ANY of them refuses every run — and the agent then either sits
     blocked or starts stripping skip markers to unblock itself.
  2. **The MUTATION silently failed to apply** — the auto-formatter reflows
     lines and a `str.replace()` anchor written from memory then matches nothing.
  3. **The test ran against a DIFFERENT COPY of the code** — an installed
     package shadowing the source tree, a stale `.pyc`, the wrong virtualenv, or
     (measured, this session) a path relative to a process whose cwd had moved to
     another worktree. The mutation applied, the run happened, the test is sound,
     and none of the other causes fits.
     **A test module can manufacture this for everyone else**, which is the
     variant that hides longest: loading a script under a SHARED `sys.modules`
     name and not restoring it. pytest imports every test module at COLLECTION,
     so the last registration wins for the session — a module collected earlier
     keeps the object it bound while production code doing a call-time
     `from <name> import …` resolves the newer one, and
     `monkeypatch.setattr(<module>, …)` then patches nobody. It passes in a
     single-file run and fails only in the full suite, in collection order, so it
     reads as a bug in whichever test depended on the patch. MEASURED on main:
     four such modules, across `review_state` and `review_scope` — both imported
     at call time by the commit gate. Use `tests.conftest.private_module`, which
     registers, execs and restores; never hand-roll the sequence.
  4. **The mutation was BEHAVIOURALLY NULL** — it applied and parses, so every
     postcondition below passes, but it changed no behaviour: swapped operands
     that commute, an edit inside a dead branch, a type annotation Python does
     not enforce (also measured this session). The remedy is a different
     MUTATION, not a different test.
  5. **A SIBLING LAYER still enforces the invariant**, so the green is correct.
     When two layers produce the same behaviour, mutate the WHOLE mechanism, not
     one of its halves. (The duplicated-layer anecdote in the review-loop section (closing-session)
     is the worked example.)
  6. **A SIBLING TEST LEAKED STATE that masks the mutation** — an undone
     `monkeypatch`, a stray `os.environ` entry, a mutated singleton or a
     module-level cache — so the mutated path is never reached in THIS run.
     It matches none of the five above: the run executed, the mutation applied
     and is not behaviourally null, the code is the right copy, and no
     production layer is enforcing anything. MEASURED in this repo: a leaked
     disable lever made the very lock under test a no-op, and five real
     failures read as a story about the mechanism instead. The remedy is test
     ISOLATION (an autouse fixture that clears the lever) — NOT a different
     mutation, and NOT a different test.
  Only after all six: the test is vacuous. The list is ordered and still not
  closed — if none of them fits, the vacuous conclusion is UNPROVEN rather than
  established: look for the cause you have not modelled before rewriting a test
  that may be sound, because rewriting a sound test is the expensive mistake
  here.
  The PRINCIPLE, which is what to remember:
  **every injection must prove it applied, by its own postcondition, before any
  result is read as RED.** Two corollaries follow, and both have bitten:
  prove it against the value THAT injection was handed, never against the
  pristine original — with two or more edits, the first edit keeps a whole-file
  `mutated != original` true while a later one silently misses its anchor, so
  the partial mutation reads as complete (assert the anchor matched the expected
  number of times); and prove it with the mutated file's OWN parser —
  `compile(src, path, "exec")` for Python (NOT `ast.parse`: that only builds a
  tree, so it accepts context-invalid constructs like a `return` moved outside a
  function or a `break` outside a loop, and the `SyntaxError` then surfaces at
  COLLECTION, where a nonzero exit reads as a successful RED), `bash -n` for
  shell — since an invalid mutation breaks collection and reads as a successful
  RED, while the wrong language's parser
  rejects a valid mutation and hides a real survivor. Restore from a file copy
  taken beforehand, never `git checkout` — the work is uncommitted — and make
  the restore ATTEMPT unconditional ON THE RUN'S OUTCOME (`trap restore EXIT`, a
  `finally:`), never the tail of an `&&` chain. Unconditional ON THE OUTCOME means
  it runs whether the run passed or failed. It does not mean it survives every
  signal. MEASURED 2026-10-22 on bash 5.2 and Python 3.12, with no signal handler
  installed: a bash `EXIT` trap ran on SIGTERM, SIGINT and SIGHUP; a Python
  `finally:` ran on SIGINT but NOT on SIGTERM or SIGHUP; neither ran on SIGKILL.
  A Python harness stopped by SIGTERM (a timeout) or SIGHUP therefore leaves the
  file mutated unless it installs handlers that turn those signals into an
  exception (see
  `.claude/skills/genesis-development/references/high-stakes-verification.md`,
  "Control the instrument"). Against SIGKILL nothing in-process helps, and the
  next run's baseline is taken from the mutated file: mutate a disposable copy,
  or check against a pristine original kept outside the process. Nor does unconditional mean the restore always OVERWRITES: what it writes
  is still gated on the hash check below. A `trap` that restores blindly is the very thing that destroys a
  concurrent edit. The expected outcome here is a NONZERO exit, and under
  `set -e` a trailing restore is exactly the statement that never runs (the
  `out=$(cmd)` entry in Common Traps is the same mechanism), so the shape that
  reads as careful leaves a deliberately-broken file in an uncommitted worktree
  on the ordinary path — as well as on interruption or a tool timeout. Take
  that copy ONCE for the whole sweep and refuse to start EACH cycle if the file
  already differs from it — a copy re-taken immediately before each mutation
  makes the check vacuous, since the file trivially matches a copy a moment
  old; the baseline exists to catch a PREVIOUS cycle that failed to restore, or
  a concurrent edit. Verify the restore by hash rather than assuming it. And
  restore ONLY what you broke: compare against
  the hash the MUTATION wrote before overwriting, because between the mutation
  and the handler another session, agent or formatter may have edited that file,
  and a blind snapshot restore silently destroys their uncommitted work — a
  final hash check does not catch this, it only confirms the overwrite
  succeeded. If the file no longer matches what the mutation wrote, PRESERVE it
  and report the conflict instead. The cleanest way to avoid the window entirely
  is to mutate inside an isolated worktree nobody else is editing.
- **A RED for the WRONG REASON is not a verify-RED — read WHICH assertion
  fired, never just that one did.** The six causes above all ask why an
  expected RED came back GREEN. This is the mirror, and the dangerous half of
  it passes review: the run failed, so the checklist is satisfied and the test
  is trusted, but it failed on something other than the defect. **The tell is
  any failure line you have not actually read.** Two shapes, and they cost
  differently:
  1. **The PRIMARY assertion fired, but on a condition the FIXTURE created**
     rather than the defect — a leftover competing candidate, an unaged file,
     stale state from setup. This one goes green on the fix, because the fix's
     side effects remove the condition, so it passes review and pins nothing.
     This is the expensive one.
  2. **A guard-the-guard / precondition assertion fired FIRST**, which proves
     the fixture never built the hazard at all. This one does NOT go green on
     the fix — the fixture is unchanged, so it fails again and says so. It
     costs a round, not a false green, and the guard is doing exactly its job
     (see the vacuous-shapes bullet below, which prescribes adding one).
     Repair the fixture and re-run before recording a RED.
  MEASURED twice in one session on the same PR: a fileless-session test whose
  file was not yet aged past the 60-second window, and a selection test with a
  competing candidate still in the tree — each reported a textbook RED while
  exercising nothing. Name the assertion that fired and confirm its message
  describes the defect. A RED you have not read is a GREEN you have not earned.
- **A fixture must create the shape its docstring claims — and the check is the
  guard-the-guard assert, not a re-read.** This is the same obligation as the
  vacuous-shapes bullet below; the addition here is WHERE the shape most often
  goes wrong, which is a contract between two components. Two ways to break it,
  both MEASURED in one session, and both PASS against the unfixed code — which
  is the only way they are caught: a **hand-written intermediate** (the test fed
  the consumer a value the real producer never emits, so the arity mismatch it
  claimed to pin was never present) and an **inlined copy of the fix** (the test
  ran the corrected logic as a snippet instead of invoking the real script, so
  it graded its own copy and the shipped file never executed). If the defect
  lives in the seam between a producer and a consumer, drive the REAL producer
  into the REAL consumer; anything typed by hand in the middle is the bug's
  hiding place, and anything inlined is a second copy of the code under test.

- **Vacuous-test shapes to check for by name** (a list of the common ones, not a
  definition). The most frequent in practice is the one that never ran at all: a
  test SKIPPED by a marker, or deselected by a `-k` filter or a wrong path, which
  reports SKIPPED or "no tests ran" and never goes red — check the count, not just
  the absence of failures. Beyond that, a test is vacuous when: its
  assertion is ALSO true on the success path (`assert x.blocked is False` where
  a successful call also returns False — assert the fact that DISTINGUISHES
  them); its setup short-circuits the path it names (passing an explicit
  argument the code prefers over the env var under test); or its fixture never
  creates the shape it claims (a `bash -c 'sleep 30 # marker'` decoy
  exec-replaces itself and loses the marker from its argv — add a
  guard-the-guard assert that the fixture really has the property); or the code
  under test HEALS the condition before the guarded step runs (an earlier
  `git diff` refreshes the index as a side effect, so a test of an
  index-refresh guard passed with the refresh deleted). To test a guard against
  a transient condition, inject the condition AFTER the last step that could heal
  it, and prove it with a mutation. Ask of every
  new test: *would this still pass if the mechanism it names were deleted?*
  When the DISTINGUISHING fact is produced by a lane the assertion cannot see,
  asking every test to assert it is a convention, and conventions decay — the
  merge gate's own suite carried that request in a fixture docstring and 3 tests
  had already drifted from it. `tests/test_hooks/conftest.py`'s `offdiff_lock` is
  the worked chokepoint: it fails any IN-PROCESS hook test whose finding was
  silently discounted as outside the diff, so a forgotten allowlist entry can no
  longer turn a not-block assertion green for the wrong reason. It fails OPEN,
  so its own self-tests are the load-bearing part — a lock nobody checks is the
  convention it replaced.
- **Contested/subtle specs: write the expectations first.** When what-should-
  happen is itself under discussion (which keys count, which states clear an
  alarm), enumerate the expectation table as failing tests BEFORE implementing
  — it forces the spec question to surface at design time instead of review
  round 4.
- **Corpus replay cannot find a false-POSITIVE class — generate the matrix.**
  Replaying real recorded inputs proves only that shapes you have ALREADY run
  still behave; it is structurally blind to a shape you have never typed. A
  guard change measured "0 false positives across 18k real commands" and still
  hard-blocked an ordinary command, because the corpus happened to contain no
  instance of the one shape that mattered: a construct the parser mis-handles
  that ALSO leaves the guard with no parsed segment. For any classifier
  or guard, enumerate the CROSS PRODUCT of the axes that actually drive the
  decision — {operation} × {the constructs your parser can mis-segment} ×
  {evidence present, absent} × {interactive, unattended} — and assert the invariant per
  cell, so an untested cell fails
  loudly instead of silently. Keep the corpus replay as the realism check; the
  generated matrix is the coverage check FOR THE MODEL YOU DECLARED, which is
  the most it can be — a construct you never thought of has no cell to skip and
  so passes in silence, which is the same false confidence one level up. Naming
  it "the coverage check" without that qualifier is the trap. Discovering new
  axes is a different instrument: differential or property testing against a
  canonical parser, which fails on shapes nobody enumerated. Skip a cell only
  with an explicit reason recorded in the skip, since a silent skip and a hole
  look identical.

  **Generate the cells from the GRAMMAR of what the rule flags, not from
  history.** The axes above are the shape; this is where they come from. MEASURED
  2026-09-09, after three corpus-based measurements had each missed a real
  regression: enumerating {program} × {option form} × {construct} × {position} —
  the axes the rule itself keys on — and running EVERY cell through real `bash`
  via an argv-printing shim AND through the real guard subprocesses on BOTH trees
  produced 746 cells and, more to the point, a control that MOVED: `main` 744
  ALLOW / 2 BLOCK, PR head 339 / 407, after the fix 534 / 212. That surfaced 206
  over-blocks which a 129,179-command corpus had shown none of. A second sweep
  built 384 cells over the same four axes. Run the matrix against main as well as
  against your branch, and compare it PER CELL, keyed by input — never by the
  aggregate counts. Two cells can swap ALLOW and BLOCK while the totals sit
  identical, and a resource-only fix legitimately preserves every verdict (the
  next bullet is exactly that case), so a matching distribution proves nothing in
  either direction. Establish liveness separately: use a deliberately mutated
  control or another known outcome that the harness must distinguish. A correct
  resource-only fix can preserve every production verdict, so it need not make
  the two production trees differ.
  **Instrument the probe before you trust 746 of anything.** These guards emit
  ASK as JSON on stdout with EXIT CODE 0, so a probe reading only the return code
  cannot tell ASK from ALLOW — and ASK is precisely the state a newly added flag
  usually produces, so the sweep reports "no change" while the new flag fires on
  every cell. Combine exit status with the JSON payload when status is zero:
  BLOCK is the non-zero gate result, while ALLOW and ASK are distinguished by
  the payload. Prove the harness can distinguish each outcome on known inputs
  before believing any aggregate it prints.
- **A correctness test cannot see a RESOURCE defect — and on a path the harness
  can SIGKILL, that defect is a bypass.** Verifying a change three ways is ONE
  verification when all three ask the same question. MEASURED 2026-09-09 on an
  in-flight fix that added a new scan to the shell parser the hook stack shares:
  it was checked at the parser level, at the guard level, and against the
  specific exploit it existed for — three passes, all CORRECTNESS, all green —
  while the scan itself was quadratic in the length of a SINGLE token, the
  ordinary accidental-O(n²) shape of a nested scan with no early exit. On the
  linear form it held flat at 0.004s (4ms) at every length; on the pathological one
  it ran to SECONDS, then minutes, at input sizes a caller can simply type. A
  PreToolUse hook that overruns its wall-clock is SIGKILLed, and a killed hook
  fails OPEN — the guard code says as much in its own comments — so a scan that
  can be made slow enough disarms the gate rather than tripping it, and because
  the parser is shared it does not stop at the one guard being edited. A FOURTH
  correctness check would have passed too.
  So, for any code the harness can kill: ask the three questions the correctness
  suite never asks — what is its complexity, what input maximises it, and what
  does the system do when it does not finish. Where "does not finish" means
  PERMIT, a super-linear scan over attacker-influenced input IS the defect,
  however correct its output. Measure its worst-case runtime in a controlled
  performance probe, and keep a deterministic regression test for the bounded
  algorithm in CI; a wall-clock assertion in an install-agnostic test is not
  reliable. Record the measured margin against the smallest timeout on its path.
  (The Generalizability
  Gate's "retention machinery does not belong on a hook path" is the same
  mechanism met from the other end: work that scales with the input, on a path
  whose deadline is a security boundary.)
  **This bullet is deliberately imprecise, and that is the second rule in it: a
  lesson about a vulnerability does not need the vulnerability's parameters.**
  The shape teaches — super-linear on attacker-chosen input, measured at seconds
  where the linear form completes in milliseconds, on a path that fails open. The exact
  input sizes, the timeout tier they defeat, the count of guards behind the
  shared parser and the quoted fail-open line teach nothing further; they only
  compose into a working recipe, and one that keeps working for every install
  still on the pre-fix code. Nor does "the fix has landed" license the specifics
  — check whether it landed on `main`, not whether a PR for it exists.
- **Anti-patterns (binding):** never assert on a mock's behavior when the real
  code path can run; never add test-only methods/branches to production
  classes; fakes implement the real contract (real method names, real return
  types — import them). Test setup so complex it needs its own debugging =
  the design is too coupled; fix the design.

### Advisory is the default; a BLOCK is what needs the argument

Before the fail-direction question below, there is a prior one, and it is the
one that gets skipped: **should this refuse at all?**

**Advisory is the default and the first step.** Escalating to a BLOCK needs a
specific, credible, MEASURED reason — the risk is that severe, or that frequent.
*"For safety"* and *"defense in depth"* are not reasons. Neither is having just
been bitten once: an n=1 incident earns a COUNTER, not a gate.

**Persisted state inside a guard needs its own justification, separate from
the decision to refuse.** Before a guard or gate grows a marker, cache or
counter that one invocation writes and a later one reads (a telemetry counter
no guard decision reads is observability, not guard state), show why the answer
cannot be derived at the moment it is needed from state that already exists,
and enumerate the lifecycle the state adds — write, read, retire, validate,
scope — because each stage is a defect site. Where a derivation exists, a second
should-fix-or-worse finding in machinery the change adds means delete it, not
harden it; where none exists, narrow the lifecycle. Approval records are out of
scope for deletion. MEASURED on the gate-menu feature at comparable size: the
persisted-marker layer drew roughly 15 should-fix-or-worse findings against
roughly 3 in the half it served (179 vs 155 non-comment lines, per #2027's PR
body); the marker designs were closed (#1863, #1999) and the counter-based one
merged (#2027). This prices the cost of STATE, not the case for gating; the
New-Store Gate (commit-path.md) asks the neighbouring question (why a NEW store rather than
an existing one), and the `genesis-architect` agent's Step 0.7 asks this one at
review time.

**Fail-open is not an automatic defect — but say WHICH question you are
answering, because there are two and they get opposite defaults.** (1) The
VERDICT question: when a guard evaluates successfully, should its design be an
advisory or a block? Advisory by default, per the above. (2) The DEGRADE
question — what a guard does when it CANNOT evaluate — is decided per boundary
by the section below, and many of this repo's Bash hooks are REQUIRED to fail
closed there by the degrade-direction contract test
(`test_every_bash_hook_declares_its_degrade_direction`); nothing in this
section loosens that, and satisfying it with a false advisory claim in the
exemption list is the named failure, not compliance. The claim here is about
(1): a control that fights the operator daily against a harm that is usually
hypothetical is a cost, not a defense. Ask which direction THIS boundary should
fail in — open, closed, or open-and-loud — from consequence, never from which
one sounds safer.

⚠ **This governs GUARDS — advisory nudges and friction hooks — never APPROVAL
BOUNDARIES.** Anything that gates autonomy expansion, spending, destruction of
data, or publication to an external surface is a sovereignty boundary, not
friction to be tuned: never a downgrade candidate, and proposing to relax one
is itself the failure. The autonomous-CLI gate, ego proposals, per-transaction
financial approval and destructive-command confirmation are canonical members
of that class, not an exhaustive list — classify by what the boundary GATES,
never by whether it appears here.

Two consequences worth stating, because they are easy to get backwards:

- **Blocking stops foreground and background equally; advisory stops neither.
  Both are fine.** What is NOT fine is an ask that reaches an UNATTENDED
  session — an ask with no human present is a block nobody intended — or a
  block that impedes background work nobody meant to impede. An ask leg is
  legitimate exactly where the shipped ones sit: at an approval boundary in a
  foreground flow, degrading to a DEFINED verdict when dispatched (the push
  guard's ask converts to deny under `_is_dispatched()`) rather than hanging.
- **A guard is written for the agent, not the user.** Nine times in ten the user
  need not know it fired.

This is a design axiom, not a preference: changing it is a redesign
conversation, not a session's call.

### Guard failure semantics — the third option is fail OPEN, LOUDLY

Every guard has TWO INDEPENDENT axes when it cannot evaluate, and collapsing
them into a single "fail-open or fail-closed" choice is what produces the bad
design:

- **DIRECTION** — what happens to the DECISION: open (the action proceeds) or
  closed (it is refused). Decided per boundary, by consequence.
- **VISIBILITY** — what happens to the FAILURE: silent, or recorded where a
  later reader will actually find it. **Silent is never correct, in either
  direction.**

The third option is the one that gets forgotten: not open, not closed, but
**open and LOUD** — the action proceeds AND the guard's inability to evaluate
becomes a durable record plus an alert.

**THE TEST IS THE REPAIR PATH, and it is measurable — do not reach for a
judgement about severity.** Ask: with this failure present, can the session
still REPAIR the failure? Name the operations repair needs — read a file, edit
it, restore it, restart a unit — and check whether your refusal predicate
matches any of them. If the repair path survives, fail-closed is available. If
it does not, fail-closed is not "safer", it is a BRICK, and the answer is
open-loud however severe the thing you wanted to stop.

The test has a worked precedent, and it is worth reading for the SHAPE of the
argument rather than as a verdict. PR #2042's body justifies its fail-closed leg
by naming what survives — *"Read/Edit/Grep are ungated and `git checkout --
<file>` matches none of the matchers"* — which is exactly this question, asked
and answered with a measurement. (The quote is from the PR BODY; the function's
own docstring makes a related but weaker claim, so cite the PR, not the symbol.)

Two cautions travel with that precedent, both MEASURED, and they are why the
test is a question you re-ask rather than a conclusion you inherit. First, an
ungated repair verb is not automatically a SAFE one: in that same degraded state
`git_discard_guard` no longer takes its recovery snapshot, and its own
`also_lost` notice says so — *"a discard run now is not recoverable from it"* —
so `git checkout` being unmatched means it RUNS, not that it is safe to run
broadly. Second, the precedent covers only ONE of that helper's two failure
legs; the other is the worked example in guard-failure-regimes.md of the test returning the opposite
answer.

SYSTEMIC failure (no usable predicate survives) defaults to open-loud (owner
ruling 2026-09-15). The regimes and the `degraded_exit` / `pretool_check` worked
examples: `.claude/skills/genesis-development/references/guard-failure-regimes.md`.

**PARTIAL failure means a usable predicate survives**, and fail-CLOSED stays
correct there — `run_guard` is the other merged instance (the first, `degraded_exit`'s
predicate-gated leg, is in guard-failure-regimes.md) (an uncaught crash in
an irreversible-action guard exits 2 instead of the exit 1 CC treats as
non-blocking). What licenses them is MEASURED. Six guards are wired to
`degraded_exit`, and their predicate-gated matchers refuse between **1.90% and
21.47%** of 74,282 unique real commands individually; PR #2042 measures the
UNION of four of them at **28.80%**, which it headlines as "roughly 29% of
ordinary work is refused while the tree is broken".

Read that union as the shape of an operability budget, NOT as its value: the
other two wired guards are outside it, so the real six-guard figure is unmeasured
and strictly higher. A seventh guard does not get to add its rate to 28.80% and
call the result the cost — that arithmetic is the population error this file
keeps catching. Measure the union you actually have. What each protects earns its
share: irreversible for `worktree_cwd_guard`, merely heavy for
`full_suite_guard` — a distinction taken from PR #2042's own table rather than
assumed, and a reminder that "what it protects" is a mixed bag even inside one
wired set.

`background_pipe_guard` is the one that was deliberately NOT wired: its degraded
matcher would refuse **70.30%** of that same corpus, and a guard refusing the
majority of ordinary work does not make the broken state safe — it makes it
unrepairable, which is the brick again by another route.

**Ambiguity about WHICH REGIME you are in resolves toward OPEN-LOUD**, because
that direction's worst case is a recorded mess and the other's is a system
nobody can reach.

**This section decides the REGIME; it does not decide the direction within
one.** Once you have established PARTIAL, the per-boundary mandate under "Never
hand-roll `gh`/bash/CLI argv parsing" and its corollaries (a)/(b)/(c) govern
which way that guard fails — and corollary (c) settles the unattended-session
case in the direction of REFUSING, after three narrowing rounds on one
predicate. Nothing here loosens it. What this section adds is the prior
question those corollaries assume has already been answered: whether a usable
predicate exists at all.

Four rules follow, each cheap:

1. **"Could not evaluate" is its own state.** Never render it as allow, clean,
   healthy, or no-finding. A check reports exactly one of four things:
   CHECKED-CLEAN, FINDINGS, COULD-NOT-CHECK, OUT-OF-SCOPE. The merge gate
   already works this way where it prints an unrecognised reviewer's comment
   rather than scoring it silently; `InjectionHealth.errors` does it by emitting
   a DEGRADED finding instead of resolving healthy.
2. **Write the degraded marker BEFORE the early return, with ZERO dependency on
   the component that failed.** A refusal path that records its reason only
   after the check it could not run has recorded nothing. READ, a sibling
   toolkit's 2026-09 incident: its blocking hooks refused every command for 30
   minutes while its own liveness canary reported OK — the dependency-missing
   branch printed its refusal and exited BEFORE the line that writes the
   heartbeat. The canary asked "did it receive a payload", never "could it
   evaluate one", so a guard refusing everything left exactly the trace a guard
   nobody called leaves.
3. **Degrade the RENDERER, never the VERDICT.** When the thing that failed is
   how you SPEAK — a serializer, a formatter, a size budget — the decision
   itself must still land. `print_json_bounded` is the in-repo instance: an
   oversized advisory loses prose, never its `permissionDecision`. **It also
   tells you when it could not keep that promise** — it returns False and emits
   ANYWAY when the envelope alone exceeds the budget or no named text key was
   trimmable, and CC discards an exit-0 hook's stderr, so that return value is
   the only signal a caller gets. Check it: "something was trimmed" is not "the
   decision landed". Another install's toolkit reaches the identical rule from
   the other side, rendering verdicts through a fallback chain after a broken
   serializer turned every refusal into empty stdout — which the harness reads
   as ALLOW.
4. **The watcher must detect COULD-NOT-EVALUATE, not merely DID-NOT-RUN.** A
   liveness check keyed on "is the timer active" or "did a heartbeat arrive"
   cannot see a guard that ran, refused everything, and wrote nothing. Ask what
   your watcher would report if the guard were evaluating nothing at all; if the
   answer is "healthy", it is measuring the wrong thing.

### Common Traps

- **`gh` listing caps:**
  `.claude/skills/genesis-development/references/gh-listing-caps.md`.

- **Fail-closed data access.** A data-access boundary must RAISE (or return a
  clearly-typed "unknown/unavailable") on missing scope or an unavailable
  dependency — it must NEVER silently return the wrong data, the singleton's
  data, or an empty result that reads as "all clear". A monitoring/consistency
  check whose dependency (Qdrant, FTS, a remote) is down reports `unknown`, never
  `healthy`/`degraded` — a dependency outage that masquerades as data
  corruption (or as cleanliness) is worse than a loud error. Prefer a helper
  that raises over one that swallows (the `batch_retrieve_point_ids`-raises vs
  `batch_retrieve_vectors`-swallows split exists for exactly this). Origin: the
  home-anchored-DB reads that silently returned no data from an empty worktree
  path, and the memory-integrity checker (2026-07).
- **Never replace a runtime the CURRENT session depends on — that is a HANDOFF,
  not a repair.** Upgrading, downgrading, reinstalling or removing the
  interpreter, Node, the CC binary, or the venv that the ACTIVE session (or its
  hooks, MCP servers, or test runner) is running on kills the thing performing
  the repair, mid-repair — and the second half of the procedure, the half that
  puts the replacement in place, never runs. The safe sequence is side-by-side:
  install the replacement ALONGSIDE, verify it works, switch the pointer, and
  only then remove the old one; and run the whole procedure from OUTSIDE the
  dependent session (a separate shell, a systemd unit, the host Guardian).
  READ, a sibling install's 2026-09 incident: a rollback script uninstalled the
  running Node before installing the intended version, the uninstall terminated
  the agent session executing it, and the machine was left with neither. Before
  touching any runtime, inventory what is running on it.
- **Silent skips are banned (provision-or-surface).** A setup/resilience
  feature that gracefully skips on a missing prerequisite (a package, a
  host knob) must either PROVISION the prerequisite (bootstrap.sh /
  host-setup.sh / a guardian reconciler) or register an effective-fact in
  `infra_profile` that the awareness posture check
  (`awareness/loop.py::_check_infra_protection_posture`) reads — so an
  unprotected box raises a standing alert instead of staying silent. A
  graceful skip with neither = a box that runs unprotected with zero
  signal (a sibling install ran weeks without swap/systemd-oomd until a
  memory spike wedged it, 2026-07). Guardrail:
  `tests/test_awareness/test_infra_protection_posture.py`.
- **Modules are NEVER subsystems.** A capability *module*
  (`src/genesis/modules/**`, an external pluggable capability — "hands,
  not brain", see `modules/base.py`) is not an internal Genesis
  *subsystem* (memory, reflection, ego, triage, autonomy, sentinel).
  Module memory writes must **never** set a `source_subsystem` value —
  that tag means "internal decisional output, exclude from default
  recall", which is wrong for module output. This is enforced
  mechanically: any `.store()` under `modules/**` passing
  `source_subsystem` is a hard CI failure in
  `tests/test_memory/test_store_subsystem_coverage.py`, which also forces
  every new memory-writer to either tag itself or be explicitly
  classified as user-context. `_KNOWN_SUBSYSTEMS`
  (`memory/retrieval.py`) is the authoritative subsystem list; adding a
  module name to it is a category error.
- **Destructive data migrations must reconcile cross-store mirror fields.**
  When a cleanup/backfill deletes data in one store (e.g. Qdrant vectors) but
  another store mirrors that data's existence (e.g.
  `memory_metadata.embedding_status`), the delete MUST also fix the mirror
  field. A deleted vector left as `embedding_status='embedded'` is a field
  that *lies*, and that lie is not cosmetic if any code path *reads* it —
  `MemoryStore._mark_superseded` gates an `update_payload` on
  `embedding_status != 'fts5_only'` and would fire a doomed write on the
  now-deleted point. Before assuming a stale field is harmless, grep for its
  *reads*, not just its writes. (Bit us in the source_subsystem purge, #918;
  fixed by #921 Step 2c — reconcile tagged rows to `fts5_only`.)
- **`immutable=1` reads miss WAL-resident writes.** A read-only `sqlite3`
  connection opened with `file:...?immutable=1` reads only the main db file
  and ignores the `-wal`, so a change you JUST committed (still
  un-checkpointed) is *invisible* — you get a false-negative "the write
  didn't land." To verify a live write, use `?mode=ro` (WAL-aware) or query
  through the server/CRUD path; reserve `immutable=1` for historical
  read-only sampling where a little staleness is fine. (A reconcile UPDATE
  read clean under `mode=ro` but appeared unchanged under `immutable=1`.)
- **Never hand-roll `gh`/bash/CLI argv parsing inside a hook or security gate.**
  Re-implementing shell/`gh` command-line semantics by hand (regex, manual token
  walking) creates an effectively UNBOUNDED adversarial-divergence tail: a good
  reviewer (Codex especially) will keep surfacing real gaps from true semantics —
  `--repo`/`-R`/`-Rvalue`, `GH_REPO`, `cd`, `&&`, `||`, `--body-file -`, duplicate
  flags, URL-vs-branch targets, enterprise hosts, `--help`, nested `bash -c` — and
  each named fix ships the next round's bug. This is the mechanism behind the
  measured Codex review-loop (an internal analysis of recent review-looping PRs:
  first-round findings were real catchable bugs, but later rounds were dominated by
  fix-churn on the hand-rolled parser itself — no finite pre-push audit bounds that
  tail). The cure is architectural,
  not "review harder": bind atomically to the real tool (e.g. `gh pr merge
  --match-head-commit`) and use a canonical parser (`shlex`/`bashlex`) — never
  bespoke semantics. Choose the fail direction PER BOUNDARY by consequence: the
  shared parser must DEGRADE gracefully (fail-open, never crash — that is
  `shell_parse.py`'s stated contract), while each security-critical caller
  (merge/push authorization) treats an unparseable command as a block (fail-closed
  THERE). This bullet and its corollaries decide the DIRECTION for one guard's
  degraded path, and they assume a usable predicate still exists. "Guard failure
  semantics" above decides the prior question — whether one does — and when none
  does (wrapper, interpreter or venv dead, or one dependency taking every guard
  at once) its repair-path test governs instead of this bullet. A parser-wide absolute fail-closed is wrong — it would deny legitimate
  uncommon commands without closing evasion paths. Same family as the
  canonical-parser lesson (regex→yaml, #1393). Loci today:
  `scripts/hooks/shell_parse.py` + `scripts/hooks/git_push_guard.py`.

  **The boundary of the class — read this BEFORE you reason yourself out of it.**
  The tar pit is NOT "who tokenizes the string." Delegating tokenization to
  `shell_parse` and asking git for repo state does NOT exempt a guard: if the
  guard's CORRECTNESS depends on modeling what a git command WILL DO — which
  flags force, which operands are paths vs refs, which modes destroy, which
  repo is targeted — it is argv→EFFECT mapping, and that mapping is the same
  unbounded open-set surface as raw string parsing. This was reasoned around
  once already (2026-08-23, PR #1432): the guard used the canonical tokenizer
  and probed live git state, the author concluded "so it's not hand-rolling,"
  and Codex returned **13 real findings (10 P1) — every one of them living in
  the argv→effect layer**. The first architect finding of that shape (a
  separated global value-flag bypass) was the CLASS signal and got
  instance-patched; the next round found the rest of the class. n=1 IS the
  signal: any reviewer finding that exposes a semantic-modeling gap in a guard
  means STOP and re-architect — never patch the named instance.

  Git argv is one instance. The wider class is a check that authorizes a future
  operation by treating a present spelling or snapshot as proof of an unresolved
  property of that operation or its target. Examples: a filename standing for
  the file that will actually be opened, a path extension for artifact kind, a
  flag spelling for whether a command executes, or an unresolved prefix for
  directory containment. Running before a tool does not by itself make a check
  open-set: an exact closed protocol can establish a closed structural claim.
  The failure is using that claim as proof of an operation or effect it does not
  establish. On #2255's #1826 example, eight of eleven audit findings and all
  twelve defects across three review rounds had that approximation shape.

  **Decision test (apply before shipping any guard):** a guard may AUTHORIZE
  (allow or exempt) only on evidence that establishes the property it promises
  for the action it admits. An inode comparison establishes identity at that
  lookup; a later pathname open needs an action-bound identity guarantee or an
  explicitly stated race limitation. A structural parse establishes the syntax
  it actually parses, not arbitrary command effects. A single chokepoint is
  useful only when its observed fact governs the admitted action. An exact-form
  whitelist that fails closed outside its members can establish membership in
  that closed set; its effect guarantee still needs its own evidence. If the
  property cannot be resolved before the event, fail closed and say why, abstain
  and say so, or check it after the event where it IS resolved (snapshot, then
  compare); never let an approximation allow. A `deny` or `ask` on an imprecise
  predicate is a separate case ("Two things keep this from reading as licence",
  below). For git: could a git
  flag you've never heard of change your guard's verdict? If yes, your claim
  is open-set — redesign to closed-set token claims (exact-form whitelists /
  literal token blocks) or to RECOVERABILITY (snapshot-then-allow, where a
  miss degrades to the status quo instead of a broken guarantee). Do not ship
  the open-set version and plan to harden it later; the review loop IS the
  hardening loop, one bug per round, and it does not converge.

  **An instrument's stated invariant is its blind spot — and so is the one it
  does not state.** Before trusting a harness's clean result, read what it
  promises to hold fixed, then ask what ELSE it holds fixed without saying so.
  READ, and EXTERNAL to this repo — a sibling toolkit's own release notes,
  2026-09, not a Genesis measurement: a fuzzer whose mutators were all fixed
  one-character strings had a whole mutant shape unreachable by construction,
  and that writeup attributes its own long-lived bypass to exactly that. The
  remedy generalises even if the number does not: vary what the instrument
  never varies, rather than running it more times.
  Method for the whole class: `.claude/skills/genesis-development/references/high-stakes-verification.md`.

  **Three corollaries, each bought with a non-converging review loop.**

  **(a) NEVER normalize the command text before a blind-spot probe.** A probe
  whose whole job is "notice that this text is unparseable" must read the RAW
  command. Preprocessing it can only ever DELETE the evidence the probe exists
  to find. MEASURED: a normalization step added in good faith — to stop a class
  of ordinary command from prompting — turned that shape into a silent
  ALLOW on two independent guards. Ordinary, not adversarial: the shape was one
  a developer writes without thinking, and the command really executed (verified
  against a shimmed binary, so the proof was execution rather than parse). The
  normalizer removed the very evidence the guard keyed on, and its own model of
  the shell's comment syntax was narrower than the shell's, so it could delete
  executed code as well. It then turned out not to be load-bearing at all: every
  case cited to justify it was one where the parser already resolved the
  operation, so its branch never ran. The fix was a DELETION, and it closed both
  defects at once. When a guard loop will not converge, look for the component
  that MODELS shell semantics and remove it — refining it is the loop.

  Scope this to a probe. Normalizing before a *tokenizer whose tokens you are
  about to use* is a different act with a different fail direction, and a
  sibling guard does exactly that, deliberately. Before calling any such site an
  instance of this rule, the question to answer is whether the normalization
  CAN change the tokenizability verdict or hide a target — and that is a
  possibility question, so a count cannot answer it. Demand a STRUCTURAL
  argument. The model is already in the repo, at
  `scripts/hooks/destructive_command_guard.py:101-120`: its replacements delete a
  line continuation — which is what the shell does with it — or insert
  whitespace and separators; none introduces a quote or an escape, so none can
  corrupt the quote balance shlex decides on. (The one place the shell keeps a
  backslash-newline literally, inside single quotes, is an over-block the guard
  states rather than hides.) That is a `cannot` — but read the next paragraph
  before reusing it, because the version of this argument that stood here for
  weeks was WRONG in a way that shipped a live bypass.

  **What that earlier version got wrong, and why it is the sharpest example on
  this page.** It added: *"a backslash-newline is a continuation, not an escape
  the shell keeps."* That is true only when the backslash is itself unescaped —
  an ODD-length run. In an EVEN-length run every backslash is escaped by its
  neighbour, so the last one is a literal character and the newline after it is
  a REAL command separator. The guard folded it anyway, deleting the separator
  and gluing the next command's first word onto the previous token, so no `rm`
  token existed and a destructive command was ALLOWED — measured end-to-end
  through the live hook, exit 0 where the plain-newline control gave exit 2. The
  legacy regex net did not save it either: that fires only when tokenizing
  FAILED, and this tokenized fine, just wrongly. Fixed 2026-09-02 by folding
  only odd-length runs.

  Note precisely what was and was not at fault, because the first attempt at
  this correction got it wrong in an instructive way: it said the quote-balance
  reasoning "was CORRECT and is not what broke", and exonerated it. Quote
  corruption is indeed not the *mechanism* of the token-glue bypass — but the
  quote-balance `cannot` **was not sound either, and it failed in the very same
  cell**. Deleting one backslash from an even-length run leaves an ODD run whose
  survivor escapes the next character; when that character is a quote, the fold
  DOES introduce an escape and shlex's balance shifts. Measured: it turned
  parseable commands UNPARSEABLE (11 of 20,000 random parseable commands under
  the old fold, 0 under the fix), which dropped them into the legacy-regex net —
  a net that matches neither `-Rf` nor `-r -f` nor `--recursive --force` nor a
  quoted `'rm'`, so they failed OPEN. One quiet universal quantifier (*every*
  backslash-newline is a continuation) falsified BOTH claims at once, and the
  old code therefore had two bypass families rather than one. The fold now
  always leaves an even-length run, which is what finally makes the `cannot`
  true.

  A structural `cannot` is only as good as the case-split it rests on, so state
  the split explicitly and enumerate it — the same trap as the direction-claim
  two paragraphs down, which was true of the operand and false of the option
  token. That both the wrong premise AND the first correction of it were written
  in a document teaching this exact discipline is the point: the danger is not
  knowing the rule, it is believing you already applied it.

  A corpus run only ever yields `did not, here`, and by the rule above it is
  structurally blind to the shape nobody typed. Run the corpus as
  corroboration, never as the proof, and pair it with a control that DOES flip
  — an unflipped corpus and an inert measurement look identical.

  State the direction too, not just a total — and then check the direction on
  the token you did not think of. The same sibling's fold used to split a word
  the shell joins, and the paragraph that stood here said the verdict could
  therefore only move toward refusing. That was measured on path operands and
  was false on the option token: the split could hide the flags, and a spelling
  the shell runs as a recursive-force removal of a protected path was allowed.
  A reviewer found it from the diff; the earlier audit had split every position
  of a path and never the option, and its zero came from benign traffic with no
  true-positive control. The fold now deletes the sequence, which is what the
  shell does, and the guard is asserted to give the same answer for the
  continued and the joined spelling. The general lesson is the one above: the
  examples you enumerate are a sample, and a direction claim needs the cell that
  would falsify it.

  Deliberately stated without the triggering shapes. A guard's defeat
  conditions are not a teaching aid, and this file is public.

  **(b) When the parse is unreliable, keep the trigger BROAD.** Every narrowing
  conjunct became a new way to starve the trigger (measured; `git_push_guard.py`,
  the comment above `_GATED_MENTION`). Since the owner ruling of 2026-09-08 the
  git-operation net REFUSES an unreadable command that names a gated operation,
  with a message giving the cause and a rewrite; it no longer asks. A line
  continuation is reported as a blind spot (`_BLIND_CONTINUATION`) and refused,
  never parsed around. Wherever the paragraphs below say `ask` or "one
  confirmation" for this net, read "refuse with an actionable message".

  This does NOT loosen the fail-closed mandate above, and the two are easy to
  read as contradicting each other. Rule (b) is scoped to the git-operation
  blind-spot net — a guard whose trigger is deliberately broad and whose false
  positive is one confirmation. It is NOT a template for every guard: where the
  false negative is an irreplaceable path or a broad recursive removal and the
  false positive is a rewrite, that asymmetry justifies refusing outright rather
  than asking. Neither the protected-paths nor the destructive-command guard has
  an `ask` branch at all — both return only 0 or 2 — so a person's presence
  genuinely cannot change their verdict.

  One clause of that used to read "hard-block an unreliable parse", and it is
  FALSE. MEASURED 2026-09-06 through both live hook entries, controls flipping:
  each guard refuses on the PARSED path and then falls back to a DIFFERENT
  matcher, neither a subset nor a superset of it — it misses spellings the
  parser catches, AND it drops the target tests the parser applies, so it also
  refuses commands the parser allows. **A guard's fail direction is a property
  of its DEGRADED path, and that path has to be read in BOTH directions.** The
  two differ in what their fallback is for: `destructive_command_guard`'s is
  fail-open by design and says so in its own docstring, so argue with the design
  rather than patching it; `protected_paths_guard`'s calls itself "conservative:
  over-blocks, never under", which is true relative to the old guard it
  reinstates (`_legacy_substring_block`'s docstring) and false of the parsed
  resolver it stands in for — one sentence, two readings, and that ambiguity is
  the defect. Verify a docstring's stated direction IN CONTEXT; never quote it as
  a fact. The shapes are deliberately not enumerated here, per the rule above.

  That guard's MODULE docstring has since been split so it no longer states one
  fail direction for both blind spots: a bounds-induced blind spot REFUSES and an
  untokenizable one falls back. The sentence quoted above still sits on the
  fallback helper, where both readings remain available, so the lesson is
  unchanged — and note the citation here is now to a SYMBOL rather than to a line
  range, because the range this paragraph originally named stopped containing the
  quote the moment that docstring was edited. A line number is a claim with a
  shelf life.

  Within the git-operation blind-spot net, the two rules are scoped by who is
  present: `ask` is the interactive form of the refusal, and where a session is
  unattended fail-closed governs and (c) applies. The operation proceeds
  unverified in neither case.

  This is the shipped shape now, not an aspiration, and one distinction inside
  it must stay visible. The shared parser still degrades to a naive split with
  NO failure signal, so a caller that only asks "did I get a matching segment?"
  allows. What closes the hole is a separate conjunction AT THE CALLER — no
  matching segment, AND the raw text is un-tokenizable, AND it names a gated
  operation — which yields `ask` interactively and a refusal when unattended.
  The parser's contract did not change and must not: it degrades, the caller
  chooses the fail direction, exactly as the mandate above requires.

  Earlier revisions of this passage described that net in the present tense
  while it was still unmerged. Both directions of that error are worth naming,
  because fixing one produces the other: an unbuilt mechanism written as
  shipped, and then — once it does ship — a hedge left standing that now
  understates the tree. A status sentence in a durable document is a claim with
  a date on it. Re-check it against the code whenever the surrounding work
  lands, not only when it is first written.

  Corollaries of the corollaries, each measured: a net that returns inline
  PRE-EMPTS every gate below it (a hard block with no other backstop was
  observed downgrading to a prompt) — set a reason and DEFER it to the tail
  where the other decisions are resolved. And an invariant pair of the form
  "never silently allowed" + "never hard blocked" is satisfied BY a block→ask
  downgrade, so pin the verdict EXACTLY wherever a hard block is the contract.
  Read "never hard blocked" here only as the shape of the trap — as an actual
  invariant it is false unscoped, and (c) below replaces it.

  **(c) The ask-cost argument does NOT survive the move to a refusal.** Rule (b)
  originally bought its broad trigger with "a false positive costs one
  confirmation" (the retired ask; git history) — and that is true only where
  someone can confirm. An unattended session has nobody
  to answer, so the obvious completion of (b) is a deny leg for that path, and
  that is where it goes wrong: the SAME broad predicate whose errors were cheap
  now produces unappealable refusals of ordinary work. MEASURED: sharing one
  predicate across both legs refused routine, entirely benign commands in
  unattended sessions, in the one failure direction the design had been chosen
  to avoid.

  Narrowing the refusal predicate is the obvious repair and it does not
  converge, for a reason worth stating precisely, because the imprecise version
  of it is false. The claim is NOT that no raw-text rule could ever work — the
  raw text does carry quote and comment syntax, and a complete canonical parser
  could read it, which is what the canonical-parser rule above tells you to
  reach for. The claim is bounded to a predicate built on the SAME degraded
  parse that failed: at that point the guard cannot say whether an occurrence of
  a gated verb is executed or merely quoted, commented, or documented, and that
  inability is the premise of the net existing. A predicate with no more
  information than the failure itself cannot both refuse the hidden operation
  and permit the inert mention. Escaping that needs a different information
  source — a fuller parser, or enforcement at the execution boundary, where
  mentions are never classified at all — not a cleverer rule over the same text.

  Three narrowing rounds on one predicate is the signature to STOP and make the
  policy decision explicitly. The decision taken on the guard this was learned
  from, and since shipped: the unattended path KEEPS refusing,
  and the invariant gets scoped rather than deleted. "A benign shape is never
  hard blocked" was simply false as written; what is true and testable is that
  it is never hard blocked where a human can approve, and where no one can, the
  refusal carries an ACTIONABLE stderr — the cause, plus a route that gives the
  gate MORE information rather than less.

  That last qualifier is load-bearing and the sloppy version of this sentence is
  a bypass instruction. Telling an operator to find "a rephrasing that avoids
  the predicate" invites mutating raw text until a degraded predicate stops
  matching, while the same unverified operation still runs — the fail-closed
  mandate defeated by its own error message. Only two routes are legitimate, and
  neither is an evasion. If the text is PROSE that merely mentions a gated verb,
  take it out of a shell command altogether — write the file with an editor tool
  — because it was never an operation to gate and never should have been parsed
  as one. If it IS the operation, express it so the parser can actually read it,
  which does not dodge the gate but submits to it. A rewrite that suppresses the
  trigger while still performing the operation is the one thing such a message
  must never suggest, and a message is not "actionable" if that is what it
  teaches. (Actionability here is a courtesy to the operator, NOT the
  RECOVERABILITY of the Decision test above, which is about a MISS degrading to
  the status quo. Same word, opposite failure direction; do not satisfy the
  Decision test by printing a nicer error.) State the narrower invariant; do not
  leave the false one standing, and do not delete the guarantee that still holds.

  Two things keep this from reading as licence. The open-set imprecision is
  tolerable here ONLY because the verdict is `ask` or `deny` and never `allow` —
  an imprecise predicate that cannot authorize anything does not violate the
  Decision test, while the same predicate wired to an allow would. And a broad
  regex over raw text is admissible ONLY as a mention scan whose outcome is an
  `ask` where a person is present and a refusal where none is — never an allow;
  the moment it maps argv to an effect and authorizes on the result, it is the
  hand-rolled-parser tar pit the mandate above forbids.

  A measurement informed this rather than settling it, and it was afterwards
  WITHDRAWN — which is the more useful half of the story. The claim was that
  across a sample of unattended sessions the leg had never fired in either
  direction. It does not survive: the transcripts it counted no longer existed
  when someone went to re-derive it, and the sample was far too small to carry
  a word as strong as "never" even while they did. Absence in a small sample is
  the expected observation for a rare event, not evidence the event cannot
  happen; the honest reading is that the leg's rate is LOW, which is a different
  claim and a weaker one. What was actually being chosen was which promise the
  suite should make, not which incident to prevent — and that conclusion never
  rested on the number, which is why it outlived it.

  A note on every number in these three rules, including the ones above. They
  come from one operator's local session transcripts at one date, and no corpus
  or harness is checked in, so a reader cannot reproduce or falsify them — the
  differing denominators are different harvests, not one corpus quoted three
  ways. That is not a hypothetical weakness: one of them was withdrawn the
  first time anyone tried, for exactly that reason. Treat them as the scale at
  which something was observed, never as a published result. A rule that only
  holds at someone else's numbers is not a rule; each of these should stand on
  its stated mechanism alone — and if one ever seems to DEPEND on a figure,
  that dependency is the defect to fix, not the figure to defend.

- **`out=$(cmd)` under `set -e` swallows the failure path — and NO linter catches
  it.** An assignment whose value is a command substitution INHERITS that
  substitution's exit status, so under `set -euo pipefail` a bare
  `out=$(cmd)` followed by `rc=$?` **never reaches the `rc=$?`** when `cmd`
  fails: errexit fires first. Any error handling keyed on `rc` is dead code on
  exactly the path it was written for. Two properties make this vicious: it is
  invisible (the function just stops, printing nothing), and it hides behind
  call sites — `f || echo …` / `if ! f` disable errexit INSIDE the function, so
  the bug stays latent until someone writes the first bare call.
  **shellcheck 0.9.0 does not flag it at any severity, including `-o all`**
  (SC2155 is the *different* `local x=$(cmd)` declare-and-assign case; measured
  2026-08-27 — no other linter was tested, and a future shellcheck could add it).
  Always write `rc=0; out=$(cmd) || rc=$?` — but **declare `local` on its own
  line first**: `local out=$(cmd) || rc=$?` NEVER fires, because `local` is a
  command and the compound takes `local`'s status (0), not the substitution's.
  MEASURED: the split form yields `rc=100`, the inline form yields `rc=0` and the
  caller proceeds as if the command succeeded — strictly WORSE than the bug this
  entry describes, since it converts a loud abort into a silent false success.
  The same applies inside the error handler: with `set -o pipefail`, `x=$(… | grep -v … | tail -1)` aborts when
  `grep` matches nothing, so a `${x:-fallback}` default written for that very
  case never runs — guard it with `|| x=""`.
  Origin: `scripts/lib/cc_version.sh` (caught only by adversarial review), then
  three instances found in one `scripts/bootstrap.sh` function whose whole
  diagnostic block was unreachable. Pinned by
  `tests/test_scripts/test_bootstrap_guards.py::test_install_pkg_*`.

### Iterative-Refinement Discipline

AI refinement cycles degrade code they were asked to "improve" — validation
gets stripped, types relaxed, function scope widened. Published measurements
show vague improvement prompts degrade security fastest across iterations.
Three binding rules:

1. **Iterate with scoped, explicit prompts** ("fix the race in X by
   serializing on Y"), never "improve/clean up/make robust".
2. **Be security-explicit when touching validation, auth, or boundaries** —
   state what must not be weakened.
3. **Diff each refinement for what it REMOVED** (constraints, guards, type
   enforcement), not just what it added.

Full failure-mode taxonomy + ordered audit passes: `.claude/skills/genesis-development/references/ai-code-audit.md`.

## Adaptive Review Protocol

**Review rides the commit, because the commit is the only mechanical
trigger.** "After each meaningful chunk" is unenforceable — nobody can gate a
vibe — but this repo commits continuously, so the commit IS the chunk, and the
commit gate already refuses unreviewed code commits in every session type
(hooks are session-agnostic; a dispatched builder hits the same gate an
interactive one does). What this paragraph adds is the named DEFAULT below the
substantial line: for an ordinary code commit, run the built-in `/code-review`
at **low** effort and let its findings feed the evidence you mark. Cheap,
catches drift while the fix is one edit instead of a review round, and it is
NOT a clearance — the marker flow is unchanged. (The gate auto-allows a
docs/config-only commit; any code change goes through the marker flow, and the
gate itself sets the depth — an ordinary edit takes a plain marker, a
substantial or prompt-surface one takes the adversarial pass.)
Every defect that
survives to the end-of-build review costs an adversarial cycle there, and
every one that survives THAT costs an external Codex round against the
escalation cap.

**For a SUBSTANTIAL change, the end-of-build adversarial review is canonical,
not the builder's choice: run `/deep-review`.** Judge substantial at end of
build over the WHOLE branch — the `merge-base(HEAD, origin/main)..working-tree`
range `/deep-review` and the CI check examine — not the last staged commit: the
commit gate classifies each commit's own staged diff, so continuous commits
leave the index empty and a branch split into individually-small commits would
otherwise slip the bar it clears in aggregate. The threshold VALUES are the
commit gate's — ≥50 reviewable lines, more than one code file, or any auth /
API / migration / prompt / agent / skill surface (the prompt-surface trigger
excludes the user-sovereign top-level CAPS docs, CLAUDE.md / SOUL.md / USER.md)
— applied here to the branch range rather than one staged commit. `/deep-review`
dispatches the adversarial pass in the required shape (genesis-architect, plus
genesis-security-reviewer when the diff touches a security surface) and writes
the evidence marker the gate reads; it is the end-of-build terminus, riding on
TOP of the during-build `/code-review` inline passes — which is exactly the
table's "Code-reviewer inline + `/deep-review`" for a substantial row, the two
playing different positions rather than a choice between them. Below
substantial, the table governs: a small focused fix ends with code-reviewer
inline. `/audit-changes` stays what it is — a light self-check that writes no
marker — and is a terminus only where the gate needs no marker at all: a
docs/config-only commit it auto-allows.

Choose the review level proportional to the change:

| Change type | Review level | Examples |
|---|---|---|
| Docs / text / comments | **None** | Markdown prose, inline comments |
| Simple mechanical | **None** | Variable rename, typo fix, import reorder |
| Small focused fix | **Code-reviewer agent inline** | Single-function bug fix, config tweak |
| Substantial change | **Code-reviewer inline + `/deep-review`** | Multi-file refactor, new MCP tool, wiring |
| Prompt / LLM behavior | **Both + extra scrutiny** | System prompts, skill instructions, routing |

Decision criteria when ambiguous: "If the change could break a runtime
path not covered by its own unit test, it needs `/deep-review`. If it only
touches things with clear, isolated test coverage, code-reviewer inline
is sufficient."

**`/review` is not in this repo — it comes from the optional `superpowers`
plugin.** Where that plugin is installed, `/review` and
`superpowers:code-reviewer` are the preferred path. Where it is not, those names
do not resolve, and older text saying "Run /review" sends you after nothing —
which is why the enforcement hooks now name the plugin as optional rather than
assuming it. **Check, don't assume, in either direction**: the plugin is offered
by the official marketplace, so its presence is per-install, not per-repo.

Always available, plugin or no: **`/deep-review`** (dispatches the adversarial
pass AND writes the evidence marker), the built-in `/code-review`, and the
by-hand `scripts/review_state.py evidence-path` → `mark` flow.
`/audit-changes` is a light self-check, not a substitute for any of them.

The enforcement hooks (`review_enforcement_prompt.py`,
`review_enforcement_commit.py`) still fire on every change — they are
safety nets, not the decision-maker. This protocol provides the
judgment framework.

### Before the first review: six mechanism axes

Every review closes with a verdict; at "Code-reviewer inline" level and above,
three protocol steps also apply: `.claude/skills/closing-session/SKILL.md`,
"Close every review with a verdict".

- **Interrogate every MECHANISM you introduce along six axes BEFORE the first
  review — original code and fixes alike.** The two bullets "A review's findings are a SAMPLE" and "Fixing the named instance" (closing-session, "Review-loop discipline") enumerate the
  class of a DEFECT, reactively, once a reviewer names one. This one is about the
  extent of a MECHANISM — a deadline, a lock, a cache, a guard, a token — which is
  correct on the path that motivated it and undefined everywhere else.
  MEASURED 2026-09-09 on 31 inline review findings from two PRs, each attributed
  by `git blame` at the SHA the reviewer was looking at, and classified as
  "original" or "post-review" by whether the blamed commit predates that PR's
  first review. Over all rounds, 21 of 31 (68%) blame to the original
  implementation. But round 1 CANNOT blame to a fix commit — none exists yet — so
  that figure is partly forced arithmetic. In the rounds that actually loop it is
  close to even: **of the 21 findings from rounds 2+, 11 were already in the
  original code and 10 were in code written to answer an earlier round.** Both
  halves are worth the same attention; neither dominates.
  Deduplicating re-posts and cross-reviewer duplicates and dropping one finding
  later refuted by measurement leaves 25 distinct findings, and **24 of those 25
  fall into six shapes.** These are the checklist:
  1. **SIGNAL** (8, the largest) — does the thing you read change exactly when
     the property you assert changes? A whole-database counter read as "did this
     table change"; an mtime read as "did content change"; empty read as
     "unbuilt"; a proxy's identity read as "same connection"; two autocommit
     reads used as one snapshot.
  2. **SCOPE and LIFETIME** (5) — a deadline per query when the operation is a
     traversal; a lock per process when the race is cross-process; a lease that
     expires mid-work; a hang moved from the operation to interpreter teardown.
  3. **INPUT DOMAIN** (5) — the values the type permits, not the ones you
     pictured: non-boolean, empty string, malformed timestamp, sub-second
     precision, unescaped path.
  4. **EQUIVALENCE** (3) — what does it claim to match, and has anything checked?
  5. **FAILURE PATH** (2) — what is left behind when the work aborts.
  6. **CALLER CONTRACT** (1) — does an explicit option the caller passed survive?
  (The 25th was a test whose identity comparison was unsound in both directions.)
  A finding is a POINT, so a fix that treats the finding as its spec inherits
  that point as its only test — which is why the fix half of that near-even split
  exists at all, and why the six axes are worth running over a fix and not only
  over a feature.
  METHOD NOTE, because getting this wrong inverted an earlier version of this
  measurement: to attribute a finding to a commit, blame the line at the SHA the
  reviewer was looking at. `original_commit_id` is the PR HEAD when the review was
  posted, not the introducing commit — one commit here touched only changelog
  files yet carries three findings on a Python file.
  FALSIFIABLE, with a denominator: #1850 pushed four commits after its first
  review round, and deduplicated they carried 5 findings between them — 1.25 per
  post-review commit. If the next three PRs whose mechanisms are interrogated on
  these six axes before pushing still average above one deduplicated finding per
  post-review commit, the axes are not the right ones. Re-derive rather than
  adding a seventh.

- **Run the pre-push adversarial pass with `/deep-review`** (`.claude/commands/deep-review.md`):
  one command that dispatches a fresh-context `genesis-architect` (+ `genesis-security-reviewer`
  on security surfaces) over the FULL branch diff with the right SHAPE — fail-open/state/TOCTOU/
  hand-rolled-parsing hunting, not a lint/secrets scan — and writes the evidence marker. This is
  the review that catches Round-1 bugs before Codex does; `/audit-changes` is only a light
  self-check.
  A PR whose diff touches the enforcement-hook surface runs the gate-fix lane: round 1 is DUAL, Codex plus `merge_gate.secondary_reviewer` run together at PR open (Codex-only when none is configured, and the PR says so). Read `.claude/skills/genesis-development/references/gate-fix-lane.md` before opening it.

### Publishing: the first push opens the PR

A blocked call, `secrets.env` and ask keys:
`.claude/skills/genesis-development/references/ask-keys-and-sigils.md`.

- **A FIRST push of the CURRENT branch to the configured public repo must open
  its PR in the same command — otherwise it is DENIED, whatever `push_publish`
  says.** The re-push no-open-PR block only catches a branch on its NEXT push,
  so a session that stopped after one push left the branch public with no PR.
  The one accepted shape is
  `git push -u origin HEAD && gh pr create --title "..." --body-file <file>`:
  one plain `git push` of the current branch (no `cd`, `git -C`, prefix,
  output filter or earlier step), `&&`, and one ready `gh pr create` using only
  title/body/body-file/fill/no-maintainer-edit options — never `--draft`/`-d`,
  `--web`, `--dry-run`, `--head`, `--repo` or `--base`, and no
  `--label`/`--assignee`/`--reviewer`/`--milestone`/`--project` (gh resolves
  those names before creating the PR and fails the whole create on one it
  cannot resolve, after the push; add them with `gh pr edit` once the PR
  exists). Its values must be LITERAL: no `$`, backtick, backslash, `*`, `?`,
  `[`, `{` or `~` anywhere in the create, quoted or not. The create must be one
  gh accepts with no terminal (MEASURED, gh 2.101): a non-empty title AND a
  body (`--body`, or `--body-file` naming a regular file in the push's
  directory that the hook can actually open and read as UTF-8, never `-`; not
  both, and at most 65,536 characters of body either way), OR exactly one of `--fill`/`-f`, `--fill-first`,
  `--fill-verbose`. A short value flag is read as gh reads it: `-F=body.md`
  names `body.md`, and `-t=` is an empty title. `||`, `;`, `&`, a newline, or a `#` before the `&&` all
  deny. Work not ready for a PR stays committed locally, unpushed. Scope is
  the re-push block's predicate (`_no_pr_block_applies`): another destination,
  or no declared public repo, keeps the old behaviour; a dry run publishes
  nothing and is not denied, and after a dry-run push a chained create keeps
  its own would-publish probe.
  **This is deliberately the client half only — what a hook can read from the
  command text.** It does not model other push spellings (another branch, a
  refspec, `--all`, an unresolvable `cd`): those keep their ask, as before. Nor
  does it model what gh resolves at RUN time — the base repository
  (`gh repo set-default`, `GH_REPO`/`GH_HOST` in the environment), the base
  branch (`branch.<b>.gh-merge-base`), whether the branch has commits ahead of
  it — or a create that fails for auth/network reasons; any of those can still
  leave a branch public with no PR until its next push, which the re-push block
  then refuses. Modelling git/gh semantics from shell text is the open-set
  trap described under "Never hand-roll `gh`/bash/CLI argv parsing" (and in
  `scripts/hooks/pr_close_advisory.py`'s docstring). **The guarantee that no
  branch reaches the public repo unchecked is the SERVER-SIDE leak scan that
  runs on every branch push, not this hook.**
- **Only the main session publishes.** An Agent-tool worker (the hook input
  carries `agent_id`) may run git and gh only from an ALLOWLIST, whatever
  `push_publish` says (owner policy 2026-10-05; an allowlist since 2026-10-10,
  because each review round found a write verb a denylist had missed): local
  git work plus git's network reads (`fetch`, `pull`, `clone`, `ls-remote`),
  and gh reads (`gh pr view/list/diff/checks/status/checkout`, `gh issue
  view/list`, `gh run view/list`, `gh search`, `gh api` GETs and GraphQL
  queries). Everything else is refused: `git push`, `git lfs lock`, an unknown
  or aliased git command, every gh write including `gh auth login`, an unknown
  gh subcommand, a non-GET `gh api` or GraphQL mutation, git options that run a
  program (`--upload-pack`, `grep -O`, `rebase --exec`), and a command naming
  git or gh in a form the guard cannot read (a line continuation, a too-long
  command, a program held in a variable). Not reached: launchers the parse does
  not open (`eval`, process substitution, `env -S`, a function body) and
  programs git starts from config or the environment. Re-pushes of an already-published PR branch, dry runs and in-thread
  finding replies are included. The list is `_GIT_SUBAGENT_OK`,
  `_GIT_FAMILY_VERBS` and `_GH_SUBAGENT_READS` in `git_push_guard.py`. Commit
  locally, then hand the worktree path, branch, head SHA, the PR title and body,
  and any comment or reply you would have posted back to the main session. A
  subagent dispatched to fix a PR therefore leaves the push and the replies to
  the session that dispatched it.

### CI checks

**CI is not one check.** `ci.yml` defines **15** jobs. `review-depth-check` and
`cc-pin-receipts` are **Advisory BY DESIGN** (they always exit 0); a red result
from another job blocks the local gate. `dependency-audit` has a narrower
exception: the audit command reports vulnerability findings without making the
job red, while setup or installation failures can still fail the job. (An
earlier version said "ten of eleven … only `review-depth-check`", wrong on both
halves; count the `jobs:` keys and inspect the steps rather than trusting any
prose figure, including this one.) Only THREE checks — `test`, `leak-detector`,
and `lint` — are server-side REQUIRED, so a red local-gate job outside that trio
does not block GitHub. Several are
reproducible locally BEFORE pushing, which is far cheaper than a red PR:

```bash
ruff check src/ tests/ scripts/
python scripts/check_external_io.py
python scripts/check_subsystem_map.py
python scripts/check_shared_artifact_consumers.py
python scripts/check_frozen_clock.py
```

Do NOT run the full pytest suite locally (it is banned, and the concurrent-test
guard blocks a second run anyway) — CI's `test` job is the blocking one.

**Detecting a running pytest: match argv STRUCTURE, never a substring.** Any
check that greps command lines for `pytest` matches ITS OWN command line —
`pgrep -f "python -m pytest"`, `ps | grep`, and a bash `case *pytest*` all
self-match, as does another session's wait-loop. Test `argv[0]`'s basename, or a
python interpreter with an adjacent `-m pytest`.

### Hook stdout: say what the degrade looks like

`out` below is `hook_output.py`'s `BoundedStdout`, the writer commit-path.md describes.

  **NEVER compute characters at the call site — say what the DEGRADE looks
  like.** `out.emit_or_degrade(text, block=…, pointer=…, notice=…, reserve=…)`
  settles every number (the divider, `print`'s newline, the closing-line
  reserve, what is already emitted) and returns which branch it took. There is
  no `fits()` for emitters to call any more, deliberately: five review findings
  in one cycle were a caller re-deriving what the writer already knew — the
  audit reserve counted twice, a `fits` that undercharged by one and approved
  blocks the writer then destroyed, a 120-char reserve for a 206-char line, a
  pointer reserved in a part that cannot emit one, a `keep` derived from the
  budget constant instead of the room. The arithmetic was never hard; having it
  in six places was. An AST test bans `.room`/`.fits` from the emitter so the
  next "just one `fits` call" cannot reintroduce the class.

  The general shape, which is the transferable part: when several call sites
  must each remember to do something — bound their output, report a failed
  read, escape untrusted text — that is a CONVENTION, and conventions are what
  reviewers keep finding one instance of at a time. Move the obligation into a
  chokepoint the callers cannot bypass, then LOCK the chokepoint with a test
  that fails when someone routes around it. A chokepoint nobody is forced
  through is a convention with better documentation.

Before any "X is absent" or "nothing adoptable exists" claim, follow
"Auditing Existing Capabilities" in `.claude/skills/spec-session/SKILL.md`.

Before writing repo-local code that decides something about a pull request, read
"Never reinvent what GitHub already does natively" in
`.claude/skills/spec-session/SKILL.md`.

## Generalizability Gate — build for ANY install, not this one

Genesis is a public, cloneable system. Every change must work on ANY user's
install, not just the machine it was written on. Standing user directive.

**Hardware/scale adaptivity.** Other installs have different RAM, disk, CPU
count, and workload scale. Never hardcode absolute resource numbers or scale
assumptions:

- Memory/disk caps: percentage-of-available or config-derived, never fixed
  GB (precedent: #1029 percentage-based memory caps). Concurrency: derive
  from `os.cpu_count()`/config, never a literal core count.
- Hard minimums are allowed but must be EXPLICIT (documented in install
  docs/config comments), not implicit assumptions that fail mysteriously.
- Workload scale varies (PR velocity, table sizes, transcript sizes):
  enumerate with pagination/bounds and LOUD truncation markers, never
  silent caps (precedent: repo-pulse `limit_hit`).
- Optional dependencies AND optional infrastructure (Ollama, GPU,
  individual API keys, a host VM/guardian, Tailscale, voice/edge hardware)
  must degrade gracefully behind detection/config — presence is never
  assumed (precedent: Ollama-optional, `API_KEY_VOYAGE`-gated reranker,
  guardian features no-op without `guardian_remote.yaml`).

**No install-specific values in code.** IPs, hostnames, usernames, absolute
`/home/<user>` paths, GitHub slugs, timezones: these belong in generated
local config (`~/.genesis/config/genesis.yaml`, written by
`setup-local-config.sh`) or config overlays — never in committed code,
defaults, or tests. Resolve repo paths via `genesis.env.repo_root()` /
`genesis_db_path()` (GENESIS_REPO_ROOT-aware); resolve GitHub slugs LIVE
(`gh repo view --json nameWithOwner`) — a configured slug can name a
real-but-wrong repo and return plausible stale data. Shipped config defaults
must work on a fresh install with ZERO overlay.

**Leak-detection patterns follow the same rule — never hardcode an install's
private literals into a tracked scanner.** A public repo's CI grep / gitleaks
rule / commit-msg hook / contribution sanitizer must ship only generic CLASS
patterns (all RFC1918, IPv6 ULA per RFC 4193, `/home/<user>` shapes — see
`scripts/check_portability.sh`). This install's SPECIFIC literals (its
hostnames, subnets, ULA prefixes, private repo name, timezone) live only in the
GENERATED `~/.genesis/release-fingerprints.txt` (built by
`genesis.contribution.fingerprints` at bootstrap; hand-edited section preserved,
backed up via `backup.sh`) and — opt-in — the public repo's
`GENESIS_PRIVATE_PATTERNS` Actions secret. A scanner that must exclude its own
definition files from scanning is self-allowlisting a leak. See procedure
`public_repo_leak_detection_design`.

**Tenant-neutral, not tenant-shaped.** Genesis is a single-user sovereign
system. Build clean single-user code; do NOT pre-genericise for multi-tenancy —
no `tenant_id` columns, ACL tables, or context objects that always resolve to
one identity — absent a committed multi-tenant requirement. Premature
genericisation taxes every change with abstraction for a customer that may never
exist, and the reliability work that WOULD precede multi-tenancy (a fail-closed
data boundary, consolidated stores, provenance) is worth doing on its own
single-user merits and makes the eventual retrofit easier as a side effect.
When that requirement lands, tenancy is a well-understood retrofit — not
insurance to carry now.

**Deploy-path answer required — "how does this reach other installs?"**
Every PR must have an answer for both an EXISTING install and a FRESH clone.
Merged-but-undeployable-elsewhere is a bug. The standard paths:

| Change type | Deploy path |
|---|---|
| Runtime code | `scripts/deploy_code_only.sh`, or `update.sh` for dependency, unit or host changes. Modes: `deploy` (the default: a locked pull + restart that queues behind update.sh and validation holds, refuses a dirty tree or a venv that does not match the pyproject, and stands the Guardian and watchdog down); `deploy` and `restart` also refuse while the server is running work a restart would cancel, naming each item: what the server itself reports at `GET /api/genesis/inflight` (internal bearer; every Claude invocation, plus a dispatched session's or CLI reflection's whole life, from before its Claude process starts until its result is delivered; NOT the short tail other subsystems run after their call returns, such as a chat turn delivering its reply: #2917), and any live Claude process below the server. They proceed only with `--allow-killing <item,…|all>` using the items the refusal prints (`<pid>@<start>` or a reported id); a server too old to answer (404) gets the process check alone, and any other failure to ask refuses unless `all` — a session the server launched must hand the restart off rather than override (launched detached, the script cannot tell it is one of them); `update.sh` does NOT refuse, and its restarts end those sessions too; `pull` (the same pull, no restart: it reports every change the running server has not loaded, and the next step); `restart`; `status`. Launch `deploy` and `restart` DETACHED as a transient unit (`systemd-run --user`, the command is in the script's header): a session's background job dies with the session (#2101). **Validators sample, they don't chase:** deploys need not be 1:1 with merges. Validate `main` at a cadence, and on a failure bisect the merged range; per-PR CI already covers per-merge granularity. Validating against the live server? Hold `flock -s -w 7200 "${GENESIS_HOME:-$HOME/.genesis}/locks/update.lock" <cmd>`, and deploy BEFORE taking the hold. The hold stops locked deploys, NOT every restarter (the watchdog, the dashboard's service routes, Guardian recovery) and not a bare `git pull`, so bracket the run and let the script judge it: keep the `bracket:` token `scripts/deploy_code_only.sh status` prints at the start (from a worktree it reports the main checkout), and run `scripts/deploy_code_only.sh status --verify <token>` at the end; exit 0 is a valid run. A token exists only when the server's boot commit is known, HEAD's runtime files (`src/`, `config/`, `pyproject.toml`, and the scripts it keeps imported, `_RUNTIME_RELOAD_SCRIPTS` in `scripts/lib/deploy_status.sh`) are the ones it booted from and were at every commit HEAD held since the boot (after a `pull` of code, even one a later commit undid: restart first — the server imports lazily, so the detour's modules stay loaded), and nothing there is edited outside git; otherwise it reads `unknown (<why>)`, which no token matches. It covers a restart (boot commit, MainPID, systemd invocation id) and an edit to an ignored override such as a `config/*.local.yaml` or to a user overlay in `~/.genesis/config`, and a change at HEAD to a script the server runs afresh (`_RUNTIME_FRESH_SCRIPTS`: voids the token, needs no restart); HEAD may move over docs, or hooks and scripts the server never runs. It is a tripwire, not a certificate: it cannot see, and reads valid through, a change to the venv's installed packages, the other files the server reads from `~/.genesis/config` (only the `*.local.yaml` overlays are fingerprinted), an edit undone without moving HEAD (a stash and its pop included), and a rewritten or backdated reflog. A daemon your command leaves behind keeps holding the lock, and overlapping holds can starve a waiting deploy (flock grants a late shared request ahead of a queued exclusive one) |
| DB schema | additive idempotent migration — applies at restart |
| One-off data fix / backfill | data-migration framework (post-boot, idempotent) — NEVER a hand-run script only this install executed |
| **Naming either migration** | **UTC timestamp id: `` `date -u +%Y%m%d%H%M%S` ``_description.py** (data migrations prefix a `d`). NEVER hand-pick the next number — the legacy 4-digit namespace is FROZEN and CI refuses a new one. An id you have to CHOOSE is an id two branches choose identically: measured 2026-09-03, one PR was renumbered twice in a day and four open PRs held live collisions, while a duplicate prefix aborts bootstrap on every install. Nobody allocates a timestamp. |
| **Changing an EXISTING migration** | **Don't — add a new one.** The legacy 4-digit set is frozen by ENUMERATED FILENAME, so renaming or deleting one fails CI. Installs that already applied `0050` will never run a renamed `0050_*`, while fresh installs will — two schemas diverging with nothing to notice. Discovery also REFUSES to run anything from a directory holding a file it cannot classify (a mistyped 13-digit id used to be skipped in silence, so the migration never ran at all). |
| Config default | repo config file (+ optional local overlay); works with no overlay |
| systemd unit / timer | registered in bootstrap.sh AND the update path — never hand-`systemctl enable`d only here |
| Hooks / MCP servers | land at next CC session start (note the mid-window in the PR) |
| Guardian / host VM | `update.sh` redeploy (Host-Deploy Gate, validating-merges) |

**When a change CANNOT deploy through the standard paths** (one-time host
action: packages, sudoers, cgroup settings, firmware), it must ship one of:
(a) a gated self-heal that reconciles on a recurring tick (precedent: the
guardian's swap reconcile — checks every tick, repairs config + live state,
opt-out flag), or (b) an explicit, documented operator step in CHANGELOG +
install docs. Silent "works here because I hand-fixed it" divergence is the
failure mode this gate exists to kill — it bites hardest on guardian/host
changes.

**Empty-state correctness — a fresh install is state zero.** Every feature
must behave correctly with NO accumulated state: empty tables, no history,
no cursor files, first run ever. First runs bound their own work
(precedent: repo-pulse `lookback_days` — never "all history"); readers of
possibly-absent tables degrade explicitly (precedents: dashboard
`charters_available: false`; charter injection byte-identical when the
migration hasn't applied yet). Test the zero state, not just the populated
one — "works here" often means "works with two years of accumulated state."

**External-tool version drift.** Other installs run different versions of
`gh`, GitNexus, Node, and Claude Code — and upgrade on their own schedule.
Never key logic on one version's observed behavior without a fallback:
prefer first-class config over output-patching, and keep the patch as a
safety net when older versions ignore the config (precedent: `.gitnexusrc`
+ the strip job for rc-unaware versions); parse external-tool output
fail-closed against the LIVE stream, never assumed semantics; pin versions
only where the system owns the pin (`cc_version.sh` + cc-align).

**A settings lever for every autonomous behavior.** Anything that acts
without a user in the loop — detached workers, scheduled jobs, auto-writes
— ships its operator lever in the SAME PR: a settings domain
(`off | propose_only | live` or equivalent) plus an env kill switch, with
invalid values degrading toward LESS write authority (precedents:
`repo_pulse` domain + `GENESIS_REPO_PULSE_DISABLED`;
`session_ledger_shadow` live-coerced to shadow). Another operator must be
able to turn your feature off — or cap its authority — without editing
code. This is "the user decides tradeoffs" applied to every install.

**Retention for every unbounded store.** Any table, log, or directory that
grows without bound ships its prune path in the SAME PR, wired into
`disk_hygiene.sh` or an existing retention tick (precedents: repo-pulse
45d prune; ledger-shadow 45d prune; label-aware attention-snapshot GC;
hook audit stores 5 MB size trim). An unbounded store is a slow disk-leak on
someone else's smaller disk — retention is part of the feature, not a follow-up.

**But retention machinery does not belong on a hook path.** `disk_hygiene.sh` IS
the retention path; a guard computing a security verdict must not also be grooming
a store. Two measured reasons, both from the same feature: a helper whose work
scaled with the command exceeded a hook's registration timeout, and a hook killed
before its `exit 2` lets the refused command RUN; and an in-hook retention engine
whose failure mode is rewriting a file produced most of that feature's defects
across five review rounds. Prefer a store shape the daily timer can bound by
deleting whole old files — that is what makes the split possible at all.

**Install-agnostic tests.** Tests must pass on a fresh clone with no
Genesis services, no live DB, no network, no `gh` auth, no local config:
synthetic fixtures only (never real usernames/slugs/IPs — doubles as the
privacy gate), injectable runners for external commands, `tmp_path` over
real paths, no wall-clock dependence. CI on GitHub's runners IS the
reference "different install" — anything a test can't exercise there needs
an injectable seam, not a skip-on-my-machine guard.

### Building a rework — report against the spec (standing owner rule, 2026-10-05)

A PR sent back with `needs-rework`, or a `needs-architecture-session` PR whose
owner decision has since been posted as a rework spec, carries that spec: a
maintainer comment headed `## Rework spec`, plus any design issue it names. An
audit or proposal without that heading is not a spec, and a
`needs-architecture-session` PR with no posted spec has an undecided design; do
not build it. Its contract is in
`.claude/docs/premise-check.md`, "Handing a verdict to a builder". The session
that takes up the rework is the reviving session (closing-session, "Never RETIRE a PR"), and it owns the following:

0. **Acknowledge the spec on the OLD PR before building.** Post one comment
   there with:
   - the spec as you understood it, in your own words;
   - the split you will build;
   - every question you need answered before you start.

   Head it `## Rework acknowledgement`, and open no PR, not even a draft, until
   it is posted. If it asks no questions, proceed once it is posted; otherwise
   wait for an answer on the old PR, from the closing session or the owner. On
   a Devin-built old PR, put `(aside)` on the first line and the heading under
   it, or the comment starts a paid Devin session. Post it with
   `gh api repos/<o>/<r>/issues/<N>/comments -F body=@<file>`: on a PR past
   its terminal round the push guard reads a `gh pr comment` whose body it
   cannot see (`--body-file`, or an inline body with a backtick or `$`) as a
   possible `@codex review` request, which asks in the foreground and is
   refused in a dispatched session. A question answered before building is cheap; the
   same question answered silently inside the build is how a rebuild drifts. No
   acknowledgement means nobody can tell whether the spec was read at all.
1. **New PRs, by default.** The rework arrives as one or more NEW PRs, with fresh
   round counts. When the LAST one opens, whoever opened it closes the old PR
   with a comment that maps every part of it, file by file, to the `file:line`
   in the replacement
   that covers it, or says why that part is moot. If any part is neither,
   leave the old PR open with a comment naming that part. Leave its labels on,
   so the rebuild stays traceable to it. If the spec carries a
   `Follow-up: <id>` line, copy it into the body of the replacement that will
   merge into main LAST, so the follow-up closes only when the whole rework
   has landed. A merge into any other base completes the marker too, so never
   put it on a PR that merges into another PR. Reworking under the old number
   happens only when the owner grants it. Devin builds a rework as fresh PR(s)
   too, and never closes the old PR; the closing session does, after the same
   coverage check (closing-session, "Devin-built PRs").
2. **Follow the split.** If the spec has a `PR-shape: SPLIT` plan, open those PRs.
   Each one carries one concern and names the PRs it depends on. Open them in
   dependency order against main; a PR whose dependency has not merged waits as
   a branch, or opens stacked with its base named. Being a rework is not itself
   a reason to stay unsplit, and a better split is a deviation (item 4), not a
   reason to skip splitting. If the spec has no split, size each PR by the rule
   in `.claude/docs/premise-check.md` step 6: under 500 counted lines is the
   target, and a `Shape:` line states why one cannot be smaller. Split a PR that
   carries more than one concern.
3. **Answer the delegated questions** in the PR body, each with your reasoning.
   If you hit a question the spec should have answered and did not, answer it
   the same way and mark it as missing from the spec. Do not resolve it
   silently in the most defensive direction: a fail-closed answer is still a
   design decision, and it can force machinery the spec never asked for.
4. **Report every deviation.** Unforeseen complications are expected; an
   unexplained deviation is not. The body of each replacement PR carries:

   ```
   ## Rework
   Replaces: #N (spec: <link to the `## Rework spec` comment>; acknowledged: <link to your item-0 comment>)
   Split: PR k of n (<the other PRs, by number or concern>)
   Kept / deleted / reshaped as the spec asked: <one line each, or "as specified">
   Deviations: <each: what differs from the spec, and the complication that forced it> | none
   Questions answered: <each delegated or missing question: the answer and why>
   ```

The closing session reads this section before it spends a review round. A
deviation with a stated reason goes to the owner as a design question. A missing
section, an unexplained deviation, or an unsplit PR the spec split goes back to
you before review. Origin: a rebuild came back as one PR, +2,538 raw lines
(881 counted, so inside the `shape` band; size was not the failure), with
conditional rendering, an uninstall retention mode and an in-place settings
writer, none of which its spec asked for, and with no note explaining any of
them. Working out whether the spec or the build had failed cost the owner a
session.

## Public Repo & Release Workflow

The public repo (`GENesis-AGI`) is the primary development repo.
Standard open-source workflow: PRs go directly to the public repo.

- **Squash merges only** — merge commits are disabled on the public repo.
  Always `git pull --rebase origin main` **on main** after merging a PR before
  committing locally, or push will be rejected (non-fast-forward).
- **Once a branch is pushed: merge main IN, never rebase it** — force-push is
  banned, so a rebased pushed branch diverges from its remote with no way to
  publish the rewrite. A merge commit on a *branch* is fine — the squash erases
  it at PR merge; "squash only" above governs merging into main, not
  reconciling a branch.
- **In a reconciliation merge, the base wins on policy.** The merge commit may
  DELETE from your branch (a rule main superseded, a test that pinned your
  now-dead behavior); it may never re-litigate main. Scope, precisely: base
  wins where main *deliberately changed a thing your branch also changes*; on
  your branch's own new work the merge integrates both sides — keep your
  feature, adopt main's surrounding changes. "Base wins" is never
  `--theirs` on a whole hunk. If you disagree with what main did, that is a
  NEW PR with its own review (a sanctioned revert PR is exactly that — the ban
  is on *unreviewed* reversals) — never a conflict resolution, which is the
  one diff nobody re-reviews. And a clean auto-merge is not a clean
  reconciliation: the superseded-rule case usually merges with NO textual
  conflict, because your rule and main's live in different hunks — so after
  merging main in, re-check every rule and test your branch carries against
  what main changed in the interval. The lived failure (2026-09-04): main
  widened a gate rule while a narrower rule for the same paths sat in review;
  restoring the branch's rule "so its tests stay green" would have silently
  reverted a deliberate policy change inside a merge commit. Delete your
  superseded rule AND its tests — a test pinning behavior the base retired is
  not coverage, it is a revert waiting to be committed — and name every such
  deletion in the PR body; if you suspect main's change is a bug rather than
  policy, file that before merging. A deleted test with no filed disagreement
  is how coverage disappears.
- **Adding tests no longer touches `ci.yml`.** The collected-test count is
  checked by the `collected-floor` CI job (#2743), which compares the test ids
  your change collects against the commit it lands on. It is a required check
  once `.github/rulesets/checks.json` is applied (`scripts/apply_rulesets.py`).
  It goes red (exit 1) when ids vanish from test files your change did not
  touch (a `conftest.py` edit does not count as touching the files below it),
  or when your tree has a collection error the base does not. It refuses
  (exit 2) when it cannot measure: a base it cannot resolve (not a 2-parent
  merge, or an unreadable pre-push commit), base dependencies that would not
  install, pytest exiting other than 0, 1 or 5, a collection that did not
  finish or collected nothing, malformed collection output, an unreadable
  changed-file list, a PR-body scanner that would not load, or an unexplained
  drop whose PR body could not be read. Read the
  step summary first; it names every vanished id. If a drop is deliberate, put
  a line `Collected-drop: <reason>` in the PR body and re-run the job (or push,
  if a re-run no longer clears it: #2624). A branch cut before #2743 that still edits the old
  `--min-collected` line resolves that conflict by taking main's side and
  deleting its own floor paragraph. The `--ceiling` (skip count) still lives on
  the dormancy-guard line: raise it only in the commit that adds the skip,
  counted on the CI runner from the junit report, never locally (installed
  tools differ, so local skip counts are the wrong denominator).
- **README is public-authoritative** — the public repo's `README.md` is
  hand-crafted and must NEVER be overwritten.
- **Never edit `CHANGELOG.md` in an ordinary PR — add a `changelog.d/`
  fragment.** (The one exception is the release-fold PR, where the
  `CHANGELOG.md` diff is produced by `scripts/assemble_changelog.py` and the
  section rename rather than written by hand — see the release procedure in
  `docs/reference/recovery-and-portability-workflow.md`.)
  One file per change, named `<YYYYMMDDHHMMSS>-<category>-<slug>.md`
  (timestamp from `date -u +%Y%m%d%H%M%S`; category is one of `added`,
  `changed`, `deprecated`, `removed`, `fixed`, `security`). The file holds
  the entry exactly as it should appear — a Markdown bullet, spliced in
  verbatim. `scripts/assemble_changelog.py` folds them into `[Unreleased]`
  at RELEASE time, not per PR.

  This is not bookkeeping preference: two branches editing one shared
  changelog insert at the same position, which git calls a conflict.
  Measured 2026-09-04 — of 49 open PRs, 21 could not merge and **18 of
  those conflicted on `CHANGELOG.md` and nothing else**. Two branches
  writing two different filenames have nothing to merge. A merge driver
  cannot substitute: GitHub ignores repository `.gitattributes`
  server-side, measured against its own merge engine.
- **CHANGELOG audience is users** — only include entries a user updating
  their install would care about. No internal refactors, README changes,
  CI tweaks, or process artifacts. Lead with the user-visible effect, not
  the implementation technique. Same bar for a fragment: it becomes a
  changelog entry verbatim.

- **Individual campaigns are user data, not infrastructure** — a campaign's
  name/prompt/targets/cadence live only in the `campaigns` DB table and the
  private backups repo; never hardcode them into tracked source. Unlike modules
  (which ship defaults under `config/modules/*.yaml`), campaigns ship ZERO
  defaults (no `config/campaigns/`). Only campaign infrastructure ships. Express
  reusable session types as generic roles (e.g. the `community-responder`
  profile), not names coupled to a live campaign. See `src/genesis/campaigns/__init__.py`.
- **External egress is gated; owner-facing egress is not** — any autonomous send to the
  outside world (Discord, Medium, Twitter/X, Slack, `DistributionManager.distribute`) MUST
  route through the capability shadow-gate (`autonomy/shadow_gate`) before the enforce stage;
  the `scripts/check_external_io.py` CI guard backstops new endpoints. Delivery TO the owner
  (Telegram/voice/email-to-owner) is NEVER gated. Full contract in `autonomy/shadow_gate.py`.
