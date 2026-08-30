"""Structural integrity checks for the dashboard webui JS.

dashboard.js defines one large Alpine store as a single object literal. In a
JS object literal a duplicated key is legal and the *second* definition
silently wins — which is how the two ``resolveApproval`` implementations
coexisted with one shadowing the other. These tests parse the store literal
by its stable indentation convention (members at exactly 8 spaces) and
assert every member name is unique.
"""

import re
from pathlib import Path

DASHBOARD_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "genesis"
    / "dashboard"
    / "webui"
    / "js"
    / "dashboard.js"
)

OVERVIEW_HTML = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "genesis"
    / "dashboard"
    / "templates"
    / "partials"
    / "tabs"
    / "overview.html"
)

STORE_OPEN = 'Alpine.store("genesisDashboard", {'
STORE_CLOSE = re.compile(r"^      \}\);")
# Getters are captured WITH their `get `/`set ` prefix, so a duplicated getter
# is caught (the accessor this file guards is one) while a legitimate
# get/set pair for the same name is not flagged as a collision.
MEMBER = re.compile(r"^        (?:async )?((?:get |set )?[A-Za-z_$][\w$]*)\s*[:(]")

# If the file shrinks below this, the indentation convention the parser
# relies on has probably changed and the scan has gone blind — fail loudly
# instead of passing on an empty member list.
MIN_EXPECTED_MEMBERS = 300


def _store_member_names() -> list[str]:
    lines = DASHBOARD_JS.read_text().splitlines()
    names: list[str] = []
    in_store = False
    for line in lines:
        if not in_store:
            if STORE_OPEN in line:
                in_store = True
            continue
        if STORE_CLOSE.match(line):
            break
        m = MEMBER.match(line)
        if m:
            names.append(m.group(1))
    return names


def test_store_literal_found_and_parsed():
    names = _store_member_names()
    assert len(names) >= MIN_EXPECTED_MEMBERS, (
        f"Only {len(names)} store members detected — the store literal or its "
        "8-space member indentation convention has changed; update this test's "
        "parser so duplicate-key detection keeps working."
    )


def test_store_member_names_are_unique():
    names = _store_member_names()
    seen: set[str] = set()
    dupes = sorted({n for n in names if n in seen or seen.add(n)})
    assert not dupes, (
        f"Duplicate keys in the genesisDashboard store literal: {dupes}. "
        "In a JS object literal the second definition silently shadows the "
        "first — merge the implementations instead."
    )


# ---------------------------------------------------------------------------
# discarded-queue counts (2026-08-30) — POSITIVE assertions, by design.
#
# Discarded work is reported by two values that can disagree: `discarded_count`
# (uncapped depth) and `discarded_items` (a LIMIT-20 review sample). The rule is
# ONE accessor, `discardedTotal`, used for every count and every gate — so no
# call site ever has to pick between the two raw values.
#
# The first version of this guard scanned for lines containing the raw field
# name `discarded_items` plus `.length`. That is the wrong key: the surviving
# instance read `discardedQueueGroups.length` — a DERIVED symbol containing
# neither token — so the guard went green while the bug shipped. A negative scan
# can always be routed around by another derived name; these tests therefore
# assert POSITIVELY that each known gate names an accessor.
# ---------------------------------------------------------------------------

NEURAL_MONITOR_HTML = (
    Path(__file__).resolve().parents[2]
    / "src" / "genesis" / "dashboard" / "templates" / "neural_monitor.html"
)
AZ_NEURAL_MONITOR_HTML = (
    Path(__file__).resolve().parents[2]
    / "az_plugins" / "genesis" / "templates" / "neural_monitor.html"
)

# ONE accessor by design. A two-accessor scheme ("print this, gate on that")
# reintroduces a fork at every call site — which is the generator this whole
# guard exists to kill.
_ACCESSOR = "discardedTotal"

# Every spelling that resolves to the capped sample. A guard keyed on only the
# raw field name is routed around by the first derived symbol someone adds.
_SAMPLE_DERIVED = ("discarded_items", "discardedQueueGroups")

# The discarded panel is one card; scanning the whole 1000-line template makes
# anchors like ":disabled=" match unrelated buttons. Extract the card first so
# every assertion below is scoped to the code under guard.
_PANEL_START = 'id="queue-review"'
_PANEL_END = 'class="health-card"'


