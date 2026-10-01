"""A malformed local routing overlay must never take routing dark.

`runtime/init/router.py` catches any exception out of `load_config` and leaves
the runtime with no router, so every LLM call site goes dark behind one log line.
The overlay (`model_routing.local.yaml`) is the one routing input CI never sees,
so it is the one that can do this.

The design here is a chokepoint, not a list of shapes:

  * `load_config` parses base + overlay. If that fails for ANY reason, the whole
    overlay is set aside, the failure is logged at ERROR and recorded as a health
    observation, and the base is parsed instead. The base is validated in CI.
    Every RESTRICTION the overlay makes still applies to that base, through one
    rule per overlay-settable field (`_FALLBACK_RULES`), so a typo cannot undo
    any of them. A test walks `_parse` and fails on a field with no rule.
  * The exception is a file that cannot be read or parsed as YAML at all: no
    restriction in it can be read, so the base would lift every one. That load
    is REFUSED (logged and recorded the same way): a router starting on it stays down.
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
    for state in (C._REPORTED_OVERLAYS, C._RECORDED_OVERLAYS, C._OBSERVATION_FAILED_AT):
        state.clear()
    yield
    for state in (C._REPORTED_OVERLAYS, C._RECORDED_OVERLAYS, C._OBSERVATION_FAILED_AT):
        state.clear()


@pytest.fixture
def recorded(monkeypatch):
    """Capture health observations instead of writing to any database."""
    rows: list[dict] = []

    def fake_create_sync_status(db_path, **kw):
        rows.append(kw)
        return "written"

    monkeypatch.setattr(
        "genesis.db.crud.observations.create_sync_status", fake_create_sync_status
    )
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
    # the whole file (a file that is not YAML at all is REFUSED: see UNPARSEABLE)
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


def _site_capped(max_retries, max_total_s):
    """The base edit for a site the fallback caps: its own copy of its profile."""

    def edit(raw):
        profile = dict(raw["retry"]["background"])
        profile.update(max_retries=max_retries, max_total_s=max_total_s)
        raw["retry"][f"fallback:{SITE}"] = profile
        raw["call_sites"][SITE]["retry_profile"] = f"fallback:{SITE}"

    return edit


def _profile_capped(name, **caps):
    return lambda raw: raw["retry"][name].update(**caps)


#: Shapes that also carry a restriction the fallback keeps, mapped to the base
#: edit that expresses it. Where the overlay names a provider or call site but
#: its value there is unreadable, the fallback reads it restrictively.
EXCLUDING = {
    # an unreadable call-site entry BLOCKS the site: its dispatch is unknown,
    # so it is kept API-only with no providers and nothing runs
    "call_site_str": lambda raw: raw["call_sites"][SITE].update(chain=[], dispatch="api"),
    "call_site_list": lambda raw: raw["call_sites"][SITE].update(chain=[], dispatch="api"),
    "chain_int": lambda raw: raw["call_sites"][SITE].update(chain=[]),
    "chain_str": lambda raw: raw["call_sites"][SITE].update(chain=[]),
    "provider_str": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
    "free_string": lambda raw: raw["providers"]["free-b"].update(free=False),
    "never_pays_string": lambda raw: raw["call_sites"][SITE].update(never_pays=True),
    "never_pays_zero": lambda raw: raw["call_sites"][SITE].update(never_pays=True),
    # an unreadable limit on a named provider disables it
    "rpm_limit_str": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
    "rpd_limit_zero": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
    # a value with no order that the overlay changes excludes the provider
    "params_list": lambda raw: raw["providers"]["free-b"].update(enabled=False),
    "open_duration_negative": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
    "open_duration_nan": lambda raw: raw["providers"]["paid-a"].update(enabled=False),
    # an unreadable max_retries is 0, one attempt per provider. An unreadable
    # max_total_s narrows nothing: 0 would stop the site calling anything.
    "max_retries_float": _profile_capped("background", max_retries=0),
    "max_retries_bool": _profile_capped("background", max_retries=0),
    # an unreadable profile NAME: 0 retries, the base profile's deadline
    "retry_profile_typo": _site_capped(0, 600),
    "retry_profile_mapping": _site_capped(0, 600),
    "retry_profile_empty": _site_capped(0, 600),
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


#: Files that are not YAML at all. No restriction in them can be read, so the
#: load is REFUSED (a router starting on it stays down) rather than falling back.
#: The second carries a restriction the base would lift, to show why.
UNPARSEABLE = {
    "unclosed_flow_list": "providers: [unclosed\n",
    "restriction_then_syntax_error": (
        "providers:\n  paid-a:\n    enabled: false\ncall_sites: [unclosed\n"
    ),
    "bad_indentation": "providers:\n  paid-a:\n    enabled: false\n   free: true\n",
}


@pytest.mark.parametrize(
    "local",
    list(MALFORMED.values()) + list(UNPARSEABLE.values()),
    ids=list(MALFORMED) + list(UNPARSEABLE),
)
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
    assert "no restrictions" in recorded[0]["content"]


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
    The fallback keeps the base's bound. An unreadable deadline does not
    lower it to 0, which would stop every site on the profile calling anything."""
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
# ── Every overlay-settable field has a restrictiveness rule ─────────────────
#
# Rounds 2 and 3 of review each found a restriction the fallback dropped
# (first `enabled`/`never_pays`/chains, then `dispatch: api` and retry caps),
# because the fallback listed the fields it kept. It now keeps a rule for EVERY
# field, and this test fails when a field is read without one.

