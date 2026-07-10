# Recovery And Portability Workflow

This document is the implementation-side workflow for portability/publicization
work. It is intentionally narrow and does not replace broader architecture
documentation.

## Before Changing Runtime Code

1. Take a VM snapshot or equivalent host-level backup.
2. Run `./scripts/capture_recovery_state.sh`.
3. Record the Genesis and Agent Zero commits being treated as the recovery
   baseline.
4. Confirm whether the Agent Zero tree is dirty before assuming any upstream
   version is compatible.

## During Migration Work

1. Keep Genesis changes on a dedicated hardening branch.
2. Keep Agent Zero changes on a separate branch if AZ edits are required.
3. Prefer parameterization over removal for path and service assumptions.
4. Use `GENESIS_ENABLE_OLLAMA=false` when validating the cloud-first path.
5. Run `./scripts/check_portability.sh` before claiming the branch is portable.

## Release Validation

The public repo (`GENesis-AGI`) is the primary repo, so there is no separate
"strip and stage" step. Leak protection is enforced on every PR by the
`leak-detector` job in `.github/workflows/ci.yml` (detect-secrets + portability
+ email scans). Releases are cut by tagging `vX.Y` on `main` and publishing a
GitHub Release from the matching `CHANGELOG.md` section.

## Current Recovery Anchors

- Genesis repo state is preserved by git plus captured diffs/untracked-file
  lists from `capture_recovery_state.sh`.
- Agent Zero compatibility must be evaluated against the exact local fork state,
  not a generic upstream release assumption.

## Container-Loss Recovery — Credentials (host-side mirror)

If the whole container is destroyed, its live credentials **and** the Tier-1
backup clone (which lives inside the container filesystem) are gone with it, and
the off-site Tier-2 copy needs the very git/GitHub credentials it would restore.
To survive this, the container mirrors its **already-encrypted** credential
bundle to the guardian shared mount every awareness cycle, and the guardian
keeps a second, host-only copy the container can never reach:

| Layer | Location | Survives container loss? |
|---|---|---|
| Live creds | container `~` | No |
| Tier-1 clone | container `~/backups/genesis-backups/` | No |
| **Shared-mount mirror** | host `…/genesis-guardian/shared/guardian/creds-mirror/` | Yes |
| **Guardian archive** | host `…/genesis-guardian/creds-archive/` | Yes (container cannot write it) |
| Passphrase escrow | host `…/shared/guardian/backup_passphrase.env` (+ archive copy) | Yes |
| Tier-2 off-site | remote backend | Yes (but needs network + creds) |

The mirror/archive carry only GPG-encrypted `*.gpg` files; the decryption
passphrase is escrowed separately. The guardian **refuses to overwrite the
archive from an empty or incomplete mirror**, so a container-side zeroing event
can never propagate into the last-line copy.

**Rebuild runbook (no network required):**

1. Create a fresh container and re-attach the `guardian-shared` incus disk device
   (host source `…/genesis-guardian/shared`), so the mirror + escrow are visible
   at `~/.genesis/shared/guardian/`.
2. Run `scripts/restore.sh`. When the Tier-1 clone payload is absent it
   automatically falls back to the mirror (or, for a host-side run, the
   `creds-archive/`), and reads the escrowed passphrase — no env var needed.
   Override the source explicitly with `GENESIS_CREDS_MIRROR=<dir>` if desired.
3. Credentials are **staged** to `~/.genesis/restore-creds/` (0700), never
   auto-placed. Move them into position: `ssh/*` → `~/.ssh/`, `gh_hosts.yml` →
   `~/.config/gh/hosts.yml`, `guardian_remote.yaml`/`genesis.yaml` →
   `~/.genesis/`, `secrets.env` → `~/genesis/secrets.env`.
4. The guardian control-plane key (`genesis_guardian_ed25519`) is in the bundle,
   so the container↔host gateway is restored in the same step.

The guardian also emits a WARNING if the mirror goes stale (its newest
credential older than `cred_integrity.mirror_stale_hours`, default 48h) — an
early signal that backups have stopped landing and a container loss would not be
recoverable from the host.
