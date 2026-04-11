# Release Script Guarantees

This document describes what `scripts/prepare-public-release.sh` guarantees
about the staged output it produces. The public distribution pipeline
(`.github/workflows/public-release.yaml` → `scripts/push-public-release.sh`)
relies on these guarantees; any change to the prepare script that weakens a
guarantee is a privacy regression.

The script is invoked two ways:

1. **Manual**: `./scripts/prepare-public-release.sh [output_dir]` on a dev
   machine, output defaults to `~/tmp/genesis-public-release/`.
2. **CI**: `.github/workflows/public-release.yaml` fires on version tag push
   (`v*`), runs the prepare script, and uploads the staging output as an
   artifact. Manual merge to GENesis-AGI follows.

Neither path auto-pushes to GENesis-AGI. Pushes require a human running
`scripts/push-public-release.sh`.

---

## Invariants

The staging output MUST satisfy these invariants. The script fails hard
(non-zero exit) if any is violated.

### 1. No user voice data

- `src/genesis/skills/voice-master/references/exemplars/` contains
  **only** `README.md`. No `social.md`, `professional.md`, `longform.md`,
  or `index.md` — those files live in the out-of-repo overlay at
  `~/.claude/skills/voice-master/exemplars/`.
- `src/genesis/skills/voice-master/references/voice-dimensions.md` does
  **not exist**. The in-repo fallback is
  `voice-dimensions-TEMPLATE.md` (generic, no user data).
- `src/genesis/skills/voice-master/references/voice-dimensions-TEMPLATE.md`
  **does exist** (non-template public releases would have no voice profile
  to fall back to).

**Enforced by**: `[3/9] Voice-master sanity check` in the prepare script
and `Verify voice-master structure` in the GH Actions workflow.

### 2. No user identity files populated with real content

- `src/genesis/identity/USER.md` is replaced with a generic template.
- `src/genesis/identity/USER_KNOWLEDGE.md` is replaced with an empty
  structure.
- Other `src/genesis/identity/*.md` files (SOUL, STEERING, REFLECTION_*,
  TASK_*, etc.) are **generic system prompts** and are shipped as-is. If
  any of them is ever modified to contain user-specific content, add it
  to the templating block in `[2/9]` or `[4/9]` of the prepare script.

**Enforced by**: the `cat >` templating in `[5/9]` of the prepare script.

### 3. No LinkedIn expertise / audience content

- `src/genesis/skills/linkedin-post-writer/SKILL.md` has its
  `## Topic Areas` and `## Audience` sections replaced with comment-only
  templates on every release.
- Other LinkedIn skills reference voice-master for voice loading and do
  not contain their own user-calibrated sections — if that changes,
  extend the templating block.

**Enforced by**: the sed-based templating in `[4/9]` of the prepare script.

### 4. No hardcoded machine identifiers

The following patterns are replaced with environment variables or generic
placeholders throughout `src/`, `config/`, `scripts/`, `docs/`, root files:

| Pattern | Replaced with |
|---|---|
| `${OLLAMA_HOST:-localhost}` | `${OLLAMA_URL:-http://localhost:11434}` or `${OLLAMA_HOST:-localhost}` |
| `${LM_STUDIO_HOST:-localhost}` | `${LM_STUDIO_HOST:-localhost:1234}` or `${LM_STUDIO_HOST:-localhost}` |
| `${VM_HOST:-localhost}` | `${VM_HOST:-localhost}` |
| `192.168.50.x` (any other) | `${LOCAL_HOST:-localhost}` |
| `${CONTAINER_IP:-localhost}` | `${CONTAINER_IP:-localhost}` |
| `${CONTAINER_IPV6:-not configured}` | `${CONTAINER_IPV6:-not configured}` |
| `${HOST_IPV6:-not configured}` | `${HOST_IPV6:-not configured}` |
| `${HOME}/` (except in install scripts) | `${HOME}/` |
| `America/New_York` | `UTC` |
| `5070ti` | `local GPU host` |
| `YOUR_GITHUB_USER/Genesis` / `YOUR_GITHUB_USER/genesis-backups` | `YOUR_GITHUB_USER/...` |

**Enforced by**: the global replacement loops in `[5b/9]` and `[5c/9]`,
and the portability scan in `[8/9]`.

### 5. No high-risk docs

These directories are removed entirely from the staging output:

- `docs/history/` (V1/nanobot project history — contains old IPs and
  hostnames)
