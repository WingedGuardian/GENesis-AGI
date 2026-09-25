"""Renaming a provider must not break an install's local overlay.

A provider rename is a BREAKING UPGRADE for any install carrying a
`model_routing.local.yaml`, and it is the one reference set the PR diff cannot
see. `_sanitize_local_overlay` migrated call sites but not providers, so after
an upgrade:

* **Variant A — hard failure.** A partial provider override
  (`providers: {glm51: {rpm_limit: 5}}`) deep-merges onto nothing, producing an
  entry with no `type`/`model`. `_parse` then subscripts it and raises
  `KeyError: 'type'`. It does NOT crash the process — `runtime/init/router.py`
  swallows it, `rt._router` stays None, and because `router` is in
  `_CRITICAL_SUBSYSTEMS` the runtime comes up with `_bootstrapped = False` and
  every LLM call site dark, announced by one log line. Silent is worse than a
  crash here.
* **Variant B — silent loss.** A call-site chain pinned to a renamed provider is
  filtered out as "unknown", so the operator's deliberate pin is discarded and
  the shipped chain runs instead. No error, no failure, just different routing.

Variant B is squarely in the dashboard's blast radius: `update_call_site_in_yaml`
writes call-site chains into the overlay, so an operator who never hand-edited
anything can still hold a stale pin.

And the shipped config TEACHES partial provider overrides — `model_routing.yaml`
tells a metered install to override `free:` in its overlay — so this is a trigger
people actually pull, not a hypothetical.

The fix migrates legacy keys at the sanitizer, which is the chokepoint that
already exists, plus a defensive `_parse` guard so no FUTURE rename can reach the
router-dark shape.
"""

from __future__ import annotations

import textwrap

import pytest

from genesis.routing.config import _RENAMED_PROVIDERS, load_config

_BASE = textwrap.dedent(
    """
    providers:
      glm:
        type: zenmux
        model: z-ai/glm-5.3
        free: false
      mistral-large-free:
        type: mistral
        model: mistral-large-latest
        free: true
    call_sites:
      31_outcome_classification:
        description: test site
        chain: [glm, mistral-large-free]
    """
).strip()


def _write(tmp_path, base: str, local: str | None):
    cfg = tmp_path / "model_routing.yaml"
    cfg.write_text(base + "\n")
    if local is not None:
        (tmp_path / "model_routing.local.yaml").write_text(textwrap.dedent(local).strip() + "\n")
    return cfg


def test_variant_a_partial_provider_override_of_a_renamed_key_still_loads(tmp_path):
    """The hard-failure variant: a partial override must not take the router down."""
    cfg = _write(
        tmp_path,
        _BASE,
        """
        providers:
          glm51:
            rpm_limit: 5
        """,
    )

    config = load_config(cfg, check_api_keys=False)

    assert "glm51" not in config.providers, "the legacy key survived the migration"
    assert "glm" in config.providers
    assert config.providers["glm"].rpm_limit == 5, (
        "the operator's override was dropped rather than migrated onto the new key"
    )
    # The base fields must survive — this is what KeyError'd before.
    assert config.providers["glm"].model_id == "z-ai/glm-5.3"


def test_variant_b_a_chain_pinned_to_a_renamed_provider_keeps_the_pin(tmp_path):
    """The silent-loss variant: the operator's pin must be translated, not dropped."""
    cfg = _write(
        tmp_path,
        _BASE,
        """
        call_sites:
          31_outcome_classification:
            chain: [glm51]
        """,
    )

    config = load_config(cfg, check_api_keys=False)

    chain = config.call_sites["31_outcome_classification"].chain
    assert chain == ["glm"], (
        f"expected the pin translated to the new name, got {chain!r} — a chain that "
        "falls back to the shipped order has silently discarded the operator's choice"
    )


def test_old_and_new_keys_together_keep_the_new_one(tmp_path):
    """Collision rule, stated rather than left to merge order.

    An explicit override of the NEW name is the operator's current intent, so it
    wins and the legacy entry is dropped.
    """
    cfg = _write(
        tmp_path,
        _BASE,
        """
        providers:
          glm51:
            rpm_limit: 5
          glm:
            rpm_limit: 9
        """,
    )

    config = load_config(cfg, check_api_keys=False)

    assert "glm51" not in config.providers
    assert config.providers["glm"].rpm_limit == 9, "the legacy key overwrote the current one"


