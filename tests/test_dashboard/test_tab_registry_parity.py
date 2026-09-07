"""Every dashboard tab is wired into ALL of its registries.

Adding a tab needs coordinated edits across `dashboard.js`, the page template
and the nav chrome, and NOTHING verified them until this file. That matters
more than it sounds, because the two worst failures are SILENT:

  * missing from the `valid` array  -> the hash router rejects the tab and
    falls back to Overview, so the nav button appears to do nothing;
  * missing its `_startTabIntervals` case -> the panel renders and never
    fetches, so it sits permanently empty with no error anywhere.

Neither raises, neither logs, and no route test can see either — a tab wired
into zero JS registries still passes every HTTP test of its endpoint.

MEASURED when this file was written: 18 tabs, of which `sessions` was missing
from `_tabInitialized` (fixed in the same commit so this lands green). That
drift was benign — Alpine's proxy tolerates the absent key — which is exactly
why it survived: nothing was broken enough to notice.

WHY SET EQUALITY AND NOT A LOOP OVER `valid`. The first version of this file
iterated the `valid` array and checked each tab against the other registries.
Its own verify-RED killed it: deleting a tab FROM `valid` left the suite GREEN,
because the deleted tab simply stopped being iterated — the single most
dangerous omission was the one shape the test could not see. A test whose
universe is one of the things it checks silently shrinks instead of failing.

So the universe is the UNION of three INDEPENDENT sources — the router
whitelist, the nav buttons, and the template includes — and each source must
equal it. Removing a tab from any one of them now breaks the equality from the
other two.

Deliberately a TEXT parse rather than a JS engine: the store is one 5000-line
object literal, and the alternative is executing the dashboard's JS in a
runtime none of CI has. The parses are pinned by
`test_registries_are_parseable_at_all`, so a refactor that defeats this file
fails loudly instead of silently passing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JS = _ROOT / "src/genesis/dashboard/webui/js/dashboard.js"
_PAGE = _ROOT / "src/genesis/dashboard/templates/genesis_dashboard.html"
_NAV = _ROOT / "src/genesis/dashboard/templates/partials/chrome/header.html"

# Tabs that legitimately have no poller. A tab whose data is static or fetched
# once does not belong in _TAB_INTERVALS, so an entry here is a DECLARATION
# that the omission is deliberate rather than an oversight.
_NO_POLLER = frozenset(
    {
        "overview",
        "chat",
        "config",
        "files",
        "internals",
        "memory",
        "knowledge",
        "references",
        "calibration",
        "backup",
    }
)


def _js() -> str:
    return _JS.read_text()


# ── The three INDEPENDENT sources ──────────────────────────────────────────


def _router_tabs(js: str) -> set[str]:
    """The hash router's whitelist. A tab absent here falls back to Overview."""
    m = re.search(r"const valid = \[(.*?)\];", js, re.S)
    assert m, "could not find the hash-router `valid` array in dashboard.js"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _nav_tabs() -> set[str]:
    """Tabs reachable by clicking. Independent of anything in dashboard.js."""
    return set(re.findall(r"location\.hash = '([A-Za-z0-9_-]+)'", _NAV.read_text()))


def _include_tabs() -> set[str]:
    """Tabs whose panel is actually rendered into the page.

    The template filename underscores what the JS key hyphenates
    (`follow-ups` -> `follow_ups.html`), which every existing tab follows.
    """
    names = re.findall(r'include "partials/tabs/([a-z0-9_]+)\.html"', _PAGE.read_text())
    return {n.replace("_", "-") for n in names}


def _universe() -> set[str]:
    return _router_tabs(_js()) | _nav_tabs() | _include_tabs()


# ── The dependent registries ───────────────────────────────────────────────


def _tab_initialized_keys(js: str) -> set[str]:
    m = re.search(r"_tabInitialized:\s*\{(.*?)\},", js, re.S)
    assert m, "could not find `_tabInitialized` in dashboard.js"
    return set(re.findall(r'"?([A-Za-z0-9_-]+)"?\s*:', m.group(1)))


def _tab_interval_keys(js: str) -> set[str]:
    m = re.search(r"_TAB_INTERVALS:\s*\{(.*?)\n        \},", js, re.S)
    assert m, "could not find `_TAB_INTERVALS` in dashboard.js"
    return set(re.findall(r'"?([A-Za-z0-9_-]+)"?\s*:\s*\[', m.group(1)))