- `docs/superpowers/` (internal product-planning specs)
- `docs/gtm/` (GTM strategy — internal marketing playbook)
- `config/research-profiles/` (user's research interests)
- `config/modules/career-agent.yaml` (user-specific)
- `config/external-modules/` (user-specific module configs)

These specific files are removed:

- `docs/plans/2026-03-05-track*.md` (user's business ideas)
- `docs/plans/2026-03-05-multi-track-*.md`
- `docs/plans/2026-03-30-career-agent-improvements.md`
- `docs/reference/2026-03-19-genesis-codebase-audit.md`
- `docs/reference/2026-03-20-article-eval-action-items.md`
- `docs/reference/CODEBASE_AUDIT_REPORT.md`
- `docs/reference/codebase-audit-report.md`
- `docs/reference/2026-03-24-split-large-files-audit.md`
- `docs/reference/networking-summary.txt`
- `docs/reference/review-summary.md`
- `docs/reference/project-outline.txt`
- `scripts/spike_*.py`
- `scripts/cc_cli_output/`
- `genesis.db`, `data/genesis.db`

**Enforced by**: the `rm -f` / `rm -rf` calls in `[2/9]` of the prepare
script.

### 6. No secrets

`detect-secrets` scans the staging output. Any finding is a failure.
CACHEDIR.TAG false positives are filtered.

**Enforced by**: `[7/9]` in the prepare script and the `Run secret scan`
step in the GH Actions workflow.

### 7. No fingerprint matches

A fingerprint scan runs against the staging output looking for:

1. Patterns from `${GENESIS_RELEASE_FINGERPRINTS:-$HOME/.genesis/release-fingerprints.txt}`
   (user-defined, one pattern per line, outside the repo).
2. Personal-email-domain regex (`@gmail.com`, `@yahoo.com`, `@hotmail.com`,
   `@outlook.com`, `@icloud.com`, `@protonmail.com`, `@proton.com`,
   `@aol.com`) with an allowlist for known-safe addresses (`noreply@`,
   `backup@genesis.local`, `feedback@anthropic.com`, `pr-bot@`,
   `@example.com`, `@example.org`).

Any match fails the release.

**Enforced by**: `[8b/9]` in the prepare script.

---

## Fingerprint File Setup

The fingerprint file lives outside the repo on purpose — the whole point
is to scan *for* these strings, so they can't live in the tree being scanned.

Default location: `~/.genesis/release-fingerprints.txt`. Override with the
`GENESIS_RELEASE_FINGERPRINTS` environment variable.

Format: one pattern per line, ripgrep regex syntax. Blank lines and lines
starting with `#` are ignored.

Example structure (do not put actual persona names in this document — put
them only in your local fingerprint file):

```
# Active persona handles (one per line, ripgrep regex syntax)
<persona-handle-1>
<persona-handle-2>
<forum-domain-the-persona-posts-on>

# Personal email addresses the generic allowlist doesn't cover
<local-part-of-personal-email>@
```

Real fingerprint patterns live only in the fingerprint file itself, which
is outside the repo. Documenting example patterns in-repo would defeat the
purpose of the scan by putting the strings it checks for into the scanned
tree.

The `~/.genesis/` directory is NOT included in `scripts/backup.sh`'s backup
scope (which only backs up `data/genesis.db`, Qdrant collections, CC
transcripts, and the auto-memory dir). The fingerprint file therefore
stays local to the developer machine.

---

## Adding New Invariants

When you add a new category of user-specific content to the repo:

1. Update the relevant templating / removal step in `prepare-public-release.sh`.
2. Add a corresponding invariant to this document.
3. If the check is structural (path existence, file contents), mirror it
   in `.github/workflows/public-release.yaml` as a defense-in-depth layer.
4. If the check is pattern-based (regex match), add the pattern to either
   the portability scan (for machine-specific identifiers) or the
   fingerprint scan (for user-specific strings).
5. Add the pattern to your `~/.genesis/release-fingerprints.txt` if it's
   a persistent user-specific string (persona names, personal handles,
   emails).

## Testing the Guarantees

Before shipping any change to `prepare-public-release.sh`:

1. Run the script against the current HEAD: `./scripts/prepare-public-release.sh /tmp/test-release`.
2. Verify the script exits 0.
3. Grep the output for generic leakage patterns (adjust `<user-name>` to
   the actual user's first name before running):
   ```
   rg -n '\b<user-name>\b|@gmail\.com|America/New_York|10\.176|YOUR_GITHUB_USER/Genesis' /tmp/test-release
   ```
4. Inject a known fingerprint into a scratch doc and verify the script
   exits non-zero. Use a pattern that is actually in your local
   fingerprint file (do not hardcode example persona names here):
   ```
   echo '<one of your fingerprint patterns> was here' \
     >> /tmp/test-release/docs/reference/scratch.md
   # Re-run the script's fingerprint scan
   ```
5. Verify no regression in existing invariants by comparing file counts
   and known-stripped paths against the previous release.

## Non-Guarantees

The script does NOT:

- Rewrite git history. Previous commits in the private repo may still
  contain user data — the script only affects the output that will be
  pushed to GENesis-AGI.
- Scrub CC session transcripts or backup data. Those are user-private
  files backed up to the private `YOUR_GITHUB_USER/genesis-backups` repo,
  not to the public GENesis-AGI.
- Run offline. It requires `rg`, `detect-secrets`, `git`, and `python3`.
- Validate that the staged output actually builds or runs. Only that it
  doesn't leak.
