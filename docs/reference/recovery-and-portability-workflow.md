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

## Release-Staging Validation

1. Run `./scripts/prepare-public-release.sh`.
2. Confirm the secret scan result.
3. Confirm the portability scan result.
4. Validate the staged tree on a fresh environment instead of relying on the
   private development VM.

## Current Recovery Anchors

- Genesis repo state is preserved by git plus captured diffs/untracked-file
  lists from `capture_recovery_state.sh`.
- Agent Zero compatibility must be evaluated against the exact local fork state,
  not a generic upstream release assumption.
