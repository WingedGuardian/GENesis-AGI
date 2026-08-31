"""Structural integrity checks for the dashboard webui JS.

dashboard.js defines one large Alpine store as a single object literal. In a
JS object literal a duplicated key is legal and the *second* definition
silently wins — which is how the two ``resolveApproval`` implementations
coexisted with one shadowing the other. These tests parse the store literal
by its stable indentation convention (members at exactly 8 spaces) and
assert every member name is unique.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
# History of this guard, kept because each version failed differently and the
# next design was chosen to fix the previous failure.
#
# v1 scanned for the raw field name `discarded_items` plus `.length`. Wrong key:
#    the surviving bug read `discardedQueueGroups.length`, a DERIVED symbol
#    containing neither token, so the scan went green while the defect shipped.
# v2 asserted POSITIVELY that each known gate named the `discardedTotal`
#    accessor. Better, but it enumerated the gates — and a gate nobody thought to
#    enumerate was not covered.
# v3 (current) removes the thing being guarded instead of guarding it: the
#    backend reconciles the depth and the sample into one object, so no frontend
#    expression chooses between two numbers. What remains is a PROHIBITION —
#    the raw keys may not appear in a frontend file at all — plus behavioural
#    checks that execute the normalisers.
#
# Two lessons from v1/v2 are load-bearing in what follows. Scope every assertion
# by EXTRACTION (the enclosing attribute, the function body), never by substring
# or proximity: an audit of v3 found three assertions that could not fail,
# because their needles also occurred elsewhere in the same file. And an
# agreement check needs an oracle: three implementations agreeing proves nothing
# if all three are wrong, so the shape harness asserts expected values too.
# ---------------------------------------------------------------------------

NEURAL_MONITOR_HTML = (
    Path(__file__).resolve().parents[2]
    / "src" / "genesis" / "dashboard" / "templates" / "neural_monitor.html"
)
AZ_NEURAL_MONITOR_HTML = (
    Path(__file__).resolve().parents[2]
    / "az_plugins" / "genesis" / "templates" / "neural_monitor.html"
)

# --------------------------------------------------------------------------
# Discarded-queue rendering
# --------------------------------------------------------------------------
# The backend reconciles the uncapped depth and the LIMIT-20 review sample into
# ONE `queues.discarded` object, so no render surface chooses between two
# numbers that can disagree. These guards are keyed as a PROHIBITION rather than
# an allowlist: the previous shape sanctioned specific expressions, and each
# review round found a new spelling that walked around it — a derived symbol, a
# line copied verbatim from a sanctioned body, a file that was not in the list.
#
# Scanning DIRECTORIES rather than named files is deliberate: a new template
# cannot escape the guard by not being enumerated.

_FRONTEND_DIRS = (
    Path(__file__).resolve().parents[2] / "src" / "genesis" / "dashboard" / "webui" / "js",
    Path(__file__).resolve().parents[2] / "src" / "genesis" / "dashboard" / "templates",
    Path(__file__).resolve().parents[2] / "az_plugins" / "genesis" / "templates",
)

# The raw producer keys. Reading either in a frontend file means that file is
# reconciling the fork again instead of consuming the reconciled object.
_RAW_KEYS = ("discarded_count", "discarded_items")

_NORMALISER_MARKS = ("Array.isArray", "Number.isFinite")


def _frontend_files() -> list[Path]:
    out: list[Path] = []
    for d in _FRONTEND_DIRS:
        assert d.is_dir(), f"frontend directory missing, guard would scan nothing: {d}"
        out.extend(p for p in d.rglob("*") if p.suffix in (".js", ".html") and p.is_file())
    assert out, "no frontend files found — the scan has gone blind"
    return out


# A RAW reader touches the payload straight off the health snapshot
# (`queues.discarded` / `queues?.discarded`) and therefore owns the job of
# surviving a malformed or absent one. A file consuming an already-normalised
# accessor — `$store.genesisDashboard.discarded` in the Alpine template — is a
# renderer, and requiring it to re-guard would push a second normalisation into
# the exact layer this change removed one from.
_RAW_READ = re.compile(r"queues\s*\??\.\s*discarded\b")


def _raw_discarded_readers() -> list[Path]:
    return [p for p in _frontend_files() if _RAW_READ.search(p.read_text())]


# Start/end markers for each raw reader's normaliser. Extraction beats a
# whole-file scan: the marks these guards look for occur elsewhere in the same
# files for unrelated reasons, so an unscoped assertion cannot fail.
_NORMALISER_BOUNDS = (
    ("function normalizeDiscarded(queues) {", "\nfunction renderDiscardedItems"),
    ("get discarded() {", "get discardedQueueGroups()"),
)


def _normaliser_body(path: Path) -> str:
    src = path.read_text()
    for start, end in _NORMALISER_BOUNDS:
        i = src.find(start)
        if i == -1:
            continue
        j = src.find(end, i)
        assert j != -1, f"{path.name}: normaliser opened but never closed"
        return src[i:j]
    raise AssertionError(
        f"{path.name} reads `queues.discarded` but defines no recognised "
        "normaliser — either it is reconciling inline, or these bounds are stale"
    )


def test_no_frontend_file_reads_the_raw_producer_keys():
    """The whole bug class, forbidden outright.

    `discarded_count` and `discarded_items` are the two values whose
    reconciliation produced three rounds of defects. The backend now publishes
    one object; a frontend reading either raw key is re-opening the fork, and no
    exemption is granted for any file, line or spelling.
    """
    offenders = [
        f"{p.name}:{i}: {ln.strip()[:100]}"
        for p in _frontend_files()
        for i, ln in enumerate(p.read_text().splitlines(), 1)
        if any(k in ln for k in _RAW_KEYS)
    ]
    assert not offenders, (
        "frontend files reading the raw producer keys instead of the reconciled "
        "`queues.discarded` object:\n  " + "\n  ".join(offenders)
    )


def test_each_reader_normalises_defensively():
    """Every file that reads the object guards the shapes transport can damage.

    An older server sends no object at all, `_or_error` can replace the whole
    queues section, and a non-numeric total would make `> 0` and `=== 0` BOTH
    false — rendering neither the list nor the empty state while the button read
    "Clear all NaN".
    """
    readers = _raw_discarded_readers()
    assert len(readers) >= 3, (
        f"expected the three raw readers (dashboard.js and both monitor pages), "
        f"found {[p.name for p in readers]} — the scan has gone blind"
    )
    for path in readers:
        # Scope to the normaliser BODY. A file-wide token scan passes on marks
        # that appear anywhere: `Array.isArray` already occurs several times in
        # both dashboard.js and neural_monitor.html for unrelated reasons, so
        # deleting it from the normaliser left the whole-file assertion green.
        body = _normaliser_body(path)
        missing = [m for m in _NORMALISER_MARKS if m not in body]
        assert not missing, (
            f"{path.name}'s discarded normaliser omits {', '.join(missing)} — a "
            "malformed or absent payload would render as a confident number"
        )


def test_the_empty_state_distinguishes_measured_zero_from_unknown():
    """"Nothing awaiting review" may only be claimed for a MEASURED zero.

    `known` is the producer's own statement about whether it counted. Gating the
    empty state on the total alone reports an unread depth as an empty queue —
    the confident-zero defect, one layer up from where it was fixed.
    """
    panel = (
        Path(__file__).resolve().parents[2]
        / "src" / "genesis" / "dashboard" / "templates" / "partials" / "tabs" / "overview.html"
    ).read_text()
    assert "discarded.known" in panel, (
        "the panel never consults `known`, so it cannot tell a counted zero "
        "from a count that failed"
    )
    assert "No discarded or expired items awaiting review" in panel
    i = panel.index("No discarded or expired items awaiting review")
    # Scope to the ENCLOSING template, not a character window. A window wide
    # enough to contain the gate also contains the neighbouring unknown-depth
    # banner, whose own `discarded.known` satisfied the assertion while the gate
    # under test had lost it — the guard passed against the exact regression.
    j = panel.rindex('<template x-if="', 0, i)
    gate = panel[j:panel.index('">', j)]
    assert "discarded.known" in gate, (
        "the empty-state message is not gated on `known` — an unread depth "
        f"would render as 'nothing awaiting review'. Gate reads: {gate[:160]}"
    )


def test_clear_all_survives_an_unknown_depth():
    """The control that clears the backlog must not vanish when the count fails.

    The DELETE removes every discarded/expired row regardless of what was
    counted, so withholding the button because the COUNT failed strands the
    backlog — the worst variant found on the monitor page, where a populated
    queue rendered with no clear control at all.
    """
    for rel in ("src/genesis/dashboard/templates/neural_monitor.html",
                "az_plugins/genesis/templates/neural_monitor.html"):
        src = (Path(__file__).resolve().parents[2] / rel).read_text()
        assert "if (!d.known || d.truncated || d.total > 1)" in src, (
            f"{rel}: the clear-all gate does not include the unknown-depth case"
        )

    panel = (
        Path(__file__).resolve().parents[2]
        / "src" / "genesis" / "dashboard" / "templates" / "partials" / "tabs" / "overview.html"
    ).read_text()
    # Anchor on the CLICK HANDLER, which is unique to this button, then take
    # the `:disabled` immediately before it. Two earlier attempts show why the
    # anchor has to be unique rather than merely nearby: searching the panel for
    # the gate expression matched a second, unrelated occurrence, and searching
    # for the first `:disabled=` matched a setup button hundreds of lines above.
    i = panel.index("clearAllDiscardedItems")
    j = panel.rindex(":disabled=", 0, i)
    binding = panel[j:panel.index('"', panel.index('"', j) + 1) + 1]
    assert "discarded.known" in binding, (
        "the clear-all button is disabled on the total alone, so a depth that "
        f"could not be read disables the only control that clears it. "
        f"Binding reads: {binding[:160]}"
    )


def test_the_three_normalisers_agree_on_every_payload_shape():
    """Execute all three copies against the shape table and compare outputs.

    Static assertions cannot tell whether three independently-edited copies
    still BEHAVE the same, and "they look similar" is what allowed the monitor
    pages to drift from dashboard.js in the first place. This runs them.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; the static guards above still apply")

    harness = Path(__file__).resolve().parents[2] / "tests" / "assets" / "discarded_shape_check.js"
    assert harness.is_file(), f"shape harness missing: {harness}"
    r = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        "the three discarded normalisers disagree on at least one payload "
        f"shape:\n{r.stdout}\n{r.stderr}"
    )


