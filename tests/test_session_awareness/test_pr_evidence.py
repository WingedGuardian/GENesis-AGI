"""The evidence model and the derived verdict — ``genesis.session_awareness.pr_evidence``.

The ACCEPTANCE BAR for this module is totality: the previous design (a caller-asserted
verdict policed by refusal rules) had documents with NO legal verdict, twice, one fix
apart. So the first test enumerates every document shape up to three claims and
asserts that each derives exactly one verdict and that the writer ACCEPTS it — a
deadlock is not patched here, it is made unrepresentable, and this is the check.

Install-agnostic: pure functions, synthetic slugs, no database.
"""

from __future__ import annotations

import itertools

import pytest

from genesis.session_awareness import pr_evidence as pe

REPO = "owner/repo"


def _raw(
    claims=(("pass", "MEASURED"),),
    controls=("a control",),
    scope_limits=(),
    established=True,
    **over,
):
    doc = {
        "repo": REPO,
        "pr": 7,
        "merge_commit": "deadbeef1234",
        "deploy": {
            "method": "content",
            "established": established,
            "detail": "present in the deployed file",
        },
        "claims": [
            {"claim": f"claim {i}", "verdict": v, "tier": t, "measurement": "m"}
            for i, (v, t) in enumerate(claims)
        ],
        "controls": list(controls),
        "scope_limits": list(scope_limits),
        "findings": [],
    }
    doc.update(over)
    return doc


def _doc(**kw) -> pe.EvidenceDocument:
    return pe.parse_evidence(_raw(**kw))


#: Every COHERENT (verdict, tier) pair — ``unverified`` pairs with NOT_VERIFIABLE_HERE
#: and only with it. The incoherent pairs are refused by the model (tested below).
_ATOMS = [
    (v, t)
    for v in pe.CLAIM_VERDICTS
    for t in pe.TIERS
    if (t == "NOT_VERIFIABLE_HERE") == (v == "unverified")
]


def _enumerate():
    atoms = _ATOMS
    for k in (1, 2, 3):
        for claims in itertools.combinations_with_replacement(atoms, k):
            for controls in ((), ("a control",)):
                for scope in ((), ("a named gap",)):
                    yield claims, controls, scope


def _enumerate_deployed():
    """The whole space again with deployment NOT established — every one of these
    must stay open, whatever its claims say."""
    yield from _enumerate()


# ── the acceptance bar: total by construction ───────────────────────────────


def test_EVERY_document_derives_exactly_one_verdict_the_writer_accepts():
    n = 0
    seen: dict[str, int] = {}
    for claims, controls, scope in _enumerate():
        doc = _doc(claims=claims, controls=controls, scope_limits=scope)
        d = pe.derive(doc)
        assert d.verdict in pe.VERDICTS
        closing = d.verdict in pe.PASS_VERDICTS
        decision = pe.decide(doc, note=None if closing else "why")
        assert isinstance(decision, pe.Decision), (claims, controls, scope, decision)
        assert decision.verdict == d.verdict
        seen[d.verdict] = seen.get(d.verdict, 0) + 1
        n += 1
    assert n == 476, "the enumeration itself must not shrink silently"
    assert set(seen) == set(pe.VERDICTS), f"every verdict must be reachable: {seen}"


def test_the_derivation_invariants_hold_on_every_document():
    for claims, controls, scope in _enumerate():
        d = pe.derive(_doc(claims=claims, controls=controls, scope_limits=scope))
        failed = any(v == "fail" and t in pe.ESTABLISHING_TIERS for v, t in claims)
        suspected = any(v == "fail" and t == "INFERRED" for v, t in claims)
        established = any(v == "pass" and t in pe.ESTABLISHING_TIERS for v, t in claims)
        assert (d.verdict == "fail-intent") == failed
        assert bool(d.failed) == failed
        if not failed:
            assert (d.verdict == "cannot-verify") == (suspected or not established)
        # An inferred failure is never lost and never closes a row.
        if suspected and not failed:
            assert any(g.startswith(pe.SUSPECTED_FAILURE) for g in d.gaps)
            assert d.verdict not in pe.PASS_VERDICTS
        if d.verdict == "pass-with-measured-gaps":
            assert d.gaps, "a gaps verdict with no named gap is the old defect"
        if d.verdict == "pass-mechanical":
            assert not scope and controls, "a clean pass carries no gap of any kind"
            assert all(t in pe.ESTABLISHING_TIERS for _, t in claims)


