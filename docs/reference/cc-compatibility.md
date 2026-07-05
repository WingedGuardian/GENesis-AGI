# Claude Code Compatibility Tracking

> **Purpose:** Track Genesis's dependency on Claude Code features, version requirements,
> and update impact. CC is Genesis's intelligence layer — every CC update potentially
> affects Genesis. This document is the reference point when CC updates arrive.
>
> **Process:** When CC updates, consult this document. The recon subsystem
> (`cc_update_analyzer.py`) auto-detects version changes and classifies impact
> through 8 evaluation lenses (see analyzer prompt for details). This document
> is updated manually after each evaluation.
>
> Created: 2026-03-09 | Last updated: 2026-07-05

---

## Current CC Version

**Pinned:** Claude Code **2.1.201** (bumped 2026-07-04 from 2.1.198; a 3-release, bugfix-dominated delta — see Version History for the evaluation). Deployment path: merge the pin, then one `scripts/update.sh` run aligns the container via `cc_ensure_local` and syncs the host via the guardian `update-cc` op — this bump is the pipeline's first single-command end-to-end run (previous bumps exercised the pieces individually). Node floor unchanged (`>=22` across 2.1.199–2.1.201). Prior state: 2.1.198 on both machines, deployed + verified 2026-07-01 (#841); host PATH shadow (a stray native-installer symlink at `~/.local/bin/claude` masking the npm copy) found and removed 2026-07-04, so each machine has exactly one CC install again. The 2.1.201 range **retains** the fullscreen-renderer scrollback fix that motivated the 2.1.173 pin. **Both** container and host install Claude Code **via npm-global** (`npm install -g @anthropic-ai/claude-code@<version>` — the container auto-detects its npm prefix, the host uses `sudo npm install -g`). There is no native-installer path.
**Pin (single source of truth):** `CC_VERSION` in `scripts/lib/cc_version.sh`, which
also exports the shared **`cc_ensure_local`** aligner. Sourced by `scripts/install.sh`,
`scripts/host-setup.sh`, `scripts/bootstrap.sh`, and `scripts/update.sh`. Bump it in one
place; the next `install.sh`/`bootstrap.sh`/`update.sh` run aligns the **container's**
own Claude Code to the pin via `cc_ensure_local` (installs when absent AND upgrades/
downgrades a drifted-but-present CC — the earlier scripts only installed when CC was
*missing*, so a bumped pin never reached an already-installed container). `update.sh`
additionally syncs the **host VM** via the guardian `update-cc` op (see "Updating Claude
Code" below).
**Minimum required by Genesis:** intentionally **not enforced** at runtime (all current code works with 2.0+). A managed-settings `requiredMinimumVersion` floor (`/etc/claude-code/managed-settings.json`, Linux-only — never read from user/project `settings.json`) was evaluated and **deliberately rejected**: a hard floor removes the incident-recovery downgrade path the project has actually used (the 2.1.90→2.1.87 scrollback rollback) and can brick CC if the floor is written above the installed version. Drift is prevented instead by the npm pin + `cc_ensure_local` (aligns the local CC to the pin on every install/bootstrap/update — exact-match, so a downgrade pin also applies) + the unified `update-cc` updater for the host + `DISABLE_AUTOUPDATER`/`DISABLE_UPDATES` (so CC never self-bumps).

---

## Updating Claude Code (host + container)

One pin, both machines, no drift:

1. Edit `CC_VERSION` in `scripts/lib/cc_version.sh` (one line). If the new CC
   version raises its `engines.node` floor, bump `NODE_MAJOR` in the same file
   in lockstep — the `cc-node-lockstep` CI job (`scripts/check_cc_node_lockstep.py`)
   fails the PR if `NODE_MAJOR` is below the pinned CC's required Node major, so
   this can't be forgotten (it fails open on a transient npm-registry error).
2. Merge to `main`.
3. Run `scripts/update.sh`. It updates the container, redeploys the Guardian
   (carrying the new gateway script), then queries the host's CC version and —
   **only if it differs from the pin** — dispatches `update-cc <pin>` to the
   Guardian gateway on the host. The dispatch is idempotent (acts only on drift)
   and non-fatal (a failed host update leaves the previous working CC in place).

The host install runs through the gateway's `update-cc <semver>` op
(`scripts/guardian-gateway.sh`): it validates the argument as a bare semver,
installs `@anthropic-ai/claude-code@<version>` using the npm that owns the in-use
`claude` (so the global prefix matches the binary the Guardian resolves via its
baked `command -v claude` path), and verifies `claude --version` afterward.

To move the host by hand:
`ssh -i ~/.ssh/genesis_guardian_ed25519 <host_user>@<host_ip> "update-cc 2.1.201"`

**Incident downgrade:** because there is no `requiredMinimumVersion` floor, the
host (or container) can be rolled back to an older known-good version the same way
(`update-cc <older>`) if a release regresses — exactly what the 2.1.90→2.1.87
rollback needed.

### One-canonical-copy policy (`cc_shadow_scan`)

The pin machinery only manages ONE copy of Claude Code per machine. Any second
copy drifts silently and eventually shadows the pinned one in some PATH context
— four real incidents in one week (2026-07): an nvm-tree copy that won
interactive PATH and showed a months-old version; a native-installer symlink in
`~/.local/bin` doing the same; ~490MB of leftover native version blobs; and a
user-prefix copy invisible to non-interactive shells, which made the updater
"reinstall" CC on every run AND silently skipped MCP registration.

`cc_shadow_scan` (in `scripts/lib/cc_version.sh`, run by install/bootstrap/
update/host-setup; mirrored compactly in the gateway's `update-cc` op for the
host) enforces the policy: the canonical copy is what `command -v claude`
resolves (all automation runs that one); every other copy on a known surface
(nvm trees, `~/.claude/local`, `~/.local/bin`, native version blobs, stale npm
prefixes) is removed — but only when provably a claude-code install; anything
ambiguous is warned about and left alone, as are `alias claude=` lines in rc
files (detected, never edited). The gateway variant is user-dir-only (it never
sudo-removes). Deliberate multi-copy setups: `CC_SHADOW_SCAN=0` opts out.

`cc_ensure_local` also probes known prefixes (`CC_PROBE_DIRS`) before declaring
CC "not installed", so a PATH-blind install is aligned in place instead of
reinstalled forever.

---

## Integration Surface — Genesis Components That Use CC

| Genesis Component | CC Feature Used | Files | Notes |
|-------------------|----------------|-------|-------|
| CCInvoker | `claude` CLI, `-p` flag, `--output-format` | `src/genesis/cc/invoker.py` | Core dispatch mechanism |
| CCReflectionBridge | Background sessions, system prompts | `src/genesis/cc/reflection_bridge/` | Light/Deep/Strategic reflection dispatch |
| CCSessionManager | Session creation/tracking | `src/genesis/cc/session_manager.py` | Session lifecycle |
| CCCheckpoint | Session pause/resume | `src/genesis/cc/checkpoint.py` | User question handling |
| CCFormatter | Output formatting | `src/genesis/cc/formatter.py` | Response parsing |
| IntentClassifier | N/A (Genesis-internal) | `src/genesis/cc/intent.py` | No CC dependency |
| Guardian Diagnosis | `-p`, `--model opus`, `--effort` (configurable, default `high`; omitted for Haiku), `--max-turns 50`, `--dangerously-skip-permissions`, `--output-format json` | `src/genesis/guardian/diagnosis.py` | Agentic diagnosis + recovery on host VM. Highest-stakes CC call in system. |

### CC CLI Flags Used by Genesis

| Flag | Used By | Purpose |
|------|---------|---------|
| `-p` | CCInvoker | Prompt input (non-interactive single-prompt mode) |
| `--output-format json` | CCInvoker | Structured output parsing |
| `--model` | CCInvoker | Model selection per session |
| `--effort` | CCInvoker | Thinking effort level |
| `--system-prompt` / `--append-system-prompt` | CCInvoker | System prompt injection |
| `--dangerously-skip-permissions` | CCInvoker | Background session permission bypass |
| `--disallowedTools` | CCInvoker | Tool blacklist for scoped sessions (inbox, mail) |
| `--mcp-config` | CCInvoker | MCP server configuration per session |
| `--resume` | CCCheckpoint | Session pause/resume |
| `--allowedTools` | CCInvoker | Tool whitelist for scoped sessions |
| `--bare` | CCInvoker | Minimal UI mode for background sessions |
| `--max-turns` | Guardian Diagnosis | Turn limit (Guardian uses config-driven value) |
| `--strict-mcp-config` | Guardian Diagnosis | Prevent global MCP config loading in diagnosis |
| `--permission-mode` | Planned | Session permission governance |

---

## CC Features — Usage Status

### Actively Used
- CLI non-interactive mode (`-p`)
- Background session dispatch with `--dangerously-skip-permissions`
- System prompt injection (`--system-prompt`, `--append-system-prompt`) — all background session paths use `--append-system-prompt`
- Effort levels (`--effort`)
- Tool blacklisting (`--disallowedTools`) and whitelisting (`--allowedTools`)
- MCP config per session (`--mcp-config`)
- Session resume (`--resume`)
- Bare mode (`--bare`)
- Turn limits (`--max-turns`) and strict MCP config (`--strict-mcp-config`) for Guardian diagnosis
- PreToolUse / PostToolUse / SessionStart / Stop / UserPromptSubmit hooks

### Planned to Use (Phase 6-7)
- **Skills system** — Load Genesis skills into CC background sessions
- **Hooks in frontmatter** (CC 2.1) — Per-session hook configuration via session_config.py
- **Forked skill context** (CC 2.1) — Skill isolation in background sessions
- **Wildcard permissions** (CC 2.1) — `Bash(*-h*)` style permission patterns
- **Hot reload** (CC 2.1) — Skill updates without session restart

### Evaluated — Not Using
- **Scheduled tasks** (CC 2.0) — Genesis uses APScheduler instead. CC scheduled tasks
  are desktop-only and less sophisticated than our depth-classified awareness loop.
  Re-evaluate if CC adds server-side scheduled tasks.
- **`/teleport` to claude.ai/code** — Not relevant for server-side Genesis.
- **Shift+Enter for newlines** — UX feature, no Genesis impact.
- **`MCP_CONNECTION_NONBLOCKING=true`** (CC 2.1.89) — Skips MCP connection wait
  entirely in `-p` mode. Too aggressive for Genesis — most background sessions
  need MCP tools early (ego queries health, reflection uses memory). The automatic
  5s connection bound (also 2.1.89) provides sufficient timeout safety without
  opt-in. Revisit if background sessions show >5s MCP hangs.

### Not Yet Evaluated
- **`defer` permission decision** (CC 2.1.89) — PreToolUse hooks can return
  `"defer"` to pause headless sessions at a tool call. Session resumes via
  `--resume` with hook re-evaluation. Potential building block for earned
  autonomy pipeline: hook returns `defer` on high-risk operations, Guardian
  approves, session resumes. Evaluate when autonomy work (gap closure roadmap)
  progresses.
- **`PermissionDenied` hook** (CC 2.1.89) — Fires after auto mode classifier
  denials. Return `{retry: true}` to tell the model it can retry. Not relevant
  while background sessions use `--dangerously-skip-permissions`. Evaluate if
  we move to auto mode + hooks.
- **Agent denial doesn't stop** (CC 2.1) — May affect CCInvoker error handling
- **Model language configuration** — Potentially useful for multi-language user support
- **Session moves** (CC 2.1) — Could enable session migration between terminals

---

## CC Update Evaluation Checklist

When a new CC version is released, run through this:

1. **Changelog review:** What changed? New features, breaking changes, deprecations?
2. **8-lens evaluation:** Check each change against: programmatic integration,
   hooks/permissions, MCP/tools, interactive CLI experience, performance/stability,
   security/trust, platform/environment, model/API. (See `_ANALYSIS_PROMPT` in
   `src/genesis/recon/cc_update_analyzer.py` for the full lens definitions.)
3. **Flag/API changes:** Are any CLI flags we use modified or deprecated?
4. **New capabilities:** Does this unlock something we're working around?
5. **Obsolescence check:** Does this make something we built unnecessary?
6. **Interactive UX check:** Does this change the foreground session experience?
   Rendering, scrollback, terminal behavior, keyboard shortcuts?
7. **Test impact:** Run Genesis CC integration tests after update.
8. **Update this document** with findings.

---

## Version History

| CC Version | Date Evaluated | Genesis Impact | Action Taken |
|------------|---------------|----------------|--------------|
| 2.0 | 2026-03-09 | Scheduled tasks noted, not adopted | Documented in research insights |
| 2.1 | 2026-03-09 | Hooks in frontmatter, forked context, wildcard perms | Queued for Phase 7 |
| 2.1.83 | 2026-03-25 | Changelog not available | Recorded in recon findings |
| 2.1.84 | 2026-03-26 | Changelog not available | Recorded in recon findings |
| 2.1.85 | 2026-03-26 | Conditional `if` for hooks, MCP env vars, OAuth improvements | Documented, no action needed |
| 2.1.86 | 2026-03-27 | Session ID header, VCS exclusions, `--resume` fix, `--bare` MCP fix | Documented, no action needed |
| 2.1.87 | 2026-03-29 | Cowork Dispatch fix | Documented, no action needed |
| 2.1.88 | 2026-03-30 | `PermissionDenied` hook, named subagents, prompt cache fix, memory leak fixes, OOM fix for large Edit | Documented, no immediate action |
| 2.1.89 | 2026-04-01 | `defer` permission, `MCP_CONNECTION_NONBLOCKING`, 5s MCP bound, scrollback regression | Analyzer prompt broadened, scrollback workaround applied, `defer` documented for future use |
| 2.1.90 | 2026-04-01 | `--resume` cache miss fix, hook exit-code-2 fix, Edit/Write format-on-save fix, SSE+transcript O(n^2)→O(n) perf | Upgraded, tested — scrollback still broken on Linux. Downgraded to 2.1.87. |
| 2.1.91–2.1.118 | — | Range not individually tracked; no Genesis-breaking changes identified in changelog review | Pending changelog backfill via cc_update_analyzer (follow-up) |
| 2.1.119 | — | PostToolUse/PostToolUseFailure hooks gain `duration_ms` field | Additive — no action needed |
| 2.1.126 | — | `--dangerously-skip-permissions` extended to bypass `.claude/`, `.git/`, `.vscode/` writes | Additive — background sessions already use this flag |
| 2.1.128 | — | `MCP: workspace` is a reserved server name | No Genesis conflict |
| 2.1.132 | — | `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` env var lands — **partial scrollback mitigation** (residual clipping remained; see Known Issues) | Added to `CCInvoker._build_env()` and `settings.json` in PR #479; removed from settings 2026-06-11 (fullscreen renderer) |
| 2.1.133 | — | All hooks gain `effort.level` JSON field + `$CLAUDE_EFFORT` env var | Additive — Genesis hooks only read fields they need |
| 2.1.138 | — | Running version before 2026-06-01 upgrade | Proven stable in production |
| 2.1.139 | — | Hooks run WITHOUT terminal access — terminal I/O silently suppressed | Safe — Genesis hooks only use stderr for logging |
| 2.1.143 | — | Stop hooks that block cap at 8 consecutive blocks | Safe — `genesis_stop_hook.py` never returns exit 2 |
| 2.1.150 | — | npm `stable` tag | Noted |
| 2.1.152 | — | `cache_creation_input_tokens` reporting bug fixed (was silently 0) | Dashboard cost numbers will appear higher — this is a correctness fix, not a regression |
| 2.1.153 | — | `/model` saves selection as default for new sessions | No background session impact (`--model` flag overrides) |
| 2.1.154 | — | Opus 4.8 support; lean system prompt default for Opus 4.8+ | Verified: `--append-system-prompt` works correctly with Opus 4.8 (tested 2026-06-01) |
| 2.1.156 | — | Fix thinking-block corruption bug for Opus 4.8 on session resume | Minimum safe version for Opus 4.8 extended thinking |
| 2.1.157 | — | Fix tmux copy-on-select regression | Relevant for Linux/tmux users |
| 2.1.159 | 2026-06-01 | Opus 4.8 stable | Tested — all 8 integration tests passed; CCInvoker E2E verified |
| 2.1.160 | 2026-06-01 | Current `latest` tag (promoted from `next` mid-upgrade) | **Upgraded to this version.** Auto-updater bumped from 2.1.159 in flight. Re-verified E2E via CCInvoker for Sonnet and Opus 4.8. |
| 2.1.161 | 2026-06-10 | Fixes `--output-format json`/`text` stdout corruption from background subagents; parallel-tool failures no longer cancel sibling calls; background sessions no longer boot a stale model from daemon env; `claude mcp` secret redaction | **Benefits Genesis** (json output + per-session `--model` pinning). No action. |
| 2.1.162 | 2026-06-10 | `claude agents --json waitingFor`; WebFetch permission precedence for preapproved domains; MCP sub-1000 ms timeout fix; read-only config-dir hang fix | Additive/fixes. Genesis blocks WebFetch via a hook, not `WebFetch()` rules — no impact. |
| 2.1.163 | 2026-06-10 | `requiredMinimumVersion`/`requiredMaximumVersion` (**managed-settings only**); `/plugin list`; Stop/SubagentStop hooks gain `additionalContext`; hook `if:"Bash(...)"` now matches inside subshells/backticks | Version floor noted (managed-only — deferred, see Current CC Version). Genesis hooks use tool-name `matcher`s, not `if:` command conditions — `if:` change does not apply. |
| 2.1.165, 2.1.167–2.1.168 | — | Bug fixes and reliability improvements | No action |
| 2.1.166 | 2026-06-10 | `fallbackModel` (up to 3 fallbacks); glob patterns in deny tool-name position; hardened cross-session `SendMessage` authority; thinking-disable on think-by-default models | `fallbackModel` deliberately **not** adopted — silent auto-degrade conflicts with "quality over cost." Glob deny redundant with `bash_safety_hook.sh`. |
| 2.1.169 | 2026-06-10 | `--safe-mode`/`CLAUDE_CODE_SAFE_MODE`; `/cd`; `disableBundledSkills`; `--mcp-config` + managed-MCP enforcement fixes; background sessions preserve `--bare`/`--ide` across retire→wake; project-env (`ANTHROPIC_MODEL`) honored on pre-warmed workers | Fixes touch flags Genesis uses (`--mcp-config`, `--bare`). Smoke-tested post-upgrade. No config change. |
| 2.1.170 | 2026-06-10 | **Claude Fable 5 (Mythos-class)** model access; fixed sessions not saving transcripts (and missing from `--resume`) when launched from a shell that inherited CC env vars | **Upgraded to this version.** Fable 5 → separate eval follow-up (background sessions pin `--model`, so no leak). Transcript fix benefits Genesis background sessions (inherited-env spawn path). |
| 2.1.172 | 2026-06-11 | Nested sub-agents (5 levels); long-conversation render perf + idle CPU reduction; background-agent fixes (project settings cross-read, stale-version attach EAUTH); `[1M][1m]` doubled-suffix fix; mouse tracking disabled on limited Windows consoles | Additive/fixes — recon classified informational. Background-agent fixes benefit Genesis dispatch paths. |
| 2.1.173 | 2026-06-11 | Fable 5 model IDs with `[1m]` suffix now normalized (1M context is default); Windows sandbox warning fix | **Upgraded to this version (container).** Settings had `"model": "claude-fable-5[1m]"` — normalization removes suffix-handling edge cases. |
| 2.1.174–2.1.196 | 2026-07-01 | Range reviewed via changelog. Overwhelmingly fixes + perf: **hook** matcher fixes (comma-separated matchers never firing @191, hyphenated matchers substring-matching @195, symlinked `.claude/settings.json` @176, `.claude/rules/` via symlinks @198); **skills** fixes (nested `.claude/skills` load + closest-cwd-wins @178, frontmatter accepts kebab/snake/camelCase @186, hot-reload no longer re-sends full listing @176, duplicate autocomplete @181/@183); **MCP** fixes (untrusted-workspace `.mcp.json` no longer auto-spawned @196 [security], `headersHelper` re-auth on 401/403 @193, discovery/OAuth retries @191, false "server disconnected" for retired tools @186); **headless/`-p`** fixes (auth-stub tools no longer exposed in headless/SDK mode @183, `--resume "No conversation found"` @187, structured-output infinite re-calling @186/@187); **auto-mode** safety (destructive git + terraform/pulumi/cdk destroy blocked @183, `Agent(type)` deny rules enforced for named spawns @186, denial reasons in transcript @193); **perf/memory** (~37% less streaming CPU + reduced long-session terminal-cache growth @191, idle-session history loss fixed @181). Removed the `TeamCreate`/`TeamDelete` tools @178 and the `/agents` wizard @198 — grep-verified **not referenced** anywhere in Genesis (`src/`, `.claude/`, `scripts/`). | No Genesis-breaking changes. Perf + session-stability fixes directly benefit long CC sessions (the marathon-session heap/history-loss class). No code change required. |
| 2.1.197 | 2026-07-01 | **Claude Sonnet 5** becomes CC's default model (native 1M-token context, promotional pricing) | Genesis pins explicit models everywhere it calls CC (`--model`, cc-sonnet/cc-haiku/opus), so the changed *default* does not auto-apply to any Genesis path. Sonnet 5 adoption → separate eval. |
| 2.1.198 | 2026-07-01 | Claude in Chrome GA; background agents auto-commit/push/draft-PR on finishing code work; built-in Explore agent inherits the session model (capped at opus); subagents + context compaction inherit extended-thinking config; `/dataviz` skill; `Notification` hook fires for background-agent completion; broad fullscreen/background/hook fixes | **Deployed + verified on both machines 2026-07-01 (#841):** container via `cc_ensure_local` (`claude --version` + headless smoke); host 2.1.173→2.1.198 via the guardian `update-cc` op (gateway-verified, targeted CC-only sync). Fullscreen renderer (the reason for the 2.1.173 pin) preserved and improved — no scrollback regression across the range. |
| 2.1.199–2.1.200 | 2026-07-04 | Bugfix-dominated, several land on known pain points: **subagents** cut off by rate limits/server errors now return partial work or fail cleanly instead of empty-success (@199) and empty-result (@200); **background-agent daemon** fixes — Linux kill-loop every ~50s after unclean shutdown (@199), stale `daemon.lock` with OS-reused PID blocking all starts (@200), silent mid-turn stops after sleep/wake (@200), old-build daemon takeover (@200); **`AskUserQuestion` no longer auto-continues** on idle timeout by default (@200 — opt back in via `/config`); "default" permission mode renamed "Manual", old value still accepted (@200); `SessionStart`/`Setup`/`SubagentStart` hooks no longer swallow stderr on exit 2 (@199); project plugins now load in git worktrees (@200); tmux 3.4+ flicker fixed via synchronized output (@200); corrupted-config reset backs up first (@200); `CLAUDE_CODE_RETRY_WATCHDOG` raises transient-error retries, `MAX_RETRIES` cap lifted (@199); SSL errors fail fast with guidance (@199) | Compat-checked against Genesis (grep-verified): nothing depends on AskUserQuestion auto-continue; no `"default"` permission-mode pinning anywhere; `disabledMcpServers` already a proper array. Daemon/subagent fixes directly target failure classes Genesis hit in production (rate-limited review agents returning empty success 2026-07-03; two unclean shutdowns 2026-07-04). `RETRY_WATCHDOG` noted as a candidate for guardian `claude -p` resilience (PR-G scope). Re-test the tmux scrollback symptom post-deploy. |
| 2.1.201 | 2026-07-04 | Sonnet 5 sessions stop using the mid-conversation system role for harness reminders — behavioral change only | **Pin bumped 2.1.198→2.1.201 (#897); DEPLOYED + VERIFIED on all four nodes 2026-07-04/05** — machine A container `/usr/local/bin` + host `/usr/bin` (gateway-verified), machine B container `~/.npm-global` + host, all reporting 2.1.201; machine B's host also healed Node 20→22 in the same pass. This was the first single-command `update.sh` pipeline exercise; it surfaced three gaps, all fixed in the follow-up pipeline-hardening PR: the re-tracked `settings.local.json` blocking the clean-tree gate, drift healing skipped on no-delta runs ("Nothing to do" after a manual pull), and `cc_ensure_local`'s verify failing falsely when the npm prefix is off-PATH in non-interactive shells. Watch reflection-session quality on Sonnet for a few days (harness-reminder role change). |

---

## Known Issues

### v2.1.89+ Scrollback Regression — RESOLVED via fullscreen renderer (2026-06-11)

CC v2.1.89 changed default terminal rendering to an alt-screen mode that destroys
terminal scrollback on Linux/tmux. Confirmed cross-platform regression (GitHub issues
#41965, #41814, #42024, #42002, #42076, #42180).

**First mitigation (partial):** CC 2.1.132 added `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`,
applied in PR #479 (`settings.json` + `CCInvoker._build_env()`), which unblocked the
version upgrade. This stopped the catastrophic alt-screen corruption but NOT the
classic renderer's residual clipping: repaints inside tmux intermittently drop chunks
of output before tmux commits them to its history (e.g., the first item of a list
missing from scrollback). Still open upstream as of 2.1.173: #52924, #46834, #60464,
#62890. The earlier "fully functional" assessment (2026-06-01) was premature — drops
are intermittent and survived light testing.

**Resolution (2026-06-11):** Interactive sessions switched to the **fullscreen
renderer** (`"tui": "fullscreen"` in user-level `~/.claude/settings.json`). The
conversation lives in CC's virtualized in-app scrollback — nothing is dropped because
tmux history is no longer the source of truth. Scroll with mouse wheel / PgUp; search
via `Ctrl+O` then `/`; export the full transcript into tmux scrollback on demand with
`Ctrl+O` then `[`. Revert with `/tui default`.

Consequently `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` was **removed** from project
and user `settings.json` — per CC docs it forces the classic renderer regardless of
the `tui` setting, so leaving it set silently defeats fullscreen mode. It remains in
`CCInvoker._build_env()` (`invoker.py`) as defense-in-depth for headless dispatch;
CC docs state renderer settings don't apply to background-session rendering, so it is
harmless there.

**Known cosmetic issue (upstream):** the `/model` banner renders literal `[1m`/`[22m`
(ESC-stripped SGR codes) around the model name — CC bug #66643, present in 2.1.173,
not fixable locally. Do not "fix" with `NO_COLOR`/`TERM=dumb`; that degrades all output.

**History:** Downgraded to v2.1.87 on 2026-04-01; ran on 2.1.138 (auto-updated before
update controls were in place); upgraded to 2.1.159 on 2026-06-01; auto-updater bumped
to 2.1.160 mid-session; 2.1.170 on 2026-06-10; 2.1.173 + fullscreen renderer on
2026-06-11.

### Install Method: npm Only (2026-04-01)

Removed the standalone installer (`curl -fsSL https://claude.ai/install.sh`)
in favor of npm-only (`npm install -g @anthropic-ai/claude-code`). The standalone
binary auto-updates on every launch, which silently overrode version pinning.
npm pinning gives full version control. Upgrades are deliberate via recon triage.

**Suppressing the native installer nag:** CC shows a status bar warning when running
from npm. Set `DISABLE_INSTALLATION_CHECKS=1` to suppress this. The install script
adds this to `~/.bashrc` automatically. Do NOT run `claude install`.

**Dual npm prefix gotcha (2026-06-01, revised 2026-06-10):** Containers may have
multiple npm prefix locations. The install script now auto-detects which prefix PATH
resolves by checking `npm config get prefix`:
- **User-level prefix** (e.g. `~/.npm-global`): installs without sudo or `--prefix`,
  so `which claude` finds the new binary directly.
- **System-level prefix** (`/usr/local` or `/usr`): installs with `sudo --prefix /usr/local`
  to avoid the `/usr/lib` misrouting issue.

For manual upgrades, use plain `npm install -g` (no `--prefix`) — it installs to
your configured prefix, which is what PATH finds:
```bash
npm install -g @anthropic-ai/claude-code@<version>
```
If your prefix requires root (`/usr/local`), add `sudo --prefix /usr/local`.

**Host VM (verified 2026-06-15):** The host installs Claude Code the **same way as
the container — npm-global**, not the native installer. On this host the npm prefix
is `/usr`, so the package lives in `/usr/lib/node_modules/@anthropic-ai/claude-code`
with the binary at `/usr/bin/claude` (on the default PATH). There is no
`~/.local/share/claude/versions/` tree and no `claude install` subcommand.
> An earlier revision of this section described a native-installer layout under
> `~/.local/` ("do not npm install on the host"); that was **stale** — the live host
> migrated to npm-global. Always trust `type -a claude` + `npm ls -g
> @anthropic-ai/claude-code` over this doc.

Update the host with the Guardian gateway's `update-cc` op (see "Updating Claude
Code" above), which runs — under `sudo` — the same npm install the container uses,
resolving npm next to the in-use `claude` so the global prefix matches. By hand:
```bash
sudo npm install -g @anthropic-ai/claude-code@<version>   # e.g. 2.1.173
claude --version                                          # verify
```
`DISABLE_AUTOUPDATER=1`/`DISABLE_UPDATES=1` are set in the host's user-level
`~/.claude/settings.json`, so updates are manual/controlled. Rollback is the same
command with an older version (there is no `requiredMinimumVersion` floor blocking it).

**Host CC reachability:** `/usr/bin/claude` is on the default PATH, so the Guardian
gateway's `version` op resolves it and reports the real version over SSH (confirmed:
`2.1.87`). Guardian diagnosis resolves the binary via `command -v claude`
(`install_guardian.sh` bakes the resolved path into `guardian.yaml`). The host CC
binary used to be managed entirely separately from the Genesis→Guardian *code* sync
(`redeploy`/`update` sync code only) — but `scripts/update.sh` now keeps the host CC
in step with the pin automatically via `update-cc`, so the two no longer drift.

### Auto-Updater Suppression Requires User-Level Settings

**Critical gotcha discovered 2026-06-01:** `DISABLE_AUTOUPDATER=1` and
`DISABLE_UPDATES=1` in the **repo's** `.claude/settings.json` are NOT sufficient to
stop CC's auto-updater. The repo settings only apply when CC is launched from the
project directory. The auto-updater runs in contexts where repo settings don't apply.

**Required:** `DISABLE_AUTOUPDATER=1` and `DISABLE_UPDATES=1` must be in the
**user-level** `~/.claude/settings.json` on every machine running Genesis (container
+ host VM). This is the file with authority for global CC behavior.

**Automated:** `scripts/install.sh` and `scripts/host-setup.sh` now create or merge
these env vars into `~/.claude/settings.json` during setup (preserving any existing
keys). Fresh installs no longer need manual intervention.

This file is per-machine and NOT tracked in the repo. Without the install-script
automation, CC silently bumps versions out from under us — we discovered this when
the host VM was running 2.1.119 despite the 2.1.87 script pin, and again mid-session
when the container auto-updated from 2.1.159 to 2.1.160.

### Cache Inflation (since ~2.1.100, accepted)

CC sends ~20K extra `cache_creation` tokens per payload by design. This is accepted
overhead — upstream has chosen not to fix it. No action taken.

### Cost Tracking Correction (2.1.152)

`cache_creation_input_tokens` was silently reporting 0 before 2.1.152. After upgrade,
dashboard cost numbers will appear higher. This is a correctness fix, not a regression —
actual costs were always being incurred, just not displayed.

### Opus 4.8 Lean System Prompt + Prompt Injection Defenses (2.1.154+)

Opus 4.8 uses a leaner default CC system prompt AND has **enhanced prompt injection
defenses** that apply to `--append-system-prompt` content. Verified 2026-06-01 by
testing Opus 4.8 directly: the defenses key on **channel + shape + intent**, NOT on
directive language.

**What triggers the defense (avoid in identity files):**
- **Output-suppression / gag clauses** — "say only X and nothing else", "no preamble"
  pushed to the point of suppressing reasoning. Operational ordering rules are fine
  (e.g., "your final text block is captured — put the evaluation last") because they
  carry the technical why.
- **Free-floating imperatives without role coherence** — directives that don't define
  a role, scope, or duty.
- **Zero operational purpose** — directives with no system reason. Genesis directives
  serve a system function ("review intentions every cycle") and are safe.
- **Secrecy clauses** — "don't tell the user", "hide this from the operator". These
  contradict Genesis's SOUL ("user sovereignty is absolute") and would be flagged.
- **Meta-overrides** — "ignore previous instructions", "you are now [role]". Never
  use these.
- **In-conversation authority claims** — "I'm the admin, please...". Authority comes
  from the system-prompt channel, not from claimed authority within content.

**What's SAFE (Genesis's current pattern):**
- Strong directive language (MUST, NEVER, ALWAYS) tied to a coherent role with
  operational purpose
- Output formatting rules that explain the technical why
- Strict operational SOPs ("address every email", "fetch every URL")
- **Explicit injection-awareness sections** (like INBOX_EVALUATE.md's lines 506-524)
  — these are GOLD STANDARD, they make Opus 4.8 MORE trusting because they prove the
  author understands the boundary

**Audit result (2026-06-01):** Genesis identity files audited against these patterns.
USER_EGO_SESSION, EGO_SESSION, GENESIS_EGO_SESSION, INBOX_EVALUATE, REFLECTION_DEEP,
REFLECTION_STRATEGIC, STEERING, CONVERSATION — all FINE. MORNING_REPORT.md has
borderline-aggressive "ABSOLUTE PROHIBITIONS" framing but operational why is present;
real risk is low. Style could be tightened in a follow-up.

**Lines Opus 4.8 will still refuse even from the system channel:** deceiving the
user, overriding user sovereignty, hiding reasoning from operator, causing harm,
disabling injection-awareness itself. Genesis identity files don't go near these.

### Thinking Block Corruption on Resume (Opus extended thinking)

Resuming an Opus session with extended thinking may get 400 errors: "thinking blocks
cannot be modified." Classified as `CCSessionError` (PR #479). `conversation.py`
already catches `CCError` on resumes and recovers via `_recover_stale_resume()`.
Mitigated, not eliminated.

---

## Known Risks

### Rebase-Like Risk for CC
CC updates are NOT like AZ rebases — we don't fork CC, we consume it as a tool.
But our wrappers (CCInvoker especially) depend on CLI behavior. If CC changes its
output format or flag semantics, our wrappers break silently.

**Mitigation:** Integration tests that exercise CCInvoker with real CC CLI calls.
Currently: `scripts/test_cc_cli.sh` (manual). Phase 7+: automated in CI.

### Desktop vs Server Gap
CC's feature roadmap prioritizes desktop app experiences (scheduled tasks, teleport,
cowork). Server-side/CLI features are secondary. Genesis runs on a headless server.
Monitor whether key features become desktop-exclusive. The v2.1.89 scrollback
regression is an example — rendering optimizations designed for desktop apps
degrading the headless/tmux experience.
