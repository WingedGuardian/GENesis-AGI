"""A bodiless key in a local routing overlay must not take routing offline.

An operator opens `model_routing.local.yaml`, types a key, and saves before
writing its body. YAML loads a bodiless key as ``None``, not as ``{}``. Depending
on which key it was, that used to delete every provider, delete every call site,
or raise out of `_parse` — and each escapes `load_config` into
`runtime/init/router.py`, which swallows it into `_bootstrapped = False`: Genesis
starts, every model call site is dark, one log line is the only evidence, and
nothing in the file looks wrong.

MEASURED 2026-09-25, every level, before the fix:

    providers:                  all providers deleted
    call_sites:                 all call sites deleted
    call_sites: {<id>:}         TypeError at config.py, router dark
    call_sites: {<id>: oops}    TypeError, router dark
    retry: {<name>:}            AttributeError, router dark
    retry: {<new-name>:}        AttributeError, router dark — and triggered by a
                                profile nothing references
    providers: {<name>:}        already absorbed by `_parse` (warns, disables it)

The entry-level shapes are the likelier ones, because `call_sites: {<id>: {...}}`
is exactly the nesting the dashboard writes, so that is what an operator edits by
hand.

Three layers hold it now, and they are tested SEPARATELY on purpose because each
turns out to be independently sufficient — mutating any one leaves the end-to-end
assertions green, so an end-to-end suite alone would stop binding any of them:

  * `_deep_merge`             — tolerates a non-mapping overlay ARGUMENT. It
                                deliberately does NOT refuse a non-mapping value
                                at a key: an overlay setting
                                `providers.<name>.params: null` is an operator
                                CLEARING an inherited map, and a revision that
                                refused it silently removed that capability.
  * `_sanitize_local_overlay` — drops a non-mapping SECTION and a non-mapping
                                ENTRY. It can tell structural keys from leaf
                                fields; the merge cannot. Its result is what
                                `update_call_site_in_yaml` writes back to disk,
                                so this is also what HEALS the operator's file.
  * `_parse`                  — per-section shape guards, so one malformed entry
                                skips instead of taking the router down.
"""

from __future__ import annotations

import textwrap

import pytest

from genesis.routing.config import load_config

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


def test_a_provider_entry_with_no_type_is_dropped_not_fatal(tmp_path):
    """An incomplete provider entry is skipped, not fatal.

    `_parse` raising here would not be a loud failure: `runtime/init/router.py`
    catches it, so the runtime comes up with `_bootstrapped = False` and every
    LLM call site dark, announced by one log line. Losing one provider is
    strictly better than losing routing entirely.
    """
    cfg = _write(
        tmp_path,
        _BASE,
        """
        providers:
          some-incomplete-entry:
            rpm_limit: 3
        """,
    )

    config = load_config(cfg, check_api_keys=False)

    assert "some-incomplete-entry" not in config.providers
    assert "glm" in config.providers, "the whole config was lost over one bad entry"


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


# ── mechanism 1: _deep_merge stays out of the way ──────────────────────────
#
# This section used to assert the OPPOSITE — that `_deep_merge` refuses any
# non-mapping over a mapping at any depth. Review showed that rule silently
# removed a real capability, so both the rule and the tests for it are gone, and
# what replaces them is the capability itself. The reason is kept because the
# next person to see a bodiless `providers:` wipe a section will reach for
# exactly the rule that was removed.


@pytest.mark.parametrize(
    "provider", ["groq-free", "gemini-free", "openrouter-free"]
)
def test_an_overlay_can_CLEAR_an_inherited_params_map(tmp_path, provider):
    """The capability an over-broad merge guard removed. `ProviderConfig.params`
    is ``dict | None`` and these three providers ship one, so clearing it is the
    only way to run them against a model that rejects the inherited setting —
    `gemini-free` ships ``{reasoning_effort: disable}``, and a model that does not
    accept `reasoning_effort` would otherwise have it forwarded with no way off.

    MEASURED 2026-09-25: under the removed guard this returned the shipped map
    instead of None, on all three.
    """
    import yaml

    raw = yaml.safe_load(_BASE)
    raw["providers"][provider] = {
        "type": "openai",
        "model": "m",
        "free": True,
        "params": {"reasoning_effort": "disable"},
    }
    cfg = tmp_path / "model_routing.yaml"
    cfg.write_text(yaml.dump(raw))
    (tmp_path / "model_routing.local.yaml").write_text(
        f"providers:\n  {provider}:\n    params: null\n"
    )

    loaded = load_config(cfg, check_api_keys=False)

    assert loaded.providers[provider].params is None, (
        f"the overlay could not clear {provider}'s inherited params map — an "
        "operator has no way to stop the shipped setting being forwarded"
    )


def test_a_bodiless_section_is_still_contained_without_that_guard(tmp_path):
    """The property the removed guard was reaching for, held where it belongs.

    Removing the merge-level rule does NOT reopen the section/entry holes, because
    `_sanitize_local_overlay` and `_parse` both know which keys are structural and
    the merge does not. Measured across all nine bodiless shapes; this is the
    end-to-end restatement so the pairing is visible in one place.
    """
    cfg = _write(tmp_path, _BASE, "providers:\n")

    loaded = load_config(cfg, check_api_keys=False)

    assert set(loaded.providers) >= {"glm", "mistral-large-free"}
    assert "31_outcome_classification" in loaded.call_sites


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