#: How `_parse` names each section's entry.
_PARSE_ENTRY_VARS = {"p": "providers", "cs": "call_sites", "rp": "retry"}


def _fields_parse_reads() -> dict[str, set[str]]:
    """The field names `_parse` reads from each section's entries, by AST."""
    import ast
    import inspect

    tree = ast.parse(textwrap.dedent(inspect.getsource(C._parse)))
    # `for required in ("type", "model"): p.get(required)` names fields too.
    loop_names: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, ast.Tuple)
            and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.iter.elts
            )
        ):
            loop_names[node.target.id] = [e.value for e in node.iter.elts]

    def names(arg: ast.AST) -> list[str]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return [arg.value]
        if isinstance(arg, ast.Name):
            return loop_names.get(arg.id, [])
        return []

    # Every OTHER use of an entry variable fails the walk rather than being
    # skipped: `"k" in p`, `p.items()`, `**p`, passing `p` to a helper. Each
    # could read a field this walker cannot name. Allowed: `.get`, a subscript,
    # the `_entry(section, name, p)` check, and a comprehension that rebinds the
    # name to something else (`[p for p in chain ...]`).
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def inside(node: ast.AST, ancestor: ast.AST) -> bool:
        while node is not None:
            if node is ancestor:
                return True
            node = parents.get(node)
        return False

    def rebound_by_comprehension(name: ast.Name) -> bool:
        node = parents.get(name)
        while node is not None:
            comprehension = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            if (
                isinstance(node, comprehension)
                and any(
                    isinstance(gen.target, ast.Name) and gen.target.id == name.id
                    for gen in node.generators
                )
                # The FIRST generator's iterable runs in the enclosing scope,
                # where the name is still the entry: `[p for p in p.items()]`.
                and not inside(name, node.generators[0].iter)
            ):
                return True
            node = parents.get(node)
        return False

    unrecognised = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Name)
            and node.id in _PARSE_ENTRY_VARS
            and isinstance(node.ctx, ast.Load)
        ):
            continue
        parent = parents[node]
        if rebound_by_comprehension(node):
            continue
        if isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = parents.get(parent)
            if isinstance(call, ast.Call) and call.args and names(call.args[0]):
                continue
        elif isinstance(parent, ast.Subscript) and parent.value is node and names(parent.slice) or (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "_entry"
        ):
            continue
        unrecognised.append(f"line {node.lineno}: {ast.unparse(parent)}")
    assert not unrecognised, (
        "`_parse` uses an entry in a way this walker cannot read field names from; "
        f"teach it the new form, or read the field with .get: {unrecognised}"
    )

    found: dict[str, set[str]] = {section: set() for section in _PARSE_ENTRY_VARS.values()}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _PARSE_ENTRY_VARS
            and node.args
        ):
            found[_PARSE_ENTRY_VARS[node.func.value.id]].update(names(node.args[0]))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in _PARSE_ENTRY_VARS
        ):
            found[_PARSE_ENTRY_VARS[node.value.id]].update(names(node.slice))
    return found


def _fields_the_shipped_config_sets() -> dict[str, set[str]]:
    shipped_path = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
    shipped = yaml.safe_load(shipped_path.read_text())
    return {
        section: {key for entry in shipped[section].values() for key in entry}
        for section in ("providers", "call_sites", "retry")
    }


def _fields_the_dashboard_writes() -> set[str]:
    import inspect

    params = inspect.signature(update_call_site_in_yaml).parameters.values()
    return {p.name for p in params if p.kind is p.KEYWORD_ONLY}