def test_the_two_formerly_DEAD_documents_each_have_their_verdict():
    """Named cases from the two deadlocks. Round 2: every claim INFERRED. Round 3's
    premise check: one MEASURED pass, no control, no scope limits."""
    inferred = _doc(claims=(("pass", "INFERRED"),))
    assert pe.derive(inferred).verdict == "cannot-verify"
    assert isinstance(pe.decide(inferred, note="inferred only"), pe.Decision)

    no_control = _doc(controls=())
    d = pe.derive(no_control)
    assert d.verdict == "pass-with-measured-gaps"
    assert d.gaps == (pe.NO_CONTROL_GAP,)
    assert isinstance(pe.decide(no_control), pe.Decision)


# ── the one judgment bit ─────────────────────────────────────────────────────


def test_park_is_legal_EXACTLY_where_the_document_derives_measured_gaps():
    for claims, controls, scope in _enumerate():
        doc = _doc(claims=claims, controls=controls, scope_limits=scope)
        d = pe.derive(doc)
        r = pe.decide(doc, park=True, note="n")
        if d.verdict == "pass-with-measured-gaps":
            assert isinstance(r, pe.Decision) and r.verdict == "cannot-verify" and r.parked
        else:
            assert isinstance(r, pe.Refusal), (d.verdict, r)
        if d.verdict == "fail-intent":
            assert r.code == 3, "parking a failure is a policy refusal"


def test_an_asserted_verdict_is_only_ever_an_equality_check():
    for claims, controls, scope in _enumerate():
        doc = _doc(claims=claims, controls=controls, scope_limits=scope)
        d = pe.derive(doc)
        for asserted in pe.VERDICTS:
            note = None if asserted in pe.PASS_VERDICTS else "n"
            r = pe.decide(doc, asserted=asserted, note=note)
            if asserted == d.verdict:
                assert isinstance(r, pe.Decision)
            else:
                assert isinstance(r, pe.Refusal)
                assert r.code == (3 if d.verdict == "fail-intent" else 2)
                assert f"derives {d.verdict}" in r.message


def test_a_note_is_required_open_and_refused_closed():
    closed = _doc()
    assert isinstance(pe.decide(closed, note="x"), pe.Refusal)
    opened = _doc(claims=(("pass", "INFERRED"),))
    r = pe.decide(opened)
    assert isinstance(r, pe.Refusal) and "not-yet-done" in r.message
    assert isinstance(pe.decide(opened, note="   "), pe.Refusal), "whitespace is not a note"


def test_a_failure_without_a_note_is_asked_what_failed_not_told_to_walk_away():
    """Audit round 3: the missing-note refusal told a validator holding a MEASURED
    failure that it might be "not-yet-done" and to "leave the row alone" — advice that
    buries the one outcome that must reach the user."""
    r = pe.decide(_doc(claims=(("fail", "MEASURED"),)))
    assert isinstance(r, pe.Refusal)
    assert "what failed" in r.message
    assert "not-yet-done" not in r.message and "Leave the row alone" not in r.message


def test_a_parked_outcome_is_not_described_as_derived():
    gappy = _doc(controls=())
    r = pe.decide(gappy, park=True)
    assert isinstance(r, pe.Refusal) and "after --park" in r.message
    assert "derives cannot-verify" not in r.message


# ── the claim vocabulary: unverified, and failures gated by tier ────────────