def _switch_cases(js: str) -> set[str]:
    """Cases inside `_startTabIntervals` ONLY.

    Anchored to that function's body rather than scanned file-wide: a
    same-named case in any unrelated switch would otherwise satisfy the
    check, which is a guard passing on the wrong evidence.
    """
    m = re.search(r"_startTabIntervals\(tab\)\s*\{(.*?)\n        \},", js, re.S)
    assert m, "could not find the `_startTabIntervals` body in dashboard.js"
    return set(re.findall(r'case "([A-Za-z0-9_-]+)":', m.group(1)))


def _fetch_state_keys(js: str) -> set[str]:
    """The `fetchState` panel registry.

    The SEVENTH registry, and the one whose omission is worst: `startFetch`,
    `finishFetch` and `failFetch` all early-return on a missing key
    (`if (!state) return;`), so `panelState()` yields "unknown" and BOTH the
    loading gate and the error banner silently never fire — the panel renders
    nothing on failure, with no error anywhere.

    It also cannot be derived: the key is camelCase (`zeroDrop`) with no
    mechanical relation to the hyphenated tab id, which is exactly why it needs
    a declared mapping and a test rather than a convention.
    """
    m = re.search(r"fetchState:\s*\{(.*?)\n        \},", js, re.S)
    assert m, "could not find `fetchState` in dashboard.js"
    return set(re.findall(r"^\s*([A-Za-z0-9_]+):\s*\{\s*state:", m.group(1), re.M))


# tab id -> its `fetchState` key, for tabs whose panel uses `panelState()`.
# A tab absent here is declaring that its template never calls panelState().
_FETCH_STATE_KEY = {
    "observations": "observations",
    "zero-drop": "zeroDrop",
}


# ── Tests ──────────────────────────────────────────────────────────────────


def test_registries_are_parseable_at_all():
    """Guard the guard.

    Every assertion below is only as good as these parses. If a refactor
    renames a registry or reshapes the literal, the regexes silently match
    nothing and the file passes vacuously — so pin non-empty results, and pin
    one tab that certainly exists.
    """
    js = _js()
    assert len(_router_tabs(js)) >= 10, "the `valid` parse looks empty — regex has drifted"
    assert len(_nav_tabs()) >= 10, "the nav parse looks empty — regex has drifted"
    assert len(_include_tabs()) >= 10, "the include parse looks empty — regex has drifted"
    assert len(_tab_initialized_keys(js)) >= 10, "`_tabInitialized` parse looks empty"
    assert len(_tab_interval_keys(js)) >= 5, "`_TAB_INTERVALS` parse looks empty"
    assert len(_switch_cases(js)) >= 10, "`_startTabIntervals` case parse looks empty"
    assert "overview" in _universe(), "the parses did not find a tab that certainly exists"


def test_the_three_independent_sources_agree_exactly():
    """Router whitelist == nav buttons == template includes.

    This is the test that catches a tab dropped from the ROUTER, which a loop
    over the router cannot see. Each difference is reported by direction so the
    failure names which edit was missed.
    """
    js = _js()
    router, nav, inc = _router_tabs(js), _nav_tabs(), _include_tabs()
    universe = router | nav | inc

    assert universe - router == set(), (
        f"tabs have a nav button and/or a panel but are MISSING from the router "
        f"whitelist — clicking them silently lands on Overview: {sorted(universe - router)}"
    )
    assert universe - nav == set(), (
        f"tabs are routable and/or rendered but have NO nav button — unreachable "
        f"by clicking: {sorted(universe - nav)}"
    )
    assert universe - inc == set(), (
        f"tabs are routable and/or in the nav but their panel is never INCLUDED — "
        f"the tab opens empty: {sorted(universe - inc)}"
    )


@pytest.mark.parametrize("tab", sorted(_universe()))
def test_every_tab_is_wired_into_every_registry(tab):
    """One tab, every registry it must appear in.

    Parametrised per tab so a failure NAMES the tab and the registry rather
    than reporting one opaque set difference. The parametrisation source is the
    UNION, so a tab removed from any single registry is still checked.
    """
    js = _js()

    assert tab in _router_tabs(js), (
        f"tab {tab!r} is missing from the router `valid` array — the nav click "
        f"silently falls back to Overview"
    )
    assert tab in _tab_initialized_keys(js), f"tab {tab!r} is missing from `_tabInitialized`"
    assert f'include "partials/tabs/{tab.replace("-", "_")}.html"' in _PAGE.read_text(), (
        f"tab {tab!r} has no include in genesis_dashboard.html — the panel is never rendered"
    )
    assert f"location.hash = '{tab}'" in _NAV.read_text(), (
        f"tab {tab!r} has no nav button in chrome/header.html — it is unreachable by clicking"
    )


