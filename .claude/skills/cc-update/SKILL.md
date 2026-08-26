---
name: cc-update
description: >-
  Update Claude Code (the CC CLI / "clog code") to a new version, or bump the pinned CC
  version. Use when the user asks to update Claude Code, bump the CC pin, evaluate a new CC
  release, or says "clog code update". Routes to the canonical, standardized process in
  docs/reference/cc-compatibility.md — do NOT re-derive the update mechanism by grepping every
  time. Do NOT use for general "what changed in CC" trivia with no intent to update.
---

# CC Update — start from the canonical source, don't rediscover

Genesis's Claude Code update process is fully standardized. This skill exists so a session
**executes** that process instead of re-deriving it (grep archaeology every time). If you find
yourself hunting for where the pin lives or how the host updates, STOP and read the doc.

## Authoritative source — read these FIRST
- `docs/reference/cc-compatibility.md` → **§Updating Claude Code (host + container)** (the exact
  steps) and **§CC Update Evaluation Checklist** (the 8-lens impact eval).
- The pin is the single source of truth: `CC_VERSION` (+ `NODE_MAJOR`) in
  `scripts/lib/cc_version.sh`.

## The process in one breath (details live in the doc)
0. **Intake — never from scratch.** Start from accumulated knowledge, not a blank grep: query
   memory (`memory_recall`) + the **pre-eval cache** (recon observations of type
   `cc_update_perversion`, written by the daily pre-eval job **where that job is deployed** — the
   verdict for the target version may already be waiting) + this doc's §Known Issues /
   §Version History + `npm view … engines.node` + a quick community/informal sweep
   (Reddit/forums) for regressions the changelog omits.

> ### ⛔ GATE — FULL CHANGELOG READ. MANDATORY, NO EXCEPTIONS. (between step 0 and step 1)
> **Read EVERY release entry in `(pinned, target]` IN FULL before step 1's triage.** No pin bump
> proceeds without this.
>
> **Fetch it fresh — nothing in Genesis maintains a changelog cache:**
> `curl -fsSL https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md -o ~/tmp/cc_changelog.md`
> That path is a scratch copy, NOT a recon-pulled cache. **Before reading, confirm the file's first
> `## ` heading equals your target version** — a stale copy that predates the target silently
> satisfies this gate while the entire new range goes unread.
>
> Scale: `(2.1.218, 2.1.246]` measured 25 releases / ~88KB. The load-bearing item can sit anywhere,
> **including deep inside the newest release**.
>
> **The analyzer and the pre-eval cache are TRIAGE SUMMARIES, never a substitute.** They prioritise;
> they do not discharge the gate.
>
> **Mark the gate done as a DURABLE row, not a chat line:** `session_ledger_add` with
> `"CC changelog gate: read (2.1.X, 2.1.Y] in full from <source>, <date>"`, and carry the same
> string into the PR body and the §Version History row added at step 4. A Version-History row with
> no "changelog read" clause means the gate was not run — treat that as blocking at review.
>
> **Delegation is allowed — with the SAME context and rigor, never a naive "summarize this."** Brief
> the sub-agent with §Delegating the full changelog read below, then **adversarially spot-check its
> load-bearing findings against ground truth**. A delegated review is not a rubber-stamp.
>
> *Origin (2026-08-26).* A session re-targeted 245→246 off the headline delta and let the
> changelog-*reading* analyzer stand in for actually reading the changelog. The cause was
> **mechanical, not just human**: on `main` the analyzer fetches only the newest 5 GitHub releases,
> keeps just the `new` version's body, and **truncates it at 1000 chars** — v2.1.246's body is
> ~9.3KB, so it saw the first ~8 of ~60 bullets and *structurally could not* surface the rest. The
> later full read found real Genesis-relevant 246 items the triage had missed: subagent `maxTurns`
> now returning **partial** output, `-p --continue`/`--resume` **plan-mode resume**, and a
> **`--strict-mcp-config` startup-hang fix** that lands directly on Guardian Diagnosis.
1. **Impact eval** — `recon_cc_update_check(old, new)` MCP (backed by
   `src/genesis/recon/cc_update_analyzer.py`). **Know its real coverage:** today it fetches only the
   newest 5 GitHub releases, matches the **target** version's tag, and truncates that body at 1000
   chars — so on a multi-release jump it sees a fraction of ONE release, or **NOTHING at all once the
   TARGET's own tag ages out of the newest 5**. That is likely mid-soak: step 2 holds a candidate for
   2-3 days while CC ships ~daily, and when it happens the LLM is handed the literal string
   "No changelog available" and still returns a confident-looking verdict. PR #1489 extends it to a
   whole-`(old, new]` map-reduce and drops the 1000-char truncation — though it still omits the
   OLDEST chunks past `_MAX_CHUNKS` on a very large range (loudly marked), so **the GATE does not
   retire when #1489 merges**. Either way the GATE's full read stays authoritative.
   Triage every hooks / MCP / CLI-flag / subagent / permissions delta.
   **Rule: a changelog claim that "we depend on X" only gates the bump if verified against LIVE
   usage** — the TodoWrite lesson: a removed feature Genesis measured 0 uses of is hygiene, not a
   blocker.