def test_every_overlay_settable_field_has_a_restrictiveness_rule():
    """ALLOWLIST polarity: a field `_parse` reads, the shipped config sets, or
    the dashboard writes, with no rule in `_FALLBACK_RULES`, fails here. Adding
    a field to the schema without deciding how a rejected overlay still
    restricts it is the defect this blocks."""
    parse = _fields_parse_reads()
    # Guard the guard: the walker must see the fields it exists to find, or an
    # empty walk would pass vacuously.
    assert {"enabled", "rpd_limit", "type", "model"} <= parse["providers"]
    assert {"chain", "dispatch", "never_pays", "retry_profile"} <= parse["call_sites"]
    assert {"max_retries", "max_total_s", "jitter_pct"} <= parse["retry"]

    shipped = _fields_the_shipped_config_sets()
    dashboard = _fields_the_dashboard_writes()
    assert "dispatch" in dashboard, "fixture: the save path's keyword fields"

    missing = {
        section: sorted(
            (parse[section] | shipped[section] | (dashboard if section == "call_sites" else set()))
            - set(C._FALLBACK_RULES[section])
        )
        for section in ("providers", "call_sites", "retry")
    }
    assert missing == {"providers": [], "call_sites": [], "retry": []}


def test_every_rule_names_a_known_operation_and_says_why():
    applicable = {
        "providers": {"off_wins", "lower", "higher", "same_or_excluded", "neutral"},
        "call_sites": {"on_wins", "chain", "dispatch", "retry_profile", "neutral"},
        "retry": {"lower", "higher"},
    }
    for section, rules in C._FALLBACK_RULES.items():
        for field, rule in rules.items():
            assert rule.kind in C._RULE_KINDS, (section, field)
            assert rule.kind in applicable[section], (section, field, rule.kind)
            assert rule.why.strip(), (section, field)
    # Every switch has a reader, and every retry cap a default.
    switches = {f for f, r in C._FALLBACK_RULES["providers"].items() if r.kind == "off_wins"}
    assert switches == set(C._PERMITS) == set(C._SWITCH_DEFAULTS) == set(C._SWITCH_LABELS)
    # Every retry field is a cap or pacing, and each has the default `_parse` uses.
    assert set(C._FALLBACK_RULES["retry"]) == set(C._RETRY_CAPS) | set(C._PACING_DEFAULTS)
    assert not set(C._RETRY_CAPS) & set(C._PACING_DEFAULTS)


# ── The rules the round-3 review found missing ──────────────────────────────


def test_a_rejected_overlay_keeps_api_only_dispatch(tmp_path, recorded):
    """The reviewer's case: `dispatch: api` keeps a `dual` site from escalating
    to the CLI when its chain is exhausted."""
    local = f"call_sites:\n  {SITE}:\n    dispatch: api\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.call_sites[SITE].dispatch == "api"
    assert f"call site {SITE} dispatch api" in recorded[0]["content"]


def _dispatcher_mode(config, site: str) -> str:
    """The mode the autonomous dispatcher resolves for a site, on this config."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from genesis.autonomy.dispatch_router import AutonomousDispatchRouter

    dispatcher = AutonomousDispatchRouter(
        router=SimpleNamespace(config=config), approval_gate=MagicMock()
    )
    return dispatcher._resolve_dispatch_mode(
        SimpleNamespace(dispatch_mode=None, api_call_site_id=site)
    )


def test_dispatch_modes_with_no_executor_in_common_block_the_site(tmp_path, recorded):
    """A base `cli` site the overlay moves to `api`: no executor is allowed by
    both. The site must stay in the config as API-only with no providers. A
    site MISSING from the config is read as `dual` by the dispatcher, which
    would reach the CLI both files ruled out for it."""
    base = _BASE.replace("    chain: [free-b]\n", "    chain: [free-b]\n    dispatch: cli\n", 1)
    local = "call_sites:\n  32_other:\n    dispatch: api\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local, base=base), check_api_keys=False)

    assert loaded.call_sites["32_other"].chain == []
    assert _dispatcher_mode(loaded, "32_other") == "api"


def test_an_api_site_whose_chain_empties_is_blocked_not_dropped(tmp_path, recorded):
    """`dispatch: api` plus a chain naming no provider of the base chain leaves
    no providers. Dropped, the dispatcher would read the site as `dual`."""
    local = (
        f"call_sites:\n  {SITE}:\n    dispatch: api\n    chain: [free-b]\n"
        + _UNRELATED_ERROR
    )
    base = _BASE.replace("chain: [paid-a, free-b]", "chain: [paid-a]", 1)
    loaded = load_config(_write(tmp_path, local, base=base), check_api_keys=False)

    assert loaded.call_sites[SITE].chain == []
    assert _dispatcher_mode(loaded, SITE) == "api"


def test_an_unreadable_deadline_still_lets_the_site_call(tmp_path, recorded):
    """EFFECT, not value: the router checks the deadline before the first
    provider too, so a deadline of 0 means no call at all."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.degradation import DegradationTracker
    from genesis.routing.router import Router
    from genesis.routing.types import BudgetStatus

    from .conftest import MockDelegate

    local = "retry:\n  background:\n    max_total_s: 5m\n  custom:\n"
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)
    cost_tracker = MagicMock(db=None)
    cost_tracker.check_budget = AsyncMock(return_value=BudgetStatus.UNDER_LIMIT)
    cost_tracker.record = AsyncMock()
    delegate = MockDelegate()
    router = Router(
        config=loaded,
        breakers=CircuitBreakerRegistry(loaded.providers),
        cost_tracker=cost_tracker,
        degradation=DegradationTracker(),
        delegate=delegate,
    )

    result = asyncio.run(router.route_call(SITE, [{"role": "user", "content": "hi"}]))

    assert loaded.retry_profiles["background"].max_total_s == 600
    assert result.success, result.error
    assert len(delegate.calls) == 1


