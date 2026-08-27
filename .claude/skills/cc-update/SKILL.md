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

## The process in one breath — step numbers MATCH §Updating Claude Code in the doc
> Deliberately the same numbering in both files: "step 4" means the same thing in each. The doc is
> authoritative for detail; this is the one-breath version. If a number here disagrees with the
> doc, the doc wins — and that disagreement is a bug worth fixing rather than working around.

**Pick the target before anything else.** `npm view @anthropic-ai/claude-code version` gives
`latest`; use an explicit older version if you are deliberately not chasing latest. Every step below
is defined over `(pinned, target]`, so nothing can begin until `target` is fixed — and re-picking it
later restarts the procedure (step 5).

> ### ⛔ GATE — FULL CHANGELOG READ. MANDATORY, NO EXCEPTIONS. (part of step 1, before any triage)
> **Read EVERY release entry in `(pinned, target]` IN FULL before step 1's triage.** No pin bump
> proceeds without this.
>
> **Fetch it fresh — nothing in Genesis maintains a changelog cache:**
> `curl -fsSL https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md -o ~/tmp/cc_changelog.md`
> (`mkdir -p ~/tmp` first — `curl -f` will not create it.) That path is a scratch copy, NOT a
> recon-pulled cache. **Before reading, confirm the file CONTAINS a `## <target>` heading**
> (`grep -c '^## <target>$'`) — a stale copy fails exactly that, which is how it would otherwise
> pass unnoticed while the whole new range goes unread. Do NOT *also* require the FIRST heading to
> equal the target: under the mid-soak rule you may deliberately finish on a target `latest` has
> already passed, and equality could never be satisfied by any re-fetch.
>
> **If the target released hours ago its heading may not be in `main`'s CHANGELOG.md yet** — the
> one case no re-fetch fixes. Cover the tail from `gh release view v<target> --repo
> anthropics/claude-code` and say in the durable row which source covered which releases.
>
> Scale: `(2.1.218, 2.1.246]` measured 25 releases / ~88KB. The load-bearing item can sit anywhere,
> **including deep inside the newest release**.
>
> **The analyzer is a TRIAGE SUMMARY, never a substitute.** It prioritises; it does not discharge
> the gate.
>
> **Mark the gate done as a DURABLE row, not a chat line:**
> `session_ledger_add(session_id=<this session>, text=…)` — `session_id` is REQUIRED, the call
> hard-fails without it — with
> `"CC changelog gate: read (2.1.X, 2.1.Y] in full from <source>, <date>"`, and carry the same
> string into the PR body and the §Version History row added at step 6. The ledger row is
> **session-scoped**, so across a 2–3 day soak the session that opens the PR is not the one that
> ran the gate — the PR-body + Version-History carry-over is what actually makes it checkable
> later. For rows added from 2026-08-26 onward, a Version-History row with no "changelog read"
> clause means the gate was not run — treat that as blocking at review.
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
1. **Evaluate** — intake, the ⛔ GATE above, then triage.
   *Intake, never from scratch:* `memory_recall` + `docs/reference/cc-compatibility.md` §Known
   Issues / §Version History + `npm view @anthropic-ai/claude-code@<target> engines.node` + a quick
   community sweep (Reddit/forums) for regressions the changelog omits.
   *Triage:* `recon_cc_update_check(old, new)` MCP (backed by
   `src/genesis/recon/cc_update_analyzer.py`). **Know its real coverage:** today it fetches only the
   newest 5 GitHub releases, matches the **target** version's tag, and truncates that body at 1000
   chars — so on a multi-release jump it sees a fraction of ONE release, or **NOTHING at all once the
   TARGET's own tag ages out of the newest 5**. That is likely mid-soak: step 5 holds a candidate for
   2-3 days while CC ships ~daily, and when it happens the LLM is handed the literal string
   "No changelog available" and still returns a confident-looking verdict. (A whole-`(old, new]`
   map-reduce rewrite exists on an **unmerged** branch — *unmerged as of 2026-08-26; grep the file
   before relying on it*. It would still omit the oldest chunks on a very large range, so **the GATE
   does not retire** either way.) Triage every hooks / MCP / CLI-flag / subagent / permissions delta.
   **Rule: a changelog claim that "we depend on X" only gates the bump if verified against LIVE
   usage** — the TodoWrite lesson: a removed feature Genesis measured 0 uses of is hygiene, not a
   blocker.