@pytest.mark.parametrize(
    ("verdict", "tier"),
    [
        (v, t)
        for v in pe.CLAIM_VERDICTS
        for t in pe.TIERS
        if (t == "NOT_VERIFIABLE_HERE") != (v == "unverified")
    ],
)
def test_an_incoherent_verdict_tier_pair_is_refused(verdict, tier):
    """Audit round 3: with only pass/fail, a claim nobody could check had to be
    marked ``pass`` (a falsification) or ``fail`` (fail-intent, unparkable)."""
    with pytest.raises(pe.EvidenceError) as exc:
        _doc(claims=((verdict, tier),))
    assert "evidence.claims[0]" in str(exc.value)


def test_an_INFERRED_failure_keeps_the_row_open_as_a_named_suspicion():
    doc = _doc(claims=(("pass", "MEASURED"), ("fail", "INFERRED")))
    d = pe.derive(doc)
    assert d.verdict == "cannot-verify" and d.failed == ()
    assert f"{pe.SUSPECTED_FAILURE}: claim 1" in d.gaps
    assert not any(g.startswith("INFERRED: ") for g in d.gaps), "listed once, as a suspicion"
    r = pe.decide(doc, park=True, note="n")
    assert isinstance(r, pe.Refusal), "already cannot-verify; --park adds nothing"


def test_an_ESTABLISHED_failure_outranks_an_inferred_one():
    doc = _doc(claims=(("fail", "READ"), ("fail", "INFERRED")))
    d = pe.derive(doc)
    assert d.verdict == "fail-intent" and d.failed == ("claim 0",)


def test_unverified_claims_alone_cannot_verify():
    d = pe.derive(_doc(claims=(("unverified", "NOT_VERIFIABLE_HERE"),)))
    assert d.verdict == "cannot-verify"
    assert "NOT VERIFIABLE HERE: claim 0" in d.gaps


# ── refusals: the advice must be advice the tool accepts ────────────────────


def _followed(doc, kwargs, r):
    """Apply what refusal *r* tells the caller to do; return the new kwargs, or None."""
    m = r.message
    if "re-run with --park" in m:
        return {**kwargs, "park": True, "note": kwargs.get("note") or "why"}
    if "--note is required" in m:
        return {**kwargs, "note": "why"}
    if "Drop --note" in m:
        return {**kwargs, "note": None}
    if m.startswith("--park refused") and "adds nothing" in m:
        return {**kwargs, "park": False}
    return None


def test_NO_refusal_recommends_an_action_the_tool_then_refuses():
    """Audit round 3: three refusal messages recommended a move the tool refused
    next (put a note in scope_limits, which changes the verdict; walk away from a
    failure; restate a parked outcome as derived). Every piece of advice is now
    FOLLOWED over the whole enumeration, and must lead to a Decision — or to a
    refusal whose advice is again followable, never to a dead end."""
    followed = 0
    for claims, controls, scope in _enumerate():
        doc = _doc(claims=claims, controls=controls, scope_limits=scope)
        for park in (False, True):
            for asserted in (None, *pe.VERDICTS):
                for note in (None, "n"):
                    kwargs = {"park": park, "asserted": asserted, "note": note}
                    r = pe.decide(doc, **kwargs)
                    for _ in range(3):
                        if not isinstance(r, pe.Refusal):
                            break
                        # EVERY refusal, followed or not: prose moved into scope_limits
                        # changes the verdict, so no advice may point there.
                        assert "scope_limits" not in r.message, r.message
                        nxt = _followed(doc, kwargs, r)
                        if nxt is None:
                            break
                        followed += 1
                        kwargs = nxt
                        r = pe.decide(doc, **kwargs)
                    if isinstance(r, pe.Refusal):
                        # Any refusal still standing must say to change the
                        # document or the assertion — never a flag move that fails.
                        assert _followed(doc, kwargs, r) is None, (claims, kwargs, r)
    assert followed > 0, "the harness must actually follow some advice"


