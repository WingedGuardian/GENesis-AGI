# Config Sources — where a setting comes from, and what wins

A routing map for agents: which surface holds a setting, which process reads it,
who wins when two surfaces disagree, and when a change takes effect. It points at
code rather than copying it — when this page and the code disagree, the code is
right and this page is stale.

| Kind of setting | Lives in | Primary reader |
|---|---|---|
| API keys, tokens, env-style toggles | `secrets.env` (repo root) | process environment |
| Per-install identity (timezone, GitHub, local-inference URLs, a few flags) | `~/.genesis/config/genesis.yaml` | `genesis.env` helpers |
| Subsystem policy (levers, modes, knobs) | `config/<name>.yaml` + a `<name>.local.yaml` overlay | per-domain config modules |
| Model routing | `config/model_routing.yaml` + `config/model_routing.local.yaml` | `genesis.routing.config` |
| Paths, tuning, kill switches | process environment only | `genesis.env`, per-domain modules |
| Prompts and persona | `src/genesis/identity/*.md` | identity loader, CC prompt assembly |
| Claude Code harness | `.claude/settings.json`, `~/.claude/settings.json` | Claude Code itself |

---

## 1. `secrets.env`

Path: `genesis.env.secrets_path()` — `SECRETS_PATH` if set, else `<repo_root>/secrets.env`.
`repo_root()` is `GENESIS_REPO_ROOT` or the checkout the imported `genesis` package
lives in, never the CWD. Gitignored; seeded from `secrets.env.example`.

### Who loads it, and how

| Process | Mechanism | Inherited env vs file |
|---|---|---|
| `genesis-server` (systemd) | `EnvironmentFile=<repo>/secrets.env` in `scripts/systemd/genesis-server.service.template` **and** `load_dotenv(..., override=True)` in `src/genesis/runtime/init/secrets.py` during bootstrap (runs after the credential-integrity self-heal step, before DB init/migrations) | **file wins** |
| `agent-zero` unit | `EnvironmentFile=` in its template | file (systemd) |
| `genesis-bridge` (legacy fallback) | no `EnvironmentFile=`; runtime `load_dotenv(override=True)` | file wins |
| MCP children (`scripts/genesis_mcp_server.py`) | `main()` copies an **allowlist** (`_MCP_VARS`) and only for keys NOT already in the env; then the health/memory/outreach/recon lifespans call `create_standalone_router()`, whose builder runs `load_dotenv(override=True)` on the **whole** file (`src/genesis/routing/standalone.py`) | net effect: **file wins, whole file** (discord-bot: allowlist only) — EXCEPT values read at module import, before either load: the database path (`_DEFAULT_DB` via `genesis_db_path()`) is fixed then, so a `GENESIS_DB_PATH` that exists only in `secrets.env` never reaches the MCP servers |
| Dispatched CC sessions | `CCInvoker._build_env` (`src/genesis/cc/invoker.py`) copies the server's `os.environ` | inherit the server's snapshot |
| CC hooks (`proactive_memory_hook.py`, `genesis_session_context.py`, `genesis_urgent_alerts.py`) | `load_dotenv(<main checkout>/secrets.env)`, default `override=False`; path hardcoded, ignores `SECRETS_PATH` | **inherited env wins** |
| One-off scripts (`reindex_fts_to_qdrant`, `backfill_session_memories`, `migrate_reference_data`, `mine_references_from_history`, `eval` CLI) | `load_dotenv(override=True)` | file wins |
| `wing_backfill`, `wing_payload_resync`, `ambient_replay` | `load_dotenv(...)`, default | inherited env wins |
| Shell (`backup.sh`, `restore.sh`) | `scripts/lib/load_secrets.sh` — literal, never expanded or executed | exported |
| `python -m genesis.db.migrations --apply` (update path) | does **not** load it (see `db/migrations/0086_seed_timezone_config.py` docstring) | — |
| Host guardian | its own file: `GUARDIAN_SECRETS` (`src/genesis/guardian/config.py`) | separate |

### When a change takes effect

- `load_dotenv` and `EnvironmentFile=` are **boot-time snapshots into `os.environ`**.
  Editing the file changes no running process. Server: restart. MCP children: next CC
  session. Hooks: next hook invocation (fresh process).