2. **Deploy current `main` FIRST** — `scripts/update.sh` as a **background task**, BEFORE aligning
   the candidate, so the soak runs on current code. Otherwise step 8 lands accumulated Genesis change
   AND the CC bump together and you cannot attribute a regression to either. Skip only if the newest
   `update_history` row (`update_history_recent` MCP) is newer than the `main` commits you would
   otherwise be bundling.
3. **Align the CONTAINER ONLY to the candidate.**
   ```bash
   source scripts/lib/cc_version.sh   # NOTE: this sets CC_VERSION to the repo PIN
   CC_VERSION=<candidate>             # standalone assignment, AFTER the source
   cc_ensure_local
   cc_shadow_scan
   ```
   ⚠️ **Never write `CC_VERSION=<candidate> cc_ensure_local` and then a bare `cc_shadow_scan`.** An
   assignment *prefix* on a **function** call does not persist past that call, so the scan would run
   on the OLD pin while the container is on the candidate — and if any copy sits at the old pin it
   crowns that one canonical and **deletes the freshly-installed candidate**
   (`cc_version.sh:251`). Full rationale: doc §Updating step 3.
   Verify a **FRESH** `claude --version` (this session keeps its old binary until relaunch).
   **Check the Node floor first** — `npm view @anthropic-ai/claude-code@<candidate> engines.node`
   vs `node -v`. If it rises above the container's Node, **STOP**: no container-side Node transition
   tool exists today (`install.sh:473-477` hardcodes `>= 20` and never reads `NODE_MAJOR`), and a
   failed align is not a clean no-op — `npm install -g` has already replaced the working CC before
   the verify runs.
4. **Validate the CANDIDATE against candidate-shaped expectations, not step 9's.** During the soak
   the correct state is **container == candidate, host == old pin** — step 9 requires BOTH to equal
   the pin, which cannot hold yet. Run the critical paths, the doc's §Known Issues, and each behavior the
   impact eval flagged; any Guardian-path check exercises the **host's old** binary, so
   candidate-specific Guardian behavior needs a container-side exercise.
   - **Measure model-alias drift HERE — nothing else will.** One `claude -p --output-format json`
     before and one after the align; compare the resolved id under `modelUsage`. See §Model-alias
     drift below.
5. **Soak 2–3 days** under real use. Rollback is one command: `source scripts/lib/cc_version.sh &&
   CC_VERSION=<pin> cc_ensure_local`. Do NOT run `update.sh` during the soak — it `unset`s any
   inherited `CC_VERSION` and re-aligns the container to the pin, reverting the candidate. Proceed
   to the public pin only after soak + explicit user sign-off.
   - **Mid-soak drift (CC ships ~daily, so `latest` WILL move during the soak):** re-target to a
     newer release mid-cycle ONLY if it fixes something touching our workflow / soak safety / a known
     issue (e.g. 2.1.245→246 fixed a background-retention sweep that reaped user-created
     `.claude/worktrees/`). Otherwise finish the soak on the pinned target and roll the delta into
     the next cycle — never silently chase-latest.
   - **A re-target RESTARTS the procedure from step 1.** New target ⇒ the durable changelog-gate row
     no longer covers the range, the candidate validation ran against a different binary, and the
     2–3 day clock resets. Otherwise you publish a target with hours of real use — defeating the gate
     exactly for the workflow-affecting releases that justify re-targeting.
6. **Bump the pin** — `CC_VERSION` in `scripts/lib/cc_version.sh`; bump `NODE_MAJOR` **in the same
   file** only if the new CC raises its `engines.node` floor (the `cc-node-lockstep` CI job,
   `scripts/check_cc_node_lockstep.py`, fails the PR otherwise). In the same PR update
   `docs/reference/cc-compatibility.md`: §Current CC Version, a §Version History row **carrying the
   changelog-gate clause from step 1**, and any new caveats.
7. **PR → CI green** (incl. `cc-node-lockstep`) → **privacy scan** → **explicit user approval** →
   squash-merge. Then `git pull --rebase origin main`.
8. **Host-Deploy Gate** — in the SAME session after merge, run `scripts/update.sh` from `~/genesis`
   as a **background task** (deploys exceed the Bash tool timeout): it aligns the **container**
   (`cc_ensure_local`) AND the **host VM** (guardian `update-cc` op) to the pin, idempotently.
   Note the nightly `genesis-cc-align.timer` is **host-only** — `cc_align_host.sh` calls
   `cc_align_host_sync` and never `cc_ensure_local`, so it closes HOST drift between updates and
   (usefully) will **not** silently revert a container candidate mid-soak.
