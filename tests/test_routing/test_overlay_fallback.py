"""A malformed local routing overlay must never take routing dark.

`runtime/init/router.py` catches any exception out of `load_config` and leaves
the runtime with no router, so every LLM call site goes dark behind one log line.
The overlay (`model_routing.local.yaml`) is the one routing input CI never sees,
so it is the one that can do this.

The design here is a chokepoint, not a list of shapes:

  * `load_config` parses base + overlay. If that fails for ANY reason, the whole
    overlay is set aside, the failure is logged at ERROR and recorded as a health
    observation, and the base is parsed instead. The base is validated in CI.
    The overlay's EXCLUSIONS (a disabled or not-free provider, `never_pays`, a
    narrowed chain) still apply to that base, so a typo cannot undo them.
  * `_parse` raises on malformed entries and wrongly typed fields rather than
    skipping them, so the chokepoint sees every error it has to contain.
  * One shape does not raise by itself: a non-mapping SECTION (a bare
    `call_sites:`) replaces the base section and `_parse` loads zero call sites.
    `_merge_overlay` refuses it so the fallback can catch it.
  * The dashboard save validates the exact text it is about to write, through the
    same merge and parse, so a save that passes cannot produce a file the next
    boot rejects.

Several tests below were carried over from PR #2215, which tried per-shape
guards first. Where that design's behaviour and this one's differ (a malformed
entry now costs the whole overlay rather than one entry), the assertion follows
this design and says so.
"""

from __future__ import annotations

import logging
import os
import textwrap
from pathlib import Path

import pytest
import yaml

import genesis.routing.config as C
from genesis.routing.config import load_config, update_call_site_in_yaml

_BASE = textwrap.dedent(
    """
    providers:
      paid-a:
        type: zenmux
        model: vendor/model-a
        free: false
      free-b:
        type: mistral
        model: vendor/model-b
        free: true
        params:
          reasoning_effort: disable
    call_sites:
      31_outcome_classification:
        chain: [paid-a, free-b]
        retry_profile: background
      32_other:
        chain: [free-b]
    retry:
      default:
        max_retries: 3
        max_total_s: 300
      background:
        max_retries: 5
        max_total_s: 600
    """
).strip()

SITE = "31_outcome_classification"


@pytest.fixture(autouse=True)
def _fresh_report_state():
    C._REPORTED_OVERLAYS.clear()
    yield
    C._REPORTED_OVERLAYS.clear()


@pytest.fixture
def recorded(monkeypatch):
    """Capture health observations instead of writing to any database."""
    rows: list[dict] = []

    def fake_create_sync(db_path, **kw):
        rows.append(kw)
        return True

    monkeypatch.setattr("genesis.db.crud.observations.create_sync", fake_create_sync)
    return rows


def _write(tmp_path: Path, local: str | None, base: str = _BASE) -> Path:
    cfg = tmp_path / "model_routing.yaml"
    cfg.write_text(base + "\n")
    if local is not None:
        (tmp_path / "model_routing.local.yaml").write_text(local)
    return cfg


def _snapshot(config):
    return (config.providers, config.call_sites, config.retry_profiles)


def _base_only(tmp_path: Path):
    ref = tmp_path / "ref"
    ref.mkdir()
    return load_config(_write(ref, None), check_api_keys=False)