@pytest.mark.parametrize("tab", sorted(_tab_interval_keys(_JS.read_text())))
def test_every_polling_tab_can_stop_its_poller(tab):
    """A tab that registers an interval must have a case that STARTS it.

    An entry in `_TAB_INTERVALS` naming a field nothing ever sets is dead
    config, and it hides the fact that the tab is not actually polling.
    """
    assert tab in _switch_cases(_js()), (
        f"tab {tab!r} is in `_TAB_INTERVALS` but has no `case {tab!r}:` in _startTabIntervals"
    )


def test_a_tab_that_starts_a_poller_can_also_stop_it():
    """The pairing that prevents a leak across tab switches.

    `_stopTabIntervals` clears only the fields `_TAB_INTERVALS` names, so a tab
    whose case calls setInterval without a matching entry leaks that timer for
    the rest of the session — it keeps polling while the user is on another
    tab, forever.
    """
    js = _js()
    intervals = _tab_interval_keys(js)
    leaking = []
    for tab in _universe():
        m = re.search(rf'case "{re.escape(tab)}":(.*?)(?=\n\s+case "|\n\s+\}}\n)', js, re.S)
        if m and "setInterval" in m.group(1) and tab not in intervals:
            leaking.append(tab)
    assert not leaking, (
        f"these tabs call setInterval but are absent from _TAB_INTERVALS, so their "
        f"pollers are never cleared on tab exit: {sorted(leaking)}"
    )


def test_a_non_polling_tab_declares_itself():
    """A tab absent from `_TAB_INTERVALS` is either deliberate or an oversight.

    This test cannot tell those apart, so it requires the deliberate ones to
    SAY SO by appearing in `_NO_POLLER`. That turns "why has this tab no
    poller?" from a question nobody can answer into a one-line declaration.
    """
    intervals = _tab_interval_keys(_js())
    undeclared = sorted(t for t in _universe() if t not in intervals and t not in _NO_POLLER)
    assert not undeclared, (
        f"these tabs have no poller and are not declared in _NO_POLLER — add them "
        f"there if that is intended, or give them a _TAB_INTERVALS entry: {undeclared}"
    )


@pytest.mark.parametrize(("tab", "key"), sorted(_FETCH_STATE_KEY.items()))
def test_a_panel_that_calls_panelState_has_a_fetchState_key(tab, key):
    """The seventh registry — silent, and undetectable by every other check.

    Without the key, `startFetch`/`finishFetch`/`failFetch` all early-return,
    `panelState()` returns "unknown", and the panel's loading gate AND its
    error banner both stop firing. The tab still routes, still renders and
    still fetches, so nothing else in this file would notice.
    """
    assert key in _fetch_state_keys(_js()), (
        f"tab {tab!r} declares fetchState key {key!r} but it is missing from the "
        f"`fetchState` registry — its loading gate and error banner are both dead"
    )


def test_every_panelState_call_in_a_tab_template_is_declared():
    """Derived from the TEMPLATES, so a new panel cannot forget the mapping.

    The mapping above is hand-written, which makes it exactly the kind of
    convention that goes stale. This reads every `panelState('x')` call out of
    the tab templates and requires each one to be a registered key — so adding
    a panel that gates on a fetch state fails here until it is wired.
    """
    tabs_dir = _ROOT / "src/genesis/dashboard/templates/partials/tabs"
    used: set[str] = set()
    for f in tabs_dir.glob("*.html"):
        used |= set(re.findall(r"panelState\('([A-Za-z0-9_]+)'\)", f.read_text()))

    assert used, "no panelState() calls found — the regex has drifted"
    missing = sorted(used - _fetch_state_keys(_js()))
    assert not missing, (
        f"tab templates call panelState() for keys absent from the `fetchState` "
        f"registry, so their loading/error gates are dead: {missing}"
    )