def test_a_provider_entry_with_no_type_is_dropped_not_fatal(tmp_path):
    """Defensive guard: a FUTURE rename must degrade, not take routing dark.

    The alias map above only covers renames we know about. This is the backstop
    for the next one — an incomplete provider is skipped with a warning instead
    of raising out of `_parse` into the router's bare `except`.
    """
    cfg = _write(
        tmp_path,
        _BASE,
        """
        providers:
          some-future-rename:
            rpm_limit: 3
        """,
    )

    config = load_config(cfg, check_api_keys=False)

    assert "some-future-rename" not in config.providers
    assert "glm" in config.providers, "the whole config was lost over one bad entry"


def test_the_alias_map_is_not_empty_and_names_every_rename():
    """Non-vacuity guard. An EMPTY map must fail here, loudly.

    A first draft of this file parametrized over the map's items and asserted only
    truthiness — so emptying `_RENAMED_PROVIDERS` produced `1 skipped`, exit 0, and
    the suite stayed green while the fix was gone. pytest SKIPS an empty
    parametrize list; a skip is not a pass, and neither is a test that cannot run.
    """
    assert len(_RENAMED_PROVIDERS) >= 7, (
        f"the alias map has {len(_RENAMED_PROVIDERS)} entries; PR #2215 renamed "
        "seven providers and entries are append-only — a shrinking map means an "
        "install that upgrades from an older version loses its overrides"
    )
    for legacy, current in _RENAMED_PROVIDERS.items():
        assert legacy and current and legacy != current


def test_no_legacy_key_was_re_introduced_as_a_live_provider():
    """A legacy KEY must never also be a shipped provider name.

    If a future PR re-introduces a provider literally named `glm51`, every
    operator's legitimate `glm51` override would be silently migrated onto `glm`
    — the map would be actively corrupting live config instead of repairing stale
    config. Cheapest possible guard against the map outliving its own premise.
    """
    from pathlib import Path

    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    shipped = yaml.safe_load((repo_root / "config" / "model_routing.yaml").read_text())
    declared = set((shipped.get("providers") or {}).keys())

    collisions = set(_RENAMED_PROVIDERS) & declared
    assert not collisions, (
        f"legacy alias key(s) {sorted(collisions)} are also live providers in "
        "config/model_routing.yaml — an operator's override of that name would be "
        "silently migrated away. Remove the alias or rename the new provider."
    )


@pytest.mark.parametrize("legacy,current", sorted(_RENAMED_PROVIDERS.items()))
def test_every_map_entry_actually_migrates_a_provider_override(tmp_path, legacy, current):
    """EVERY entry is exercised, not just the one the bug report happened to name.

    The first draft covered `glm51` behaviourally and left the other six asserted
    only for truthiness — so deleting any of them failed nothing.
    """
    base = textwrap.dedent(
        f"""
        providers:
          {current}:
            type: zenmux
            model: some/model
            free: false
        call_sites:
          31_outcome_classification:
            description: test site
            chain: [{current}]
        """
    ).strip()
    cfg = _write(tmp_path, base, f"providers:\n  {legacy}:\n    rpm_limit: 7")

    config = load_config(cfg, check_api_keys=False)

    assert legacy not in config.providers, f"{legacy} survived migration"
    assert config.providers[current].rpm_limit == 7, (
        f"the override on legacy name {legacy!r} was not migrated onto {current!r}"
    )


@pytest.mark.parametrize("legacy,current", sorted(_RENAMED_PROVIDERS.items()))
def test_every_map_entry_actually_migrates_a_pinned_chain(tmp_path, legacy, current):
    """The Variant-B half, for every entry rather than one."""
    base = textwrap.dedent(
        f"""
        providers:
          {current}:
            type: zenmux
            model: some/model
            free: false
          fallback:
            type: mistral
            model: m
            free: true
        call_sites:
          31_outcome_classification:
            description: test site
            chain: [{current}, fallback]
        """
    ).strip()
    cfg = _write(
        tmp_path, base, f"call_sites:\n  31_outcome_classification:\n    chain: [{legacy}]"
    )

    config = load_config(cfg, check_api_keys=False)

    assert config.call_sites["31_outcome_classification"].chain == [current], (
        f"a chain pinned to legacy name {legacy!r} did not translate to {current!r}"
    )