def test_an_unknown_asserted_verdict_is_refused():
    r = pe.decide(_doc(), asserted="PASS")
    assert isinstance(r, pe.Refusal) and r.code == 2


# ── the strict model: every prose field, every hostile value ────────────────

_PROSE_PATHS = [
    ("merge_commit",),
    ("deploy", "detail"),
    ("claims", 0, "claim"),
    ("claims", 0, "measurement"),
    ("controls", 0),
    ("scope_limits", 0),
    ("findings", 0, "summary"),
    ("findings", 0, "disposition"),
]
_HOSTILE = [{"sha": "x"}, ["x"], 42, 4.2, True, None, "", "   "]


def _set(doc: dict, path: tuple, value) -> dict:
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return doc


@pytest.mark.parametrize("path", _PROSE_PATHS, ids=lambda p: ".".join(map(str, p)))
@pytest.mark.parametrize("value", _HOSTILE, ids=repr)
def test_every_prose_field_refuses_every_non_prose_value(path, value):
    """Codex round 2 (:160) and CodeRabbit round 1: ``str(x).strip()`` let a dict
    satisfy merge_commit and a null satisfy a floor. The parametrisation is over EVERY
    prose field, not a sample — a sample is how two of six fields got fixed last time."""
    # Every list the paths index into must be non-empty, or the fixture — not the
    # model — raises, and a green run would prove nothing about that field.
    raw = _raw(scope_limits=("a gap",), findings=[{"summary": "s", "disposition": "d"}])
    with pytest.raises(pe.EvidenceError) as exc:
        pe.parse_evidence(_set(raw, path, value))
    rendered = "evidence" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)
    assert rendered in str(exc.value), "the refusal names the exact field path"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pr", True),
        ("pr", "7"),
        ("pr", 0),
        ("pr", -3),
        ("pr", 2**63),
        ("repo", "noslash"),
        ("repo", 7),
        ("repo", ""),
        ("claims", []),
    ],
)
def test_the_key_fields_are_strict(field, value):
    with pytest.raises(pe.EvidenceError) as exc:
        pe.parse_evidence(_raw(**{field: value}))
    assert f"evidence.{field}" in str(exc.value)


@pytest.mark.parametrize("bad", ["owner/repo ", " owner/repo", "owner/repo\n"])
def test_a_padded_repo_is_refused_BY_NAME(bad):
    with pytest.raises(pe.EvidenceError) as exc:
        pe.parse_evidence(_raw(repo=bad))
    assert "whitespace" in str(exc.value)


def test_an_unknown_field_is_refused_rather_than_dropped():
    """``scope_limit`` for ``scope_limits`` — dropping it silently would lose exactly
    the gap the writer meant to name."""
    with pytest.raises(pe.EvidenceError) as exc:
        pe.parse_evidence(_raw(scope_limit=["the gap"]))
    assert "evidence.scope_limit" in str(exc.value)


@pytest.mark.parametrize("tier", ["measured", "Measured", "VERIFIED"])
def test_a_case_variant_or_invented_tier_is_refused(tier):
    raw = _raw()
    raw["claims"][0]["tier"] = tier
    with pytest.raises(pe.EvidenceError):
        pe.parse_evidence(raw)


def test_a_non_object_document_is_refused():
    with pytest.raises(pe.EvidenceError):
        pe.parse_evidence(["not", "an", "object"])


# ── replay: every external finding, by id ───────────────────────────────────
#
# 20 top-level findings across rounds 1-2 (PR #2416). The ones about the DOCUMENT or
# the VERDICT replay here; the rest replay where their surface lives:
#   CRUD lifecycle  4111855463 4111855535 4111877377 4139155327 -> test_pr_verifications.py
#   serialised cap  4111877382                                  -> test_pr_verification_closer.py
#   readers         4111877373 4111877379 4139155459 4139340596
#                   4139340600 4139340606                       -> test_verification_readers.py
#   skill inventory 4111877357 (AGENTS.md regeneration)         -> no runtime surface


