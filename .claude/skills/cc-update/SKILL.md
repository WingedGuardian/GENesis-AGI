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
   `cc_update_perversion`, filled continuously by the daily pre-eval job — the verdict for the
   target version may already be waiting) + this doc's §Known Issues / §Version History + the FULL
   changelog `(old, new]` range + `npm view … engines.node` + a quick community/informal sweep
   (Reddit/forums) for regressions the changelog omits.
1. **Impact eval** — `recon_cc_update_check(old, new)` MCP (backed by
   `src/genesis/recon/cc_update_analyzer.py`; PR-A extends it to evaluate the WHOLE `(old, new]`
   range rather than only the newest release). Treat the analyzer as a **triage summary, not a
   substitute** — step 0's manual read of the intermediate changelogs stays authoritative for
   anything load-bearing. Triage every hooks / MCP / CLI-flag / subagent / permissions delta.
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