def test_a_chain_naming_both_old_and_new_does_not_duplicate(tmp_path):
    """Translation must DEDUPE, or it creates an invariant violation.

    The write path rejects duplicate providers outright
    ("Chain must not contain duplicate providers"), and a duplicate makes failover
    retry one provider twice against the same breaker and budget ledger. Pre-fix
    the legacy entry was filtered out as stale, so this shape is only reachable
    once translation exists — i.e. this diff introduced it.
    """
    cfg = _write(
        tmp_path,
        _BASE,
        """
        call_sites:
          31_outcome_classification:
            chain: [glm51, glm]
        """,
    )

    config = load_config(cfg, check_api_keys=False)

    assert config.call_sites["31_outcome_classification"].chain == ["glm"], (
        "translation produced a duplicate provider in the chain"
    )


@pytest.mark.parametrize("bad", ["", "  glm51: broken"])
def test_a_half_edited_overlay_entry_does_not_take_routing_dark(tmp_path, bad):
    """`glm51:` with no body parses as None; `glm51: broken` as a str.

    Both used to raise AttributeError before any completeness check, and that
    exception is swallowed by runtime/init/router.py — so the runtime came up with
    every LLM call site dark behind one log line. Worse, migrating a None onto the
    live key would DESTROY the base provider's entry.
    """
    local = "providers:\n  glm51:\n" if bad == "" else f"providers:\n{bad}\n"
    cfg = _write(tmp_path, _BASE, local)

    config = load_config(cfg, check_api_keys=False)

    assert "glm" in config.providers, "a half-edited overlay destroyed the base provider"
    assert config.providers["glm"].model_id == "z-ai/glm-5.3"
    assert "glm51" not in config.providers


def test_alias_map_targets_exist_in_the_shipped_config():
    """Every migration TARGET must be a provider the shipped config actually has.

    Without this, a typo in the map migrates an override onto a key that does not
    exist, which lands back in the incomplete-provider shape the guard above only
    degrades — silently losing the override instead of honouring it.
    """
    from pathlib import Path

    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    shipped = yaml.safe_load((repo_root / "config" / "model_routing.yaml").read_text())
    declared = set((shipped.get("providers") or {}).keys())

    missing = {new for new in _RENAMED_PROVIDERS.values() if new not in declared}
    assert not missing, (
        f"alias map points at provider(s) absent from config/model_routing.yaml: "
        f"{sorted(missing)} — an override migrated onto one of these is silently lost"
    )


def test_a_dashboard_save_preserves_an_override_on_a_renamed_provider(tmp_path):
    """The SAVE path is the third overlay reader, and the only one that WRITES.

    Measured on review: once `_parse` learned to SKIP an incomplete provider
    instead of raising, a dashboard save carrying a legacy provider key stopped
    failing loudly (HTTP 400) and started SUCCEEDING while silently dropping the
    operator's override from the returned config — which
    `dashboard/routes/routing.py` then installs via `reload_config()`. A loud,
    correct rejection became a live-router divergence that only heals on restart.

    That regression was introduced BY the `_parse` guard, which is why it is
    pinned here and not left to the load-path tests: they all passed throughout.
    """
    from genesis.routing.config import update_call_site_in_yaml

    cfg = _write(tmp_path, _BASE, "providers:\n  glm51:\n    rpm_limit: 5")

    updated = update_call_site_in_yaml(cfg, "31_outcome_classification", default_paid=True)

    assert updated.providers["glm"].rpm_limit == 5, (
        "the save path dropped the operator's override instead of migrating it — "
        "the live router would run without it until the next restart"
    )
    assert "glm51" not in updated.providers


def test_a_dashboard_save_is_not_blocked_by_a_legacy_name_in_a_chain(tmp_path):
    """The mirror failure: every save failing over an invisible name.

    Measured on review: an overlay chain pinned to a legacy provider made EVERY
    save fail with "references unknown provider 'glm51'" — while the load path had
    already migrated it, so the dashboard displayed `glm`, the runtime routed to
    `glm`, and only the save failed, citing a name shown nowhere.
    """
    from genesis.routing.config import update_call_site_in_yaml

    cfg = _write(
        tmp_path,
        _BASE,
        "call_sites:\n  31_outcome_classification:\n    chain: [glm51]",
    )

    updated = update_call_site_in_yaml(cfg, "31_outcome_classification", default_paid=True)

    assert updated.call_sites["31_outcome_classification"].chain == ["glm"]


