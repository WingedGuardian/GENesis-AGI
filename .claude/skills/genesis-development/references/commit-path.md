# Commit path

Mandatory for any session that commits: build sessions and closing sessions
both read this before their first commit.

### Test-First Discipline

Adapted from superpowers `test-driven-development`, scoped to where it pays:

- **Bug fixes: failing reproduction test FIRST — always.** Before touching the
  code, write the minimal test that reproduces the bug and WATCH IT FAIL for
  the expected reason. Then fix; the same test proves the fix and pins the
  regression. A repro test written after the fix proves nothing (it never
  caught the bug).
- **Verify-RED, always and everywhere.** Any new test must be seen to FAIL
  (correctly) at least once — via the bug, a reverted fix, or a deliberately
  broken assertion — before its green is trusted. A test that has only ever
  passed may be testing nothing; a whole suite passing every review round
  while a reviewer keeps finding real spec bugs is the tell that the tests
  encode the same wrong spec as the code.
- **Verify-RED of a safety fence runs the test UNFENCED.** Removing a guard,
  stub or sandbox to watch its test fail means whatever the test executes
  reaches the real system for that run. Read the test first and make its probe
  harmless even unguarded: `systemctl --user is-active <a unit that does not
  exist>`, told apart from the stub by the stub's own log, never the dangerous
  action itself. (Origin: the fence test in PR #2935, for issue #2863: its verify-RED ran a real
  `systemctl --user stop genesis-server` and took a live server down for about
  3.5 minutes.)
