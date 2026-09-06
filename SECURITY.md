# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Genesis, please report it
responsibly:

1. **DO NOT** open a public GitHub issue.
2. Use [GitHub Security Advisories](../../security/advisories) to create a
   private report, or contact the project maintainers directly.
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We aim to acknowledge security reports within 48 hours and provide a
substantive response within one week.

## Supported Versions

| Version | Supported |
|---------|-----------|
| v3.x    | Yes       |
| < v3    | No        |

Genesis v3 is a ground-up rebuild. Earlier versions (v1/v2) are not maintained
and should not be used.

---

## Security Architecture

Genesis is an autonomous agent system. The same principle that governs its
autonomy also governs its security: **trust is earned through verified
behavior, not assumed.**

An autonomous system that can act on your behalf must also be a system you
can trust with increasing responsibility over time. The security model is
not a lockdown bolted onto an agent -- it is the autonomy model itself. Every
layer described below exists because Genesis takes seriously the question:
"what has this system demonstrated it can be trusted to do?"

### Earned Autonomy as Security Model

Genesis implements a graduated autonomy framework. The **shipped** ladder is
four levels, `L1`-`L4` (`genesis.autonomy.types.AutonomyLevel`); the design
document describes an eventual `L5`-`L7`, which is **not built**. Higher levels
are unlocked through demonstrated competence, verified by the system and
approved by the operator.

Each autonomy level gates specific capabilities:

- **L1**: Simple tool use — fully autonomous.
- **L2**: Known-pattern tasks — mostly autonomous.
- **L3**: Novel tasks — propose and execute with a checkpoint.
- **L4**: Proactive outreach — threshold-gated and governed.

Autonomy permissions are stored per-category and can be revoked instantly.
There are **four** categories (`genesis.autonomy.types.AutonomyCategory`):
`direct_session`, `background_cognitive`, `sub_agent`, `outreach`. The operator
always has final authority. See
`docs/architecture/genesis-v3-autonomous-behavior-design.md` for the full
framework, and read it as design intent rather than as a description of what
currently ships.

**The autonomous-CLI approval gate is the hard boundary underneath all of it.**
Every autonomous background Claude Code session must be rooted in an explicit
operator approval: `manual_approval_required` (`autonomy/cli_policy.py`)
defaults to `True`, and `AutonomousCliApprovalGate` refuses to dispatch without
one. No autonomy level unlocks past it, and it is not a tunable — a fork that
defaults it off has removed the guarantee the rest of this section describes.

### Container Isolation

Genesis is designed to run in an isolated environment (container or VM). This
is not optional hardening -- it is the assumed deployment model. The container
boundary limits blast radius: even if an autonomous action goes wrong, the
damage is contained to the Genesis environment.

Recommendations:
- Run Genesis in a dedicated container or VM, not on a shared workstation.
- Use a non-root user account.
- Restrict network egress to required endpoints (LLM APIs, Qdrant, Ollama).

### Network Exposure & Management Ports

Genesis exposes a local dashboard over HTTP (and, optionally, a remote-desktop
/ noVNC console). So it can be reached through a reverse proxy or a private
overlay network, the bundled service unit binds the dashboard to all interfaces
(`0.0.0.0`) by default rather than to loopback. When a dashboard password is
set, **state-changing** `/api` requests are gated: `check_api_mutation_auth`
(`dashboard/auth.py`) runs as an app-level `before_request` and requires either
the internal bearer token or an authenticated same-origin cookie, with CSRF
checked from `Sec-Fetch-Site`/`Origin`/`Referer` and failing closed. `/v1`
enforces its own separate bearer.

Read the limits of that gate carefully, because they decide whether network
isolation is still load-bearing for you — it is:

- It covers **mutations only**. `GET`/`HEAD`/`OPTIONS` stay open by design, so
  health probes and dashboard polling keep working. Anything readable through
  the API is readable by anyone who can reach the port.
- It is **inert when no dashboard password is set**, which is the default.
- It exempts the `/api/genesis/auth/*` login endpoints, and it can be disabled
  outright with `GENESIS_DASHBOARD_API_AUTH=off`.