# ── A BODILESS overlay key wipes what the base holds there ──────────────────
#
# Variant C, found in review on this PR and then widened by an adversarial audit
# of the first fix. Same router-dark outcome as variant A, reached with no rename
# involved: an operator opens the overlay, types a key, and saves before writing
# its body. YAML loads that as None, and `_deep_merge` used to plain-assign it
# over whatever the base held.
#
# The review named the SECTION level (`providers:`). The audit showed the first
# fix closed only that level, and that the ENTRY level below it
# (`call_sites: {<id>:}`) was still fatal — and likelier, because
# `call_sites: {<id>: {...}}` is the nesting the dashboard itself writes.
#
# There are now TWO mechanisms and they are tested SEPARATELY on purpose. Once
# `_deep_merge` refuses a non-mapping over a mapping, an end-to-end `load_config`
# assertion passes even with the sanitizer's drop deleted — so an end-to-end test
# alone would stop binding the sanitizer at all. The split below is what keeps
# each one held:
#
#   `_deep_merge`            — CORRECTNESS. The router loads. Tested directly.
#   `_sanitize_local_overlay`— HEALING. The poison is removed from the dict that
#                              `update_call_site_in_yaml` writes back to disk, and
#                              its `.setdefault(...)` at :695 therefore returns
#                              `{}` instead of a `None` that raises outside the
#                              validation try. Tested by inspecting the returned
#                              overlay, not the loaded config.


def _base_dict():
    """The shipped-config shape, as `yaml.safe_load` returns it."""
    import yaml

    return yaml.safe_load(_BASE)


# ── mechanism 1: _deep_merge refuses a non-mapping over a mapping ───────────

@pytest.mark.parametrize(
    "bad", [None, "oops", 5, ["a"]], ids=["none", "str", "int", "list"]
)
def test_deep_merge_keeps_the_base_mapping_against_a_non_mapping(bad):
    """The root-cause guard, at the level the recursion actually reaches."""
    from genesis.routing.config import _deep_merge

    merged = _deep_merge({"providers": {"glm": {"type": "zenmux"}}}, {"providers": bad})

    assert merged["providers"] == {"glm": {"type": "zenmux"}}, (
        f"a {type(bad).__name__} overlay replaced a base mapping — every provider "
        "or call site under it would be gone and the router would load dark"
    )


def test_deep_merge_applies_a_non_mapping_where_the_base_has_no_mapping(bad=None):
    """CONTROL. The guard is about mappings, not about None.

    Overwriting a scalar, a list, or an absent key with whatever the overlay says
    is the merge's entire job. Without this, a guard that refused every None would
    pass the tests above while silently breaking ordinary overrides.
    """
    from genesis.routing.config import _deep_merge

    merged = _deep_merge(
        {"free": True, "chain": ["a"], "n": 1},
        {"free": None, "chain": ["b"], "n": 2, "brand_new": None},
    )

    assert merged["free"] is None
    assert merged["chain"] == ["b"]
    assert merged["n"] == 2
    assert merged["brand_new"] is None


def test_deep_merge_survives_a_non_mapping_overlay_argument():
    """`update_call_site_in_yaml:699` hits this shape with a poisoned entry.

    It raised there OUTSIDE the validation try at :778, so the dashboard save
    returned 500 with no backup written, rather than failing validation cleanly.
    """
    from genesis.routing.config import _deep_merge

    assert _deep_merge({"chain": ["glm"]}, None) == {"chain": ["glm"]}


@pytest.mark.parametrize("section", ["providers", "call_sites"])
@pytest.mark.parametrize("body", ["", "  # just a comment\n"])
def test_a_bodiless_section_does_not_delete_the_base_section(tmp_path, section, body):
    """End to end: the config still loads with everything the base shipped.

    Both spellings of "no body" are kept for documentation rather than coverage —
    `yaml.safe_load` returns None for each, so they are one path.
    """
    cfg = _write(tmp_path, _BASE, f"{section}:\n{body}")

    loaded = load_config(cfg, check_api_keys=False)

    assert set(loaded.providers) >= {"glm", "mistral-large-free"}
    assert "31_outcome_classification" in loaded.call_sites


@pytest.mark.parametrize(
    "overlay",
    [
        "call_sites:\n  31_outcome_classification:\n",
        "call_sites:\n  31_outcome_classification: oops\n",
        "call_sites:\n  31_outcome_classification: [glm]\n",
        "providers:\n  glm:\n",
    ],
    ids=["call_site_none", "call_site_str", "call_site_list", "provider_none"],
)
def test_a_bodiless_ENTRY_does_not_take_the_router_dark(tmp_path, overlay):
    """The level the first fix missed, and the likelier one.

    MEASURED before this fix, each of these escaped `load_config` as a TypeError
    or AttributeError — `cs["chain"]` at config.py:578 and `rp.get` at :462 have
    no shape guard — and `runtime/init/router.py` swallowed it into
    `_bootstrapped = False`: Genesis up, every LLM call site dark, one log line.
    """
    cfg = _write(tmp_path, _BASE, overlay)

    loaded = load_config(cfg, check_api_keys=False)

    assert "31_outcome_classification" in loaded.call_sites
    assert loaded.call_sites["31_outcome_classification"].chain
    assert set(loaded.providers) >= {"glm", "mistral-large-free"}