@pytest.mark.parametrize(
    ("finding", "raw", "kwargs", "expect"),
    [
        # P1: refuse to close when every claim is unverifiable
        (
            "4111877391",
            _raw(claims=(("unverified", "NOT_VERIFIABLE_HERE"),), scope_limits=("g",)),
            {"asserted": "pass-with-measured-gaps"},
            "refused",
        ),
        # Devin: a mechanical pass can lack measurements
        (
            "4111855492",
            _raw(claims=(("pass", "INFERRED"),)),
            {"asserted": "pass-mechanical"},
            "refused",
        ),
        # P2: require a real negative control
        ("4111877361", _raw(controls=()), {"asserted": "pass-mechanical"}, "refused"),
        # P2: bind non-closing verdicts to the claim outcomes
        ("4111877368", _raw(), {"asserted": "fail-intent", "note": "n"}, "refused"),
        # P2 round 2: reject scope limits on a clean mechanical pass
        (
            "4139340593",
            _raw(scope_limits=("rollback path not exercised",)),
            {"asserted": "pass-mechanical"},
            "refused",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and v.isdigit() else "",
)
def test_replay_verdict_findings(finding, raw, kwargs, expect):
    r = pe.decide(pe.parse_evidence(raw), **kwargs)
    assert isinstance(r, pe.Refusal), f"finding {finding} would be reproduced"


@pytest.mark.parametrize(
    ("finding", "mutate"),
    [
        # P1: bind the evidence document to the repository
        ("4111877387", lambda d: d.pop("repo")),
        # P2 round 2: require strings for prose evidence fields
        ("4139340595", lambda d: d.__setitem__("merge_commit", {"sha": "deadbeef"})),
        # CodeRabbit: reject non-string entries in scope_limits and controls
        ("4111862685", lambda d: d.__setitem__("controls", [{}])),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_replay_document_findings(finding, mutate):
    raw = _raw()
    mutate(raw)
    with pytest.raises(pe.EvidenceError):
        pe.parse_evidence(raw)


# ── the generated reason ────────────────────────────────────────────────────


def test_the_reason_names_gaps_and_failures():
    gappy = _doc(claims=(("pass", "MEASURED"), ("unverified", "NOT_VERIFIABLE_HERE")))
    reason = pe.reason_for(gappy, pe.decide(gappy))
    assert reason.startswith("PASS-WITH-MEASURED-GAPS — 2 claim(s)")
    assert "NOT VERIFIABLE HERE: claim 1" in reason

    failing = _doc(claims=(("fail", "MEASURED"),))
    assert "failed: claim 0" in pe.reason_for(failing, pe.decide(failing, note="n"))


def test_a_suspected_failure_note_refusal_says_MEASURE_and_names_the_claim():
    """Fresh review of the audit fixes: the note refusal gave precondition advice
    ("not-yet-done") to a suspicion, whose route is measuring it."""
    doc = _doc(claims=(("pass", "MEASURED"), ("fail", "INFERRED")))
    r = pe.decide(doc)
    assert isinstance(r, pe.Refusal)
    assert "MEASURE" in r.message and f"{pe.SUSPECTED_FAILURE}: claim 1" in r.message
    assert "not-yet-done" not in r.message


def test_asserting_fail_intent_on_a_suspicion_advises_measuring_not_relabelling():
    doc = _doc(claims=(("pass", "MEASURED"), ("fail", "INFERRED")))
    r = pe.decide(doc, asserted="fail-intent", note="n")
    assert isinstance(r, pe.Refusal) and r.code == 2
    assert "Never relabel a tier" in r.message
    assert "change the document if it is wrong" not in r.message


# ── deployment gates the verdict (Codex round 3, P1) ────────────────────────


def test_an_UNDEPLOYED_document_never_closes_and_never_accuses():
    """Every coherent document, with deployment not established: each derives
    cannot-verify with the deployment gap FIRST, each failure is kept as a named
    gap rather than dropped or escalated, and the writer accepts it with a note."""
    n = 0
    for claims, controls, scope in _enumerate_deployed():
        doc = _doc(claims=claims, controls=controls, scope_limits=scope, established=False)
        d = pe.derive(doc)
        assert d.verdict == "cannot-verify" and d.failed == ()
        assert d.gaps[0].startswith(pe.NOT_DEPLOYED)
        fails = sum(1 for v, _ in claims if v == "fail")
        assert sum(g.startswith(pe.UNDEPLOYED_FAILURE) for g in d.gaps) == fails
        assert isinstance(pe.decide(doc, note="deploy first"), pe.Decision)
        for asserted in pe.PASS_VERDICTS:
            assert isinstance(pe.decide(doc, asserted=asserted), pe.Refusal)
        n += 1
    assert n == 476


def test_deploy_established_is_REQUIRED_and_strictly_boolean():
    for bad in ("true", 1, None):
        raw = _raw()
        raw["deploy"]["established"] = bad
        with pytest.raises(pe.EvidenceError) as exc:
            pe.parse_evidence(raw)
        assert "evidence.deploy.established" in str(exc.value)
    raw = _raw()
    del raw["deploy"]["established"]
    with pytest.raises(pe.EvidenceError) as exc:
        pe.parse_evidence(raw)
    assert "evidence.deploy.established" in str(exc.value)


def test_an_undeployed_note_refusal_names_deployment_and_not_yet_done():
    r = pe.decide(_doc(established=False))
    assert isinstance(r, pe.Refusal)
    assert pe.NOT_DEPLOYED in r.message and "not-yet-done" in r.message


def test_replay_codex_4140860459_stale_tree_cannot_close():
    """P1: a passing MEASURED claim gathered on a tree without the merge closed the
    obligation, because derive() never looked at deploy."""
    doc = _doc(established=False)
    assert pe.derive(doc).verdict == "cannot-verify"
    assert isinstance(pe.decide(doc, asserted="pass-mechanical"), pe.Refusal)


def test_an_undeployed_failure_keeps_its_TIER():
    """Fresh review of round 4: an INFERRED and a MEASURED failure on a stale tree
    rendered identically, losing which one was only a suspicion."""
    d = pe.derive(_doc(claims=(("fail", "MEASURED"), ("fail", "INFERRED")), established=False))
    assert f"{pe.UNDEPLOYED_FAILURE} (MEASURED): claim 0" in d.gaps
    assert f"{pe.UNDEPLOYED_FAILURE} (INFERRED): claim 1" in d.gaps


def test_a_stale_tree_mismatch_says_deploy_never_flip_established():
    """Fresh review of round 4: "change the document" invited flipping established
    to reach a verdict — the tier-relabel temptation, one field over."""
    doc = _doc(claims=(("fail", "MEASURED"),), established=False)
    for asserted in ("fail-intent", "pass-mechanical"):
        r = pe.decide(doc, asserted=asserted, note="n")
        assert isinstance(r, pe.Refusal)
        assert "Never set established" in r.message
        assert "change the document if it is wrong" not in r.message


def test_a_negative_ANCESTRY_probe_is_told_to_confirm_by_content_or_behaviour():
    """Ancestry is never a negative verdict (a stacked PR lands inside its parent's
    squash); the advice must not treat an ancestry miss as deployment's absence."""
    raw = _raw(established=False)
    raw["deploy"]["method"] = "ancestry"
    r = pe.decide(pe.parse_evidence(raw))
    assert isinstance(r, pe.Refusal)
    assert "content or behaviour" in r.message and "never a negative verdict" in r.message
    other = pe.decide(_doc(established=False))
    assert "content or behaviour" not in other.message


def test_no_refusal_listing_renders_an_empty_label():
    for doc in (
        _doc(established=False),
        _doc(claims=(("pass", "MEASURED"), ("fail", "INFERRED"))),
    ):
        r = pe.decide(doc)
        assert isinstance(r, pe.Refusal) and "\n  : " not in r.message
