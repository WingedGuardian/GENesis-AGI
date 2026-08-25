# Shared-Artifact Consumer Registry

Some artifacts — credential files, shared config — are read by several modules,
and a doc (usually the writer) describes *who* consumes them. When a new consumer
is added and that doc is not updated, the doc silently **lies**, and a session
that grounds on it can state a false fact about the system.

This registry is the deterministic backstop, enforced in CI by
`scripts/check_shared_artifact_consumers.py`. Each `yaml shared-artifact` fenced
block declares an artifact and its consumers; the guard fails (exit 1) when the
declared `readers` diverge from the code that actually references the artifact
under `src/` and `scripts/`. Fields (`artifact`, `documented_in`, `match_literals`,
`readers`, `allowlist`) and their semantics are documented in that script's module
docstring — read it before adding an entry.

> **Editing note:** the guard scans this file with a raw regex, so it treats every
> `yaml shared-artifact` fenced block as a live entry. Keep this file to real
> entries only — put format examples in the guard script, never here.

`match_literals` carries **two kinds** of signal so both access patterns are seen:
the **filename** literal (direct file access) and the canonical **loader symbol**
(transitive access through the accessor function). Both are plain substrings, so no
LSP or code-graph is needed in CI. Prose is not a consumer and tests reference
artifacts as fixtures, so only `src/` and `scripts/` are scanned. This guards
enrolled artifacts by their consumer set — it is not a general prose↔code checker.

## Registered artifacts

### `cc_oauth_token.env` — fallback CC OAuth setup-token

A `claude setup-token` credential (`~/.genesis/cc_oauth_token.env`, synced to the
host shared mount) injected as `CLAUDE_CODE_OAUTH_TOKEN` **only** when a primary
`claude login` is confirmed dead. Written by `scripts/store_cc_token.sh`; accessed
directly by filename *and* transitively via `credential_bridge.load_cc_oauth_token()`.

```yaml shared-artifact
artifact: cc_oauth_token.env
documented_in: scripts/store_cc_token.sh
match_literals: [cc_oauth_token.env, load_cc_oauth_token]
readers:
  - src/genesis/cc/login_health.py
  - src/genesis/guardian/diagnosis.py
  - src/genesis/onboarding/floor.py
  - scripts/guardian-gateway.sh
  - scripts/cc-slot.sh
allowlist:
  - src/genesis/guardian/credential_bridge.py
```

- `cc/login_health.py` — injects it into the container's own CC sessions
  (foreground + background) when the container login is hard-expired.
- `guardian/diagnosis.py` — injects it into the host Guardian recovery brain when
  the host's own login is dead (transitive, via the loader).
- `onboarding/floor.py` — reads it as an auth-present signal at bootstrap.
- `scripts/guardian-gateway.sh` — references the synced host copy.
- `scripts/cc-slot.sh` — on interactive slot CREATE, when the primary login is
  dead (decision via `genesis.cc.login_gate`, which reuses `login_health`),
  extracts the token INSIDE the pane shell and exports `CLAUDE_CODE_OAUTH_TOKEN`
  so the session survives without a re-login prompt (login-dead-conditional;
  lever `GENESIS_CC_SLOT_OAUTH`).
- `guardian/credential_bridge.py` (allowlist) — the loader/propagator home
  (`load_cc_oauth_token`, `_CC_TOKEN_SOURCE`), not a business consumer.