- **Bound every mutant run with its own short timeout.** A mutation often turns
  a test into a HANG rather than a failure (a mutant that never sets an event,
  never cancels a task), and a hung run holds the box-wide test lock for the
  whole per-test timeout while every other session waits. MEASURED: one sweep's
  hung mutant held it for 53 minutes. Pass `-p timeout -o timeout=<N>` to each
  mutant run, with N = max(60, 10 × the baseline run's slowest test). Spell it
  `-p timeout`, never `-p pytest_timeout`: the plugin registers under the name
  `timeout`, and the module name aborts pytest with "Plugin already registered"
  (MEASURED, pytest 9.0.2 with pytest-timeout 2.4.0). And read the test result,
  not the exit code: a missing plugin also exits 1, which a sweep would count
  as a killed mutant.
- **Never edit a shell script while a test that runs it is in progress.** bash
  reads a script as it executes it, so an edit mid-run yields bogus syntax
  errors that look like real failures. Wait for the run to finish, or test a
  copy.

Continued in build-session, "Test-First Discipline, continued": the causes of a
RED that comes back GREEN, a RED for the wrong reason, vacuous-test shapes and
the mutation restore rules.

### Review depth is machine-checked (the front-stop)

The "substantial → adversarial /review-level audit" decision (build-session, Adaptive Review Protocol) is no longer
left to judgment alone (that judgment is exactly what failed on PR #1353 — an
under-depth inline pass was self-certified as sufficient). The commit gate now
COMPUTES substantiality from the staged diff
(`review_scope.classify_change_substantiality`) and BLOCKS a substantial change
whose review marker is not an ADVERSARIAL audit (`review_enforcement_commit.py`
Rule 2.5). Substantiality is a surface-area × risk model — ≥50 reviewable lines OR
>1 code file OR an auth/api/migrations file OR an **executable prompt/agent/skill
surface** — and that last set is WIDER than it used to say here: read
`review_enforcement_commit._PROMPT_SURFACE_PREFIXES` + `_is_prompt_surface` rather
than a copied list. It covers `.claude/agents|commands|skills/*`,
`src/genesis/skills/*`, **`src/genesis/identity/`**, and **any**
`src/genesis/**/prompts/` path — so editing `src/genesis/identity/CODE_AUDITOR.md` or an
executor prompt forces `substantial` even at one line, which the old list denied.
So the "Prompt / LLM behavior → both + extra scrutiny" row (build-session, Adaptive Review Protocol) is machine-enforced (a
trivial edit to one is depth-audited). User-sovereign top-level CAPS docs
(`SOUL.md`/`USER.md`/`CLAUDE.md`) are exempt. Clearance binds the marker to the
reviewed diff's FULL content, so re-staging different content after the audit
re-blocks. A precision-filtered "no findings" inline pass is FALSE CONFIDENCE for a
substantial change — not clearance. Depth is override-exempt: a findings
`# review-override` does NOT waive it; only a loud, announced-on-stderr `# depth-ack`
does (announced, not RECORDED — nothing persists it, unlike the six PR-merge sigils
in closing-session's Pre-Merge Gate, whose rows survive the session; the word "logged" now means a durable row) (the
audited escape for a genuine format mismatch). "Adversarial" is verified
STRUCTURALLY, and the recognised vocabulary is NOT what an earlier version of this
line said. `review_state._LADDER_LABEL_RE` accepts
**BLOCKER · SHOULD-FIX · CRITICAL · HIGH · MEDIUM · LOW · P1/P2/P3** — **`NOTE` is
not in it, and `MEDIUM` is** (the old text had that exactly backwards). A second
pattern, `_LADDER_PHRASE_RE`, also accepts a `"severity":` key or the phrase
`scope check`, which is why a `genesis-architect` audit clears the LADDER test even
when every finding is NOTE-level: it emits a `Scope Check:` block. That is ONE of
THREE tests, all required — `_evidence_is_adversarial` is a conjunction, so the
gate also demands `file:line` engagement (or the CODE_AUDITOR JSON's `"file"` +
`"line"` pair) and at least `_MIN_EVIDENCE_CHARS` of substance. A scope-check
header alone does NOT pass. The trap is a HAND-WRITTEN audit that is all
NOTE-level and carries neither marker — it fails on the ladder test before the
other two are reached. So run the real recall-tuned audit (`genesis-architect` /
`CODE_AUDITOR.md`); do not hand-write a soft prompt.

## The Gate Machinery — the sequence, and why it bites

Ten enforcement layers sit between a change and `main`. Learning them by hitting
them costs a session real time, every time. The canonical sequence:

```bash
python3 scripts/review_state.py evidence-path     # -> ~/.genesis/review_evidence/<key>.txt
# ... write the adversarial audit to exactly that path ...
git add <files>                                   # STAGE FIRST — mark hashes --cached
python3 scripts/review_state.py mark              # INTERNAL genesis-architect audit — plain mark, never counts
git commit -F <msg-file>                          # bare, not piped (see below)
# (a non-Anthropic cross-model round would instead be: mark --source external --defects|--clean)
git push -u origin HEAD && gh pr create --title "..." --body-file <file>   # first push: ONE command (approve the dialog)
gh pr comment <N> --body "@codex review"          # after EVERY subsequent push
python3 scripts/hooks/git_push_guard.py --check-pr <N>
gh pr merge <N> --squash --admin --match-head-commit <head>   # verbatim from --check-pr
```

**Ordering and lifetime rules that are not obvious:**

- **Stage → mark → commit.** `mark` hashes `git diff --cached`. Re-staging or
  amending after marking invalidates it, and the check fails CLOSED.
- **Evidence expires in 30 minutes.** The audit file must be recent when you
  `mark`, and the marker itself expires on the same clock. A long detour between
  audit and commit means re-marking.
- **`evidence-path` and `mark` key off the PROCESS cwd**, not a flag. Run them
  from the same worktree as the commit or the key diverges silently.
- **A successful commit WIPES the marker.** The next commit needs a fresh review;
  this is deliberate, not a bug.
- **`--match-head-commit` is mechanically required**, not a nicety — the merge
  arm blocks outright when a verified head exists and the flag is absent. Copy
  the whole command from `--check-pr` output rather than reconstructing it.

### Sigils, chained commits and worktree removal

- **Ack sigils bind per-guard, and mostly to the LAST pipeline segment.**
  `git commit ... | tail  # audit-ack` puts the ack on `tail`. Run the commit
  bare. Some guards accept a sigil on any segment, others only on the offending
  one — do not generalise from one guard's behaviour.
- **Sigils must lead the trailing comment.** The override is read from the
  leading run of recognised tokens, so `# see audit-ack notes` overrides nothing.
- **`--no-verify` is blocked before any override is even considered.** There is
  no way to skip the native hooks; fix the cause.
- **Chained commits are heavily restricted** — across worktrees, blocked
  outright; within one, later commits must be pure `--amend` with no intervening
  git command. `cd "$VAR" && git commit` fails closed with a *branch-verification*
  message, which reads like a branch problem and is not: use a literal path.
- **Worktree removal is not yours to do.** `git worktree remove` is blocked;
  `scripts/worktree_lifecycle.py` owns it: it archives stale worktrees on a
  daily timer and never deletes them (archive retention is off, #2504). Leave a dead worktree alone.
- **Editing a tracked git hook blocks the commit** until its hash is re-recorded
  (`scripts/update_hook_versions.sh`).
- **The primary checkout is the deployed install; a guard keeps hand edits out
  of it — two different ways.** `scripts/hooks/main_checkout_guard.py`
  (foreground and dispatched alike) REFUSES a Write/Edit/MultiEdit/NotebookEdit of
  a TRACKED file in the primary checkout its own script lives in. Bash it never
  refuses: it snapshots the deploy root's tracked state before the command
  (PreToolUse, keyed by `tool_use_id`) and compares after it (PostToolUse and
  PostToolUseFailure), and if a tracked file newly differs, an already-dirty one
  changed again, or HEAD moved, the session gets an advisory naming the files —
  with a restore command only when the call was aimed at the deploy root (its
  cwd, or the root's path in the command) and no merge is in progress, and never
  an instruction to move HEAD. An earlier version predicted what a shell command would
  write from its own model of cp/mv/install, the shell and git; an audit measured
  that model wrong in both directions, so it was removed rather than patched — the
  "could a flag you've never heard of change the verdict?" test (build-session, Common Traps), applied.
  The Bash check reports after the fact: it prevents nothing, a file written and
  run by the same command has already run, and anything else that changed the
  deploy root during the command (a concurrent deploy) is reported too. Make the
  change in a worktree from `origin/main` and open a PR; if the advisory fires,
  restore the deploy root with the command it names. Untracked files, linked
  worktrees, other repositories and the deploy scripts' `EPHEMERAL_DIRTY_RE`
  paths are never reported. Sessions carrying `GENESIS_UPDATE_TIER=1` (exactly)
  — the dashboard update pipeline's — are exempt. Off-switches belong to the
  owner: `GENESIS_MAIN_CHECKOUT_GUARD=0`, or the `main_checkout_guard` settings
  domain, whose overlay is read only from `${GENESIS_HOME:-~/.genesis}/config/`,
  never from the checkout.
- **⚠ `scripts/hooks/*` is NOT synced — and a WORKTREE edit is still not live.**
  `sync-hooks.sh` copies only the five GIT hooks (`commit-msg`, `post-commit`,
  `pre-commit`, `prepare-commit-msg`, `pre-push`) plus one helper into
  `.git/hooks/`. `.claude/hooks/genesis-hook` launches entry-point hook scripts
  such as `git_push_guard.py`; that guard imports the shared `shell_parse.py`
  module. `hook_output.py` is instead imported by other hook entry points. The
  launcher resolves `HOOK_ROOT` to the **MAIN
  worktree**, not the tree you are sitting in
  (`HOOK_ROOT="$MAIN_ROOT"` unless `GENESIS_HOOK_DEV_LOCAL=1`). That is
  deliberate — it stops per-branch hook drift, measured 2026-08 at 60 of 70
  worktrees running a stale `full_suite_guard`. Three consequences:
  (a) editing a guard in a worktree changes NOTHING until the branch reaches main;
  (b) to exercise a guard change in place, set `GENESIS_HOOK_DEV_LOCAL=1` — it
  runs the worktree copy and ANNOUNCES itself on stderr, so it is never a silent
  downgrade; (c) **a main checkout behind `origin/main` runs a STALE gate in every
  worktree** — MEASURED 2026-09-10 at 7,535 lines local vs 8,971 on `origin/main`,
  a 1,436-line divergence that had been silently deciding every gate call. After
  merging a PR, sync the main checkout, or the gate you are testing against is not
  the gate that ships.
  Two earlier versions of this bullet were wrong in opposite directions: one said
  such an edit "changes nothing until `sync-hooks.sh` copies it" (wrong mechanism),
  the replacement said it is "live on the very next Bash call" (wrong in every
  worktree, which is where this repo mandates you work).
- **`.github/**` and prompt surfaces are never "docs"** for the doc/config
  auto-allow (`_is_docs_or_config` returns False for both). They are NOT
  equivalent beyond that: only a PROMPT surface forces `substantial` and thereby
  the depth gate. A lone sub-50-line `.github/` workflow edit classifies as
  `inline` (its `_scope_tag` is `config`, which is not domain-sensitive), so
  Rule 2.5 demands nothing. An earlier version said both "always reach the depth
  gate", which over-states the workflow half.

## Pre-Commit Gate

Verify before any commit:

- `git diff --cached --stat` — every file in the diff belongs to your work
- `git status --short` — check untracked files (should be staged or ignored)
- Review level applied matches the adaptive protocol (build-session)
- Staged files do not include secrets (`secrets.env`, `.env`, credentials)
- **New-Store Gate (anti-proliferation).** A new persistent store — a DB table, a
  Qdrant collection, a file-plane under `~/.genesis/` — needs a written
  justification for why an existing store cannot hold it, plus a note on how it
  stays consistent with related stores (and its retention + backup path). The
  memory subsystem already sprawls across ~9 logical systems / 3 physical planes
  because this gate did not exist; a table that "felt cleaner" is how store #10
  is born. Reuse an existing store or an existing convention (e.g. the
  `~/.genesis/eval/golden/` install-local golden-set convention, #1143) unless
  the justification is real. Prefer NOT reusing a store whose SEMANTICS differ
  (don't shoehorn a store-health row into the model-eval `eval_runs` table just
  because it is "a table that exists").

- **Private-data scan before every push:** genesis-development, "Private data
  never reaches the public repo".

- GROUNDWORK-tagged code not accidentally deleted
- **PR shape, after your last commit and before opening the PR (#2737; owner,
  2026-10-05).** It reads committed history, so staged changes are not counted:
  commit first. Count the branch in counted lines, which excludes tests, prose,
  changelog fragments, and blank and comment lines, and counts a moved line
  once:

      # from the repo root, after `git fetch origin main`; a stacked PR passes its parent branch as --base.
      python3 scripts/pr_shape.py --base origin/main

  It prints `<counted> <band>`. It reads the diff through the same hardened git
  runner the review gates use, and exits 2 on a git error, so a failed diff never
  reads as `0 ok`. Use this command, not a hand-written `git diff | count_diff`:
  an attributes file, a textconv driver or a replace ref can each shrink a raw
  diff at exit 0.

  - Under 500 is the target.
  - From 500 to 1,000, add a `Shape:` line to the PR body saying why it
    cannot be smaller.
  - Over 1,000 needs the owner's approval: ask in the PR body; the owner
    decides before merge.

  It is a guideline you weigh, not a wall: a legitimate reason, stated, is
  enough. Prefer one concern per PR (one mechanism or behaviour change a
  reviewer can accept or reject alone); that too is weighed.
  Full rule: `.claude/docs/premise-check.md`, step 6.
- New capabilities registered in `_capabilities.py` + bootstrap manifest
- **Conventional commit prefixes**: `feat:`, `fix:`, `refactor:`, `docs:`,
  `test:`, `chore:`. Scope optional: `feat(ego): add cadence manager`.
  Subject line under 72 characters. Dominant category wins if mixed.
- **NEVER push to main or merge into main without a PR and user approval.**
  Enforced by PreToolUse hook.
- **Targeted tests during development.** Run ONLY the relevant test file(s)
  for your changes. NEVER run the full test suite locally — CI handles that.
  Check CI via `gh pr checks`. Bare `pytest` without a file path is banned.
- **Heavy jobs go through `genesis.hostmetrics run`.** A build, a parallel test
  run, a media encode, anything that holds gigabytes or several cores for
  minutes: launch it with the Genesis venv's interpreter
  (`~/genesis/.venv/bin/python` on a standard install; a session shell usually
  has no bare `python`) as `… -m genesis.hostmetrics run --name N --ram GB --cpu
  PCT -- CMD`. It checks the budget first (GO/WAIT/NO/ASK), then runs the job in a
  systemd scope capped at its estimates that other sessions' `status` and
  `preflight` see; without a systemd user manager it runs uncapped and invisible,
  and says so. `preflight` checks without running. It is not a way around the
  full-suite rule: test targets must still be named, and do not count on the
  full-suite guard to see inside the wrapper. Guide:
  `docs/reference/resource-budget.md`.
- **Deleting user or project data goes through the trash, not `rm`.** A file or
  directory you did not create in this session (memory and plans, config, a
  deliverable, notes) goes to `… -m genesis.trash put PATH --reason R`, which
  renames it into `~/.genesis/trash/` with a tombstone so
  `… -m genesis.trash restore ENTRY` can bring it back. `rm` stays fine for
  what you created yourself and for temp (`~/tmp`, cc-tmp, the scratchpad).
  Unlike `rm -f`, a missing path is a refusal (exit 1); a path starting with
  `-` goes after `--`. The trash makes a deletion recoverable; it does not
  replace asking the user first where deleting their data needs their OK.
  When `put` refuses (another volume, the database, a file a running process
  is using), ask the user; do
  not fall back to `rm`. Guide: `docs/reference/trash.md`.
- **Commit continuously**: after every logical unit of work. Uncommitted = lost.
- **PR closes a ledger item → cite `Ledger: <item-id>` in the PR body** (the
  32-hex `session_ledger` row id, own line, e.g. `Ledger: 71337fab…`). The
  repo-pulse worker auto-absorbs the row with PR evidence at the next session
  boundary — deterministic, reversible via `session_ledger_update`. A bare id
  mention WITHOUT the `Ledger:` marker is context, not completion (the pulse
  only proposes it). Find ids via `session_charter` or the charter injection
  block.
- **Anything a hook prints to CC's context has a hard, measured ceiling**
  (CLAUDE.md "Hook Output Persistence"). The SessionStart injection is therefore
  four `--part` entries with per-part budgets, CI ceilings on every tracked
  identity file (`tests/test_scripts/test_context_injection_budget.py`), and a
  watcher over the harness's own filings. Adding protocol text to an identity
  file is a budget decision: put DETAIL in a reference doc and a pointer in the
  injected file. If you add or grow a hook's stdout, measure it against the
  cap; after a CC bump, re-run the probe (`GENESIS_CTX_PROBE_BYTES`; procedure:
  `docs/reference/cc-compatibility.md`).
  `hook_output.py`'s `BoundedStdout` enforces the budget at the WRITER, so a
  block that forgets to check cannot overrun (2 of 12 blocks checked when the
  budget was per-caller). A Stop hook that `print`s on exit 0 is INERT.