# Every shape here must fall back to the base. The ids name the shape.
MALFORMED = {
    # the whole file
    "yaml_syntax_error": "providers: [unclosed\n",
    "top_level_list": "- a\n- b\n",
    "top_level_scalar": "nonsense\n",
    # a section that is not a mapping (these replace the base section)
    "providers_none": "providers:\n",
    "call_sites_none": "call_sites:\n",
    "retry_none": "retry:\n",
    "call_sites_empty_list": "call_sites: []\n",
    "call_sites_false": "call_sites: false\n",
    "providers_str": "providers: oops\n",
    "retry_int": "retry: 5\n",
    # an entry that is not a mapping, known and new names
    "call_site_none": f"call_sites:\n  {SITE}:\n",
    "call_site_str": f"call_sites:\n  {SITE}: oops\n",
    "call_site_list": f"call_sites:\n  {SITE}: [paid-a]\n",
    "provider_none": "providers:\n  paid-a:\n",
    "provider_new_none": "providers:\n  brand-new:\n",
    "provider_str": "providers:\n  paid-a: oops\n",
    "retry_none_entry": "retry:\n  background:\n",
    "retry_new_none": "retry:\n  custom:\n",
    "retry_new_str": "retry:\n  custom: oops\n",
    "retry_default_none": "retry:\n  default:\n",
    # fields
    "chain_none": f"call_sites:\n  {SITE}:\n    chain:\n",
    "chain_int": f"call_sites:\n  {SITE}:\n    chain: 5\n",
    "chain_str": f"call_sites:\n  {SITE}:\n    chain: paid-a\n",
    "retry_profile_typo": f"call_sites:\n  {SITE}:\n    retry_profile: backgorund\n",
    "retry_profile_mapping": f"call_sites:\n  {SITE}:\n    retry_profile:\n      a: 1\n",
    "max_total_s_str": "retry:\n  background:\n    max_total_s: x\n",
    "default_max_total_s_str": "retry:\n  default:\n    max_total_s: x\n",
    "max_retries_float": "retry:\n  background:\n    max_retries: 1.5\n",
    "max_retries_bool": "retry:\n  background:\n    max_retries: true\n",
    "rpm_limit_str": "providers:\n  paid-a:\n    rpm_limit: fast\n",
    "open_duration_negative": "providers:\n  paid-a:\n    open_duration_s: -1\n",
    "free_string": "providers:\n  free-b:\n    free: 'false'\n",
    "never_pays_string": f"call_sites:\n  {SITE}:\n    never_pays: 'no'\n",
    "never_pays_zero": f"call_sites:\n  {SITE}:\n    never_pays: 0\n",
    "params_list": "providers:\n  free-b:\n    params: [a]\n",
    "provider_new_without_type": "providers:\n  brand-new:\n    rpm_limit: 3\n",
    "rpd_limit_zero": "providers:\n  paid-a:\n    rpd_limit: 0\n",
    # NaN compares False to everything: an unbounded deadline, a stuck breaker
    "max_total_s_nan": "retry:\n  default:\n    max_total_s: .nan\n",
    "max_total_s_inf": "retry:\n  background:\n    max_total_s: .inf\n",
    "open_duration_nan": "providers:\n  paid-a:\n    open_duration_s: .nan\n",
    # a falsy value is a malformed overlay, not an absent one
    "top_level_false": "false\n",
    "top_level_zero": "0\n",
    "top_level_empty_list": "[]\n",
    "top_level_empty_str": "''\n",
    # only null means the default profile; an empty name is a typo
    "retry_profile_empty": f"call_sites:\n  {SITE}:\n    retry_profile: ''\n",
}

#: Shapes that also carry an exclusion the fallback keeps, mapped to the base
#: edit that expresses it. Where the overlay names a provider or call site but
#: its value there is unreadable, the fallback reads it restrictively.
EXCLUDING = {
    "call_site_str": lambda raw: raw["call_sites"][SITE].update(chain=[]),
    "call_site_list": lambda raw: raw["call_sites"][SITE].update(chain=[]),
    "chain_int": lambda raw: raw["call_sites"][SITE].update(chain=[]),
    "chain_str": lambda raw: raw["call_sites"][SITE].update(chain=[]),
    "provider_str": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
    "free_string": lambda raw: raw["providers"]["free-b"].update(free=False),
    "never_pays_string": lambda raw: raw["call_sites"][SITE].update(never_pays=True),
    "never_pays_zero": lambda raw: raw["call_sites"][SITE].update(never_pays=True),
    # an unreadable limit on a named provider disables it
    "rpm_limit_str": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
    "rpd_limit_zero": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
}
NO_EXCLUSION = [k for k in MALFORMED if k not in EXCLUDING]


def _base_edited(tmp_path: Path, edit):
    ref = tmp_path / "edited"
    ref.mkdir()
    raw = yaml.safe_load(_BASE)
    edit(raw)
    return load_config(_write(ref, None, base=yaml.safe_dump(raw)), check_api_keys=False)


@pytest.mark.parametrize("shape", NO_EXCLUSION)
def test_a_malformed_overlay_falls_back_to_the_base(tmp_path, recorded, shape):
    """The whole overlay is set aside and routing runs on the base, intact."""
    loaded = load_config(_write(tmp_path, MALFORMED[shape]), check_api_keys=False)

    assert _snapshot(loaded) == _snapshot(_base_only(tmp_path))
    assert len(recorded) == 1, "the fallback happened without a health observation"


