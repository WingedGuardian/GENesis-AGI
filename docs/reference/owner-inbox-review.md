# Owner-requested inbox review

The Tier-1 `.claude/skills/inbox-review/SKILL.md` workflow is for an explicit
owner request in an existing attended conversation. `CLAUDE.md` routes that
request to an explicit file Read. The packaged server inherits its repository
working directory; custom launches must provide that project context or load
the file explicitly. Foreground Codex follows the existing external-client
policy, without Genesis session registration. Gateway/unknown context does not
become owner-authenticated through the skill.

## Prerequisites and activation

Install the report output-protection and item-accounting changes plus generic
manual-only catalog/discovery support before activating this skill. Deploy
through the controlled deployment path. Explicitly regenerate the catalog with
`python3 scripts/generate_skill_catalog.py` and restart cached consumers as
described in [manual-only-skills.md](manual-only-skills.md). A PR or merged skill
definition alone does not prove the running catalog/consumers were refreshed.
Refresh the portable inventory with `python3 scripts/export_agents_md.py` when
changing tracked skill definitions; its output is committed, not a live catalog.

The skill requires a fresh successful report, full note and evaluation bodies,
bounded history, current-source checks for alleged defects, substantive grouped
cards and actual owner decisions. Existing memory and per-review outcomes are
the records. There is no automatic re-evaluation, wipe, build or new tracker.
See the skill for the presentation and partial-write recovery contract.

## Read-only history helper

Run with the Genesis venv interpreter:

```sh
python3 scripts/inbox_review_rulings.py --db /path/to/genesis.db
```

Omitting `--db` uses `genesis.env.genesis_db_path`, including its existing
environment overrides. SQLite opens in `mode=ro`, `query_only` and a read
transaction. Exit 0 returns strict JSON; exit 2 reports a database/read/schema
failure to stderr, with no successful empty result. It creates no database.

The helper scans retained `memory_fts` rows and filters the exact whitespace
token `inbox_disposition`. The FTS phrase tokenizer missed NBSP/em-space tag
boundaries in the isolated qualification, so it is not the enumeration filter.
Duplicates and full matching content/source/tags/collection/metadata remain.
Deprecated, expired, superseded, missing and malformed metadata are historical
evidence, not silently discarded or adjudicated as current owner authority.
Malformed BLOB/nonfinite SQLite values get typed JSON representations. Non-text
tag rows are reported separately and set `complete: false`; NULL tags contain
no exact token. An empty result is scoped, never proof of no prior decision.
Untagged memory, missing FTS entries and other channels remain outside scope.

## Validation limits

Isolated fixtures exercise SQL/CLI behavior, unchanged database bytes, complete
metadata, Unicode tags, missing evidence and snapshots. Combined worktree checks
exercise catalog/hook/executor discovery and the report/helper interfaces.
These deterministic checks do not qualify native slash-command behavior on all
clients, live LLM judgment or the quality of future owner decisions. No live
memory or inbox mutation is needed for qualification.