- The built-in web terminal and the noVNC console are **not** covered by it.

This is safe **only under the assumed deployment model: the host is not
publicly exposed.** The dashboard is meant to be reachable through one of:

- a **private overlay network** (e.g., Tailscale / WireGuard), where only your
  own devices can reach the port; and/or
- a **host-side reverse proxy** that forwards to the container.

The threat model is **public exposure — not your LAN or private overlay.** If
you run Genesis on a host reachable from the public internet, you must restrict
the management ports yourself; Genesis does not assume an authenticating
gateway in front of them.

**Operator checklist:**
- Do **not** port-forward the dashboard or console ports from a public router.
- Bind or firewall the management ports to your private/overlay network — e.g.
  restrict the dashboard port to your overlay's address range (Tailscale uses
  the `100.64.0.0/10` CGNAT range) with `nftables`/`ufw`, or change the service
  unit to bind a specific private interface instead of `0.0.0.0`.
- Treat the built-in web terminal and the noVNC console as **unauthenticated
  administrative access**: anyone who can reach those ports can drive Genesis.
  For the dashboard API, assume the same for reads and for any install with no
  dashboard password set. Network isolation remains the primary control; the
  mutation gate above is a second layer, not a replacement for it.

Security audits should verify this network restriction (firewall / overlay)
rather than re-flagging the `0.0.0.0` bind, which is intentional for the
proxy/overlay deployment model.

### Tool-Level Guards (PreToolUse Hooks)

Genesis uses PreToolUse hooks to enforce tool-level security policies at
runtime. These hooks fire on every tool invocation, including autonomous
sessions, and cannot be bypassed by the agent.

Examples of enforced policies:
- Blocking shell commands that match dangerous patterns (e.g. `rm -rf /`)
- Blocking destructive git operations, pushes and merges that have not met the
  repository's review gates, and writes to protected paths
- Blocking web fetches to known-problematic URLs
- Blocking editable installs pointed at a worktree, which would redirect
  system-wide imports

Hooks are configured in `.claude/settings.json`, and **which hook fires is
decided by the tool matcher** -- they are not one program:

- `Bash` is matched by a family of dedicated guards under `scripts/hooks/`
  (`destructive_command_guard.py`, `git_discard_guard.py`,
  `protected_paths_guard.py`, `git_push_guard.py`, `worktree_cwd_guard.py`,
  and others) plus a small inline matcher.
- `WebFetch`/`WebSearch` is matched by `scripts/hooks/web_tools_gate.py`.
- `Write`/`Edit` is matched by `scripts/behavioral_linter.py`, which lints file
  **content** against `config/behavioral_rules/*.yaml`. It never sees a shell
  command or a URL.

They are the inner guardrail -- the last line of defense when autonomy
permissions have already been granted.

Not every safety mechanism is a hook, and the distinction is a real difference
in guarantee. Process-group kill validation, for example, lives in
`genesis.util.proc_kill` and hardens **Genesis's own** subprocess management at
runtime; no hook inspects agent-authored code for it. A hook cannot be bypassed
by the agent; a runtime helper only protects the call sites that use it.

### Secrets Management

Genesis uses an environment-file approach for secrets:

- All API keys and tokens live in `secrets.env` at the project root.
- This file is gitignored and should be set to mode `0600` (owner read/write
  only).
- The `genesis.env` module (`src/genesis/env.py`) resolves the secrets path
  at runtime, with support for `SECRETS_PATH` environment variable override.
- A `detect-secrets` scan and a blocking `gitleaks` scan (`.gitleaks.toml`
  rules: API keys plus install-specific IP/hostname patterns) run in CI on
  every PR to verify no secrets leak into the public repo.
- Scripts read `secrets.env` as data (`scripts/lib/load_secrets.sh` or the
  Python dotenv reader) — never `source` it; a sourced value containing
  `$(...)` would execute.

**Rules:**
- Never commit API keys, tokens, or credentials to version control.
- Never hardcode secrets in source files.
- Rotate keys regularly. Use separate keys for development and production.

### Data Protection (Qdrant Delete Guard)