def _discarded_panel() -> str:
    """The queue-review CARD only.

    Scoping matters: anchors like ``:disabled=`` match unrelated buttons
    elsewhere in a 1200-line template, so an unscoped ``any(...)`` would pass on
    a different button while the one under guard regressed. The first attempt at
    this helper used an end anchor that occurs BEFORE the start anchor, so it
    silently returned the rest of the file and scoped nothing — hence the
    bounded assertion below.
    """
    src = OVERVIEW_HTML.read_text()
    a = src.index(_PANEL_START)
    nxt = src.find(_PANEL_END, a + len(_PANEL_START))
    panel = src[a:nxt] if nxt != -1 else src[a:]
    total = len(src.splitlines())
    assert len(panel.splitlines()) < total * 0.5, (
        "the panel extractor is not scoping — it returned "
        f"{len(panel.splitlines())} of {total} lines. Re-check the anchors."
    )
    return panel


def _accessor_body(name: str) -> str:
    js = DASHBOARD_JS.read_text()
    i = js.index(f"get {name}()")
    return js[i : js.index("},", i)]


def _member_line_span(name: str) -> set[int]:
    """1-based line numbers occupied by a store member's body.

    Exemptions must be scoped by POSITION, not by text. Comparing a line to the
    concatenated body of the sanctioned accessors (`ln not in sanctioned_js`)
    exempts any line ANYWHERE in the file that happens to be textually identical
    to one inside an accessor -- so the guard could be walked past by copying a
    sanctioned line verbatim, which is exactly the evasion it exists to stop.
    """
    lines = DASHBOARD_JS.read_text().splitlines()
    start = None
    for i, ln in enumerate(lines, 1):
        if f"get {name}()" in ln or ln.startswith(f"        {name}("):
            start = i
            break
    assert start is not None, f"member `{name}` not found in dashboard.js"
    for j in range(start, len(lines) + 1):
        if lines[j - 1] == "        },":
            return set(range(start, j + 1))
    raise AssertionError(f"`{name}` body never closed at member indentation")


def test_alpine_store_exposes_exactly_one_accessor():
    """One number, used for every count and every gate."""
    js = DASHBOARD_JS.read_text()
    assert f"get {_ACCESSOR}()" in js, f"dashboard.js must expose `{_ACCESSOR}`"
    assert "get discardedAny()" not in js, (
        "a second accessor reintroduces the print-vs-gate fork that generated "
        "this bug class — keep exactly one"
    )
    body = _accessor_body(_ACCESSOR)
    assert "discarded_count ??" in body, (
        "must use `??` — `||` replaces a legitimate 0 with the sample length"
    )
    assert "Math.max" in body, (
        "must be max(count, sample) — both divergences are reachable and the "
        "larger value is the honest one in each"
    )


def test_clear_all_control_is_gated_on_the_accessor():
    """The button that clears every row must not be gated on the 20-row sample.

    On /genesis/monitor that exact mistake left a 148-row backlog with no
    clear-all control at all.
    """
    panel = _discarded_panel()
    assert "clearAllDiscardedItems()" in panel, "clear-all button vanished from the panel"
    disabled = [ln for ln in panel.splitlines() if ":disabled=" in ln]
    assert disabled, "clear-all button lost its :disabled binding"
    assert any(_ACCESSOR in ln for ln in disabled), (
        "the clear-all :disabled must read the accessor:\n  "
        + "\n  ".join(ln.strip()[:120] for ln in disabled)
    )


def test_empty_state_is_gated_on_the_total_not_the_sample():
    """The exact 2026-08-30 BLOCKER, pinned.

    `discardedQueueGroups` derives from the LIMIT-20 sample. Gating the
    "nothing to review" message on it rendered that message directly beneath
    "showing 0 of 148" with an enabled "Clear all 148" button.
    """
    lines = _discarded_panel().splitlines()
    idx = next(
        (i for i, ln in enumerate(lines)
         if "No discarded or expired items awaiting review" in ln),
        None,
    )
    assert idx is not None, "empty-state message vanished"
    gate = next((lines[j] for j in range(idx - 1, -1, -1) if "x-if=" in lines[j]), "")
    assert f"{_ACCESSOR} === 0" in gate, (
        "the empty-state message must be gated on the accessor === 0, not on "
        f"the sample-derived group list. Its gate is:\n  {gate.strip()[:160]}"
    )