# ── A NEW bodiless entry, not just an override of a known one ───────────────
#
# Found by review on the fix above. The top-level guard deliberately leaves an
# overlay key the base does not define ALONE, because `_parse` ignores
# unrecognised top-level keys. I carried that reasoning one level down, where it
# does not hold: `_parse` ITERATES entries, so a NEW name is parsed like any
# other and a bodiless one is exactly as fatal as a bodiless override.
#
# MEASURED 2026-09-25 with the base-presence condition still in place — `retry`
# was the only section with no protection at all for a new name:
#
#   retry:      {custom:}  (new)   AttributeError, router dark
#   retry:      {default:} (known) caught by the sanitizer
#   call_sites: {99_new:}  (new)   caught by the stale-call-site filter
#   providers:  {newprov:} (new)   caught by _parse's own shape guard


@pytest.mark.parametrize(
    "overlay",
    [
        "retry:\n  custom:\n",
        "retry:\n  custom: oops\n",
        "call_sites:\n  99_brand_new:\n",
        "providers:\n  brand_new_provider:\n",
    ],
    ids=["retry_new", "retry_new_str", "call_site_new", "provider_new"],
)
def test_a_bodiless_NEW_entry_does_not_take_the_router_dark(tmp_path, overlay):
    """A profile nothing references must not be able to disable all routing."""
    cfg = _write(tmp_path, _BASE, overlay)

    loaded = load_config(cfg, check_api_keys=False)

    assert "31_outcome_classification" in loaded.call_sites
    assert set(loaded.providers) >= {"glm", "mistral-large-free"}
    assert "default" in loaded.retry_profiles


def test_parse_skips_a_malformed_retry_profile_in_the_BASE_config(tmp_path):
    """The backstop, bound independently of the sanitizer.

    The sanitizer only ever sees the OVERLAY, so it cannot protect against a typo
    in the shipped config. `_parse` guards providers this way already; retry had
    no equivalent, which is why a single unfinished profile could take every call
    site down. Tested with NO overlay at all, so only the parser can be what saves
    it.
    """
    base = _BASE + textwrap.dedent(
        """
        retry:
          default:
            max_retries: 3
          half_written:
        """
    )
    cfg = _write(tmp_path, base, None)

    loaded = load_config(cfg, check_api_keys=False)

    assert "default" in loaded.retry_profiles
    assert "half_written" not in loaded.retry_profiles
    assert "31_outcome_classification" in loaded.call_sites


@pytest.mark.parametrize(
    "section,name",
    [("retry", "brand_new_profile"), ("providers", "brand_new_provider")],
)
def test_the_sanitizer_removes_a_poisoned_NEW_entry_from_what_is_written_back(
    section, name
):
    """Binds the sanitizer's widened entry predicate, which nothing else holds.

    `_parse`'s shape guards make the CONFIG correct whether or not the sanitizer
    drops a new bodiless entry, so no end-to-end assertion can see this — measured:
    restoring the old base-presence condition leaves every load test green. What
    only the sanitizer does is remove the poison from the dict
    `update_call_site_in_yaml` writes back at :786, so the operator's file is
    healed at the next dashboard save rather than carrying an entry that is
    silently skipped at every load forever.
    """
    from genesis.routing.config import _sanitize_local_overlay

    result = _sanitize_local_overlay(_base_dict(), {section: {name: None}})

    assert name not in result.get(section, {})


# ── The class, swept rather than reported ───────────────────────────────────
#
# Three review rounds each found ONE instance of the same shape: a bodiless YAML
# key reaching a parser that assumes a mapping. Rather than wait for a fourth,
# every section `_parse` iterates was swept with a malformed entry in the BASE
# config and NO overlay, so only the parser's own guards can be what saves it.
#
# MEASURED 2026-09-25 at the time of the sweep: providers guarded, retry guarded
# (added the round before), call_sites RAISING `TypeError: 'NoneType' object is
# not subscriptable`. This test is what stops the set drifting apart again.


@pytest.mark.parametrize("section", ["providers", "call_sites", "retry"])
@pytest.mark.parametrize("bad", [None, "oops", ["a"]], ids=["none", "str", "list"])
def test_parse_skips_a_malformed_entry_in_ANY_base_section(tmp_path, section, bad):
    """One unusable entry must never take the whole router down.

    No overlay is written, so the sanitizer and `_deep_merge` are both out of the
    picture — this binds `_parse`'s own shape guards and nothing else.
    """
    import yaml

    raw = yaml.safe_load(_BASE)
    # _BASE ships no `retry` section, so create it WITH a usable default — the
    # point is that a malformed sibling is skipped, not that the section is absent.
    raw.setdefault(section, {})
    if section == 'retry':
        raw[section].setdefault('default', {'max_retries': 3})
    raw[section]['deliberately_malformed'] = bad
    cfg = tmp_path / "model_routing.yaml"
    cfg.write_text(yaml.dump(raw))

    loaded = load_config(cfg, check_api_keys=False)

    assert "31_outcome_classification" in loaded.call_sites, (
        f"a malformed entry under '{section}' took down an unrelated call site"
    )
    assert "glm" in loaded.providers
    assert "default" in loaded.retry_profiles
    assert "deliberately_malformed" not in loaded.call_sites