def test_a_rejected_overlay_keeps_lower_retry_caps(tmp_path, recorded):
    # The unrelated error sits inside `retry:`: a second top-level `retry:` key
    # would replace this section, since YAML keeps the last duplicate.
    local = "retry:\n  background:\n    max_retries: 1\n    max_total_s: 30\n  custom:\n"
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.retry_profiles["background"].max_retries == 1
    assert loaded.retry_profiles["background"].max_total_s == 30


def test_a_higher_or_absent_retry_cap_is_not_applied(tmp_path, recorded):
    """null is no deadline, which is looser than the base's, so it is not kept."""
    local = "retry:\n  background:\n    max_retries: 9\n    max_total_s: null\n  custom:\n"
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.retry_profiles["background"].max_retries == 5
    assert loaded.retry_profiles["background"].max_total_s == 600


def test_moving_a_site_to_a_tighter_profile_is_kept(tmp_path, recorded):
    """The site keeps its base profile's pacing, capped at the tighter caps."""
    local = f"call_sites:\n  {SITE}:\n    retry_profile: default\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    policy = loaded.retry_profiles[loaded.call_sites[SITE].retry_profile]
    assert (policy.max_retries, policy.max_total_s) == (3, 300)
    assert loaded.retry_profiles["background"].max_retries == 5, "the shared profile is untouched"


def test_moving_a_site_to_a_profile_the_overlay_defines_is_kept(tmp_path, recorded):
    local = (
        f"call_sites:\n  {SITE}:\n    retry_profile: quick\n"
        "retry:\n  quick:\n    max_retries: 0\n    max_total_s: 5\n  custom:\n"
    )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    policy = loaded.retry_profiles[loaded.call_sites[SITE].retry_profile]
    assert (policy.max_retries, policy.max_total_s) == (0, 5)


def test_moving_a_site_to_a_looser_profile_changes_nothing(tmp_path, recorded):
    local = "call_sites:\n  32_other:\n    retry_profile: background\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.call_sites["32_other"].retry_profile == "default"


def test_an_overlay_that_changes_a_model_excludes_the_provider(tmp_path, recorded):
    """The operator runs a different model there. The base's model is not one
    they chose, so the fallback does not call it."""
    local = "providers:\n  paid-a:\n    model: vendor/other\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert "paid-a" not in loaded.providers


def test_an_overlay_that_restates_a_value_excludes_nothing(tmp_path, recorded):
    """CONTROL for `same_or_excluded`: the same model, and params that merge to
    the same map, are no change."""
    local = (
        "providers:\n  paid-a:\n    model: vendor/model-a\n"
        "  free-b:\n    params:\n      reasoning_effort: disable\n" + _UNRELATED_ERROR
    )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert _snapshot(loaded) == _snapshot(_base_only(tmp_path))


def test_a_longer_breaker_rest_is_kept_and_a_shorter_one_is_not(tmp_path, recorded):
    """A provider that rests longer after tripping is probed less often."""
    local = (
        "providers:\n  paid-a:\n    open_duration_s: 900\n  free-b:\n    open_duration_s: 1\n"
        + _UNRELATED_ERROR
    )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert loaded.providers["paid-a"].open_duration_s == 900
    assert loaded.providers["free-b"].open_duration_s == 120


def test_a_neutral_field_is_set_aside(tmp_path, recorded):
    local = "providers:\n  paid-a:\n    keep_alive: 5m\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert _snapshot(loaded) == _snapshot(_base_only(tmp_path))


def test_a_restriction_under_a_renamed_provider_name_still_applies(tmp_path, recorded, monkeypatch):
    """An upgrading install's overlay can still use a provider's old name. The
    accepted path migrates it; the fallback must read it the same way."""
    monkeypatch.setitem(C._RENAMED_PROVIDERS, "paid-a-old", "paid-a")
    local = (
        "providers:\n  paid-a-old:\n    rpd_limit: 7\n"
        "call_sites:\n  32_other:\n    chain: [paid-a-old]\n" + _UNRELATED_ERROR
    )
    base = _BASE.replace("    chain: [free-b]\n", "    chain: [free-b, paid-a]\n", 1)
    loaded = load_config(_write(tmp_path, local, base=base), check_api_keys=False)

    assert loaded.providers["paid-a"].rpd_limit == 7
    assert loaded.call_sites["32_other"].chain == ["paid-a"]