def test_panel_explains_a_populated_count_with_an_unloadable_sample():
    """count>0 with an empty sample must say so, not claim there is nothing."""
    panel = _discarded_panel()
    assert "review sample could not be loaded" in panel, (
        "the panel must explain the count>0 / empty-sample state explicitly"
    )


def test_neural_monitor_surfaces_do_not_gate_on_the_sample():
    """Both copies of the /genesis/monitor page carry the same class."""
    for path in (NEURAL_MONITOR_HTML, AZ_NEURAL_MONITOR_HTML):
        src = path.read_text()
        assert "queues.discarded_count ?? items.length" in src, (
            f"{path.name}: count must use `??`, not `||`"
        )
        assert "if (items.length > 1) {" not in src, (
            f"{path.name}: the clear-all button must not be gated on the sample "
            "length — that left a populated backlog with no clear control."
        )
        assert "showing ' + items.length" in src, (
            f"{path.name}: must label the sample as truncated when count > sample"
        )


def test_no_sample_derived_gate_escapes_the_accessor():
    """A predicate on ANY sample-derived value must be paired with the accessor.

    Keyed on the failure that survived a whole review round: the offending gate
    read `discardedQueueGroups.length` — a DERIVED symbol containing neither
    `discarded_count` nor `discarded_items`, so a scan for the raw field name
    walked straight past it. Both spellings are covered here, and the one
    legitimate sample predicate (the unloadable-sample branch) is legitimate
    precisely because it ALSO reads the accessor.
    """
    offenders: list[str] = []
    js = DASHBOARD_JS.read_text()
    sanctioned = _member_line_span(_ACCESSOR) | _member_line_span("discardedQueueGroups")
    for lineno, ln in enumerate(js.splitlines(), 1):
        if any(t in ln for t in _SAMPLE_DERIVED) and ".length" in ln and lineno not in sanctioned:
            offenders.append(f"dashboard.js:{lineno}: {ln.strip()[:110]}")
    for lineno, ln in enumerate(OVERVIEW_HTML.read_text().splitlines(), 1):
        if not (any(t in ln for t in _SAMPLE_DERIVED) and ".length" in ln):
            continue
        if _ACCESSOR in ln:  # paired with the accessor -> legitimate
            continue
        offenders.append(f"overview.html:{lineno}: {ln.strip()[:110]}")
    assert not offenders, (
        "Sample-derived predicate not paired with the accessor — this is the "
        "shape that survived a review round:\n  " + "\n  ".join(offenders)
    )


