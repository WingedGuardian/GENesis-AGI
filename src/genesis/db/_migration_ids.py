"""The migration-id contract: what filenames are legal, in one place.

**STDLIB-ONLY, ON PURPOSE — do not add a third-party import to this module.**
``scripts/check_migration_prefixes.py`` path-imports it from a CI job that runs
a bare ``python3`` with no ``setup-python`` and no ``pip install``. An earlier
version of that guard imported the two RUNNER modules instead, to bind to their
real patterns; the intent was right but the runners carry module-scope
``import aiosqlite`` / ``from genesis.db.crud import …``, so the guard pulled in
117 modules and exited 2 on every PR — enforcing nothing, while every test
passed because pytest runs inside the project venv.

New migrations use a UTC timestamp id (``date -u +%Y%m%d%H%M%S``). The legacy
hand-allocated 4-digit ids are FROZEN — see ``migrations/runner.py`` for why
allocation was the thing worth removing.

Every file in a migrations directory is CLASSIFIED, and the residue is a
violation
-----------------------------------------------------------------------------
The first version of this contract checked properties of the id SET — no
duplicates, and min/max inside a frozen window. Four independent review findings
came out of that one choice, because a set-property is blind in two directions:

* it never sees a file the pattern did not match (a mistyped
  ``2026090320000_x.py`` is 13 digits, so ``if m:`` skipped it — in the guard AND
  in ``_migration_discovery``, meaning the migration silently NEVER RAN and the
  code deployed without its schema change); and
* it is invariant under mutations that keep the endpoints (``0092`` sits inside
  ``0001..0093`` but does not exist on the default branch, so a hand-allocated
  ``0092_new.py`` passed; renaming ``0050_old.py`` to ``0050_new.py`` left the
  id set identical; and deleting every legacy file left the legacy loop empty,
  which read as clean).

So the contract is now stated the other way round, as a total function over
filenames: a name either IS a frozen legacy migration, or IS a well-formed
timestamp migration, or is NOT a migration at all — and anything left over is a
violation. Adding a new way to be wrong no longer requires a new rule.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

#: Namespace labels. These are the dict keys below and the human-readable
#: nouns in every message the guard prints, deliberately the same string.
SCHEMA = "schema migration"
DATA = "data migration"

#: Schema migrations: a frozen 4-digit legacy id OR a 14-digit UTC timestamp.
#: Width-anchored alternation (``{4}|{14}``, never ``{4,14}``) so a mistyped
#: 13- or 15-digit id cannot quietly found a third id namespace with its own
#: ordering semantics — it fails to match and is caught as MALFORMED below.
#:
#: ``[0-9]`` and not ``\d``: ``\d`` also matches non-ASCII decimal digits, so
#: ``\d{14}`` accepts full-width and Arabic-Indic numerals whose codepoints sort
#: nowhere near the ASCII ids the ordering invariant is stated over.
#:
#: The ID is ASCII; the DESCRIPTION after the underscore is ``\w+``, and the
#: asymmetry is the whole point. Only the id participates in ordering and
#: uniqueness, so only the id needs a codepoint guarantee. Restricting the
#: description too was collateral, and it broke names that already work:
#: ``0094_café.py`` and ``d0012_修復.py`` import fine today (a Python module name
#: may be any identifier), and a downstream fork carrying one would have had
#: discovery start raising — aborting database init on the schema side, and
#: skipping the whole batch on the data side — over a naming preference of ours.
SCHEMA_MIGRATION_PATTERN = re.compile(r"^([0-9]{4}|[0-9]{14})_\w+\.py$")

#: Data migrations: the same, ``d``-prefixed. The prefix keeps the two
#: namespaces disjoint, so they can never cross-collide in one claims map.
DATA_MIGRATION_PATTERN = re.compile(r"^(d(?:[0-9]{4}|[0-9]{14}))_\w+\.py$")

MIGRATION_PATTERNS: dict[str, re.Pattern[str]] = {
    SCHEMA: SCHEMA_MIGRATION_PATTERN,
    DATA: DATA_MIGRATION_PATTERN,
}

#: "This file PRESENTS as a migration." Deliberately far looser than the real
#: patterns: it is what makes the residue detectable at all. A name that looks
#: like a migration but does not parse as one is an ERROR, whereas a name that
#: never looked like one (``runner.py``, ``__init__.py``, ``_util.py``,
#: ``stale_embedding_repair.py`` — the only non-migration files in either
#: directory, measured 2026-09-04) is simply not our business.
#:
#: ``d?`` on BOTH namespaces on purpose: a ``d0001_x.py`` misfiled into
#: ``migrations/``, or a bare ``0001_x.py`` misfiled into ``data_migrations/``,
#: is exactly the silent no-op this class of check exists to catch.
#:
#: ``\d`` here and ``[0-9]`` in the strict patterns above, and the asymmetry is
#: load-bearing. This net must be WIDER than what is legal, or a name that is
#: illegal *because* of its digits escapes classification and is skipped in
#: silence — the very outcome the ASCII restriction exists to prevent. Measured
#: 2026-09-04: with ``[0-9]`` here, ``２０２６０９０４００００００_bad.py``
#: (full-width digits) classified NOT_A_MIGRATION and the guard reported CLEAN.
#: ``[dDｄＤ]*`` and not ``d?``, for the same reason as ``\d``: the net must
#: cover the MISTAKE space, not the legal space — and the mistake space is
#: found by asking what the mistake GENERATORS produce, not by patching named
#: instances. This net has been too narrow three times, each a different
#: generator: ASCII ``[0-9]`` waved through an IME's full-width digits; ``d?``
#: waved through a doubled keystroke (``dd2026…``) and a case habit
#: (``D2026…``); and the review of THAT fix found the first generator still
#: alive in the prefix position (full-width ``ｄ２０２６…``) — the same IME that
#: produces full-width digits produces the full-width letter in the same
#: keystroke. Every one of these presented as a migration to a human and to no
#: code, so the file was skipped in silence, in runtime discovery AND in CI —
#: the never-runs outcome this whole contract exists to refuse.
CANDIDATE_PATTERN = re.compile(r"^[dDｄＤ]*\d")

#: Timestamps must be at or after this year. This is the ORDERING INVARIANT
#: made checkable, not a taste: every frozen legacy id begins with ``0``, so
#: requiring a real calendar year >= 2020 forces a leading ``2`` and therefore
#: guarantees every timestamp id sorts after every legacy id, on a fresh
#: install and an existing one alike.
#:
#: Without it, ``00000000000000`` is 14 ASCII digits, duplicates nothing, and is
#: NOT legacy-width — so the earlier window check never even looked at it. On a
#: fresh install it sorts before ``0001`` and runs first; on an existing install
#: every legacy id is already applied, so it runs last. Same tree, opposite
#: order, nothing detecting it. (Its 4-digit twin ``0000`` was found and fixed
#: one review round earlier in this same change; the 14-digit sibling was not
#: swept at the same time.)
TIMESTAMP_EPOCH_YEAR = 2020

#: The EXACT frozen legacy filenames per namespace — not a range, and not a set
#: of ids. Filenames, because the id alone cannot tell ``0050_old.py`` from
#: ``0050_new.py``: existing installs have ``0050`` in ``schema_migrations`` and
#: skip the renamed file forever, while fresh installs execute it, and the two
#: schemas diverge with nothing to notice.
#:
#: A range cannot express this set. ``0092`` is INSIDE ``0001..0093`` yet absent
#: here — the id was consumed by a rename and never landed — so an endpoint
#: check admitted a brand-new hand-allocated ``0092_*.py``. Enumeration is what
#: makes the gap representable.
#:
#: NOT a content pin. An edit to an applied migration still passes: measured
#: 2026-09-04, 2 of 92 legacy files were edited well after they merged
#: (``0051_entity_layer.py`` +8 days, ``0015_rename_confusable_call_sites.py``
#: +2 months), which is the same fresh-vs-existing divergence one level down.
#: Pinning content is tracked separately — it is a new repo-wide rule (every
#: future edit becomes a CI failure needing a hash bump) and wants its own
#: decision, not a side effect of this one.
#:
#: This set is CLOSED. Nothing is ever added: every new migration is a
#: timestamp, so no id is ever allocated by hand again. If a legacy-id migration
#: authored before this scheme lands merges to the default branch while this is
#: in flight, add its filename here in that same PR — the guard's message says
#: so by name.
FROZEN_LEGACY_FILES: dict[str, frozenset[str]] = {
    SCHEMA: frozenset(
        {
            "0001_add_update_history.py",
            "0002_add_eval_tables.py",
            "0003_eval_results_skipped.py",
            "0004_follow_ups.py",
            "0005_surplus_not_before.py",
            "0006_follow_up_verification.py",
            "0007_ego_proposal_board.py",
            "0008_follow_up_pinned.py",
            "0009_intake_token_enforcement.py",
            "0010_bitemporal_memory.py",
            "0011_j9_eval_tables.py",
            "0012_ego_proposals_status_check.py",
            "0013_migrate_refs_to_episodic.py",
            "0014_eval_results_metadata.py",
            "0015_rename_confusable_call_sites.py",
            "0016_memory_subsystem_tag.py",
            "0017_deep_floor_24h.py",
            "0018_dream_cycle.py",
            "0019_cache_and_errors.py",
            "0020_proposal_goal_id.py",
            "0021_ego_integrity_tracking.py",
            "0022_reflection_corpus.py",
            "0023_procedural_scenario.py",
            "0024_update_history_conflicts_status.py",
            "0025_outcome_events.py",
            "0026_ego_calibration_snapshots.py",
            "0027_cognitive_file_modifications.py",
            "0028_otel_spans.py",
            "0029_memory_links_link_type_pk.py",
            "0030_capability_grants.py",
            "0031_pending_email_sends.py",
            "0032_seed_email_standard_grant.py",
            "0033_autonomy_earn_lose.py",
            "0034_follow_up_kind_domain_goal.py",
            "0035_backfill_procedure_invocation_count.py",
            "0036_rename_procedure_tiers.py",
            "0037_rename_procedure_speculative_to_draft.py",
            "0038_pending_outreach_thread_recipient.py",
            "0039_procedural_surfaced_count.py",
            "0040_inbox_drop_batching.py",
            "0041_surplus_outcome_quality.py",
            "0042_attention_events.py",
            "0043_surplus_judge_score.py",
            "0044_capability_shadow_events.py",
            "0045_attention_acceptance_note.py",
            "0046_attention_events_source.py",
            "0047_build_candidates.py",
            "0048_task_states_source.py",
            "0049_dead_letter_created_index.py",
            "0050_canonicalize_bitemporal_ts.py",
            "0051_entity_layer.py",
            "0052_drop_stale_code_audit_job_health.py",
            "0053_table_inbox_watch_markers.py",
            "0054_origin_class.py",
            "0055_immunity_shadow_events.py",
            "0056_pending_outreach_null_id_backfill.py",
            "0057_origin_class_sessions_observations.py",
            "0058_session_charters.py",
            "0059_session_ledger_shadow.py",
            "0060_data_migrations_ledger.py",
            "0061_job_run_events_alert_events.py",
            "0062_repo_pulse.py",
            "0063_user_goals_origin.py",
            "0064_ledger_predictions.py",
            "0065_entity_adjudications.py",
            "0066_ego_directive_decisions.py",
            "0067_autonomy_events.py",
            "0068_voice_graduation.py",
            "0069_calibration_cells.py",
            "0070_reflex_arc.py",
            "0071_ego_proposal_revisions.py",
            "0072_job_health_error_type.py",
            "0073_memory_integrity.py",
            "0074_memory_reconcile_runs.py",
            "0075_follow_up_revisit_condition.py",
            "0076_follow_ups_idea_kind.py",
            "0077_backfill_revalidate_at.py",
            "0078_ego_proposal_scope.py",
            "0079_pending_issue_posts.py",
            "0080_cc_sessions_extracted_byte.py",
            "0081_mw1_extraction_judgment.py",
            "0082_mw2_link_metadata.py",
            "0083_mw3_entity_types_cards.py",
            "0084_repo_pulse_target_kind.py",
            "0085_backfill_observation_origin.py",
            "0086_seed_timezone_config.py",
            "0087_pending_issue_posts_adopted.py",
            "0088_ego_intentions_origin.py",
            "0089_marketing_prospects.py",
            "0090_ws2_calibration_sunset.py",
            "0091_topic_recency_stamps.py",
            "0093_entity_adjudication_approval.py",
        }
    ),
    DATA: frozenset(
        {
            "d0001_origin_class_qdrant.py",
            "d0002_resolve_duplicate_session_alerts.py",
            "d0003_purge_extraction_goals.py",
            "d0004_purge_retired_job_health.py",
            "d0005_reverify_false_dispatch_failures.py",
            "d0006_purge_surplus_ops_telemetry.py",
            "d0007_clear_orphaned_completed_at.py",
            "d0008_reconcile_memory_cross_store.py",
            "d0009_resync_memory_class_qdrant.py",
            "d0010_backfill_skill_proposal_dampening.py",
            "d0011_reembed_stale_procedure_embeddings.py",
        }
    ),
}

# --- classification outcomes -------------------------------------------------
#: Not a migration and never looked like one — ``runner.py``, ``__init__.py``.
NOT_A_MIGRATION = "not-a-migration"
#: A frozen legacy migration, present under its original filename.
FROZEN_LEGACY = "frozen-legacy"
#: A well-formed new-scheme migration: 14 ASCII digits, a real UTC timestamp.
TIMESTAMP = "timestamp"
#: Well-formed and perfectly RUNNABLE, but not one of THIS repo's frozen legacy
#: files: a new hand-allocated 4-digit id, or a renamed frozen one.
#:
#: Its own verdict because runnable-ness and allowed-ness are different
#: questions and only one of them belongs at boot. This id has a unique
#: prefix, sorts correctly, and would apply cleanly; it violates a repo POLICY,
#: not a runtime invariant. Genesis ships to forks, and a fork's own
#: ``0094_fork_custom.py`` is the ONLY convention that existed before this
#: change — refusing it at runtime would leave that fork with no database after
#: a pull, for a rule that is ours and not theirs. So the runtime RUNS it and
#: warns; CI refuses it, which is where a policy about this repo's namespace
#: can be enforced without reaching into anyone else's install.
DISALLOWED_LEGACY = "disallowed-legacy"
#: Presents as a migration and CANNOT run: no runner discovers it, so it would
#: be skipped in silence. Always a violation, everywhere.
MALFORMED = "malformed"

#: The verdicts a runner may execute. Consumers switch on THIS, never on the
#: negative verdicts — see :func:`scan_directory`.
RUNNABLE = (FROZEN_LEGACY, TIMESTAMP, DISALLOWED_LEGACY)


def is_legacy_width(prefix: str) -> bool:
    """True for a 4-digit-width id (``0091``/``d0011``), not a 14-digit one.

    WIDTH only — it says nothing about whether the id is an ALLOWED legacy id.
    Membership of the frozen set is ``FROZEN_LEGACY_FILES``, and conflating the
    two is what let a fresh ``0092_new.py`` through an endpoint check.
    """
    digits = prefix[1:] if prefix.startswith("d") else prefix
    return len(digits) == 4


def is_valid_timestamp_id(prefix: str) -> bool:
    """True if ``prefix`` is a real UTC ``YYYYMMDDHHMMSS`` at/after the epoch year.

    Rejects impossible calendar values (month 13, day 32, hour 25), non-ASCII
    digits, and every id that would sort before the frozen legacy namespace.
    """
    core = prefix[1:] if prefix.startswith("d") else prefix
    if re.fullmatch(r"[0-9]{14}", core) is None:
        return False
    try:
        stamp = datetime.strptime(core, "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return stamp.year >= TIMESTAMP_EPOCH_YEAR


def classify(namespace: str, filename: str) -> str:
    """Classify one directory entry. Total: every name gets exactly one answer.

    ``namespace`` is :data:`SCHEMA` or :data:`DATA`. Returns one of
    :data:`NOT_A_MIGRATION`, :data:`FROZEN_LEGACY`, :data:`TIMESTAMP`,
    :data:`DISALLOWED_LEGACY`, :data:`MALFORMED`.
    """
    if CANDIDATE_PATTERN.match(filename) is None:
        return NOT_A_MIGRATION
    if not filename.endswith(".py"):
        # ``20260904000000_x.PY`` is a migration-shaped name Python will never
        # import — the same never-runs silence, entering through the extension
        # test instead of the digit test. A candidate with a case-mangled .py
        # is refused; a candidate that is genuinely another filetype
        # (``0094_notes.txt``, ``.pyc``) is not our business.
        return MALFORMED if filename.lower().endswith(".py") else NOT_A_MIGRATION

    match = MIGRATION_PATTERNS[namespace].match(filename)
    if match is None:
        # Presents as a migration, does not parse as one: a mistyped width, a
        # misfiled ``d`` prefix, a non-ASCII digit. Never silently skipped.
        return MALFORMED

    if filename in FROZEN_LEGACY_FILES[namespace]:
        return FROZEN_LEGACY
    if is_legacy_width(match.group(1)):
        return DISALLOWED_LEGACY
    return TIMESTAMP if is_valid_timestamp_id(match.group(1)) else MALFORMED


def rejection_reason(namespace: str, filename: str) -> str:
    """A one-line, actionable explanation for a rejected filename.

    Covers both :data:`MALFORMED` and :data:`DISALLOWED_LEGACY`. Separate from
    :func:`classify` so the runtime and the CI guard print the same sentence;
    a caller that only needs the verdict pays nothing for it.
    """
    match = MIGRATION_PATTERNS[namespace].match(filename)
    new_id = "`date -u +%Y%m%d%H%M%S`_description.py" + (
        " with a leading 'd'." if namespace == DATA else "."
    )
    if match is None:
        return (
            f"{filename!r} looks like a {namespace} but does not match "
            f"{MIGRATION_PATTERNS[namespace].pattern} — so it is discovered by "
            f"nothing and would never run. Name it {new_id}"
        )
    prefix = match.group(1)
    if is_legacy_width(prefix):
        # NOTE: the frozen set is NOT offered here as a remedy. It is editable
        # by the very PR that violates it, so advertising it at the moment of
        # violation is an invitation to grow a set whose entire value is that it
        # never grows. Adding to it is a deliberate, reviewed act with its own
        # failing test to answer to (test_the_frozen_legacy_set_is_closed).
        return (
            f"{filename!r} allocates the legacy-width id {prefix!r}. That "
            f"namespace is FROZEN to an enumerated set of "
            f"{len(FROZEN_LEGACY_FILES[namespace])} files and {prefix!r} is not "
            f"one of them, so this is either a new hand-allocated id or a "
            f"renamed frozen file. Hand-allocating is precisely what made two "
            f"branches claim the same id; use {new_id}"
        )
    return (
        f"{filename!r} has a 14-digit id {prefix!r} that is not a real UTC "
        f"timestamp at or after {TIMESTAMP_EPOCH_YEAR} — so its position in the "
        f"run order is undefined relative to the frozen ids. Use "
        f"`date -u +%Y%m%d%H%M%S`."
    )


def scan_directory(
    namespace: str, directory: Path
) -> tuple[list[tuple[str, str, Path]], list[str], list[str]]:
    """Classify every entry in ``directory``. Returns ``(runnable, unrunnable, disallowed)``.

    ``runnable`` is ``[(id, stem, path)]`` in id order. ``unrunnable`` and
    ``disallowed`` are human-readable reasons.

    ONE implementation, shared by the runtime and the CI guard, because the two
    previously carried the same hand-copied loop — so a fail-open in it was a
    fail-open twice, needing to be found twice.

    It switches on the POSITIVE verdicts and raises on an unrecognised one. The
    earlier loops switched on the two NEGATIVE verdicts and accepted everything
    else, which is not a total switch at all: it is an accept-by-default whose
    residue grows silently every time a verdict is added. That is not
    hypothetical — adding :data:`DISALLOWED_LEGACY` to fix the boot-refusal
    scope would have been silently accepted as runnable by both callers.
    """
    runnable: list[tuple[str, str, Path]] = []
    unrunnable: list[str] = []
    disallowed: list[str] = []

    for path in sorted(directory.iterdir()):
        verdict = classify(namespace, path.name)
        if verdict == NOT_A_MIGRATION:
            continue
        if verdict == MALFORMED:
            unrunnable.append(rejection_reason(namespace, path.name))
            continue
        if verdict not in RUNNABLE:
            raise RuntimeError(
                f"unhandled migration classification {verdict!r} for "
                f"{path.name!r} — refusing to guess whether it should run"
            )
        if verdict == DISALLOWED_LEGACY:
            disallowed.append(rejection_reason(namespace, path.name))
        match = MIGRATION_PATTERNS[namespace].match(path.name)
        if match is None:  # pragma: no cover - classify() already proved this
            raise RuntimeError(
                f"internal inconsistency: {path.name!r} classified {verdict!r} "
                f"but does not match {MIGRATION_PATTERNS[namespace].pattern}"
            )
        runnable.append((match.group(1), path.stem, path))

    # ORDER: every legacy-width id before every timestamp id, then by id within
    # each band. A raw filename sort ALMOST does this — every frozen legacy id
    # begins with '0' and every timestamp with '2' — but "begins with 0" holds
    # only for the frozen 0001..0093 set, NOT for a downstream FORK's own
    # legacy-width id. A fork's ``3000_custom.py`` (or ``d3000_…``) sorts AFTER a
    # ``2026…`` timestamp by leading character, so a fresh clone would run it
    # after the timestamp while the fork that authored it ran it before the
    # timestamp existed — divergent order across installs, exactly what the
    # ordering invariant exists to prevent. The band key makes the invariant
    # true by construction instead of by a leading-digit coincidence, and it
    # is a no-op for this repo (its legacy ids all begin with 0 already).
    runnable.sort(key=lambda r: (is_valid_timestamp_id(r[0]), r[0]))
    return runnable, unrunnable, disallowed