9. **Post-deploy validation (SAME session) — critical paths AND known/tabled issues, not just a
   smoke.** Container + host `claude --version` == pin (host via the gateway `version` op /
   `~/.genesis/host_gateway_state.json`); guardian tick healthy; a CCInvoker / headless `claude -p`
   smoke on a **FRESH** process (this foreground session keeps its OLD binary until relaunch);
   **re-check the doc's §Known Issues + any tabled CC bugs against the new version**; and verify each
   behavior the impact eval flagged (e.g. an MCP arg-typing or `-p` result-shape change) on the live
   path, not just that the flag still parses.
10. **Leverage + capture** — for each newly-available capability Genesis would want, file the
   detection→behavior follow-up (below). Store what was learned to memory + this doc + the KB so the
   next update stays execute-not-rediscover.

## Delegating the full changelog read (the GATE — part of step 1)

You may hand the read to a sub-agent — but it must be done **with the same context and scrutiny you
would apply yourself**. A generic "read this changelog and tell me what you think" is NOT the gate:
the agent lacks the Genesis impact surfaces, so it returns feature summaries instead of consequences.
Brief it with THIS (adapt the range/source):

> FIRST confirm `~/tmp/cc_changelog.md` CONTAINS a `## <target>` heading; if it does not, re-fetch
> before reading (`mkdir -p ~/tmp` first):
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
> - **Guardian host recovery** — the host VM's diagnosis call: a DIFFERENT machine, and the
>   highest-stakes CC call in the system. Actual shape (`src/genesis/guardian/diagnosis.py:546-555`):
>   `claude -p --model <cfg> --output-format json [--effort <cfg>] --max-turns <cfg>
>   --dangerously-skip-permissions`, **plus `--mcp-config <path> --strict-mcp-config` only when
>   `config/no_mcp.json` exists** (otherwise it logs a warning and runs without them). Defaults:
>   model `opus`, effort `high`, max-turns 50. So a change to `--effort` semantics, or to
>   `--mcp-config`/`--strict-mcp-config` handling, lands squarely on this call.
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
> Include the release number for every finding. Flag anything you are unsure about rather than
> dropping it.
>
> **Coverage receipt (required, not optional).** End your report with one line per release in the
> range, in order — `2.1.NNN: <finding tag(s)>` or `2.1.NNN: nothing relevant` — so every release is
> explicitly accounted for. Do not pad findings, but do not omit a release from this list either: a
> missing line reads as "never read", not as "nothing to say". If you run short on turns or budget,
> STOP and say exactly which releases you did not reach rather than returning a report that looks
> complete — partial output no longer fails on its own, so an unflagged early stop is invisible to
> the person reading you.

Then **verify before you trust it**: independently re-derive the load-bearing findings (the ones that
would gate the bump or change our code) from the changelog text and the actual Genesis code —
refute-by-default, per the "verify multi-agent output" rule. The delegation saves you reading time,
not judgment.

**And verify COVERAGE separately from accuracy — they fail differently.** Re-deriving the findings
the agent returned can only catch findings that are *wrong*; it cannot detect releases the agent
never opened. From CC **2.1.246 onward** (the current pin is 2.1.218 — check which version the delegating
session is actually running) a subagent that hits `maxTurns` returns **partial output without
failing**, so a silent stop two-thirds through the range is indistinguishable from a genuinely short
report. Close that by construction: enumerate every `## ` release heading in `(pinned, target]`
yourself (`grep -n '^## ' ~/tmp/cc_changelog.md`), and require the agent to acknowledge each release
explicitly — including the ones it judged irrelevant. Reconcile the two lists before marking the
gate done. Any release without an acknowledgement means the gate is **not** done, no matter how
good the returned findings look. Ask for the per-release acknowledgement in the brief, not
afterwards — a re-ask cannot recover context the agent already dropped.

## Model-alias drift — CHECK IT BY HAND, nothing watches it
`--model opus` (and every alias) silently re-points to a new full model id when Anthropic bumps the
family — **measured live: `opus` resolved `claude-opus-4-8` → `claude-opus-5` across a CC bump**,
with no warning anywhere. This is NOT a downgrade, so the tier-based downgrade detector is blind to
it, and **no drift detector exists on `main` today** — there is nothing to alert you.

So make it an explicit step-4 validation item: run one `claude -p --output-format json` before and
after the candidate align and compare the resolved id under `modelUsage` (note `model_used` is a Genesis-side `CCOutput`
attribute, NOT a field of the raw `claude -p` JSON), not just
that the alias still works. The posture is deliberate *float* (accept the newer model), but floating
without checking means the model behind every CCInvoker and experiment call can change unnoticed.

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
