# Backup History Migration

## Why

Before PR #48 (merged 2026-04-16), `scripts/backup.sh` stored SQLite dumps,
CC transcripts, and auto-memory files as plaintext in the `genesis-backups`
git repo. Only `secrets.env` was encrypted.

Post-PR #48, all PII-bearing payloads are GPG-encrypted before push.
However, **git history retains the old plaintext commits** indefinitely.
The backup repo is private, but plaintext PII in git history is still a
liability — especially since the memory system stores credentials by design.

This migration removes those plaintext payloads from all history, leaving
encrypted (`.gpg`) versions and everything else intact.

## When to run

**Once**, after you've confirmed that a few encrypted backup cycles have
landed successfully (check `~/.genesis/backup_status.json` — look for
`secrets_encrypted: true` and non-zero `qdrant_collections`).

**Skip entirely** if your Genesis install was set up after PR #48 — your
backup history never contained plaintext payloads.

## Prerequisites

Install `git-filter-repo`:

```bash
# pip (works everywhere)
pip install git-filter-repo

# Ubuntu 22.04+
sudo apt install git-filter-repo

# macOS
brew install git-filter-repo
```

## Step-by-step

### 1. Update your backup repo

```bash
cd ~/backups/genesis-backups
git pull
```

### 2. Preview (dry-run)

```bash
~/genesis/scripts/migrate-backup-history.sh --dry-run
```

This shows what would be removed without making changes.

### 3. Run the migration

```bash
~/genesis/scripts/migrate-backup-history.sh --yes
```

The script:
- Removes `data/genesis.sql`, `data/genesis.db`, `transcripts/*.jsonl`,
  and `memory/**/*.{md,json}` from ALL commits in history
- Preserves all `.gpg` files, Qdrant snapshots, config overlays, and
  secrets
- Runs `git gc --aggressive --prune=now` to reclaim disk space
- Reports size before and after

### 4. Force-push

The script prints the exact commands but does NOT push automatically:

```bash
cd ~/backups/genesis-backups
git push --force --all origin
git push --force --tags origin
```

### 5. Verify

```bash
# Should return no results:
git log --all --oneline -- data/genesis.sql
git log --all --oneline -- 'transcripts/*.jsonl'

# Should still show encrypted history:
git log --all --oneline -- data/genesis.sql.gpg
```

## Risks

- **Force-push rewrites remote history.** Any other clone of the backup
  repo (e.g., on another machine) must re-clone. Since the backup repo is
  per-install, this is usually just one machine.

- **Commit SHAs change.** If you have external references to specific
  backup commits (unlikely), they become dangling.

- **Irreversible locally.** After the rewrite + gc, the old plaintext is
  gone from the local clone. The force-push removes it from the remote.
  If you're nervous, clone the backup repo to a scratch directory first
  and run there:
  ```bash
  git clone ~/backups/genesis-backups /tmp/migration-test
  ~/genesis/scripts/migrate-backup-history.sh --backup-dir /tmp/migration-test
  ```

## Manual fallback

If you prefer not to use the script:

```bash
cd ~/backups/genesis-backups

git filter-repo \
  --invert-paths \
  --path data/genesis.sql \
  --path data/genesis.db \
  --path-glob 'transcripts/*.jsonl' \
  --path-glob 'memory/*.md' \
  --path-glob 'memory/**/*.md' \
  --path-glob 'memory/**/*.json' \
  --force

git gc --aggressive --prune=now
git push --force --all origin
```