@pytest.mark.parametrize("shape", list(EXCLUDING))
def test_an_unreadable_entry_is_read_restrictively(tmp_path, recorded, shape):
    """A provider or call site the overlay names, with a value there that cannot
    be read, is not handed back to the base's permissive settings."""
    loaded = load_config(_write(tmp_path, MALFORMED[shape]), check_api_keys=False)

    assert _snapshot(loaded) == _snapshot(_base_edited(tmp_path, EXCLUDING[shape]))
    assert len(recorded) == 1


@pytest.mark.parametrize("local", list(MALFORMED.values()), ids=list(MALFORMED))
def test_strict_overlay_raises_for_the_same_shapes(tmp_path, local):
    """The operator-initiated reload path: say the file is broken, load nothing."""
    with pytest.raises(Exception):  # noqa: B017 - the shapes raise different types
        load_config(_write(tmp_path, local), check_api_keys=False, strict_overlay=True)


def test_a_bare_call_sites_header_no_longer_empties_routing_silently(tmp_path, recorded):
    """The one shape the fallback alone could not catch.

    MEASURED on main before this change: `call_sites:` with nothing under it
    loaded ZERO call sites and raised nothing, so there was nothing to fall back
    from. Asserted separately so a regression in `_merge_overlay`'s section check
    is named, not hidden in the parametrized sweep.
    """
    loaded = load_config(_write(tmp_path, "call_sites:\n"), check_api_keys=False)

    assert set(loaded.call_sites) == {SITE, "32_other"}


def test_the_fallback_is_loud(tmp_path, recorded, caplog):
    cfg = _write(tmp_path, f"call_sites:\n  {SITE}:\n    chain: 5\n")

    with caplog.at_level(logging.ERROR, logger="genesis.routing.config"):
        load_config(cfg, check_api_keys=False)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "rejected" in errors[0].getMessage()
    assert "chain must be a list" in errors[0].getMessage()
    row = recorded[0]
    assert row["source"] == "routing"
    assert row["type"] == "init_degradation"
    assert row["priority"] == "high"
    assert "model_routing.local.yaml" in row["content"]


# ── The fallback keeps the overlay's exclusions ──────────────────────────────

#: An unrelated error elsewhere in the file: an undefined retry profile entry.
_UNRELATED_ERROR = "retry:\n  custom:\n"


def test_a_rejected_overlay_keeps_its_disabled_providers(tmp_path, recorded):
    """The reviewer's case: a typo anywhere must not re-enable a provider the
    operator turned off, and so route (and spend) through it."""
    local = "providers:\n  paid-a:\n    enabled: false\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert "paid-a" not in loaded.providers
    assert loaded.call_sites[SITE].chain == ["free-b"]
    assert "provider paid-a disabled" in recorded[0]["content"]


def test_a_rejected_overlay_keeps_never_pays(tmp_path, recorded):
    local = f"call_sites:\n  {SITE}:\n    never_pays: true\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.call_sites[SITE].never_pays is True


def test_a_rejected_overlay_keeps_a_narrowed_chain(tmp_path, recorded):
    local = f"call_sites:\n  {SITE}:\n    chain: [free-b]\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.call_sites[SITE].chain == ["free-b"]


def test_a_rejected_overlay_keeps_a_provider_marked_not_free(tmp_path, recorded):
    """`free: false` is what makes a `never_pays` site skip a provider."""
    local = "providers:\n  free-b:\n    free: false\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.providers["free-b"].is_free is False


def test_a_rejected_overlay_never_widens_the_base(tmp_path, recorded):
    """CONTROL. Only exclusions survive the fallback. Nothing the overlay says
    can enable a provider, mark one free, raise a limit, clear never_pays, move
    a site off the CLI, or add to a chain."""
    base = (
        _BASE.replace("free: false", "free: false\n    enabled: false", 1)
        .replace("free: true", "free: true\n    rpd_limit: 100", 1)
        .replace(
            "    chain: [free-b]\n",
            "    chain: [free-b]\n    never_pays: true\n    dispatch: cli\n",
            1,
        )
    )
    local = textwrap.dedent(
        """
        providers:
          paid-a:
            enabled: true
            free: true
          free-b:
            rpd_limit: 500
        call_sites:
          32_other:
            never_pays: false
            chain: [paid-a, free-b]
            dispatch: dual
        """
    ) + _UNRELATED_ERROR
    ref = tmp_path / "ref"
    ref.mkdir()
    expected = load_config(_write(ref, None, base=base), check_api_keys=False)

    loaded = load_config(_write(tmp_path, local, base=base), check_api_keys=False)

    assert "paid-a" not in expected.providers, "fixture: the base must disable paid-a"
    assert expected.call_sites["32_other"].never_pays is True, "fixture: base never_pays"
    assert expected.call_sites["32_other"].dispatch == "cli", "fixture: base dispatch"
    assert expected.providers["free-b"].rpd_limit == 100, "fixture: base rpd_limit"
    assert _snapshot(loaded) == _snapshot(expected)
    assert "no exclusions" in recorded[0]["content"]