- Dashboard **Provider Keys** editor (`PUT /api/genesis/secrets`,
  `src/genesis/dashboard/routes/secrets.py`) writes the file atomically **and** sets
  `os.environ` in the server process only; it returns `needs_restart: true`. Code that
  reads env per call in the server sees the new value; anything that captured it at
  construction does not. CC sessions the server dispatches AFTER the edit inherit it
  (`CCInvoker._build_env` copies the server's environment), and so can their MCP
  children; already-running processes, foreground sessions and their MCP servers keep
  their snapshot.

### The registry role of `secrets.env.example`

The dashboard parses `secrets.env.example` once at import (`_parse_example_file`):
`# ─── Group ───` → group, `# --- Label ---` → label, `# Used by:` → description,
`# Signup:` → a rendered `https://` link, `KEY=` → a key, `# KEY=` → an *optional
override* (clearable to unset; the editor comments the line out rather than writing
`KEY=`). The parsed set is the whitelist: `PUT` rejects any key not in it.
`USER_TIMEZONE` and `GENESIS_TIMEZONE` are deliberately hidden (`_HIDDEN_KEYS`). A
missing `secrets.env` is created from the example on first `PUT`. Contract tests:
`tests/test_dashboard/test_secrets_registry.py`, `tests/test_dashboard/test_timezone_settings.py`.
Consequence: **adding or removing a key in the example changes the dashboard.**

---

## 2. `~/.genesis/config/genesis.yaml`

**Created by:** `scripts/setup-local-config.sh` (run by hand — no installer script
calls it), the dashboard timezone control (creates the file if absent), and migration
`0086` (one-time timezone seed). Template: `config/genesis.yaml.example`.

**Read by:** `genesis.env._local_config()`. Path is `Path.home()/.genesis/config/genesis.yaml`
— **not** `GENESIS_HOME`. Parsed once and **cached for the process lifetime**; the only
invalidations are the dashboard timezone route and migration `0086`. A non-mapping
root ignores the whole file (warning); a non-mapping section ignores that section
(`_local_section`, warning). Strings in yaml booleans go through `_yaml_bool`
(`"false"`, `"no"`, `"0"`, `"off"`, and empty all mean false).

### Precedence per helper (`src/genesis/env.py`) — they differ

| Helper | 1st | 2nd | Default | "env set" means |
|---|---|---|---|---|
| `user_timezone()` | yaml `timezone` (valid IANA) | env `USER_TIMEZONE` (valid IANA) | `UTC` | **yaml first** |
| `ollama_url()` | env `OLLAMA_URL` | yaml `network.ollama_url` | `http://localhost:11434` | non-empty |
| `lm_studio_url()` | env `LM_STUDIO_URL` | yaml `network.lm_studio_url` | `http://localhost:1234/v1` | non-empty |
| `lm_studio_health_url()` | env `LM_STUDIO_HEALTH_URL` | derived from `lm_studio_url()` | — | present (empty → `""`) |
| `qdrant_url()` | env `QDRANT_URL` | *(no yaml layer)* | `http://localhost:6333` | present |
| `ollama_enabled()` | env `GENESIS_ENABLE_OLLAMA` | yaml `network.ollama_enabled` | `False` | **present** |
| `embed_priority_tier()` | env `GENESIS_EMBED_PRIORITY_TIER` | yaml `memory.embed_priority_tier` | `True` | **present** |
| `build_lane_enabled()` | env `GENESIS_BUILD_LANE_ENABLED` | yaml `build_lane.enabled` | `False` | **present** |
| `models_md_synthesis_enabled()` | env `GENESIS_MODELS_MD_SYNTHESIS_OFF` (inverted) | yaml `models_md_synthesis.enabled` | `True` | present |
| `github_user()` | env `GENESIS_GITHUB_USER` | yaml `github.user` | `""` | non-empty |
| `github_public_repo()` | env `GENESIS_GITHUB_PUBLIC_REPO` | yaml `github.public_repo` | `GENesis-AGI` | non-empty |

"Present" means `os.environ.get(...) is not None`: `KEY=` (empty) **counts as set** and,
for the three boolean helpers marked in bold, reads as **true** (`""` is not a falsey
token on the env branch, unlike `_yaml_bool`).

**Timezone specifics.** The dashboard control (`/api/genesis/settings/timezone`,
`src/genesis/dashboard/routes/state.py`) writes the file, invalidates the cache and
reloads `genesis.util.tz` — display updates live, but every
`CronTrigger(timezone=user_timezone())` is bound at registration, so **schedules
re-time on restart**. Migration `0086` copied a non-UTC `USER_TIMEZONE` into the file
once; after that, `USER_TIMEZONE` is consulted only when the file has no valid
`timezone`. Installers still write `USER_TIMEZONE` into `secrets.env`.

**Other readers of the same file** (outside `env.py`, own parsers, no shared cache):
`scripts/hooks/git_push_guard.py` and `scripts/review_scope.py` (`merge_gate.*`, env
seams such as `GENESIS_MERGE_GATE_DOC_FINDINGS` first); `src/genesis/contribution/fingerprints.py`
(`timezone`, `github.public_repo`, `github.private_repo`). Backed up by `scripts/backup.sh`;
restored by `src/genesis/guardian/cred_integrity.py`.

**Routing sees it too.** `${VAR}` placeholders in `model_routing.yaml` for
`OLLAMA_URL`, `LM_STUDIO_URL`, `LM_STUDIO_HEALTH_URL`, `GENESIS_ENABLE_OLLAMA` resolve
through the `genesis.env` accessors (`_ENV_ACCESSORS` in `src/genesis/routing/config.py`);
every other placeholder is env → inline `:-default` → left literal.

---

## 3. `config/*.yaml` and `.local.yaml` overlays

Base files in `config/` are tracked upstream defaults. Four patterns exist:

**A. Standard settings domain** — `genesis._config_overlay.merge_local_overlay`, used by
43 modules under `src/` plus two hook scripts. Effective value:
`code DEFAULTS ← config/<name>.yaml ← overlay`. The overlay path is resolved
**user-dir first**: `~/.genesis/config/<name>.local.yaml` if it exists, else the repo
sibling `config/<name>.local.yaml` (gitignored). **Only one of the two is read.**
Deep merge; lists replace. An unparseable or non-mapping overlay is ignored with a
warning (once per file mtime). A broken BASE file is not uniformly safe: some loaders fall back to `DEFAULTS`, while others (e.g. `src/genesis/resilience/config.py`, `src/genesis/inbox/config.py`) call `yaml.safe_load` unguarded and raise, which stops the initialization path that loads them.
Many loaders re-read on every call (e.g. `src/genesis/session_awareness/pr_watch_config.py`);
the domain's `needs_restart` flag is the stated contract.

- **Writers:** `settings_update` MCP tool and the dashboard Settings tab (same backend,
  `src/genesis/mcp/health/settings.py`). Per-domain validator → atomic write to
  `~/.genesis/config/<name>.local.yaml` with a `# set-by: <actor> @ <utc>` provenance
  header. The write starts from whichever overlay the resolver found, so the first
  save copies a repo-sibling overlay into the user dir, which then shadows it.
- **Registry:** 48 domains in `_DOMAIN_REGISTRY`. `readonly` domains refuse writes;
  `recon_*` route to the `recon_config` tool; disabling
  `autonomous_cli_policy.manual_approval_required` needs `confirm_disable_approval_gate`.
- **Takes effect:** per the domain's `needs_restart` flag. Restart-required today:
  `resilience`, `inbox_monitor`, `autonomy`, `guardian`, `content_sanitization`,
  `surplus`, `ego`, `channels`, `observability`. The rest are flagged no-restart.
- **Kill switches:** env-only `GENESIS_<DOMAIN>_DISABLED` / `_OFF` (39 distinct names),
  which override the yaml (example: `src/genesis/memory/integrity_config.py`, truthy
  tokens `1`/`true`/`yes` — check each module's own token set). **None is in
  `secrets.env.example`**, so none is settable from the dashboard — put it in
  `secrets.env` and restart, or set it in the process env.

**B. Model routing** — `src/genesis/routing/config.py::load_config` reads only the
**repo sibling** `config/model_routing.local.yaml` (never `~/.genesis/config/`). Dashboard
call-site edits (`update_call_site_in_yaml`) write there. The overlay is sanitized:
call sites absent from the base are dropped, and chain entries naming a provider absent
from the **base** `providers:` are dropped — an overlay cannot add a usable provider.
Provider keys resolve dynamically as `API_KEY_<TYPE>`, `<TYPE>_API_KEY` or
`<TYPE>_API_TOKEN` (`_resolve_api_key` in `src/genesis/routing/litellm_delegate.py`).

**C. Whole-file user override** — outreach: `~/.genesis/config/outreach.yaml`, if present,
**replaces** `config/outreach.yaml` as the base (`src/genesis/outreach/config.py`,
written by `save_outreach_config`); the overlay is merged on top of whichever won.

**D. Raw file editor** — dashboard `PUT /api/genesis/config-files/<name>`
(`src/genesis/dashboard/routes/config.py`) writes the **tracked base** `config/<name>.yaml`
directly (read-only list excepted). The edit dirties the checkout and is shadowed by any
overlay key.

---

## 4. Other sources

- **Process env only** (no file layer): paths — `GENESIS_REPO_ROOT`, `SECRETS_PATH`,
  `GENESIS_DB_PATH`, `GENESIS_HOME`, `VENV_PATH`, `CLAUDE_HOME`, `GENESIS_PLANS_DIR`,
  `GENESIS_OUTPUT_DIR`, `GENESIS_CC_PROJECT_ID`; tuning — `GENESIS_DB_BUSY_TIMEOUT_MS`
  (MCP children default it to 15000), read-pool sizes, `GENESIS_RECALL_RERANK_RPM`.
  All in `src/genesis/env.py`. They reach a process via the unit, `secrets.env`, or the parent.
- **`env.example`** — a template of deployment/topology env vars. Its header suggests
  "`.env` at the repo root", but nothing in `src/` or `scripts/` loads a repo-root `.env`;
  put these in `secrets.env` or the unit environment.
- **Identity** — `src/genesis/identity/*.md`. Tracked prompts, plus gitignored per-install
  files: `USER.md` (user-edited, seeded from `USER.md.example`) and runtime-generated
  `USER_KNOWLEDGE.md` and `TRIAGE_CALIBRATION.md`. `IdentityLoader` caches
  per instance (the runtime's perception loader is built at init, so hand edits need a
  restart); `src/genesis/cc/system_prompt.py` re-reads per call. The dashboard file editor
  may edit these, and may create one only where a `.example` exists.
- **Claude Code** — `.claude/settings.json` (tracked, hook wiring, ships to every clone);
  `.claude/settings.local.json` (gitignored); `~/.claude/settings.json` (user level;
  `scripts/setup_claude_config.py --global` applies `config/cc-global-settings.yaml`, and
  `scripts/cc_settings_align.sh` re-asserts the auto-updater keys daily). How they merge is
  Claude Code's rule, not Genesis's. `.mcp.json` is rendered from
  `config/mcp.json.template`: `install.sh` leaves an existing file alone, but
  `scripts/bootstrap.sh` runs `scripts/setup_claude_config.py`, which REWRITES it whenever
  it differs from the template, so custom entries do not survive a bootstrap. Its Genesis servers carry
  no `env` block, so they get env from the CC process plus `secrets.env` (section 1).

---

## Traps

1. **The dotenv snapshot.** Editing `secrets.env` changes nothing that is running. A
   docstring saying a switch works "without a restart" means it is read per call, not
   that anything re-reads the file (`daily_budget_disabled`, `memory_rerank_off`,
   `skill_gate_off` in `src/genesis/env.py` have no in-process setter).
2. **Empty is not unset.** `KEY=` in `secrets.env` shadows `genesis.yaml` for every
   "present" helper, and turns `GENESIS_ENABLE_OLLAMA`, `GENESIS_BUILD_LANE_ENABLED` and
   `GENESIS_EMBED_PRIORITY_TIER` **on**. To defer to the yaml, comment the line out.
3. **Timezone is inverted.** `genesis.yaml` beats `USER_TIMEZONE`; changing the env var
   on an install whose file has a valid zone does nothing.
4. **The overlay is not always where you think.** Settings domains read
   `~/.genesis/config/<name>.local.yaml` first and ignore the repo sibling when it exists;
   routing reads only `config/model_routing.local.yaml`; outreach may read a whole-file
   `~/.genesis/config/outreach.yaml`. `settings_get` shows the domain-standard merge, which
   for `model_routing` and `outreach` is not necessarily what the runtime loads.
5. **`GENESIS_HOME` does not move config.** `genesis.yaml`, the overlay user dir, and the
   settings writer all use `Path.home()/.genesis/config`.
6. **MCP children get the whole file.** The `_MCP_VARS` allowlist in
   `scripts/genesis_mcp_server.py` is superseded by the standalone router's full
   `override=True` load, so a value the parent put in a child's env is overwritten by any
   same-named key in `secrets.env`.
7. **`EnvironmentFile=` beats `Environment=`** regardless of order (measured note in
   `scripts/systemd/genesis-disk-hygiene.service.template`): a `PATH` or `TMPDIR` in
   `secrets.env` replaces the server unit's pinned value.
8. **Never put a CC OAuth token in `secrets.env`.** It would reach every dispatched CC
   session via the server's env; it has its own file (`src/genesis/guardian/credential_bridge.py`).
9. **The raw config-file editor edits tracked files.** Prefer `settings_update`, which
   writes an overlay that survives `git pull`.
