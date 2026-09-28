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


def test_the_external_name_boundaries_resolve_aliases():
    """Every site that accepts a provider name from OUTSIDE the config calls the
    CONDITIONAL resolver.

    Asserted on the SOURCE rather than by driving each entry point, because the
    failure being guarded is a site that forgets the call — which a behavioural
    test of the others would not reveal. The enumeration is stated rather than
    implied, and it GREW: `standalone_router` was recorded as not needing this on
    the grounds that it "looks up a name it just read FROM the config". That was
    wrong — `_resolve` takes a caller's name, and two reviewers found it
    independently. A claim about which sites are in the class is only as good as
    the reading behind it.

    It requires `_resolve_provider_alias`, NOT the bare `_current_provider_name`:
    the unconditional form is itself the defect, because it rewrites a name a
    caller-supplied config still defines and thereby makes a live provider
    unreachable.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "genesis"
    boundaries = (
        "eval/runner.py",
        "eval/cli.py",
        "eval/surplus_executor.py",
        "experimentation/standalone_router.py",
    )
    for rel in boundaries:
        text = (src / rel).read_text()
        assert re.search(r"_resolve_provider_alias\s*\(", text), (
            f"{rel} looks up a provider by a name it was GIVEN but does not resolve "
            "legacy aliases conditionally — a rename breaks it while the same name "
            "keeps working inside a local overlay"
        )
        assert not re.search(r"(?<!_resolve_provider_alias)\b_current_provider_name\s*\(", text), (
            f"{rel} still calls _current_provider_name UNCONDITIONALLY — that "
            "rewrites a name a caller-supplied config legitimately defines, making "
            "a live provider unreachable (Devin severe, 2026-09-25)"
        )


def test_a_caller_supplied_config_keeps_its_own_legacy_key():
    """The behavioural arm the source grep cannot give: a config that still
    DEFINES a renamed key must keep working through it.

    `run_eval` accepts a caller-supplied `RoutingConfig`, which need not be the
    shipped file and may legitimately define a key the shipped one retired. The
    unconditional rewrite asked for the NEW name, did not find it, and raised —
    so a provider present under its own key became unreachable.
    """
    from genesis.routing.config import _resolve_provider_alias

    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))

    # Config defines the LEGACY key only: it is live, so leave it alone.
    assert _resolve_provider_alias(legacy, {legacy, "groq-free"}) == legacy
    # Config defines the CURRENT key only: this is the upgrade the map exists for.
    assert _resolve_provider_alias(legacy, {current, "groq-free"}) == current
    # Config defines BOTH: the exact key wins — it cannot be the stale one.
    assert _resolve_provider_alias(legacy, {legacy, current}) == legacy
    # Config defines NEITHER: unchanged, so the caller's own error names what the
    # caller actually asked for rather than a substitution it never mentioned.
    assert _resolve_provider_alias(legacy, {"groq-free"}) == legacy
    # A name with no alias at all is never touched.
    assert _resolve_provider_alias("groq-free", {"groq-free"}) == "groq-free"


# ── RC1: the alias applies only to a key the BASE has retired ────────────────


_BASE_LIVE = """
providers:
  glm51:
    type: openrouter
    model: z-ai/glm-5.1
    rpm_limit: 5
  groq-free:
    type: groq
    model: llama-3.3-70b
retry_profiles:
  default:
    max_attempts: 2
call_sites:
  triage:
    chain: [groq-free]