def test_no_raw_sample_length_read_outside_the_accessors():
    """Backstop for NEW code.

    The two accessor bodies are the only sanctioned readers of the raw sample
    length; the truncation label legitimately prints it. Anything else is the
    bug class returning.
    """
    js = DASHBOARD_JS.read_text()
    sanctioned = _member_line_span(_ACCESSOR) | _member_line_span("discardedQueueGroups")
    offenders = [
        f"dashboard.js:{i}: {ln.strip()[:110]}"
        for i, ln in enumerate(js.splitlines(), 1)
        if "discarded_items" in ln and ".length" in ln and i not in sanctioned
    ]
    # In the template the sanctioned reader is the truncation label, and it is
    # sanctioned because it ALSO reads the accessor -- not because it contains
    # the word "showing", which any new line could adopt for free.
    offenders += [
        f"overview.html:{i}: {ln.strip()[:110]}"
        for i, ln in enumerate(OVERVIEW_HTML.read_text().splitlines(), 1)
        if "discarded_items" in ln and ".length" in ln and _ACCESSOR not in ln
    ]
    assert not offenders, (
        "Raw LIMIT-20 sample-length reads outside the accessors:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_is_not_blind():
    """Blindness guard: the scans above pass trivially if nothing is there."""
    html = OVERVIEW_HTML.read_text()
    js = DASHBOARD_JS.read_text()
    assert html.count(_ACCESSOR) >= 6, (
        "overview.html should render/gate on the accessor in several places"
    )
    assert js.count(_ACCESSOR) >= 4, "dashboard.js should define and use the accessor"
    for path in (NEURAL_MONITOR_HTML, AZ_NEURAL_MONITOR_HTML):
        assert path.exists(), f"{path} missing — the scan would silently cover nothing"


def _member_body(name: str) -> str:
    """The body of a store member declared as ``name() {`` or ``get name() {``.

    Scoped by the store's 8-space indentation convention, same as the member
    parser above: read to the first line that closes a member (``        },``).
    """
    js = DASHBOARD_JS.read_text()
    for opener in (f"get {name}()", f"{name}()"):
        i = js.find(opener)
        if i != -1:
            break
    assert i != -1, f"dashboard.js does not define `{name}`"
    lines = js[i:].splitlines()
    out = []
    for ln in lines:
        out.append(ln)
        if ln == "        },":
            break
    else:
        raise AssertionError(f"`{name}` body never closed at member indentation")
    return "\n".join(out)


def _code_only(body: str) -> str:
    """`body` with whole-line `//` comments removed.

    Ordering assertions must read the CODE. The first version of the guard
    below compared positions of a phrase that also appeared in the explanatory
    comment it sat next to, so it reported the check as mis-ordered purely
    because prose mentioned the branch it was guarding. A guard satisfiable --
    or breakable -- by a comment is not a guard.
    """
    return "\n".join(
        ln for ln in body.splitlines() if not ln.strip().startswith("//")
    )


def test_queue_verdict_is_unknown_when_the_section_failed():
    """A failed queues section must not be reported as healthy.

    `_or_error` replaces a failed gather section with `{"status": "error"}` --
    truthy, and carrying NO `errors` key. So a verdict that only inspects
    `errors` reads every depth field as a missing-zero and falls through to
    "healthy - queues are clear", rendered in the same card as the panel's own
    "Queue data unavailable" banner and folded into the top-level status.

    Zeros that were never measured are not a clean bill of health.
    """
    body = _code_only(_member_body("queuesSemantic"))
    assert 'queues.status === "error"' in body, (
        "queuesSemantic() must treat a failed section as unknown; keying only "
        "on `errors` misses _or_error's shape, which has no `errors` key"
    )
    # Ordering is the whole point: the check is worthless after the depth
    # comparisons, because those already returned a verdict built from zeros.
    assert body.index('queues.status === "error"') < body.index('state: "healthy"'), (
        "the failed-section check must precede the depth checks, or the "
        "verdict is already decided from counters that were never collected"
    )


def test_unknown_banner_is_scoped_to_its_own_source():
    """The banner must not deny numbers the same panel is displaying.

    `queues.errors` is ONE list shared by all four queue sources, each entry
    source-prefixed. Keyed on the whole list, a `deferred_work` failure printed
    "Queue data unavailable - this is not a confirmed zero" directly above a
    correctly-counted discarded list with an enabled "Clear all 148".
    """
    body = _code_only(_member_body("queuesDataUnknown"))
    assert '(q.errors || []).length > 0' not in body, (
        "queuesDataUnknown must not fire on an unrelated source's failure"
    )
    assert 'startsWith("discarded")' in body, (
        "scope the banner to discarded-side failures (entries are prefixed)"
    )
    assert 'q.status === "error"' in body, (
        "a whole-section failure must still mark the count unknown"
    )


def test_neural_monitor_count_survives_a_non_numeric_payload():
    """`Math.max` coerces, so an unguarded non-numeric count yields NaN.

    NaN makes `=== 0`, `> items.length` and `> 1` ALL false at once, so the
    header would read "Discarded (NaN)" and the clear-all button would vanish
    -- the exact defect this block was written to fix, re-entering through
    coercion. dashboard.js guards it; both plain-JS copies must too.
    """
    for path in (NEURAL_MONITOR_HTML, AZ_NEURAL_MONITOR_HTML):
        src = path.read_text()
        assert "Number.isFinite" in src, (
            f"{path.name}: guard the reported count with Number.isFinite before "
            "Math.max, or a non-numeric count silently removes the clear control"
        )
