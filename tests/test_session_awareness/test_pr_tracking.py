"""Routing is a separate checkpoint, never a replacement measurement verdict."""

import pytest

from genesis.session_awareness.pr_evidence import parse_evidence
from genesis.session_awareness.pr_tracking import assess_tracking, concerns, public_receipt
from tests.test_scripts.test_pr_verification_closer import _doc

URL = "https://github.com/owner/repo/issues/12"


def receipt(target="findings/0", **over):
    return dict(kind="public", target=target, url=URL, quote="timer drops events",
                rationale="This task repairs the dropped timer events measured here.") | over


async def assess(doc, body="timer drops events"):
    return await assess_tracking(parse_evidence(doc), read_public=lambda *a: body)


@pytest.mark.parametrize("detail", [
    "latent flaw in an unconnected security adapter",
    "install-local dependency compatibility warning",
    "acceptance task with identifiers retained only in local evidence",
])
async def test_public_default_does_not_depend_on_ownership_or_security_labels(detail):
    doc = _doc(findings=[dict(summary=detail, disposition="see receipt")], tracking=[receipt()])
    assert (await assess(doc)).complete


async def test_free_text_and_unrelated_or_duplicate_receipts_do_not_complete():
    finding = dict(summary="timer drops events", disposition="reported in chat")
    for entries in ([], [receipt(quote="another defect")], [receipt(), receipt()],
                    [receipt(target="findings/99")]):
        result = await assess(_doc(findings=[finding], tracking=entries))
        assert not result.complete


@pytest.mark.parametrize("target,over", [
    ("findings/0", dict(findings=[dict(summary="defect", disposition="accepted")])),
    ("claims/0", dict(claims=[dict(claim="timer", verdict="fail", tier="MEASURED", measurement="drops events")])),
    ("gaps/0", dict(gaps=[dict(kind="not_yet_done", detail="test undone", command="run test")])),
    ("gaps/0", dict(gaps=[dict(kind="owner_only", detail="needs device")])),
    ("gaps/0", dict(gaps=[dict(kind="event_gated", detail="next run", trigger="next run", recheck="measure")])),
    ("deploy", dict(deploy=dict(method="content", established=False, detail="not deployed"))),
])
async def test_actionable_targets_cannot_be_relabelled_accepted_limits(target, over):
    entries = [dict(kind="accepted_limit", target=target, rationale="reported in chat")]
    result = await assess(_doc(**over, tracking=entries))
    assert not result.complete
    assert any("actionable" in reason for reason in result.pending)


async def test_every_nonactionable_limit_requires_its_own_rationale():
    doc = _doc(controls=[], gaps=[dict(kind="method", detail="observed once")],
               claims=[dict(claim="timer", verdict="pass", tier="INFERRED", measurement="read config")])
    assert set(concerns(parse_evidence(doc))) == {"controls", "gaps/0", "claims/0"}
    assert not (await assess(doc)).complete
    doc["tracking"] = [dict(kind="accepted_limit", target=t, rationale="Accepted measurement limit: no further work claimed.")
                       for t in concerns(parse_evidence(doc))]
    assert (await assess(doc)).complete


async def test_unavailable_readback_is_pending_not_an_exception():
    def unavailable(*args):
        raise LookupError("offline")

    doc = parse_evidence(_doc(findings=[dict(summary="defect", disposition="filed")], tracking=[receipt()]))
    result = await assess_tracking(doc, read_public=unavailable)
    assert not result.complete and "offline" in result.pending[0]


async def test_one_issue_can_cover_multiple_concerns_without_duplicate_reads():
    calls = []
    def read(*args):
        calls.append(args)
        return "timer drops events"
    doc = parse_evidence(_doc(findings=[dict(summary="first", disposition="filed"),
                                         dict(summary="second", disposition="filed")],
                             tracking=[receipt(), receipt(target="findings/1")]))
    assert (await assess_tracking(doc, read_public=read)).complete
    assert len(calls) == 1


def live_read(**changes):
    responses = {
        "repos/owner/repo": {"private": False},
        "repos/owner/repo/issues/12": {"html_url": URL, "state": "open", "body": "timer drops events"},
        "repos/owner/repo/issues/comments/99": {
            "html_url": URL + "#issuecomment-99",
            "issue_url": "https://api.github.com/repos/owner/repo/issues/12", "body": "comment content",
        },
    }
    for path, update in changes.items():
        responses[path].update(update)
    return responses.__getitem__


def test_public_reader_verifies_issue_and_comment_identity():
    assert public_receipt(URL, "owner/repo", read=live_read()) == "timer drops events"
    assert public_receipt(URL + "#issuecomment-99", "owner/repo", read=live_read()) == "comment content"


@pytest.mark.parametrize("changes", [
    {"repos/owner/repo": {"private": True}},
    {"repos/owner/repo/issues/12": {"pull_request": {}}},
    {"repos/owner/repo/issues/12": {"state": "closed"}},
    {"repos/owner/repo/issues/12": {"html_url": URL + "3"}},
    {"repos/owner/repo/issues/comments/99": {"issue_url": "https://api.github.com/repos/owner/repo/issues/13"}},
])
def test_nonactionable_or_mismatched_destinations_are_refused(changes):
    with pytest.raises(LookupError):
        public_receipt(URL + "#issuecomment-99", "owner/repo", read=live_read(**changes))


@pytest.mark.parametrize("url", [URL.replace("owner/repo", "owner/other"), URL + "/", URL + "?x=y",
                                  "https://elsewhere.invalid/issues/12", "file:///tmp/task"])
def test_reference_does_not_choose_a_host_or_different_tracker(url):
    with pytest.raises(LookupError):
        public_receipt(url, "owner/repo", read=lambda path: pytest.fail("invalid ref reached transport"))


async def test_unreachable_hypothetical_can_explicitly_accept_no_work_limit():
    doc = _doc(claims=[dict(claim="hypothetical device", verdict="unverified",
                           tier="NOT_VERIFIABLE_HERE", measurement="no real actor or resource")],
               tracking=[dict(kind="accepted_limit", target="claims/0",
                              rationale="No real actor/resource exists; no useful task can be picked up.")])
    assert (await assess(doc)).complete


async def test_genuinely_private_task_needs_reader_and_explicit_disclosure_judgment(monkeypatch):
    from genesis.session_awareness import pr_tracking
    from genesis.session_awareness.pr_evidence import EvidenceError

    private = dict(kind="private", target="findings/0", id="a" * 32, quote="sensitive acceptance task",
                   rationale="This task records the required acceptance measurement.",
                   disclosure_harm="The useful task would expose an individual's private identity.",
                   redaction_insufficient="The identity is the decision target; supporting evidence alone cannot express it.")
    doc = _doc(findings=[dict(summary="acceptance task", disposition="private")], tracking=[private])
    assert not (await assess_tracking(parse_evidence(doc))).complete
    async def private_read(db, identifier):
        return "sensitive acceptance task"
    monkeypatch.setattr(pr_tracking, "private_receipt", private_read)
    assert (await assess_tracking(parse_evidence(doc))).complete
    del private["redaction_insufficient"]
    with pytest.raises(EvidenceError):
        parse_evidence(doc)