def test_a_rejected_overlay_keeps_cli_dispatch(tmp_path, recorded):
    """`dispatch: cli` keeps a site off the API chain. The dashboard's dispatch
    selector writes exactly this key, so it is common overlay content."""
    local = f"call_sites:\n  {SITE}:\n    dispatch: cli\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.call_sites[SITE].dispatch == "cli"


def test_a_rejected_overlay_keeps_a_lower_daily_limit(tmp_path, recorded):
    local = "providers:\n  paid-a:\n    rpd_limit: 5\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.providers["paid-a"].rpd_limit == 5


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [("tpd_limit", "5000", 5000), ("rpm_limit", "0.5", 0.5), ("rpd_limit", "10" * 200, int("10" * 200))],
    ids=["tpd", "rpm_float", "rpd_huge_int"],
)
def test_each_limit_takes_the_lower_value(tmp_path, recorded, field, value, expected):
    """Every limit the accepted path accepts is read the same way here, including
    an integer too large for a float (which once raised out of the fallback)."""
    local = f"providers:\n  paid-a:\n    {field}: {value}\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert getattr(loaded.providers["paid-a"], field) == expected


def test_an_rpm_limit_of_zero_is_no_limit_not_an_unreadable_one(tmp_path, recorded):
    """`rpm_limit: 0` is valid on the accepted path and turns the rate gate off.
    It must not read as unreadable and disable the provider."""
    local = "providers:\n  paid-a:\n    rpm_limit: 0\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert "paid-a" in loaded.providers
    assert loaded.providers["paid-a"].rpm_limit is None


def test_a_base_rpm_of_zero_is_narrowed_by_the_overlay(tmp_path, recorded):
    base = _BASE.replace("free: false", "free: false\n    rpm_limit: 0", 1)
    local = "providers:\n  paid-a:\n    rpm_limit: 5\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local, base=base), check_api_keys=False)

    assert loaded.providers["paid-a"].rpm_limit == 5


def test_a_defect_in_the_exclusions_code_does_not_take_routing_dark(
    tmp_path, recorded, monkeypatch
):
    def broken(*_a, **_k):
        raise RuntimeError("defect")

    monkeypatch.setattr(C, "_restrict_base", broken)
    loaded = load_config(_write(tmp_path, _UNRELATED_ERROR), check_api_keys=False)

    assert _snapshot(loaded) == _snapshot(_base_only(tmp_path))
    assert "could NOT be applied" in recorded[0]["content"]


def test_a_rejected_overlay_keeps_the_order_of_a_narrowed_chain(tmp_path, recorded):
    """Order decides which provider is tried, and paid, first."""
    local = f"call_sites:\n  {SITE}:\n    chain: [free-b, paid-a]\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.call_sites[SITE].chain == ["free-b", "paid-a"]


def test_a_change_in_the_kept_exclusions_is_reported_again(tmp_path, recorded):
    """The base can change under an unchanged overlay and change what is kept."""
    local_path = tmp_path / "model_routing.local.yaml"
    local_path.write_text("retry:\n  custom:\n")
    C._report_overlay_rejected(local_path, ValueError("same"), ["provider a disabled"])
    C._report_overlay_rejected(local_path, ValueError("same"), ["provider a disabled"])
    C._report_overlay_rejected(local_path, ValueError("same"), [])

    assert len(recorded) == 2


def test_a_chain_naming_only_unknown_providers_is_no_override(tmp_path, recorded):
    """Mirrors the accepted path, where `_sanitize_local_overlay` drops a chain
    left with no provider the base defines."""
    local = f"call_sites:\n  {SITE}:\n    chain: [gone-provider]\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.call_sites[SITE].chain == ["paid-a", "free-b"]