def test_queue_verdict_is_unknown_when_the_section_failed():
    """A failed queues section must not be reported as healthy.

    `_or_error` replaces a failed section with `{"status": "error"}` — truthy,
    and carrying NO `errors` key. So a verdict that only inspects `errors` reads
    every depth field as a missing-zero and falls through to "healthy - queues
    are clear", rendered in the same card as the panel's own "Queue data
    unavailable" notice and folded into the top-level status.

    Restored here after the guard rewrite dropped it with the helpers it used.
    The production check was never removed; only its test was, which is the
    quieter half of that mistake.
    """
    js = DASHBOARD_JS.read_text()
    i = js.index("queuesSemantic() {")
    body = js[i:js.index("\n        },", i)]
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))
    assert 'queues.status === "error"' in code, (
        "queuesSemantic() must treat a failed section as unknown; keying only "
        "on `errors` misses _or_error's shape, which carries no `errors` key"
    )
    assert code.index('queues.status === "error"') < code.index('state: "healthy"'), (
        "the failed-section check must precede the depth checks, or the verdict "
        "is already decided from counters that were never collected"
    )


# `total` is a FLOOR when the depth could not be read: the count contributes
# nothing, so it collapses to the rows in hand — at most the sample cap.
# Rendering it bare produced "showing 20 of 20" for a 148-row queue, a total
# asserted from a number that was never a total.
#
# The distinction is therefore made ONCE, in each normaliser, as `totalLabel`
# ("148" when measured, "20+" when not). Predicates may read `.total`; nothing
# may render it. That is checkable without knowing which enclosing block a
# given expression sits in — the first version of this guard tried to decide
# that from the line alone and flagged two correctly-guarded sites, because the
# guards were on the enclosing template and two lines further down.
_RENDERS_RAW_TOTAL = re.compile(r"discarded\.total(?!Label)(?!\s*(?:>|<|===|!==|==)\s*\d)")


def test_no_frontend_renders_the_raw_total():
    """Only `totalLabel` may reach a user; `.total` is for predicates.

    Guards the CLASS rather than the three sites that had it wrong, because the
    same mistake is available to every new expression that prints the value —
    and the label makes it unrepresentable rather than merely currently-correct.
    """
    offenders = [
        f"{p.name}:{i}: {ln.strip()[:110]}"
        for p in _frontend_files()
        for i, ln in enumerate(p.read_text().splitlines(), 1)
        if _RENDERS_RAW_TOTAL.search(ln)
    ]
    assert not offenders, (
        "these render the discarded depth directly instead of via `totalLabel`, "
        "so when the count query fails they state the sample size as a measured "
        "total:\n  " + "\n  ".join(offenders)
    )


def test_every_normaliser_publishes_the_label():
    """All three copies must expose it, or a surface silently renders undefined."""
    for path in _raw_discarded_readers():
        body = _normaliser_body(path)
        assert "totalLabel" in body, (
            f"{path.name}'s normaliser does not publish `totalLabel` — any site "
            "rendering it there would print undefined"
        )
        assert "known ? String(total)" in body, (
            f"{path.name}: the label must distinguish a measured depth from a floor"
        )
