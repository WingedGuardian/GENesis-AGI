"""R2 — call-site metadata hygiene + dispatch-gate behavior.

Locks three things the 2026-06-19 hygiene pass fixed:
  * the 8 previously metadata-blind sites now carry accurate metadata and
    auto-derive their cost (no frozen/wrong manual label);
  * meta `dispatch`/`cc_model` no longer drift from authoritative YAML
    (the broadened drift guard would have caught the 27 mislabel);
  * both dispatch-consuming gates in call_sites.py — cost_policy retention
    (line 180) and CC chain-entry insertion (line 273) — treat cli/dual as
    CC-dispatch, via the shared `_CC_DISPATCH` set.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

import genesis.routing.config as routing_config_mod
from genesis.observability._call_site_meta import _CALL_SITE_META
from genesis.observability.snapshots.call_sites import _CC_DISPATCH, call_sites
from genesis.routing.types import (
    CallSiteConfig,
    ProviderConfig,
    ProviderState,
    RetryPolicy,
    RoutingConfig,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_YAML_PATH = _REPO_ROOT / "config" / "model_routing.yaml"


def _live_config() -> RoutingConfig:
    return routing_config_mod.load_config_from_string(_YAML_PATH.read_text())


# The 8 sites that were absent from _CALL_SITE_META before this pass.
_NEWLY_DOCUMENTED = [
    "judge",
    "voice_conversation",
    "21_session_observer",
    "44_task_premortem",
    "45_intelligence_intake",
    "41_resume_review_pass1",
    "42_resume_review_pass2",
    "failure_exit_gate",
]


# ── A: the 8 formerly-blind sites ───────────────────────────────────────────


@pytest.mark.parametrize("site_id", _NEWLY_DOCUMENTED)
def test_newly_documented_sites_have_core_metadata(site_id: str):
    meta = _CALL_SITE_META.get(site_id)
    assert meta is not None, f"{site_id} is still blind in _CALL_SITE_META"
    assert meta.get("description"), f"{site_id} missing description"
    assert meta.get("category"), f"{site_id} missing category"
    assert meta.get("model_tier"), f"{site_id} missing model_tier"


@pytest.mark.parametrize("site_id", _NEWLY_DOCUMENTED)
def test_newly_documented_sites_auto_derive_cost(site_id: str):
    # These are api/dual sites, not CC-subscription. They must NOT carry a manual
    # dispatch/cost_policy — that would freeze a (possibly stale) cost label and
    # bypass auto-derivation. They also must not be wired=False (all are live).
    meta = _CALL_SITE_META[site_id]
    assert "dispatch" not in meta, f"{site_id} should omit dispatch (auto-derive)"
    assert "cost_policy" not in meta, f"{site_id} should omit manual cost_policy"
    assert meta.get("wired") is not False, f"{site_id} is live; must not be wired=False"


# ── B: drift guard (would have caught the 27_pre_execution_assessment mislabel) ─


def test_meta_dispatch_matches_authoritative_yaml():
    """Every meta entry that sets `dispatch` must agree with the normalized YAML
    dispatch (YAML is what routing actually uses). This catches a meta entry that
    claims a CC mode for a site that really routes via API (the 27 bug)."""
    cfg = _live_config()
    mismatches = []
    for sid, meta in _CALL_SITE_META.items():
        md = meta.get("dispatch")
        if md is None:
            continue
        cs = cfg.call_sites.get(sid)
        assert cs is not None, f"{sid} sets meta dispatch={md!r} but is not a YAML call site"
        if md != cs.dispatch:
            mismatches.append((sid, md, cs.dispatch))
    assert not mismatches, f"meta/YAML dispatch drift: {mismatches}"


def test_meta_cc_model_matches_yaml():
    raw = yaml.safe_load(_YAML_PATH.read_text())["call_sites"]
    mismatches = []
    for sid, meta in _CALL_SITE_META.items():
        mcc = meta.get("cc_model")
        rcc = raw.get(sid, {}).get("cc_model")
        if mcc is not None and rcc is not None and mcc != rcc:
            mismatches.append((sid, mcc, rcc))
    assert not mismatches, f"meta/YAML cc_model drift: {mismatches}"


def test_meta_dispatch_uses_canonical_vocab_only():
    """No meta entry may use the retired `cc` alias or any non-canonical mode."""
    bad = {
        k: v["dispatch"]
        for k, v in _CALL_SITE_META.items()
        if v.get("dispatch") and v["dispatch"] not in routing_config_mod._VALID_DISPATCH_MODES
    }
    assert not bad, f"non-canonical/retired dispatch values in meta: {bad}"


def test_27_pre_execution_assessment_not_mislabeled_as_cc():
    """Regression lock: 27 is a dual API site (real chain), not a CC/Opus site."""
    meta = _CALL_SITE_META["27_pre_execution_assessment"]
    assert "dispatch" not in meta
    assert "cc_model" not in meta


def test_cc_dispatch_set_contents():
    """Both gates key off this RUNTIME set; lock its contents (cli + dual + the
    retired cc alias, kept defensively).

    NOTE the deliberate asymmetry with ``test_meta_dispatch_uses_canonical_vocab_only``:
    ``"cc"`` belongs in this runtime set (so a stray legacy value can't silently
    mislabel cost) but is NOT a valid value to AUTHOR in ``_CALL_SITE_META`` — the
    canonical-vocab test enforces that meta entries use only {api, cli, dual}.
    These are not contradictory: one guards runtime tolerance, the other guards
    hand-authored hygiene."""
    assert sorted(_CC_DISPATCH) == ["cc", "cli", "dual"]


# ── C/E: the two dispatch gates in call_sites() ─────────────────────────────


def _provider(name: str, *, free: bool) -> ProviderConfig:
    return ProviderConfig(
        name=name, provider_type="test", model_id="m",
        is_free=free, rpm_limit=None, open_duration_s=120,
    )


def _config(call_sites_map: dict, providers: dict) -> RoutingConfig:
    return RoutingConfig(
        providers=providers,
        call_sites=call_sites_map,
        retry_profiles={"default": RetryPolicy()},
    )


def _registry() -> MagicMock:
    def _breaker(_name: str) -> MagicMock:
        cb = MagicMock()
        cb.state = ProviderState.CLOSED
        cb.consecutive_failures = 0
        cb.trip_count = 0
        return cb

    reg = MagicMock()
    reg.get.side_effect = _breaker
    return reg


@pytest.mark.asyncio
async def test_cli_site_retains_manual_cost_policy():
    """cli sites keep their manual 'CC background' label instead of auto-deriving
    a misleading 'Paid primary' (regression originally caught via 7_ego_cycle,
    now removed from YAML — 6_strategic_reflection is an equivalent cli+Opus site)."""
    providers = {
        "openrouter-sonnet": _provider("openrouter-sonnet", free=False),
        "openrouter-opus": _provider("openrouter-opus", free=False),
    }
    cfg = _config(
        {
            "5_deep_reflection": CallSiteConfig(id="5_deep_reflection", chain=["openrouter-sonnet"]),
            "6_strategic_reflection": CallSiteConfig(id="6_strategic_reflection", chain=["openrouter-opus"]),
        },
        providers,
    )
    result = await call_sites(db=None, routing_config=cfg, breakers=_registry())
    assert result["5_deep_reflection"]["cost_policy"] == "CC background (Sonnet)"
    assert result["6_strategic_reflection"]["cost_policy"] == "CC background (Opus)"
    assert "Paid primary" not in result["6_strategic_reflection"]["cost_policy"]


@pytest.mark.asyncio
async def test_deprecated_removed_site_not_resurrected_by_stale_last_run():
    """A DEPRECATED_REMOVED site (e.g. 7_ego_cycle, superseded by the #26 ego
    split) must NOT reappear as an active tile off an old call_site_last_run row.
    Removing it from YAML is not enough — a stale historical row would otherwise
    keep resurrecting it. See snapshots/call_sites.py deprecated-skip guard."""
    import aiosqlite

    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE TABLE call_site_last_run (call_site_id TEXT PRIMARY KEY, "
        "last_run_at TEXT, provider_used TEXT, model_id TEXT, response_text TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, success INTEGER)"
    )
    await db.execute(
        "INSERT INTO call_site_last_run VALUES "
        "('7_ego_cycle', '2026-04-24T16:46:51+00:00', 'cc', 'claude-opus-4-6', 'x', 10, 20, 1)"
    )
    await db.commit()
    # 7_ego_cycle is NOT in routing config (removed from YAML) — only the stale row.
    cfg = _config({}, {})
    result = await call_sites(db=db, routing_config=cfg, breakers=_registry())
    await db.close()
    assert "7_ego_cycle" not in result, (
        "DEPRECATED_REMOVED site resurrected by a stale last_run row"
    )


@pytest.mark.asyncio
async def test_non_yaml_last_run_site_carries_its_meta():
    """A CC-dispatched (non-YAML) site like 7_genesis_ego_cycle enters the snapshot
    ONLY via the call_site_last_run merge; it must still carry its _CALL_SITE_META
    (description/category/cost_policy) so the monitor tile is not a bare stub."""
    import aiosqlite

    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE TABLE call_site_last_run (call_site_id TEXT PRIMARY KEY, "
        "last_run_at TEXT, provider_used TEXT, model_id TEXT, response_text TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, success INTEGER)"
    )
    await db.execute(
        "INSERT INTO call_site_last_run VALUES "
        "('7_genesis_ego_cycle', '2026-07-14T02:19:00+00:00', 'cc', 'claude-sonnet-5', 'x', 10, 20, 1)"
    )
    await db.commit()
    cfg = _config({}, {})  # 7_genesis_ego_cycle is deliberately NOT a YAML site
    result = await call_sites(db=db, routing_config=cfg, breakers=_registry())
    await db.close()
    entry = result["7_genesis_ego_cycle"]
    assert entry["status"] == "active"
    assert entry.get("description")  # meta merged in via the last_run else-branch
    assert entry.get("category") == "reasoning"
    assert entry.get("cost_policy") == "CC background (Sonnet)"


@pytest.mark.asyncio
async def test_cli_site_shows_cc_chain_entry():
    """A cli site renders a CC/{model} entry in chain_health (line-273 gate)."""
    providers = {"openrouter-sonnet": _provider("openrouter-sonnet", free=False)}
    cfg = _config(
        {"5_deep_reflection": CallSiteConfig(id="5_deep_reflection", chain=["openrouter-sonnet"])},
        providers,
    )
    result = await call_sites(db=None, routing_config=cfg, breakers=_registry())
    chain = result["5_deep_reflection"]["chain_health"]
    assert any(entry.get("is_cc") for entry in chain), f"no CC entry in chain: {chain}"


@pytest.mark.asyncio
async def test_api_site_auto_derives_and_has_no_cc_entry():
    """An api site (judge) auto-derives 'Paid primary' and gets no CC entry."""
    providers = {"openrouter-deepseek-v4": _provider("openrouter-deepseek-v4", free=False)}
    cfg = _config(
        {"judge": CallSiteConfig(id="judge", chain=["openrouter-deepseek-v4"])},
        providers,
    )
    result = await call_sites(db=None, routing_config=cfg, breakers=_registry())
    assert result["judge"]["cost_policy"].startswith("Paid primary")
    chain = result["judge"]["chain_health"]
    assert not any(entry.get("is_cc") for entry in chain)


# ── D: Model Fusion — non-routing node, seeded idle, active with a run ───────
# The deliberate MCP tool (OpenRouter Fusion, raw httpx — not the router) is not
# a routing call site, so it would never appear until its first run. call_sites()
# seeds it idle so it is ALWAYS present on the monitor, then flips to active once
# a call_site_last_run row exists.


@pytest.mark.asyncio
async def test_model_fusion_seeded_idle_without_run():
    cfg = _config({}, {})
    result = await call_sites(db=None, routing_config=cfg, breakers=_registry())
    mf = result.get("model_fusion")
    assert mf is not None, "model_fusion node missing from snapshot"
    assert mf["status"] == "idle"
    assert mf["routing"] is False
    # Backend meta is layered onto the seed → self-describing payload.
    assert mf["category"] == "reasoning"
    assert mf["cost_policy"] == "Paid (OpenRouter Fusion)"
    # The monitor's detail panel/tooltip key off status_reason == "WIRED" to render
    # this idle-but-wired on-demand node as "not yet run" instead of "groundwork".
    # Lock it so the seed can't silently drop the flag the frontend depends on.
    assert mf["status_reason"] == "WIRED"


@pytest.mark.asyncio
async def test_model_fusion_active_with_recorded_run(tmp_path):
    import aiosqlite

    from genesis.observability.call_site_recorder import record_last_run

    path = tmp_path / "cs.db"
    async with aiosqlite.connect(str(path)) as conn:
        await conn.execute(
            """CREATE TABLE call_site_last_run (
                call_site_id TEXT PRIMARY KEY, last_run_at TEXT NOT NULL,
                provider_used TEXT, model_id TEXT, response_text TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                success INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)"""
        )
        await conn.commit()
        await record_last_run(
            conn, "model_fusion", provider="openrouter",
            model_id="fusion:budget", response_text="verdict text",
        )
        result = await call_sites(db=conn, routing_config=_config({}, {}), breakers=_registry())
    mf = result["model_fusion"]
    assert mf["status"] == "active"  # seed no-ops; the recorded row drives active
    assert mf["routing"] is False
    assert mf["last_run_model"] == "fusion:budget"
    assert mf["last_response"] == "verdict text"