def test_a_legacy_and_current_provider_key_settle_as_the_accepted_path_does(
    tmp_path, recorded, monkeypatch
):
    """Both keys resolve to one provider. The accepted path keeps the CURRENT
    key's entry and drops the legacy one, so the fallback must too, not apply
    both. CONTROL: the legacy key alone still restricts."""
    monkeypatch.setitem(C._RENAMED_PROVIDERS, "paid-a-old", "paid-a")
    both = (
        "providers:\n  paid-a-old:\n    enabled: false\n  paid-a:\n    enabled: true\n"
        + _UNRELATED_ERROR
    )
    loaded = load_config(_write(tmp_path, both), check_api_keys=False)
    assert "paid-a" in loaded.providers

    alone = "providers:\n  paid-a-old:\n    enabled: false\n" + _UNRELATED_ERROR
    control = tmp_path / "control"
    control.mkdir()
    loaded = load_config(_write(control, alone), check_api_keys=False)
    assert "paid-a" not in loaded.providers


def test_a_rejected_overlay_keeps_longer_retry_pacing(tmp_path, recorded):
    """Pacing is not neutral: the router checks the deadline before each retry,
    so longer waits fit fewer attempts. Longer waits and less jitter are kept;
    shorter ones are not."""
    local = (
        "retry:\n  background:\n    base_delay_ms: 4000\n    max_delay_ms: 90000\n"
        "    backoff_multiplier: 3\n    jitter_pct: 0\n"
        "  default:\n    base_delay_ms: 1\n    jitter_pct: 0.9\n  custom:\n"
    )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    bg = loaded.retry_profiles["background"]
    assert (bg.base_delay_ms, bg.max_delay_ms, bg.backoff_multiplier, bg.jitter_pct) == (
        4000, 90000, 3, 0
    )
    default = loaded.retry_profiles["default"]
    assert (default.base_delay_ms, default.jitter_pct) == (500, 0.25)


def test_a_huge_kept_backoff_never_overflows_a_retry(tmp_path, recorded):
    """A kept multiplier and ceiling this large overflow the backoff power on
    the third attempt; `compute_delay` must cap it rather than raise mid-call."""
    from genesis.routing.retry import compute_delay

    local = (
        "retry:\n  background:\n    max_delay_ms: 1.0e+300\n"
        "    backoff_multiplier: 1.0e+300\n  custom:\n"
    )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    policy = loaded.retry_profiles["background"]
    assert policy.backoff_multiplier == 1.0e300
    for attempt in range(policy.max_retries + 1):
        assert compute_delay(policy, attempt) <= policy.max_delay_ms * 1.25 / 1000


@pytest.mark.parametrize("where", ["same_profile", "moved_site"])
def test_an_unreadable_retry_wait_reads_as_no_retries(tmp_path, recorded, where):
    """Pacing restricts, so an unreadable wait is read the restrictive way, like
    an unreadable cap: no retries. CONTROL: a readable wait keeps the base's."""
    if where == "same_profile":
        local = "retry:\n  background:\n    base_delay_ms: slow\n  custom:\n"
    else:
        local = (
            f"call_sites:\n  {SITE}:\n    retry_profile: slow\n"
            "retry:\n  slow:\n    base_delay_ms: slow\n  custom:\n"
        )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)
    assert loaded.retry_profiles[loaded.call_sites[SITE].retry_profile].max_retries == 0

    control = tmp_path / "control"
    control.mkdir()
    readable = local.replace("base_delay_ms: slow", "base_delay_ms: 600")
    loaded = load_config(_write(control, readable), check_api_keys=False)
    # A whole overlay profile that omits max_retries has `_parse`'s default, 3.
    expected = 5 if where == "same_profile" else 3
    assert loaded.retry_profiles[loaded.call_sites[SITE].retry_profile].max_retries == expected


def test_moving_a_site_to_a_slower_profile_keeps_its_pacing(tmp_path, recorded):
    local = (
        f"call_sites:\n  {SITE}:\n    retry_profile: slow\n"
        "retry:\n  slow:\n    max_retries: 9\n    base_delay_ms: 5000\n  custom:\n"
    )
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    policy = loaded.retry_profiles[loaded.call_sites[SITE].retry_profile]
    assert (policy.max_retries, policy.base_delay_ms) == (5, 5000)
    assert loaded.retry_profiles["background"].base_delay_ms == 500, "shared profile untouched"


# ── The health observation survives a failed write ──────────────────────────


