"""Structural integrity checks for the dashboard webui JS.

dashboard.js defines one large Alpine store as a single object literal. In a
JS object literal a duplicated key is legal and the *second* definition
silently wins — which is how the two ``resolveApproval`` implementations
coexisted with one shadowing the other. These tests parse the store literal
by its stable indentation convention (members at exactly 8 spaces) and
assert every member name is unique.
"""

import json
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

# Deliberate escape hatch for the uppercase-'OPEN' ban below. Spelled as a
# constant so `grep -r` finds every exemption in one shot.
_ALLOW_UPPERCASE_OPEN = "guard-allow-uppercase-open"


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


def test_a_recovered_exact_depth_is_not_reported_as_an_unknown_counter():
    """An error string must not outrank a counter that proved itself exact.

    `queues.errors` and `discarded.known` are two independent answers to "is
    this counter known", which is the same fork — one level up — as the
    `discarded_count`/`discarded_items` pair this branch removed. When the
    COUNT query fails but the capped sample reads to completion, the depth is
    now EXACT and `known` says so, while `errors` still carries the old
    "count query failed" string. Keying the verdict on the presence of any
    error then renders an exact number beside "some queue counters could not be
    collected" — the panel contradicting itself, which is the class of defect
    this whole branch exists to close.

    Errors stay in the payload: the COUNT really did fail and an operator
    should see it. What changes is that a diagnostic stops doubling as an
    exactness signal for the one counter that publishes its own.

    Behavioural, not a source scan: the real function body is executed against
    real payloads, so a rewrite that keeps the tokens and loses the property
    still fails.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    js = DASHBOARD_JS.read_text()
    i = js.index("queuesSemantic() {")
    body = js[i + len("queuesSemantic() {"): js.index("\n        },", i)]
    # queuesSemantic() reads `this.discarded`, so the REAL normaliser is wired
    # in rather than a stand-in — a fake here could satisfy the verdict while
    # disagreeing with what the panel actually renders.
    j = js.index("get discarded() {")
    norm = js[j + len("get discarded() {"): js.index("\n        },", j)]

    # Each case: (label, queues payload, expected state). Every depth is 0 —
    # a non-zero discarded depth returns "error" several branches earlier, so
    # only an empty queue reaches the line under test at all.
    _exact_zero = {"total": 0, "sample": [], "known": True, "sample_truncated": False}
    _unknown_zero = {"total": 0, "sample": [], "known": False, "sample_truncated": False}
    cases = [
        (
            "count failed, sample proved the depth exactly zero",
            {"errors": ["discarded: count query failed"], "discarded": _exact_zero},
            "healthy",
        ),
        (
            "sample failed while the count proved the depth exactly zero",
            {"errors": ["discarded: sample query failed"], "discarded": _exact_zero},
            "healthy",
        ),
        (
            "a counter that did NOT recover still decides the verdict",
            {"errors": ["dead_letter: count query failed"], "discarded": _exact_zero},
            "unknown",
        ),
        (
            "discarded error while the depth is genuinely not exact",
            {"errors": ["discarded: count query failed"], "discarded": _unknown_zero},
            "unknown",
        ),
        (
            "a recovered error does not mask an unrecovered one beside it",
            {"errors": ["discarded: count query failed", "dead_letter: count query failed"],
             "discarded": _exact_zero},
            "unknown",
        ),
        (
            "a failed section still outranks everything",
            {"status": "error"},
            "unknown",
        ),
    ]

    script = (
        "function verdict(queues) {\n"
        "  const self = { health: { queues } };\n"
        "  Object.defineProperty(self, 'discarded', {\n"
        "    get: function () { return (function () {" + norm + "\n}).call(self); }\n"
        "  });\n"
        "  return (function () {" + body + "\n}).call(self); }\n"
        "const cases = " + json.dumps(cases) + ";\n"
        "let bad = 0;\n"
        "for (const [label, payload, want] of cases) {\n"
        "  const got = verdict(payload);\n"
        "  if (!got || got.state !== want) {\n"
        "    bad++;\n"
        "    console.log('MISMATCH: ' + label + ' -> ' + JSON.stringify(got) + ' want ' + want);\n"
        "  }\n"
        "}\n"
        "console.log(bad === 0 ? 'OK' : 'FAIL ' + bad);\n"
    )
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"node failed: {r.stderr[:800]}"
    assert r.stdout.strip().endswith("OK"), (
        "queuesSemantic() disagreed with the expected verdicts:\n" + r.stdout
    )


def test_no_frontend_file_compares_breaker_state_to_an_uppercase_literal():
    """Breaker state is emitted LOWERCASE; comparing to 'OPEN' can never match.

    `dashboard/routes/routing.py` emits `cb.state.value`, and `ProviderState` is
    a StrEnum whose values are "closed"/"open"/"half_open". Four consumers
    compared against the literal 'OPEN', so the provider dot rendered green, the
    toggle read "disable", and the Provider Keys dot stayed green regardless of
    the real breaker state — the dashboard was structurally incapable of showing
    a tripped provider.

    Scans DIRECTORIES so a newly added file cannot escape the guard.

    The ban is UNSCOPED on purpose. An earlier revision required the offending
    line to also match /cb_state|cbState|breaker/, reasoning that an unscoped
    ban would trip on an unrelated uppercase literal. MEASURED across the exact
    directories this scans: zero occurrences of 'OPEN' or "OPEN" of any kind.
    The false positive was hypothetical and the blindness it bought was real —
    the natural two-line reintroduction

        const s = (this.routingConfig?.cb_states || {})[p];
        if (s === 'OPEN') { ... }

    puts the comparison on a line with no breaker context, and the scoped guard
    passed it. A genuine future false positive gets an explicit, greppable
    exemption via the allowlist token below, so an exemption is a deliberate act
    rather than a silent hole.
    """
    offenders = []
    for path in _frontend_files():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if _ALLOW_UPPERCASE_OPEN in line:
                continue  # deliberate, greppable exemption
            if "'OPEN'" in line or '"OPEN"' in line:
                offenders.append(f"{path.name}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "frontend contains an uppercase 'OPEN' literal. Breaker state is emitted "
        "LOWERCASE ('open'/'half_open'/'closed'), so a comparison against it can "
        "never match. If this literal is genuinely unrelated to breaker state, "
        f"add the token {_ALLOW_UPPERCASE_OPEN!r} in a comment on that line:\n  "
        + "\n  ".join(offenders)
    )


def test_breaker_open_helper_is_case_insensitive_and_total():
    """Behavioural: execute the real helper, don't pattern-match its source."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; the static guard above still applies")

    js = DASHBOARD_JS.read_text()
    # breakerIsOpen delegates to breakerState, so BOTH real bodies are wired —
    # extracting one and stubbing the other would test a fake.
    j = js.index("breakerState(providerName) {")
    state_body = js[j + len("breakerState(providerName) {"): js.index("\n        },", j)]
    i = js.index("breakerIsOpen(providerName) {")
    body = js[i + len("breakerIsOpen(providerName) {"): js.index("\n        },", i)]

    cases = [
        ("lowercase open (what the API actually emits)", "open", True),
        ("uppercase OPEN (defensive)", "OPEN", True),
        ("mixed case", "Open", True),
        ("closed", "closed", False),
        ("half_open is NOT open", "half_open", False),
        ("missing provider", None, False),
    ]
    script = (
        "function mk(v){ const self={ routingConfig:{ cb_states: v===null?{}:{p:v} },\n"
        "    breakerState(n){ return (function(providerName){" + state_body + "\n}).call(this,n); } };\n"
        "  return (function(providerName){" + body + "\n}).call(self,'p'); }\n"
        "const cases=" + json.dumps(cases) + ";\n"
        "let bad=0;\n"
        "for (const [label,val,want] of cases){ const got=mk(val);\n"
        "  if (got!==want){ bad++; console.log('MISMATCH: '+label+' -> '+got+' want '+want); } }\n"
        "console.log(bad===0?'OK':'FAIL');\n"
    )
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"node failed: {r.stderr[:400]}"
    assert r.stdout.strip().endswith("OK"), r.stdout