# ── mechanism 2: the sanitizer HEALS the file the dashboard writes back ─────

@pytest.mark.parametrize(
    "overlay,gone",
    [
        ({"providers": None}, "providers"),
        ({"call_sites": None}, "call_sites"),
        ({"providers": "oops"}, "providers"),
    ],
    ids=["providers_none", "call_sites_none", "providers_str"],
)
def test_the_sanitizer_removes_a_poisoned_SECTION_from_what_is_written_back(
    overlay, gone
):
    """Asserted on the RETURNED OVERLAY, not on the loaded config.

    `update_call_site_in_yaml` dumps this dict straight to disk (:786), so what
    survives here is what the operator's file looks like after the next dashboard
    save. A `load_config` assertion cannot see this — `_deep_merge` already makes
    the config correct either way — which is exactly why it is tested here.
    """
    from genesis.routing.config import _sanitize_local_overlay

    result = _sanitize_local_overlay(_base_dict(), overlay)

    assert gone not in result, (
        f"'{gone}' survived sanitization, so it would be written back to the "
        "operator's overlay and silently re-ignored at every load"
    )


@pytest.mark.parametrize(
    "section,name",
    [
        ("call_sites", "31_outcome_classification"),
        ("providers", "glm"),
    ],
)
def test_the_sanitizer_removes_a_poisoned_ENTRY_from_what_is_written_back(
    section, name
):
    """The entry level of the same property — and the dashboard-500 fix.

    `update_call_site_in_yaml:695` calls `.setdefault(call_site_id, {})` on this
    result. A surviving `None` entry makes `setdefault` return it, and the
    `_deep_merge` at :699 then raised OUTSIDE the try at :778 — a 500 with no
    backup written. Dropping the entry here makes `setdefault` return `{}`.
    """
    from genesis.routing.config import _sanitize_local_overlay

    result = _sanitize_local_overlay(_base_dict(), {section: {name: None}})

    assert name not in result.get(section, {})


def test_the_sanitizer_leaves_an_overlay_key_the_base_does_not_have(tmp_path):
    """Binds the half of the predicate an audit measured as UNBOUND.

    The guard reads `isinstance(base_raw.get(key), dict) and not isinstance(val, dict)`.
    A mutation battery showed the FIRST conjunct could be deleted with every test
    still green — so nothing held it. Deleting an operator's own addition would be
    its own silent defect, and `_parse` ignores unknown top-level keys, so the
    correct behaviour is to leave it alone.
    """
    from genesis.routing.config import _sanitize_local_overlay

    result = _sanitize_local_overlay(_base_dict(), {"brand_new_section": None})

    assert "brand_new_section" in result


def test_an_overlay_that_is_not_a_mapping_at_all_is_ignored():
    """A top-level list or scalar used to raise on `.items()` — router dark."""
    from genesis.routing.config import _sanitize_local_overlay

    assert _sanitize_local_overlay(_base_dict(), ["a", "b"]) == {}
    assert _sanitize_local_overlay(_base_dict(), "nonsense") == {}


def test_the_dropped_section_is_announced(tmp_path, caplog):
    """Dropping silently would trade one invisible failure for another."""
    import logging

    cfg = _write(tmp_path, _BASE, "providers:\n")

    with caplog.at_level(logging.WARNING, logger="genesis.routing.config"):
        load_config(cfg, check_api_keys=False)

    assert any(
        "providers" in r.message and "not a mapping" in r.message
        for r in caplog.records
    ), f"the dropped section was not announced; records={[r.message for r in caplog.records]}"


def test_a_valid_overlay_section_is_still_applied(tmp_path):
    """CONTROL — the guards must not become a blanket drop.

    Without this, widening either predicate until the tests above pass would
    eventually discard every overlay, and the operator's real overrides would
    vanish with the same silence this whole change exists to remove.
    """
    cfg = _write(
        tmp_path,
        _BASE,
        """
        providers:
          glm:
            rpm_limit: 7
        """,
    )

    loaded = load_config(cfg, check_api_keys=False)

    assert loaded.providers["glm"].rpm_limit == 7, (
        "a VALID overlay section was dropped — a guard is over-broad"
    )
    assert set(loaded.providers) >= {"glm", "mistral-large-free"}