Genesis stores episodic memory and knowledge in Qdrant vector collections.
A delete guard in the collections module prevents accidental bulk deletion
of production data. This was implemented after a real incident where test
execution deleted production memory.

The guard:
- Blocks collection-level delete operations unless explicitly overridden.
- Ensures test fixtures use isolated collections that do not collide with
  production data.

### Untrusted Content & Write-Path Isolation

Genesis ingests content it did not author -- fetched pages, mail, documents,
third-party messages. The risk is not that this content is stored; it is that
stored content is later *auto-consumed* into privileged state, where it becomes
instruction rather than data.

**Provenance stamping.** Every stored memory and observation carries an
`origin_class` -- `owner`, `first_party`, or an external/untrusted class.

**Two privileged-write paths are gated on it**, fail-closed on `NULL` or an
unrecognised value (`genesis.security.immunity.is_trusted_for_privileged_write`):

- the user model, which will only fold in deltas from trusted origins
  (`memory/user_model.py`), and
- the autonomy dispatcher, which will not pick up a `task_detected` observation
  from an untrusted origin (`autonomy/dispatcher.py`).

**Scope this claim precisely: those two paths are gated, not "all untrusted
content is isolated."** External-origin content can still reach a model's
context through ordinary recall and summarisation surfaces. What the gate
prevents is untrusted content *auto-promoting itself* into the user model or
into an autonomous dispatch without an operator in the loop.

**Irreversible memory operations require approval.** Entity merges delete
mentions and links and cannot be un-merged, so they are no longer applied on
staleness alone: the applier consumes only approved proposals
(`memory/entity_adjudication.py`), and a pre-delete snapshot is journaled so an
applied merge can be reconstructed.

**Session identifiers are validated before use as path components.** Session ids
arrive from outside the process and several hooks interpolate them into
filesystem paths. A single shared validator (`is_safe_session_id` in
`scripts/hooks/hook_input.py`) rejects traversal shapes, separators, null bytes,
the empty string, and over-long values, and is used in place of the hand-copied
per-hook checks that previously disagreed with each other.

### External Egress

Delivery **to the operator** -- Telegram, voice, mail addressed to you -- is
never gated. You are the recipient; gating it would only obstruct you.

Autonomous egress to the **outside world** is a different matter, and the honest
current state is mixed:

- **Enforcing:** mail sending passes a real gate that can hold a send
  (`autonomy/email_gate.py`).
- **Observe-only:** the capability gate at the Discord and GitHub-issue doors
  (`autonomy/shadow_gate.py`) records what it *would* decide and does **not**
  hold anything. Its own contract says so. Treat it as instrumentation ahead of
  an enforcement stage, not as a control that is protecting you today.
- **Build-time backstop:** `scripts/check_external_io.py` runs in CI and fails
  the build when a new hardcoded external endpoint appears outside an allowlist.
  It reasons about literal endpoints in source, so it cannot see egress routed
  through a browser session or a third-party integration layer.

If you are deciding whether to trust this system with an outbound channel, the
first two bullets are the ones that matter: one channel enforces, the rest are
watched.


### Dependency Security

Python dependencies are declared in `pyproject.toml` (the sole dependency
source — there is no `requirements.txt`). Known-CVE scanning runs automatically:

- **CI** — the `dependency-audit` job in `.github/workflows/ci.yml` runs
  `pip-audit` against the resolved runtime tree on every PR/push and weekly,
  failing on any new (untriaged) advisory. Already-triaged, not-reachable
  advisories are listed with rationale in that job.
- **Dependabot** — GitHub's dependency-graph security alerts are enabled for
  the repo, and `.github/dependabot.yml` keeps the GitHub Actions current.

To scan locally:

```bash
pip install pip-audit && pip-audit
```

### Incident Response

If you suspect a security issue:

1. Revoke any compromised API keys immediately.
2. Review logs for unauthorized actions or unexpected tool calls.
3. Check for unexpected file modifications in the Genesis directory.
4. Rotate all credentials.
5. Report the incident to project maintainers.

## License

See LICENSE file for details.