def test_secret_health_counts_half_open_as_degraded():
    """Codex P2, confirmed: HALF_OPEN was rendering the provider key GREEN.

    `observability/snapshots/call_sites.py` treats a first-chain provider in
    OPEN *or* HALF_OPEN as a degraded call site. `secretHealthStatus` counted
    only 'open', so a provider parked in HALF_OPEN painted the Provider Keys
    indicator healthy.

    That was latent before this change and is live after it: refusing to
    probe-heal a permanent/quota failure means such a breaker can sit HALF_OPEN
    indefinitely on a low-traffic provider. It is the same "degraded renders as
    healthy" defect this PR fixes one layer down, so shipping the fix without
    this would have re-created the bug in the surface meant to reveal it.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    js = DASHBOARD_JS.read_text()

    def _body(sig):
        i = js.index(sig)
        return js[i + len(sig): js.index("\n        },", i)]

    # Every helper in the chain is the REAL extracted body — a hand-copied stub
    # would let this pass against a broken production helper.
    script = (
        "const ctx = {\n"
        "  _KEY_TO_PROVIDER_TYPES: { K: ['t'] },\n"
        "  routingConfig: { providers: { p1: {type:'t'}, p2: {type:'t'} }, cb_states: {} },\n"
        "  breakerState(providerName) {" + _body("breakerState(providerName) {") + "\n  },\n"
        "  breakerVerdict(providerName) {" + _body("breakerVerdict(providerName) {") + "\n  },\n"
        "  secretHealthStatus(keyEntry) {" + _body("secretHealthStatus(keyEntry) {") + "\n  },\n"
        "};\n"
        "const cases = [\n"
        "  [{p1:'closed',   p2:'closed'},    'healthy'],\n"
        "  [{p1:'half_open',p2:'closed'},    'degraded'],\n"
        "  [{p1:'HALF_OPEN',p2:'closed'},    'degraded'],\n"
        "  [{p1:'open',     p2:'closed'},    'degraded'],\n"
        "  [{p1:'open',     p2:'open'},      'error'],\n"
        "  [{p1:'half_open',p2:'half_open'}, 'degraded'],\n"
        "];\n"
        "let bad = 0;\n"
        "for (const [states, want] of cases) {\n"
        "  ctx.routingConfig.cb_states = states;\n"
        "  const got = ctx.secretHealthStatus({key:'K', status:'validated'});\n"
        "  if (got !== want) { bad++; console.log('FAIL', JSON.stringify(states), 'want', want, 'got', got); }\n"
        "}\n"
        "console.log(bad === 0 ? 'OK' : 'FAIL ' + bad);\n"
    )
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"node failed: {r.stderr[:800]}"
    assert r.stdout.strip().endswith("OK"), (
        "secretHealthStatus disagreed with the backend's own OPEN-or-HALF_OPEN "
        "= degraded rule:\n" + r.stdout
    )


def test_breaker_verdict_is_three_state_and_degrades_safely():
    """HALF_OPEN is probation, not failure — and the helper must survive an
    absent `cb_detail` (older server or cached payload) by falling back to the
    two-state reading rather than throwing or rendering healthy.

    Executes the REAL extracted bodies, chained the way the store chains them.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    js = DASHBOARD_JS.read_text()

    def body(sig):
        i = js.index(sig)
        return js[i + len(sig): js.index("\n        },", i)]

    state_b = body("breakerState(providerName) {")
    verdict_b = body("breakerVerdict(providerName) {")
    openedby_b = body("breakerOpenedBy(providerName) {")
    tooltip_b = body("breakerTooltip(providerName) {")

    script = (
        "const ctx = {\n"
        "  routingConfig: {},\n"
        "  breakerState(providerName) {" + state_b + "\n  },\n"
        "  breakerVerdict(providerName) {" + verdict_b + "\n  },\n"
        "  breakerOpenedBy(providerName) {" + openedby_b + "\n  },\n"
        "  breakerTooltip(providerName) {" + tooltip_b + "\n  },\n"
        "};\n"
        "const cases = [\n"
        "  [{cb_states:{p:'closed'}}, 'healthy'],\n"
        "  [{cb_states:{p:'open'}}, 'failing'],\n"
        "  [{cb_states:{p:'half_open'}}, 'unverified'],\n"
        # case-insensitive input, and unknown/absent config
        "  [{cb_states:{p:'HALF_OPEN'}}, 'unverified'],\n"
        "  [{cb_states:{}}, 'healthy'],\n"
        "  [{}, 'healthy'],\n"
        "];\n"
        "let bad = 0;\n"
        "for (const [cfg, want] of cases) {\n"
        "  ctx.routingConfig = cfg;\n"
        "  const got = ctx.breakerVerdict('p');\n"
        "  if (got !== want) { bad++; console.log('VERDICT FAIL', JSON.stringify(cfg), 'want', want, 'got', got); }\n"
        "}\n"
        # opened_by plumbing, then the absent-cb_detail fallback
        "ctx.routingConfig = {cb_states:{p:'half_open'}, cb_detail:{p:{state:'half_open', opened_by:'call'}}};\n"
        "if (ctx.breakerOpenedBy('p') !== 'call') { bad++; console.log('OPENEDBY FAIL call'); }\n"
        "if (!ctx.breakerTooltip('p').includes('real calls failed')) { bad++; console.log('TOOLTIP FAIL call'); }\n"
        "ctx.routingConfig = {cb_states:{p:'half_open'}, cb_detail:{p:{state:'half_open', opened_by:'probe'}}};\n"
        "if (!ctx.breakerTooltip('p').includes('health probe')) { bad++; console.log('TOOLTIP FAIL probe'); }\n"
        # No cb_detail at all: still 'unverified', and must not throw.
        "ctx.routingConfig = {cb_states:{p:'half_open'}};\n"
        "if (ctx.breakerVerdict('p') !== 'unverified') { bad++; console.log('FALLBACK FAIL verdict'); }\n"
        "if (ctx.breakerOpenedBy('p') !== null) { bad++; console.log('FALLBACK FAIL openedBy'); }\n"
        "try { ctx.breakerTooltip('p'); } catch (e) { bad++; console.log('FALLBACK THREW', e.message); }\n"
        "console.log(bad === 0 ? 'OK' : 'FAIL ' + bad);\n"
    )
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"node failed: {r.stderr[:800]}"
    assert r.stdout.strip().endswith("OK"), (
        "breakerVerdict/breakerOpenedBy/breakerTooltip disagreed:\n" + r.stdout
    )