2. **Local-first gate (EXPLICIT — stronger than a normal PR).** Before ANY public pin change, align
   the **CONTAINER ONLY** to the candidate: `source scripts/lib/cc_version.sh && CC_VERSION=<candidate>
   cc_ensure_local` (`cc_ensure_local` is a shell function in that lib — source it first or it's
   command-not-found; it's container-only, so the host stays at the pin). Verify a FRESH
   `claude --version`, run the post-deploy validation (step 7) on the candidate, then **SOAK 2–3
   days** under real use. Rollback is one command: `source scripts/lib/cc_version.sh &&
   CC_VERSION=<pin> cc_ensure_local`. Do NOT run `update.sh` during the soak — it `unset`s any
   inherited `CC_VERSION` and re-aligns the container to the pin, reverting the candidate. Proceed
   to the public pin only after soak + explicit user sign-off.
   - **Mid-soak drift (CC ships ~daily, so `latest` WILL move during the soak):** re-target to a
     newer release mid-cycle ONLY if it fixes something touching our workflow / soak safety / a known
     issue (e.g. 2.1.245→246 fixed a background-retention sweep that reaped user-created
     `.claude/worktrees/`). Otherwise finish the soak on the pinned target and roll the delta into
     the next cycle — never silently chase-latest.
3. **Bump the pin** — `CC_VERSION` in `scripts/lib/cc_version.sh`. Bump `NODE_MAJOR` **only if** the
   new CC raises its `engines.node` floor — the `cc-node-lockstep` CI job
   (`scripts/check_cc_node_lockstep.py`) fails the PR otherwise.
4. **Update the doc** — `docs/reference/cc-compatibility.md`: §Current CC Version + a Version-History
   row + any new caveats (checklist step 8).
5. **PR → CI green** (incl. `cc-node-lockstep`) → private-data scan → **explicit user approval** →
   squash-merge. Then `git pull --rebase origin main`.
6. **Host-Deploy Gate** — in the SAME session after merge, run `scripts/update.sh` from `~/genesis`:
   it aligns the **container** (`cc_ensure_local`) AND the **host VM** (guardian `update-cc` op) to
   the pin, idempotently. Deploys exceed the Bash tool timeout — run it as a **background task**, not
   foreground. Between updates the nightly `genesis-cc-align.timer` closes drift.
7. **Post-deploy validation (SAME session) — critical paths AND known/tabled issues, not just a
   smoke.** Container + host `claude --version` == pin (host via the gateway `version` op /
   `~/.genesis/host_gateway_state.json`); guardian tick healthy; a CCInvoker / headless `claude -p`
   smoke on a **FRESH** process (this foreground session keeps its OLD binary until relaunch);
   **re-check this doc's §Known Issues + any tabled CC bugs against the new version**; and verify each
   behavior the impact eval flagged (e.g. an MCP arg-typing or `-p` result-shape change) on the live
   path, not just that the flag still parses.
8. **Leverage + capture** — for each newly-available capability Genesis would want, file the
   detection→behavior follow-up (below). Store what was learned to memory + this doc + the KB so the
   next update stays execute-not-rediscover.

## Delegating the full changelog read (the GATE, between steps 0 and 1)

You may hand the read to a sub-agent — but it must be done **with the same context and scrutiny you
would apply yourself**. A generic "read this changelog and tell me what you think" is NOT the gate:
the agent lacks the Genesis impact surfaces, so it returns feature summaries instead of consequences.
Brief it with THIS (adapt the range/source):

> FIRST confirm `~/tmp/cc_changelog.md`'s first `## ` heading equals `<target>`; if it does not,
> re-fetch before reading:
> `curl -fsSL https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md -o ~/tmp/cc_changelog.md`
> (nothing maintains that file — a stale copy makes this whole review vacuous).
>
> Read EVERY release entry in `(<pinned>, <target>]` IN FULL. Do NOT summarize generically, and do
> NOT treat the newest release as already-known — the load-bearing item can sit anywhere, **including
> deep inside the newest release** (that is exactly what the last miss looked like). For each release,
> judge every entry against these Genesis
> impact surfaces and report only entries with a concrete Genesis consequence, tagged
> **RISK** (could break us) / **GAIN** (fixes something we hit) / **LEVERAGE** (new capability we'd want):
> - **Hooks** — PreToolUse approval gate, `bash_safety_hook`, review-enforcement hooks; exit-code-2
>   blocking semantics, `additionalContext`, matcher parsing (Genesis uses `matcher` only — no `if`
>   conditions); **and the hook INPUT PAYLOAD SHAPE** — a renamed/removed field silently disables
>   every guard (see `tests/test_scripts/test_hook_input_contract.py`).
> - **MCP** — our servers under `claude -p --mcp-config`; arg typing (incl. empty-`{}` schemas),
>   connect/reconnect, interrupted-call reporting, tool-search/deferred tools.
> - **`claude -p` / CCInvoker** — output/result JSON shape, `modelUsage`, resume/continue,
>   mid-stream error handling, stdin/stdout.
> - **Flags Genesis actually passes** — `-p`, `--output-format json`, `--model`, `--effort`,
>   `--append-system-prompt`, `--mcp-config`, `--strict-mcp-config`, `--allowedTools`,
>   `--disallowedTools`, `--bare`, `--max-turns`, `--resume`, `--dangerously-skip-permissions`
>   (full table: `docs/reference/cc-compatibility.md` §Integration Surface).
> - **Guardian host recovery** — the host VM's `claude -p --model opus --max-turns 50
>   --dangerously-skip-permissions --strict-mcp-config --output-format json` diagnosis call: a
>   DIFFERENT machine, and the highest-stakes CC call in the system.
> - **Subagents / workflows** — spawn depth defaults, fork/background defaults, `maxTurns`
>   behavior, concurrency caps.
> - **Skills / slash commands / plugins** — auto-invocation changes, skill discovery + frontmatter,
>   plugin loading (2.1.215 removed CC's proactive `/code-review` + `/verify`, making Genesis's own
>   review-enforcement hooks the primary trigger rather than a backstop).
> - **Permissions / auto mode / Monitor** — anything changing what is auto-approved.
> - **Worktrees** — this install runs ~45; isolation, retention sweeps, `--worktree`.
> - **Cross-session** — `SendMessage`/`ListAgents`, sockets in rootless containers.
> - **Security** — credential handling, permission-bypass fixes, sandbox/redaction.
> - **Model / routing** — alias→model drift, family step-down, pricing/limits.
> Include the release number for every finding. If a release has nothing relevant, say so — do not
> pad. Flag anything you are unsure about rather than dropping it.

Then **verify before you trust it**: independently re-derive the load-bearing findings (the ones that
would gate the bump or change our code) from the changelog text and the actual Genesis code —
refute-by-default, per the "verify multi-agent output" rule. The delegation saves you reading time,
not judgment.

## Model-alias drift is watched automatically
`--model opus` (and every alias) silently re-points to a new full model id when Anthropic bumps the
family (measured: `opus` → `claude-opus-5` at a bump). The CCInvoker drift detector (`on_model_drift`,
state at `~/.genesis/cc_model_resolution.json`) fires a one-shot ALERT on any alias→id change, so a
bump that swaps the model behind an alias is never silent. This is a deliberate *float + alert*
posture (accept the newer model, but know about it), NOT a version pin.

## Durable facts (don't re-derive; verify against the doc if a memory contradicts this)
- **Both container and host install CC via npm-global — there is NO native-installer path**
  (`/usr/bin/claude` → `/usr/lib/node_modules/@anthropic-ai/claude-code`; container under its own
  npm prefix). One canonical copy per machine, enforced by `cc_shadow_scan`. (If a stored memory
  claims the host uses the native installer, it is stale — the doc is authoritative.)
- `origin` = **`GENesis-AGI`** = the **public** repo — merging the pin publishes it; installs pull
  it via `update.sh`. There is no separate private→public step.
- Auto-updater is disabled (`DISABLE_AUTOUPDATER` / `DISABLE_UPDATES` in user-level
  `~/.claude/settings.json`), so the pin is the only mover; an ad-hoc `npm i -g` gets healed back
  to the pin on the next align run.
- Downgrade is supported and deliberate — the pin can go DOWN and `update-cc <older>` rolls the
  host back; there is NO `requiredMinimumVersion` floor (it would remove the incident-recovery
  path and can brick CC).

## Watch-items a CC bump can change under Genesis
Genesis leans on CC internals; a release can shift them silently. When evaluating, check whether
the delta touches:
- **Hooks** — exit-code-2 blocking semantics, `additionalContext`, matcher parsing (our approval
  gate + `bash_safety_hook` + review-enforcement hooks depend on these).
- **Subagents** — nesting default + `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` /
  `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` (2.1.217 made nesting opt-in); and whether
  workflow/subagent file-writes still hit our PreToolUse approval gate under `claude -p`.
- **Skills/commands** — e.g. 2.1.215 removed CC's proactive `/code-review` + `/verify`
  auto-invocation (Genesis's own review-enforcement layer is now the primary trigger, not a
  backstop).
- **`--flags` Genesis passes** (`--model`, `--mcp-config`, `--bare`, `-p`, `--append-system-prompt`).

Capture any such change as a follow-up + a doc entry — that is how the next update stays
execute-not-rediscover.

## Detection → behavior (new-capability adoption path)

The impact eval treats a new feature as *informational* by design ("available"
is not "required") — correct for alerting, but it left no path from "detected"
to "Genesis actually starts using it". So, when the changelog review flags a
NEW skill/command/flag that Genesis would plausibly WANT (it overlaps an
existing Genesis workflow, replaces a hand-rolled mechanism, or covers a known
gap), do BOTH:

1. File the informational KB entry as usual (no alert — calibration unchanged).
2. Create a `follow_up_create` row (`work_state="ready"`, low priority) naming
   the SPECIFIC instruction change, e.g. "once CC >= vX.Y.Z is pinned: prefer
   native `/design` over the gstack `design-*` skills for UI drafting; decide
   precedence and update the relevant skill/CLAUDE.md instruction". A
   capability nobody wires into an instruction is a capability Genesis never
   reaches for.

Origin (2026-08-18): the `/design` research-preview announcement had no route
from detection to adoption; the user asked "how do we make sure you
automatically leverage this when you should?" — this step is the answer.