"""

_BASE_RETIRED = _BASE_LIVE.replace("  glm51:", "  glm:").replace(
    "z-ai/glm-5.1", "z-ai/glm-5.3"
)


def _load_with_overlay(tmp_path, base_text: str, overlay_text: str):
    base = tmp_path / "model_routing.yaml"
    base.write_text(base_text)
    (tmp_path / "model_routing.local.yaml").write_text(overlay_text)
    return load_config(base, check_api_keys=False)


def test_an_override_on_a_key_the_base_still_defines_SURVIVES(tmp_path):
    """The defect, from the operator's side.

    `load_config` takes a caller-supplied path, so the base need not be the
    shipped file and may legitimately define a key the shipped one renamed. The
    migration used to fire regardless: it popped the override, retargeted it at
    the new name, found the base lacked that name, and dropped it. The load
    SUCCEEDED, so nothing failed — the customization was just replaced by the
    base default with a warning to show for it.

    MEASURED before the guard: rpm_limit came back 5 (the base value) instead of
    the declared 99.
    """
    cfg = _load_with_overlay(
        tmp_path, _BASE_LIVE, "providers:\n  glm51:\n    rpm_limit: 99\n"
    )
    assert "glm51" in cfg.providers, "the live key was renamed out from under the base"
    assert cfg.providers["glm51"].rpm_limit == 99, "the operator's override was dropped"


def test_a_chain_rung_naming_a_live_key_SURVIVES(tmp_path):
    """The same defect one layer over, and the worse half of it.

    A chain entry was translated unconditionally, so a rung naming a key the base
    still defines was retargeted, failed the stale filter on the next line, and
    was removed from the chain — changing what gets ROUTED rather than one limit.

    MEASURED before the guard: `[glm51, groq-free]` loaded as `['groq-free']`.
    """
    cfg = _load_with_overlay(
        tmp_path,
        _BASE_LIVE,
        "call_sites:\n  triage:\n    chain: [glm51, groq-free]\n",
    )
    assert cfg.call_sites["triage"].chain == ["glm51", "groq-free"]


def test_a_retired_key_STILL_MIGRATES(tmp_path):
    """The control, and the reason the guard is a narrowing rather than a repeal.

    When the base HAS retired the key, migrating is the whole point of the alias
    map. Without this arm, a fix that simply stopped migrating would pass the two
    tests above and silently break every real upgrade.
    """
    cfg = _load_with_overlay(
        tmp_path, _BASE_RETIRED, "providers:\n  glm51:\n    rpm_limit: 99\n"
    )
    assert "glm51" not in cfg.providers
    assert cfg.providers["glm"].rpm_limit == 99, "the upgrade path stopped working"


def test_a_retired_chain_rung_STILL_MIGRATES(tmp_path):
    """The chain half of the same control."""
    cfg = _load_with_overlay(
        tmp_path,
        _BASE_RETIRED,
        "call_sites:\n  triage:\n    chain: [glm51, groq-free]\n",
    )
    assert cfg.call_sites["triage"].chain == ["glm", "groq-free"]


# ── RC2: state persisted under a provider NAME survives the rename ───────────


def _providers_for(names):
    """Minimal ProviderConfig map keyed by ``names``."""
    from genesis.routing.types import ProviderConfig

    return {
        n: ProviderConfig(
            name=n,
            provider_type="openrouter",
            model_id="x/y",
            is_free=False,
            rpm_limit=None,
            open_duration_s=120,
        )
        for n in names
    }


def _registry(tmp_path, providers, persisted: dict):
    import json

    from genesis.routing.circuit_breaker import CircuitBreakerRegistry

    f = tmp_path / "circuit_breaker_state.json"
    f.write_text(json.dumps(persisted))
    return CircuitBreakerRegistry(providers=providers, state_file=f, persist=False)


@pytest.mark.parametrize("held", ["open", "half_open"])
def test_a_held_breaker_survives_a_rename_across_restart(tmp_path, held):
    """THE finding: a provider being HELD BACK must not resume taking traffic.

    `load_state` matched persisted rows to providers EXACTLY, so after a rename
    the row was silently dropped and the renamed provider got a fresh CLOSED
    breaker. An install with an actively failing provider therefore resumed
    routing to it on the first restart after the upgrade, until enough NEW
    failures tripped it again — reintroducing the latency and paid-fallback churn
    that persisting this state exists to prevent. (Codex P1, Devin severe.)

    Both non-closed states are parametrized because both are persisted: `.state`
    assigns HALF_OPEN when merely READ, so a held provider is as likely to be on
    disk as half_open as open.
    """
    from genesis.routing.types import ProviderState

    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))
    reg = _registry(
        tmp_path,
        _providers_for([current]),
        {legacy: {"state": held, "consecutive_failures": 3, "trip_count": 2}},
    )

    cb = reg._breakers.get(current)
    assert cb is not None, f"the row under '{legacy}' was dropped instead of migrated"
    assert cb._state is not ProviderState.CLOSED, (
        f"'{current}' restored CLOSED from a persisted '{held}' — it would take traffic"
    )
    assert cb._trip_count > 0, "the trip count was lost, so backoff restarts from scratch"


def test_the_current_names_row_wins_when_both_generations_are_persisted(tmp_path):
    """Ordering, stated rather than left to dict order.

    A state file written across an upgrade boundary can hold BOTH keys. One pass
    in dict order would let whichever row came first decide — the same
    order-dependence the overlay migration was already fixed for. The row under
    the CURRENT name is the newer fact and must win, whichever order they appear
    in, so both orderings are asserted.
    """
    from genesis.routing.types import ProviderState

    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))
    for ordering in (
        {legacy: {"state": "open", "trip_count": 9}, current: {"state": "closed", "trip_count": 0}},
        {current: {"state": "closed", "trip_count": 0}, legacy: {"state": "open", "trip_count": 9}},
    ):
        reg = _registry(tmp_path, _providers_for([current]), ordering)
        cb = reg._breakers[current]
        assert cb._state is ProviderState.CLOSED, (
            f"the legacy row overrode the current one (order: {list(ordering)})"
        )
        assert cb._trip_count == 0


def test_an_exact_row_is_untouched_when_no_rename_applies(tmp_path):
    """The control. A fix that migrated indiscriminately would pass the tests
    above while corrupting every ordinary restore."""
    from genesis.routing.types import ProviderState

    reg = _registry(
        tmp_path,
        _providers_for(["groq-free"]),
        {"groq-free": {"state": "open", "trip_count": 4}},
    )
    cb = reg._breakers["groq-free"]
    assert cb._state is ProviderState.OPEN
    # 3, not 4: `load_state` deliberately caps the trip count on an OPEN restore,
    # because escalating backoff is for consecutive failures within a session, not
    # across restarts that may span weeks. Asserting the cap rather than the raw
    # persisted value keeps this a control for the MIGRATION without silently
    # re-specifying behaviour that predates it.
    assert cb._trip_count == 3


def test_a_row_for_a_provider_that_no_longer_exists_is_still_dropped(tmp_path):
    """The other control: migration must not resurrect a genuinely removed
    provider. `_resolve_provider_alias` returns the name unchanged when neither
    generation is configured, and an unknown name has no breaker to create."""
    reg = _registry(
        tmp_path,
        _providers_for(["groq-free"]),
        {"retired-entirely": {"state": "open", "trip_count": 4}},
    )
    assert "retired-entirely" not in reg._breakers


def test_a_held_breaker_survives_a_rename_across_a_HOT_RELOAD(tmp_path):
    """The path a restart test cannot reach, and it needs no restart to lose the hold.

    `update_providers` MERGES rather than replaces, so after a rename the registry
    holds both generations in `_providers` while `_breakers` still holds the live
    breaker under the OLD key — and `Router.reload_config`'s subsequent
    `get(new_name)` creates a fresh CLOSED breaker beside it. The provider resumes
    taking traffic mid-run, with no restart and no sign to the operator.
    """
    from genesis.routing.types import ProviderState

    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))
    reg = _registry(tmp_path, _providers_for([legacy]), {})
    cb = reg.get(legacy)
    cb._state = ProviderState.OPEN
    cb._trip_count = 2

    reg.update_providers(_providers_for([current]))

    assert legacy not in reg._breakers, "the stale key kept the breaker"
    moved = reg._breakers.get(current)
    assert moved is cb, "a NEW breaker was created instead of moving the live one"
    assert moved._state is ProviderState.OPEN
    assert moved._trip_count == 2


def test_a_hot_reload_never_overwrites_an_existing_breaker(tmp_path):
    """The control for the move: an existing breaker under the new name is live
    state and must win. Overwriting it would discard a real hold to honour a
    stale one."""
    from genesis.routing.types import ProviderState

    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))
    reg = _registry(tmp_path, _providers_for([legacy, current]), {})
    old = reg.get(legacy)
    old._state = ProviderState.OPEN
    new = reg.get(current)
    new._trip_count = 7

    reg.update_providers(_providers_for([current]))

    assert reg._breakers[current] is new, "the live breaker was replaced by a stale one"
    assert reg._breakers[current]._trip_count == 7


def test_one_unresolvable_row_does_not_abort_every_other_restore(tmp_path):
    """An unresolvable row must be SKIPPED, not allowed to kill the whole load.

    Found by a mutation sweep, and the failure mode is worse than the one the
    sibling test covers. `_alias_target` returns None when neither generation is
    configured; without that check `get()` is called with an unknown name, raises
    `KeyError` from `self._providers[provider]`, and the blanket `except` around
    `load_state` swallows it — so ONE stale row silently discards EVERY restore in
    the file, including live holds. A single-row fixture cannot see that: it looks
    identical to correctly skipping the row.
    """
    from genesis.routing.types import ProviderState

    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))
    reg = _registry(
        tmp_path,
        _providers_for([current]),
        {
            # Ordered first on purpose: the damage is done before the good row.
            "retired-entirely": {"state": "open", "trip_count": 5},
            legacy: {"state": "open", "trip_count": 2},
        },
    )

    assert "retired-entirely" not in reg._breakers
    cb = reg._breakers.get(current)
    assert cb is not None, (
        "a stale row aborted the whole restore — the live hold on "
        f"'{current}' was lost with it"
    )
    assert cb._state is ProviderState.OPEN


def test_the_legacy_pass_runs_AFTER_the_current_pass(tmp_path):
    """Ordering asserted by its CONSEQUENCE, not by reading the loops.

    The sibling test asserts the current row wins, but it survived a mutation that
    deleted the legacy pass outright — with no legacy pass the current row is the
    only one, so it "wins" trivially. This pins the pass ORDER: both rows carry a
    live state, the legacy one carries a DIFFERENT trip count, and the current
    one's value must be the one that lands whichever order the dict lists them.
    """
    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))
    for ordering in (
        {legacy: {"state": "open", "trip_count": 3}, current: {"state": "open", "trip_count": 1}},
        {current: {"state": "open", "trip_count": 1}, legacy: {"state": "open", "trip_count": 3}},
    ):
        reg = _registry(tmp_path, _providers_for([current]), ordering)
        cb = reg._breakers[current]
        assert cb._trip_count == 1, (
            f"the legacy row's trip count landed (order: {list(ordering)}) — the "
            "legacy pass ran first, or the already-restored guard did not hold"
        )


def test_the_standalone_router_resolves_a_legacy_name_BEHAVIOURALLY(tmp_path):
    """The source grep cannot say WHICH site calls the resolver — this can.

    Found by a mutation sweep: removing `_resolve`'s call left the file's OTHER
    call (in `default_judge_chain`) in place, so the grep-based boundary test
    stayed green while the boundary under test was broken. Drives the constructor
    with a legacy name against a config that defines only the current one.
    """
    from genesis.experimentation.standalone_router import StandaloneLiteLLMRouter

    legacy, current = next(iter(sorted(_RENAMED_PROVIDERS.items())))
    base = tmp_path / "model_routing.yaml"
    base.write_text(
        f"providers:\n"
        f"  {current}:\n"
        f"    type: openrouter\n"
        f"    model: some/model\n"
        f"retry_profiles:\n  default:\n    max_attempts: 2\n"
        f"call_sites:\n  judge:\n    chain: [{current}]\n"
    )
    cfg = load_config(base, check_api_keys=False)

    router = StandaloneLiteLLMRouter(provider_name=legacy, config=cfg)

    assert router._provider_name == current, (
        f"a saved experiment naming '{legacy}' did not resolve to '{current}' — "
        "it would raise 'unknown provider' before issuing a call"
    )