def test_a_failed_observation_write_is_retried(tmp_path, monkeypatch):
    """A locked database at the first report must not lose the observation."""
    statuses = iter(["failed", "written"])
    calls: list[dict] = []

    def status(db_path, **kw):
        calls.append(kw)
        return next(statuses)

    monkeypatch.setattr("genesis.db.crud.observations.create_sync_status", status)
    clock = [1000.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    cfg = _write(tmp_path, _UNRELATED_ERROR)

    load_config(cfg, check_api_keys=False)
    load_config(cfg, check_api_keys=False)
    assert len(calls) == 1, "a retry within the interval would cost a write per poll"

    clock[0] += C._OBSERVATION_RETRY_S
    load_config(cfg, check_api_keys=False)
    load_config(cfg, check_api_keys=False)
    assert len(calls) == 2, "the failed write was not retried, or was retried after success"


def test_a_deduplicated_observation_is_not_retried(tmp_path, monkeypatch):
    """A duplicate means the row is already recorded: retrying would repeat
    the write forever."""
    calls: list[dict] = []

    def status(db_path, **kw):
        calls.append(kw)
        return "duplicate"

    monkeypatch.setattr("genesis.db.crud.observations.create_sync_status", status)
    clock = [1000.0]
    monkeypatch.setattr(C.time, "monotonic", lambda: clock[0])
    cfg = _write(tmp_path, _UNRELATED_ERROR)

    load_config(cfg, check_api_keys=False)
    clock[0] += 10 * C._OBSERVATION_RETRY_S
    load_config(cfg, check_api_keys=False)

    assert len(calls) == 1


def test_create_sync_status_tells_a_duplicate_from_a_failure(tmp_path):
    import sqlite3

    from genesis.db.crud.observations import create_sync, create_sync_status
    from genesis.db.schema import TABLES

    db = tmp_path / "genesis.db"
    conn = sqlite3.connect(db)
    conn.executescript(TABLES["observations"])
    conn.close()
    kw = {
        "source": "routing",
        "type": "init_degradation",
        "priority": "high",
        "content": "x",
        "content_hash": "h",
    }

    assert create_sync_status(str(db), **kw) == "written"
    assert create_sync_status(str(db), **kw) == "duplicate"
    assert create_sync_status(str(tmp_path / "missing" / "no.db"), **kw) == "failed"
    # The boolean API is unchanged: True only for a written row.
    assert create_sync(str(db), **{**kw, "content_hash": "h2"}) is True
    assert create_sync(str(db), **{**kw, "content_hash": "h2"}) is False


# ── The save path after the merge with the rename migration ─────────────────


def test_a_save_can_replace_a_malformed_chain_on_the_site_it_edits(tmp_path):
    """The chain this save replaces is never read, so it must not block the
    save that repairs it."""
    cfg = _write(tmp_path, f"call_sites:\n  {SITE}:\n    chain: paid-a\n")

    update_call_site_in_yaml(cfg, SITE, chain=["free-b"])

    written = yaml.safe_load((tmp_path / "model_routing.local.yaml").read_text())
    assert written["call_sites"][SITE]["chain"] == ["free-b"]


def test_a_save_that_leaves_a_malformed_chain_in_place_is_refused(tmp_path):
    cfg = _write(tmp_path, f"call_sites:\n  {SITE}:\n    chain: paid-a\n")

    with pytest.raises(ValueError, match="chain must be a list"):
        update_call_site_in_yaml(cfg, SITE, never_pays=True)


def test_an_unhashable_chain_rung_is_a_value_error_not_a_crash(tmp_path):
    """The dashboard maps only ValueError to 400; anything else is a 500."""
    cfg = _write(tmp_path, "call_sites:\n  32_other:\n    chain: [[free-b]]\n")

    with pytest.raises(ValueError, match="could not be read"):
        update_call_site_in_yaml(cfg, SITE, never_pays=True)


def test_an_api_site_whose_providers_are_all_disabled_is_kept_blocked(
    tmp_path, recorded, caplog
):
    """ACCEPTED path too: an API-only site left with no providers stays in the
    config. Dropped, the dispatcher would read it as `dual` and could reach the
    CLI. A `dual` site with no providers is still dropped: it permits the CLI."""
    base = _BASE.replace(
        "    chain: [free-b]\n", "    chain: [free-b]\n    dispatch: api\n", 1
    ).replace("free: true", "free: true\n    enabled: false", 1)
    with caplog.at_level(logging.WARNING, logger="genesis.routing.config"):
        loaded = load_config(_write(tmp_path, None, base=base), check_api_keys=False)

    assert loaded.call_sites["32_other"].chain == []
    assert _dispatcher_mode(loaded, "32_other") == "api"
    assert loaded.call_sites[SITE].chain == ["paid-a"], "fixture: a dual site keeps paid-a"
    # A site that cannot run is a WARNING, as a dropped one was.
    assert any(
        r.levelno == logging.WARNING and "32_other" in r.getMessage() and "BLOCKED" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize(
    "retry_section",
    ["retry:\n  background:\n    max_retries: 5\n", "retry:\n", ""],
    ids=["no_default", "null_section", "no_section"],
)
def test_a_default_cap_applies_when_the_base_leaves_default_implicit(
    tmp_path, recorded, retry_section
):
    """`_parse` supplies a `default` profile the base does not spell out. A
    lower cap on it must still land there."""
    base_head = _BASE.split("retry:\n", 1)[0].replace("    retry_profile: background\n", "")
    base = base_head + retry_section
    local = "retry:\n  default:\n    max_retries: 1\n  custom:\n"
    loaded = load_config(_write(tmp_path, local, base=base.rstrip()), check_api_keys=False)

    assert loaded.retry_profiles["default"].max_retries == 1
def test_a_blocked_essential_site_does_not_raise_system_degradation(tmp_path, recorded):
    """A blocked site is a configuration state, not a provider outage. On the
    shipped config, one unreadable overlay entry for an essential site must not
    count it as uncovered: that would raise ESSENTIAL degradation, and the
    router would then shed nearly every other routed call site."""
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.essential import ESSENTIAL_CLOUD_SITES, build_essential_provider_map
    from genesis.routing.types import DegradationLevel

    shipped = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
    site = "4_light_reflection"
    assert site in ESSENTIAL_CLOUD_SITES, "fixture: the site must be essential"
    local = f"call_sites:\n  {site}: oops\n"
    loaded = load_config(
        _write(tmp_path, local, base=shipped.read_text().rstrip()), check_api_keys=False
    )
    assert loaded.call_sites[site].chain == [], "fixture: the site must be blocked"

    registry = CircuitBreakerRegistry(
        loaded.providers,
        state_file=tmp_path / "breakers.json",
        persist=False,
        essential_sites=build_essential_provider_map(loaded),
    )

    assert registry.uncovered_essential_sites() == []
    assert registry.compute_degradation_level() == DegradationLevel.NORMAL



def test_every_essential_site_blocked_stays_in_coverage_mode(tmp_path, recorded):
    """All essential sites blocked by configuration is not "no map": the
    registry must stay in coverage mode (NORMAL), not fall back to the legacy
    provider-count check, which reads every paid provider open as ESSENTIAL."""
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.essential import ESSENTIAL_CLOUD_SITES, build_essential_provider_map
    from genesis.routing.types import DegradationLevel, ErrorCategory

    shipped = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
    local = "call_sites:\n" + "".join(f"  {site}: oops\n" for site in ESSENTIAL_CLOUD_SITES)
    loaded = load_config(
        _write(tmp_path, local, base=shipped.read_text().rstrip()), check_api_keys=False
    )
    essential = build_essential_provider_map(loaded)
    assert essential == {}, "fixture: every essential site must be blocked"

    registry = CircuitBreakerRegistry(
        loaded.providers,
        state_file=tmp_path / "breakers.json",
        persist=False,
        essential_sites=essential,
    )
    paid = [
        name
        for name, cfg in loaded.providers.items()
        if cfg.provider_type != "ollama" and not cfg.is_free
    ]
    assert paid, "fixture: the shipped config has paid providers"
    for name in paid:
        for _ in range(10):
            registry.get(name).record_failure(ErrorCategory.TRANSIENT)

    assert registry.compute_degradation_level() == DegradationLevel.NORMAL

    # CONTROL: no map at all is still the legacy check, which does degrade.
    legacy = CircuitBreakerRegistry(
        loaded.providers, state_file=tmp_path / "b2.json", persist=False, essential_sites=None
    )
    for name in paid:
        for _ in range(10):
            legacy.get(name).record_failure(ErrorCategory.TRANSIENT)
    assert legacy.compute_degradation_level() != DegradationLevel.NORMAL


def test_a_reload_rebuilds_the_essential_map_and_a_bare_registry_stays_legacy(tmp_path):
    """The blocked set is a property of the config, so a reload after fixing the
    overlay must restore coverage. A registry built with no map is untouched."""
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry

    managed = CircuitBreakerRegistry(
        {}, clock=lambda: 0, persist=False, state_file=tmp_path / "a.json", essential_sites={}
    )
    managed.refresh_essential_sites({"9_fact_extraction": ["p1"]})
    assert managed.uncovered_essential_sites() == ["9_fact_extraction"]

    bare = CircuitBreakerRegistry({}, clock=lambda: 0, persist=False, state_file=tmp_path / "b.json")
    bare.refresh_essential_sites({"9_fact_extraction": ["p1"]})
    assert bare.uncovered_essential_sites() == []


# ── An overlay that is not YAML at all is refused, not contained ────────────


@pytest.mark.parametrize("shape", list(UNPARSEABLE))
def test_an_unparseable_overlay_refuses_the_load(tmp_path, recorded, caplog, shape):
    """No restriction in the file can be read, so the base would lift all of
    them. The load is refused: `runtime/init/router.py` then leaves routing down
    until the file is fixed, as it did before the fallback existed."""
    local = UNPARSEABLE[shape]
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(local)  # fixture: the text really is not YAML

    with (
        caplog.at_level(logging.ERROR, logger="genesis.routing.config"),
        pytest.raises(yaml.YAMLError),
    ):
        load_config(_write(tmp_path, local), check_api_keys=False)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "load was REFUSED" in errors[0].getMessage()
    assert len(recorded) == 1, "the refusal happened without a health observation"
    row = recorded[0]
    assert row["source"] == "routing"
    assert row["priority"] == "high"
    assert "model_routing.local.yaml" in row["content"]
    assert "will not come up" in row["content"]


def test_an_undecodable_overlay_is_refused_too(tmp_path, recorded):
    """A file that cannot be read as text holds no readable restriction either."""
    cfg = _write(tmp_path, None)
    (tmp_path / "model_routing.local.yaml").write_bytes(b"providers:\n  \xff\xfe: {}\n")

    with pytest.raises(UnicodeDecodeError):
        load_config(cfg, check_api_keys=False)
    assert len(recorded) == 1


def test_a_parse_valid_but_invalid_overlay_still_falls_back(tmp_path, recorded):
    """CONTROL for the refusal: the same restriction, in a file that parses but
    fails validation, is contained. Routing loads, and the restriction holds."""
    local = "providers:\n  paid-a:\n    enabled: false\n" + _UNRELATED_ERROR
    loaded = load_config(_write(tmp_path, local), check_api_keys=False)

    assert "paid-a" not in loaded.providers
    assert len(recorded) == 1
    assert "will not come up" not in recorded[0]["content"]


def test_the_refusal_is_reported_once_per_file_version(tmp_path, recorded, caplog):
    """`load_config` runs on every dashboard vitals poll: one broken file must be
    one ERROR line and one observation per version, not one per poll."""
    cfg = _write(tmp_path, UNPARSEABLE["unclosed_flow_list"])
    local = tmp_path / "model_routing.local.yaml"

    with caplog.at_level(logging.ERROR, logger="genesis.routing.config"):
        for _ in range(3):
            with pytest.raises(yaml.YAMLError):
                load_config(cfg, check_api_keys=False)
    assert len(recorded) == 1
    assert len([r for r in caplog.records if "load was REFUSED" in r.getMessage()]) == 1

    stat = local.stat()
    os.utime(local, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(yaml.YAMLError):
        load_config(cfg, check_api_keys=False)
    assert len(recorded) == 2, "an edited (still broken) file was not reported again"


def test_an_unparseable_overlay_leaves_the_runtime_with_no_router(tmp_path, monkeypatch, caplog):
    """What "routing stays down" means at boot: `runtime/init/router.py` catches
    the raise, logs it, and sets no router, breakers, cost tracker or dead-letter
    queue."""
    from types import SimpleNamespace

    from genesis.runtime.init import router as init_router

    monkeypatch.setattr(C, "_report_overlay_rejected", lambda *a, **k: None)
    (tmp_path / "config").mkdir()
    _write(tmp_path / "config", UNPARSEABLE["restriction_then_syntax_error"])
    monkeypatch.setattr("genesis.env.repo_root", lambda: tmp_path)
    rt = SimpleNamespace(
        _router=None, _circuit_breakers=None, _cost_tracker=None, _dead_letter_queue=None
    )

    with caplog.at_level(logging.ERROR, logger="genesis.runtime"):
        init_router.init(rt)

    assert rt._router is None
    assert rt._circuit_breakers is None
    failed = [r for r in caplog.records if r.getMessage() == "Failed to initialize router"]
    assert failed and isinstance(failed[0].exc_info[1], yaml.YAMLError)


def test_coverage_mode_survives_a_reload_with_no_essential_site(tmp_path):
    """A managed registry reloaded with a config that has no essential site
    stores None. The next reload that has them must restore coverage; keyed on
    the current map, it was ignored until a restart."""
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry

    managed = CircuitBreakerRegistry(
        {}, clock=lambda: 0, persist=False, state_file=tmp_path / "a.json",
        essential_sites={"9_fact_extraction": ["p1"]},
    )
    managed.refresh_essential_sites(None)
    assert managed._essential_sites is None, "fixture: the first reload leaves no map"
    managed.refresh_essential_sites({"9_fact_extraction": ["p1"]})
    assert managed.uncovered_essential_sites() == ["9_fact_extraction"]

    # CONTROL: a registry built with no map stays on the legacy check.
    bare = CircuitBreakerRegistry({}, clock=lambda: 0, persist=False, state_file=tmp_path / "b.json")
    bare.refresh_essential_sites(None)
    bare.refresh_essential_sites({"9_fact_extraction": ["p1"]})
    assert bare.uncovered_essential_sites() == []
