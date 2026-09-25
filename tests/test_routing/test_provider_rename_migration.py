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


# ── The collision rule is only true if the walk order makes it true ─────────


def test_a_chained_rename_keeps_the_NEWEST_override(monkeypatch):
    """Found in review. With `A -> B -> C`, an overlay can hold both generations.

    Both resolve to `C`, so one of them collides and is dropped — and WHICH one
    is decided entirely by the order the map is walked. In insertion order `A`
    migrates first and `B` is then dropped, so the OLDEST override wins, silently
    inverting the rule the function's own docstring states.

    MEASURED before the fix, with this exact fixture: `rpm_limit` 1 survived where
    99 should have.

    Nothing in the shipped map is more than one hop today, so this cannot fire on
    a real install yet. It is here so the rule still holds the first time a
    provider is renamed twice — which is precisely when nobody will be looking.
    """
    from genesis.routing import config as C

    monkeypatch.setattr(
        C, "_RENAMED_PROVIDERS", {**C._RENAMED_PROVIDERS, "glm51": "glm", "glm": "glm-v2"}
    )
    base = {"providers": {"glm-v2": {"type": "zenmux", "model": "m"}}, "call_sites": {}}

    result = C._sanitize_local_overlay(
        base, {"providers": {"glm51": {"rpm_limit": 1}, "glm": {"rpm_limit": 99}}}
    )

    assert result["providers"]["glm-v2"]["rpm_limit"] == 99, (
        "the OLDEST generation's override survived — the collision rule says the "
        "replacement is the operator's current intent, and the walk order broke it"
    )
    assert "glm51" not in result["providers"]
    assert "glm" not in result["providers"]


def test_rename_depth_orders_shallowest_first(monkeypatch):
    """The mechanism the test above depends on, asserted directly.

    Without this, a future edit could satisfy the collision test by some other
    accident of ordering and leave the depth function unbound.
    """
    from genesis.routing import config as C

    monkeypatch.setattr(
        C, "_RENAMED_PROVIDERS", {"a": "b", "b": "c"}
    )

    assert C._rename_depth("a") == 2
    assert C._rename_depth("b") == 1
    assert C._rename_depth("c") == 0
    assert sorted(C._RENAMED_PROVIDERS, key=C._rename_depth) == ["b", "a"]


# ── A rename must not break the PUBLIC inputs ──────────────────────────────
#
# Found in review: the migration was applied to the overlay and nowhere else, so
# a legacy name kept working inside `model_routing.local.yaml` while the SAME
# name failed at the command line. Three boundaries take a provider name from
# outside the config and are therefore in the class; the rest iterate the config
# and cannot see a stale name.


@pytest.mark.parametrize("legacy,current", sorted(_RENAMED_PROVIDERS.items()))
def test_a_legacy_name_still_resolves_at_a_lookup_boundary(legacy, current):
    """Every alias resolves to a provider the SHIPPED config actually defines.

    Parametrized over the whole map rather than a sample, so adding a rename
    without its target failing here is not possible.
    """
    from pathlib import Path

    from genesis.routing.config import _current_provider_name

    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root / "config" / "model_routing.yaml", check_api_keys=False)

    assert _current_provider_name(legacy) == current
    assert current in cfg.providers, (
        f"'{legacy}' migrates to '{current}', which the shipped config does not "
        "define — the alias is a dead end"
    )


def test_the_eval_boundaries_resolve_aliases():
    """The three sites that accept an external name all call the resolver.

    Asserted on the SOURCE rather than by driving each CLI, because the failure
    being guarded is a site that forgets the call — which a behavioural test of
    the other two would not reveal. The enumeration is stated rather than implied:
    these are the boundaries that take a provider name as an argument or a flag.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "genesis" / "eval"
    for name in ("runner.py", "cli.py", "surplus_executor.py"):
        text = (src / name).read_text()
        assert re.search(r"_current_provider_name\s*\(", text), (
            f"eval/{name} looks up a provider by a name it was given but does not "
            "resolve legacy aliases — a rename breaks it while the same name keeps "
            "working inside a local overlay"
        )