@pytest.mark.parametrize("local", ["", "# only a comment\n"], ids=["empty", "comment"])
def test_an_empty_overlay_file_is_not_a_fallback(tmp_path, recorded, local):
    """CONTROL for the falsy shapes: an empty file holds nothing, it is not broken."""
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert _snapshot(loaded) == _snapshot(_base_only(tmp_path))
    assert recorded == []


def test_each_file_version_is_recorded_despite_the_real_dedup(tmp_path, monkeypatch):
    """Against the real create_sync and table: it dedups unresolved rows on the
    content hash, so an edited file with the same error text must hash apart."""
    import sqlite3

    from genesis.db.schema import TABLES

    db = tmp_path / "genesis.db"
    conn = sqlite3.connect(db)
    conn.executescript(TABLES["observations"])
    conn.close()
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)
    cfg = _write(tmp_path, "- a\n")
    local = tmp_path / "model_routing.local.yaml"

    load_config(cfg, check_api_keys=False)
    stat = local.stat()
    os.utime(local, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    load_config(cfg, check_api_keys=False)

    rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert rows == 2


def test_the_fallback_is_reported_once_per_file_version(tmp_path, recorded):
    """load_config runs on every dashboard vitals read. One broken file must not
    become one error line and one database write per request."""
    cfg = _write(tmp_path, "retry:\n  custom:\n")
    local = tmp_path / "model_routing.local.yaml"

    load_config(cfg, check_api_keys=False)
    load_config(cfg, check_api_keys=False)
    assert len(recorded) == 1

    stat = local.stat()
    os.utime(local, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    load_config(cfg, check_api_keys=False)
    assert len(recorded) == 2, "an edited (still broken) file was not reported again"


def test_a_broken_base_still_raises(tmp_path, recorded):
    """CONTROL. The fallback trusts the base because CI validates it. It must not
    swallow a broken base, or it would hide exactly the error CI exists to catch."""
    broken = _BASE.replace("type: zenmux", "type: [zenmux]")

    with pytest.raises(ValueError, match="type must be a non-empty string"):
        load_config(_write(tmp_path, "call_sites:\n", base=broken), check_api_keys=False)


def test_the_effective_raw_matches_the_config_that_was_loaded(tmp_path, recorded):
    """The dashboard's CC display reads raw fields through `_load_effective`, so
    a rejected overlay's cc_model must not be shown as if it were in effect."""
    local = f"call_sites:\n  {SITE}:\n    cc_model: Opus\n    chain: 5\n"
    _, raw = C._load_effective(_write(tmp_path, local), check_api_keys=False)

    assert "cc_model" not in raw["call_sites"][SITE]


# ── Controls: valid overlays still apply ──────────────────────────────────────


def test_a_valid_overlay_is_still_applied(tmp_path, recorded):
    local = textwrap.dedent(
        f"""
        providers:
          paid-a:
            rpm_limit: 7
        call_sites:
          {SITE}:
            chain: [free-b]
        retry:
          background:
            max_retries: 9
        """
    )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.providers["paid-a"].rpm_limit == 7
    assert loaded.call_sites[SITE].chain == ["free-b"]
    assert loaded.retry_profiles["background"].max_retries == 9
    assert recorded == []


def test_an_overlay_can_clear_an_inherited_params_map(tmp_path, recorded):
    """Carried from #2215, where a merge-level guard once removed this capability.
    `params: null` is an operator clearing the shipped map, not a malformed value."""
    loaded = load_config(
        _write(tmp_path, "providers:\n  free-b:\n    params: null\n"), check_api_keys=False
    )

    assert loaded.providers["free-b"].params is None
    assert recorded == []


def test_a_null_retry_profile_clears_to_default(tmp_path, recorded):
    """Carried from #2215. `retry_profile: null` is the same clearing idiom."""
    loaded = load_config(
        _write(tmp_path, f"call_sites:\n  {SITE}:\n    retry_profile:\n"),
        check_api_keys=False,
    )

    assert loaded.call_sites[SITE].retry_profile == "default"
    assert recorded == []


def test_a_null_flag_clears_to_false(tmp_path, recorded):
    """Same idiom for the boolean fields, whose default is false."""
    loaded = load_config(
        _write(tmp_path, f"call_sites:\n  {SITE}:\n    never_pays:\n    default_paid:\n"),
        check_api_keys=False,
    )

    assert loaded.call_sites[SITE].never_pays is False
    assert loaded.call_sites[SITE].default_paid is False
    assert recorded == []


def test_an_explicit_null_max_total_s_is_honoured(tmp_path, recorded):
    """None means "no aggregate cap" and is a documented choice, not a malformed
    value. The bound below is about MALFORMED defaults, never this."""
    loaded = load_config(
        _write(tmp_path, "retry:\n  background:\n    max_total_s: null\n"),
        check_api_keys=False,
    )

    assert loaded.retry_profiles["background"].max_total_s is None


def test_deep_merge_still_applies_non_mapping_values_at_field_depth():
    """CONTROL, carried from #2215: overwriting a scalar, a list or an absent key
    is the merge's whole job."""
    merged = C._deep_merge(
        {"free": True, "chain": ["a"], "n": 1},
        {"free": None, "chain": ["b"], "n": 2, "brand_new": None},
    )

    assert merged == {"free": None, "chain": ["b"], "n": 2, "brand_new": None}


# ── The default retry profile stays bounded ───────────────────────────────────


@pytest.mark.parametrize(
    "local",
    [
        "retry:\n  default:\n",
        "retry:\n  default: oops\n",
        "retry:\n  default:\n    max_total_s: x\n",
        "retry:\n",
    ],
    ids=["none", "str", "field_str", "section_none"],
)
def test_a_malformed_default_profile_inherits_the_bases_bound(tmp_path, recorded, local):
    """#2215's per-shape guard substituted an unbounded `RetryPolicy()` here.
    The fallback inherits the base's default instead."""
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.retry_profiles["default"].max_total_s == 300


def test_the_shipped_default_retry_profile_is_bounded():
    """The fallback's bound comes from the shipped base, so pin it there."""
    shipped_path = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
    shipped = yaml.safe_load(shipped_path.read_text())
    assert isinstance(shipped["retry"]["default"].get("max_total_s"), int | float)


# ── The save path writes only what the next load accepts ─────────────────────


def test_a_save_that_would_not_load_is_refused_and_nothing_is_written(tmp_path):
    """#2215's F3, reproduced: its save accepted this overlay and the next load
    rejected it. Here the save refuses and leaves the file as it was."""
    local = f"retry:\n  custom:\ncall_sites:\n  {SITE}:\n    retry_profile: custom\n"
    cfg = _write(tmp_path, local)
    local_path = tmp_path / "model_routing.local.yaml"
    before = local_path.read_bytes()

    with pytest.raises(ValueError, match="failed validation"):
        update_call_site_in_yaml(cfg, SITE, never_pays=True)

    assert local_path.read_bytes() == before
    assert not local_path.with_suffix(".yaml.bak.1").exists()


@pytest.mark.parametrize("local", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_save_that_succeeds_always_loads_strictly(tmp_path, local):
    """The property the save path owes: if it wrote, the next boot accepts it."""
    cfg = _write(tmp_path, local)
    local_path = tmp_path / "model_routing.local.yaml"
    before = local_path.read_bytes()
    try:
        update_call_site_in_yaml(cfg, "32_other", default_paid=True)
    except ValueError:
        assert local_path.read_bytes() == before, "a refused save still wrote the file"
        return

    loaded = load_config(cfg, check_api_keys=False, strict_overlay=True)
    assert loaded.call_sites["32_other"].default_paid is True


@pytest.mark.parametrize("local", ["call_sites:\n", "call_sites:\n  32_other:\n"])
def test_a_save_replaces_an_empty_header_it_needs(tmp_path, local):
    """A bare `call_sites:` or `<id>:` holds nothing, so the edit may fill it."""
    cfg = _write(tmp_path, local)

    update_call_site_in_yaml(cfg, "32_other", default_paid=True)

    written = yaml.safe_load((tmp_path / "model_routing.local.yaml").read_text())
    assert written["call_sites"]["32_other"] == {"default_paid": True}
    assert load_config(cfg, check_api_keys=False, strict_overlay=True)


@pytest.mark.parametrize(
    "local",
    ["call_sites: oops\n", "call_sites:\n  32_other: [a]\n", "providers: [unclosed\n", "- a\n"],
    ids=["section_str", "entry_list", "yaml_error", "top_level_list"],
)
def test_a_save_never_overwrites_something_it_cannot_read(tmp_path, local):
    cfg = _write(tmp_path, local)
    local_path = tmp_path / "model_routing.local.yaml"

    with pytest.raises(ValueError):
        update_call_site_in_yaml(cfg, "32_other", default_paid=True)

    assert local_path.read_text() == local


def test_a_no_op_save_refuses_a_broken_overlay(tmp_path, recorded):
    """The dashboard reloads the router with what the save returns. A save with
    nothing to change must not hand back the fallback over a running overlay."""
    cfg = _write(tmp_path, f"call_sites:\n  {SITE}:\n    chain: 5\n")

    with pytest.raises(ValueError, match="could not be loaded"):
        update_call_site_in_yaml(cfg, SITE)


def test_a_save_is_validated_through_the_same_sanitizer_as_boot(tmp_path):
    """A stale call site left in the overlay is dropped by the sanitizer at boot.
    The save must judge the file the way boot will, so it may not refuse over it.
    This is what binds the save to the boot pipeline: validating the raw dict
    (the old save) or skipping the sanitizer both refuse this legitimate save."""
    cfg = _write(tmp_path, "call_sites:\n  99_removed:\n    chain: [gone-provider]\n")

    update_call_site_in_yaml(cfg, "32_other", default_paid=True)

    loaded = load_config(cfg, check_api_keys=False, strict_overlay=True)
    assert loaded.call_sites["32_other"].default_paid is True


def test_the_effective_raw_is_the_base_when_parsing_fails(tmp_path, recorded):
    """The parse-stage failure: the merge succeeds, then `_parse` rejects it."""
    local = (
        f"call_sites:\n  {SITE}:\n    cc_model: Opus\nretry:\n  background:\n    max_total_s: x\n"
    )
    _, raw = C._load_effective(_write(tmp_path, local), check_api_keys=False)

    assert "cc_model" not in raw["call_sites"][SITE]


def test_a_new_cause_at_the_same_mtime_is_reported_again(tmp_path, recorded):
    """The base can change under an unchanged overlay (a pull without a restart)."""
    local_path = tmp_path / "model_routing.local.yaml"
    local_path.write_text("retry:\n  custom:\n")
    C._report_overlay_rejected(local_path, ValueError("first cause"))
    C._report_overlay_rejected(local_path, ValueError("first cause"))
    C._report_overlay_rejected(local_path, ValueError("second cause"))

    assert len(recorded) == 2


def test_an_unknown_top_level_key_does_not_trigger_the_fallback(tmp_path, recorded):
    """CONTROL. `_parse` ignores keys it does not read, so an operator's own
    addition must not cost the rest of the overlay."""
    local = "brand_new_section:\nproviders:\n  paid-a:\n    rpm_limit: 7\n"
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.providers["paid-a"].rpm_limit == 7
    assert recorded == []


def test_a_fractional_rpm_limit_is_still_accepted(tmp_path, recorded):
    """CONTROL. 0.5 means one call per two minutes; the rate gate divides by it."""
    loaded = load_config(
        _write(tmp_path, "providers:\n  paid-a:\n    rpm_limit: 0.5\n"), check_api_keys=False
    )

    assert loaded.providers["paid-a"].rpm_limit == 0.5
    assert recorded == []


@pytest.mark.parametrize("bad", ["[]", "false", "oops"])
def test_a_non_mapping_section_in_the_BASE_raises(tmp_path, bad):
    """Binds `_parse`'s own section check, which `_merge_overlay` shadows for
    overlays. The base must fail loudly, never load with zero call sites."""
    base = _BASE.replace("call_sites:\n", f"call_sites: {bad}\nunused_call_sites:\n", 1)

    with pytest.raises(ValueError, match="section 'call_sites' must be a mapping"):
        load_config(_write(tmp_path, None, base=base), check_api_keys=False)


def test_an_undefined_retry_profile_in_the_BASE_still_raises(tmp_path):
    """Carried from #2215: a name defined nowhere is a typo, and the base is not
    contained by any fallback, so it must be loud."""
    base = _BASE.replace("retry_profile: background", "retry_profile: backgorund")

    with pytest.raises(ValueError, match="unknown retry profile 'backgorund'"):
        load_config(_write(tmp_path, None, base=base), check_api_keys=False)


def test_a_mapping_retry_profile_fails_with_a_sentence(tmp_path):
    """Carried from #2215: the strict path names the field, not a bare TypeError."""
    local = f"call_sites:\n  {SITE}:\n    retry_profile:\n      a: 1\n"

    with pytest.raises(ValueError, match="retry_profile must be a string or null"):
        load_config(_write(tmp_path, local), check_api_keys=False, strict_overlay=True)
