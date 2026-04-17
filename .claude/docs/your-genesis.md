# Your Genesis

Your Genesis install is one operational system. Under the hood, it's
organized as:

## The public repo — `GENesis-AGI`

Where Genesis development happens. Everyone clones this. PRs go here.
Bugs get filed as issues here. This is the single source of truth for
the codebase.

## Your fork (private) — automatic

When you first contribute a bug fix via `genesis contribute <sha>`,
Genesis auto-creates a private fork in your GitHub namespace (via
`gh api POST /repos/<owner>/<repo>/forks`). Subsequent contributions push
sanitized commits to that fork and open PRs against the public repo. You
don't manage this directly — the contribution pipeline does.

If you want to keep personal customizations (tweaks to your `CLAUDE.md`,
custom skills, research profiles), commit them to a branch in your fork.
Never push custom commits to the public repo's `main`.

## Your backup (private) — `<your-gh-user>/genesis-backups`

Operational backup of everything your Genesis has accumulated: SQLite DB,
Qdrant snapshots, memory files, CC transcripts, local config overlays,
and GPG-encrypted secrets. Written every 6 hours by `scripts/backup.sh`.

**All PII-bearing payloads are encrypted** with `GENESIS_BACKUP_PASSPHRASE`
before push: SQLite dump, memory files, transcripts, and secrets. The
private repo is defense in depth; encryption is the primary defense.
(Qdrant snapshots are opaque binary blobs and ship unencrypted; encrypting
them is tracked as a follow-up.)

## Recovery

Disk died? You get everything back in two steps:

1. `git clone <your-fork-url> ~/genesis` — code + customizations
2. `scripts/bootstrap.sh && scripts/restore.sh` — services + data
   (you'll be prompted for `GENESIS_BACKUP_PASSPHRASE`)

After those two steps, you're running the same Genesis you were before
disk failure.

## What goes where — quick reference

| What | Where | Why |
|------|-------|-----|
| Your customizations to code | Your fork, as git commits | Versioned, diffable, contributable upstream when desired |
| Your memory / DB / secrets | Your backup repo, encrypted | Changes constantly; separating keeps code history clean |
| Nothing | The public repo, from you directly | Public repo is shared; contribute via fork + PR |

## The contribution flow (decentralized bug fixing)

1. You're using Genesis, hit a bug in Genesis code.
2. Ask Genesis to fix it → it commits to your working copy.
3. Genesis offers: "Contribute upstream?"
4. Say yes → `genesis contribute <sha>` runs:
   - Sanitizer strips personal paths, secrets, PII from the diff
   - Auto-forks if missing
   - Pushes sanitized commit to your fork
   - Opens a PR from your fork → public repo
   - Maintainer reviews on GitHub like any other PR

This flow is for **bug fixes on existing code**, not new features. It's
designed to decentralize bug fixing so non-expert users can still
contribute the fixes their install discovers. For features or larger
changes, use the standard open-source flow (see `CONTRIBUTING.md`).

## Commands

| Command | What it does |
|---------|---|
| `scripts/backup.sh` | Encrypt and push all state to your backup repo (cron: every 6h) |
| `scripts/restore.sh` | Restore all state from your backup repo |
| `scripts/restore.sh --dry-run` | Report what would be restored without touching the filesystem |
| `python -m genesis restore` | Python CLI wrapper around `restore.sh` |
| `genesis contribute <sha>` | Open an upstream PR for a committed bug fix |
