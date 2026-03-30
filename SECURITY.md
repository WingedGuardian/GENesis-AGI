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

Genesis implements a graduated autonomy framework with seven levels (L0-L6).
New installations start at L0 (no autonomous action). Higher levels are
unlocked through demonstrated competence, verified by the system and approved
by the operator.

Each autonomy level gates specific capabilities:

- **L0-L1**: Observe and notify only. No autonomous actions.
- **L2**: Execute pre-approved, reversible actions.
- **L3**: Handle routine decisions within established patterns.
- **L4+**: Broader autonomous operation, unlocked per-category with explicit
  operator approval.

Autonomy permissions are stored per-category (six categories) and can be
revoked instantly. The operator always has final authority. See
`docs/architecture/genesis-v3-autonomous-behavior-design.md` for the full
framework.

### Container Isolation

Genesis is designed to run in an isolated environment (container or VM). This
is not optional hardening -- it is the assumed deployment model. The container
boundary limits blast radius: even if an autonomous action goes wrong, the
damage is contained to the Genesis environment.

Recommendations:
- Run Genesis in a dedicated container or VM, not on a shared workstation.
- Use a non-root user account.
- Restrict network egress to required endpoints (LLM APIs, Qdrant, Ollama).

### Tool-Level Guards (PreToolUse Hooks)

Genesis uses PreToolUse hooks to enforce tool-level security policies at
runtime. These hooks fire on every tool invocation, including autonomous
sessions, and cannot be bypassed by the agent.

Examples of enforced policies:
- Blocking shell commands that match dangerous patterns (e.g., `rm -rf /`,
  `os.killpg` with unvalidated PGID)
- Blocking web fetches to known-problematic URLs
- Preventing `pip install -e` to worktree paths (prevents system-wide
  redirection of imports)

Hooks are configured in `.claude/settings.json` and enforced by
`scripts/behavioral_linter.py`. They are the inner guardrail -- the last
line of defense when autonomy permissions have already been granted.

### Secrets Management

Genesis uses an environment-file approach for secrets:

- All API keys and tokens live in `secrets.env` at the project root.
- This file is gitignored and should be set to mode `0600` (owner read/write
  only).
- The `genesis.env` module (`src/genesis/env.py`) resolves the secrets path
  at runtime, with support for `SECRETS_PATH` environment variable override.
- A `detect-secrets` scan runs as part of the public release process to verify
  no secrets leak into the distribution repo.

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

### Dependency Security

```bash
# Check Python dependencies for known vulnerabilities
pip-audit

# Review and update regularly
pip install --upgrade -r requirements.txt
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
