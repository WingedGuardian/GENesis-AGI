"""Private journal regression and accounting tests; all evidence is synthetic."""

import os
import subprocess
import sys
from decimal import Decimal

import pytest

from genesis.eval.qualification import evidence
from genesis.eval.qualification.evidence import Campaign, Incomplete, digest


def manifest(budget="0.3", maximum="0.1"):
    return {
        "version": 1,
        "budget": budget,
        "binding": {"source": "synthetic"},
        "attempts": [
            {
                "id": f"case-{i}",
                "alias": f"model-{i % 2}",
                "model": f"synthetic/model-{i % 2}",
                "upstream": "Synthetic",
                "endpoint": "https://synthetic.invalid/chat/completions",
                "request_hash": digest([i]),
                "max_charge": maximum,
            }
            for i in range(4)
        ],
    }


def journal(tmp_path, **kwargs):
    from genesis.eval.qualification.accounting import Journal

    return Journal(tmp_path / "campaign", manifest(**kwargs))


def dispatch(campaign, attempt="case-0"):
    campaign.reserve(attempt)
    campaign.dispatch(attempt)


def observe(campaign, attempt="case-0", **changes):
    row = campaign.state.attempts[attempt]["spec"]
    answer = {
        "model": row["model"],
        "upstream": row["upstream"],
        "generation_id": f"gen-{attempt}",
        "usage": {"cost": "0.1"},
        "finish_reason": "stop",
        "content": "synthetic answer",
    }
    answer.update(changes)
    campaign.observe(attempt, answer)
    return answer


def settle(campaign, attempt="case-0", charge="0.1", **changes):
    row = campaign.state.attempts[attempt]["spec"]
    receipt = {
        "generation_id": f"gen-{attempt}",
        "model": row["model"],
        "upstream": row["upstream"],
        "charge": charge,
    }
    receipt.update(changes)
    campaign.settle(attempt, receipt)


def test_campaign_files_private_locked_and_torn_tail_discarded(tmp_path):
    # Original case name retained; the approved recovery contract now forbids truncation.
    directory = tmp_path / "campaign"
    with Campaign(directory) as campaign:
        campaign.append("dispatch", alias="a")
        with pytest.raises(Incomplete, match="in use"), Campaign(directory):
            pass
    assert directory.stat().st_mode & 0o777 == 0o700
    answers = directory / "answers.jsonl"
    assert answers.stat().st_mode & 0o777 == 0o600
    with answers.open("ab") as stream:
        stream.write(b'{"kind": "answer", "alias"')
    saved = answers.read_bytes()
    with Campaign(directory) as campaign:
        assert campaign.torn_tail and [r["kind"] for r in campaign.lines] == ["dispatch"]
        with pytest.raises(Incomplete, match="uncertain write"):
            campaign.append("failure")
    assert answers.read_bytes() == saved
    with answers.open("wb") as stream:
        stream.write(evidence.canonical({"kind": "dispatch"}) + b'\n{"kind":"other"}\n')
    with pytest.raises(Incomplete, match="line 2"), Campaign(directory):
        pass


def test_campaign_directory_must_be_private(tmp_path):
    directory = tmp_path / "open"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(Incomplete, match="0700"), Campaign(directory):
        pass


def test_decimal_boundary_and_cross_model_budget_survive_restart(tmp_path):
    with journal(tmp_path) as campaign:
        for i in range(3):
            dispatch(campaign, f"case-{i}")
            observe(campaign, f"case-{i}")
            settle(campaign, f"case-{i}")
        assert campaign.state.settled == Decimal("0.3")
    with journal(tmp_path) as campaign:
        with pytest.raises(Incomplete, match="budget"):
            campaign.reserve("case-3")
        assert campaign.state.settled == Decimal("0.3")


@pytest.mark.parametrize(
    "amount", [True, False, 0.1, -1, "-0", "-1", "NaN", "Infinity", "1e2", None]
)
def test_currency_rejects_non_decimal_strings(tmp_path, amount):
    with pytest.raises(Incomplete), journal(tmp_path, budget=amount):
        pass


@pytest.mark.parametrize("phase", ["reservation", "dispatch", "observation", "settlement"])
def test_restart_never_resends_and_retains_liability(tmp_path, phase):
    with journal(tmp_path) as campaign:
        campaign.reserve("case-0")
        if phase != "reservation":
            campaign.dispatch("case-0")
        if phase in {"observation", "settlement"}:
            observe(campaign)
        if phase == "settlement":
            settle(campaign)
    with journal(tmp_path) as campaign:
        if phase != "settlement":
            assert campaign.state.reserved == Decimal("0.1")
            with pytest.raises(Incomplete):
                campaign.reserve("case-1")
        else:
            assert campaign.state.answers["case-0"]["content"] == "synthetic answer"
        with pytest.raises(Incomplete):
            campaign.dispatch("case-0")


def test_missing_billing_cannot_be_acknowledged_away(tmp_path):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign)
        campaign.fail("case-0", "synthetic timeout")
        with pytest.raises(Incomplete):
            campaign.acknowledge("failure:case-0", "verified operational recovery")
        assert campaign.state.reserved == Decimal("0.1")


def test_settled_failure_requires_explicit_resume_and_preserves_failure(tmp_path):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign)
        campaign.fail("case-0", "synthetic observer failure")
        settle(campaign)
    with journal(tmp_path) as campaign:
        with pytest.raises(Incomplete):
            campaign.reserve("case-1")
        campaign.acknowledge("failure:case-0", "verified receipt and retained answer")
        dispatch(campaign, "case-1")
        assert any(r["kind"] == "failure" for r in campaign.lines)


@pytest.mark.parametrize(
    "receipt",
    [
        {"charge": "0.11"},
        {"charge": "-1"},
        {"charge": True},
        {"charge": "NaN"},
        {"model": "wrong"},
        {"upstream": "wrong"},
        {"generation_id": ""},
    ],
)
def test_bad_receipts_persist_without_releasing_reservation(tmp_path, receipt):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign)
        settle(campaign, **receipt)
        assert campaign.lines[-1]["kind"] == "billing"
        assert campaign.state.reserved == Decimal("0.1")
        with pytest.raises(Incomplete):
            campaign.reserve("case-1")


def test_conflicting_receipts_stop_even_after_settlement(tmp_path):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign)
        settle(campaign)
        settle(campaign, charge="0.09")
        with pytest.raises(Incomplete):
            campaign.reserve("case-1")
        assert campaign.state.reserved == Decimal("0.1")


def test_verified_charge_without_answer_remains_incomplete(tmp_path):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign, content=None)
        settle(campaign)
        assert campaign.state.settled == Decimal("0.1")
        assert "case-0" not in campaign.state.answers
        campaign.acknowledge("answer:case-0", "verified charge; response lost")
        dispatch(campaign, "case-1")
        assert "case-0" not in campaign.state.answers


def test_manifest_is_immutable_and_legacy_cannot_execute(tmp_path):
    with journal(tmp_path):
        pass
    changed = manifest()
    changed["binding"] = {"source": "different"}
    from genesis.eval.qualification.accounting import Journal

    with pytest.raises(Incomplete, match="manifest"), Journal(tmp_path / "campaign", changed):
        pass
    legacy = tmp_path / "legacy"
    with Campaign(legacy) as campaign:
        campaign.append("dispatch", alias="historical")
    with pytest.raises(Incomplete, match="legacy"), Journal(legacy, manifest()):
        pass


def test_historical_read_is_read_only_even_with_torn_tail(tmp_path):
    directory = tmp_path / "campaign"
    with Campaign(directory) as campaign:
        campaign.append("answer", content="synthetic\u0085\u2028\u2029")
    with (directory / "answers.jsonl").open("ab") as stream:
        stream.write(b'{"kind":')
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in directory.iterdir()}
    read = Campaign.read(directory)
    assert read.lines[0]["content"] == "synthetic\u0085\u2028\u2029" and read.torn_tail
    assert before == {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in directory.iterdir()}
    with pytest.raises(FileNotFoundError):
        Campaign.read(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("point", ["write", "fsync"])
def test_failed_durable_write_poison_writer(tmp_path, monkeypatch, point):
    with Campaign(tmp_path / "campaign") as campaign:

        def broken(*_args):
            raise OSError("synthetic durable write failure")

        monkeypatch.setattr(evidence.os, point, broken)
        with pytest.raises(OSError):
            campaign.append("dispatch", alias="synthetic")
        monkeypatch.undo()
        with pytest.raises(Incomplete):
            campaign.append("answer", content="cannot continue")


@pytest.mark.parametrize("name", ["campaign.lock", "answers.jsonl"])
@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_private_file_links_are_rejected(tmp_path, name, link):
    directory = tmp_path / "campaign"
    with Campaign(directory):
        pass
    original = directory / name
    other = tmp_path / "other"
    original.rename(other)
    if link == "symbolic":
        original.symlink_to(other)
    else:
        os.link(other, original)
    with pytest.raises((OSError, Incomplete)), Campaign(directory):
        pass


def test_caller_mutation_does_not_change_durable_state(tmp_path):
    frozen = manifest()
    from genesis.eval.qualification.accounting import Journal

    with Journal(tmp_path / "campaign", frozen) as campaign:
        frozen["budget"] = "999"
        rows = campaign.lines
        rows[0]["manifest"]["budget"] = "999"
        spec = campaign.state.attempts["case-0"]["spec"]
        spec["max_charge"] = "999"
        assert campaign.state.manifest == manifest()
        dispatch(campaign)
        assert campaign.state.reserved == Decimal("0.1")


def test_exact_currency_precision_is_independent_of_decimal_context(tmp_path):
    from decimal import localcontext

    from genesis.eval.qualification.accounting import Journal

    tiny = "0." + "0" * 99 + "1"
    frozen = manifest(budget="0.1" + "0" * 98 + "1")
    frozen["attempts"][1]["max_charge"] = tiny
    with localcontext() as context:
        context.prec = 2
        with Journal(tmp_path / "campaign", frozen) as campaign:
            dispatch(campaign)
            observe(campaign)
            settle(campaign)
            dispatch(campaign, "case-1")
            observe(campaign, "case-1", usage={"cost": tiny})
            settle(campaign, "case-1", charge=tiny)
            assert campaign.state.settled == Decimal(frozen["budget"])


@pytest.mark.parametrize("generation", [None, "", " ", False, 0, [], {}])
def test_missing_or_invalid_observed_generation_cannot_be_invented(tmp_path, generation):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign, generation_id=generation)
        settle(campaign)
        assert campaign.state.reserved == Decimal("0.1")
        with pytest.raises(Incomplete):
            campaign.reserve("case-1")


def test_raw_append_cannot_bypass_stop(tmp_path):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        campaign.fail("case-0", "synthetic failure")
        with pytest.raises(Incomplete):
            campaign.append("reservation", attempt="case-1", amount="0.1")


def test_resume_undispatched_reservation_never_releases_liability(tmp_path):
    with journal(tmp_path) as campaign:
        campaign.reserve("case-0")
    with journal(tmp_path) as campaign:
        with pytest.raises(Incomplete):
            campaign.dispatch("case-0")
        campaign.acknowledge("reservation:case-0", "verified durable absence of dispatch")
        assert campaign.state.reserved == Decimal("0.1")
        campaign.dispatch("case-0")
        assert sum(r["kind"] == "reservation" for r in campaign.lines) == 1


@pytest.mark.parametrize(
    "record",
    [
        b'{"kind":[]}\n',
        b'{"kind":"failure","kind":"answer"}\n',
        b'{"kind":"answer","usage":1e999}\n',
    ],
)
def test_malformed_historical_json_is_structured_incomplete(tmp_path, record):
    directory = tmp_path / "campaign"
    with Campaign(directory):
        pass
    (directory / "answers.jsonl").write_bytes(record)
    with pytest.raises(Incomplete):
        Campaign.read(directory)


def test_short_write_and_directory_barrier_fail_closed(tmp_path, monkeypatch):
    with Campaign(tmp_path / "campaign") as campaign:
        write = evidence.os.write
        monkeypatch.setattr(evidence.os, "write", lambda fd, data: write(fd, data[:7]))
        with pytest.raises(OSError, match="short"):
            campaign.append("dispatch", alias="synthetic")
        monkeypatch.undo()
        with pytest.raises(Incomplete):
            campaign.append("answer", content="must not append")
    assert Campaign.read(tmp_path / "campaign").torn_tail

    def broken(_path):
        raise OSError("directory durability failure")

    monkeypatch.setattr(evidence, "sync_directory", broken)
    with pytest.raises(OSError, match="durability"), Campaign(tmp_path / "failed"):
        pytest.fail("must not yield a campaign before its directory is durable")


def test_invalid_order_and_cross_attempt_acknowledgement_are_rejected(tmp_path):
    with journal(tmp_path) as campaign:
        for kind in ("dispatch", "observation", "billing", "failure"):
            with pytest.raises(Incomplete):
                campaign.append(kind, attempt="case-0")
        dispatch(campaign)
        observe(campaign)
        campaign.fail("case-0", "synthetic failure")
        settle(campaign)
        with pytest.raises(Incomplete):
            campaign.append(
                "acknowledgement",
                attempt="case-1",
                incident="failure:case-0",
                resolution="cross-attempt resolution",
            )


def test_unknown_generation_and_contradictory_identity_never_resume(tmp_path):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign, model="wrong")
        settle(campaign)
        with pytest.raises(Incomplete):
            campaign.acknowledge("billing:case-0", "asserting resolution is insufficient")
        assert campaign.state.reserved == Decimal("0.1")


@pytest.mark.parametrize("phase", ["reservation", "dispatch", "observation", "settlement"])
def test_process_exit_at_each_durable_boundary_retains_exact_state(tmp_path, phase):
    import json

    from genesis.eval.qualification.accounting import Journal

    code = """
import json, os, sys
from pathlib import Path
from genesis.eval.qualification.accounting import Journal
with Journal(Path(sys.argv[1]), json.loads(sys.argv[3])) as campaign:
    campaign.reserve("case-0")
    if sys.argv[2] != "reservation":
        campaign.dispatch("case-0")
    if sys.argv[2] in {"observation", "settlement"}:
        campaign.observe("case-0", {"model": "synthetic/model-0", "upstream": "Synthetic",
                                   "generation_id": "gen-case-0", "content": "synthetic"})
    if sys.argv[2] == "settlement":
        campaign.settle("case-0", {"model": "synthetic/model-0", "upstream": "Synthetic",
                                  "generation_id": "gen-case-0", "charge": "0.1"})
    os._exit(0)
"""
    env = {**os.environ, "PYTHONPATH": str(evidence.Path(evidence.__file__).parents[3])}
    subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "campaign"), phase, json.dumps(manifest())],
        check=True,
        env=env,
        timeout=30,
    )
    with Journal(tmp_path / "campaign", manifest()) as campaign:
        if phase == "settlement":
            assert campaign.state.settled == Decimal("0.1")
            assert campaign.state.answers["case-0"]["content"] == "synthetic"
        else:
            assert campaign.state.reserved == Decimal("0.1")
            with pytest.raises(Incomplete):
                campaign.reserve("case-1")
        with pytest.raises(Incomplete):
            campaign.dispatch("case-0")


def test_duplicate_generation_identity_stops_the_whole_campaign(tmp_path):
    with journal(tmp_path) as campaign:
        dispatch(campaign)
        observe(campaign)
        settle(campaign)
        dispatch(campaign, "case-1")
        observe(campaign, "case-1", generation_id="gen-case-0")
        settle(campaign, "case-1", generation_id="gen-case-0")
        with pytest.raises(Incomplete):
            campaign.reserve("case-2")
        assert campaign.state.reserved == Decimal("0.2")
        assert campaign.state.settled == Decimal(0)


def test_raw_dispatch_cannot_bypass_recovery_on_reopen(tmp_path):
    with journal(tmp_path) as campaign:
        campaign.reserve("case-0")
    with journal(tmp_path) as campaign, pytest.raises(Incomplete):
        campaign.append(
            "dispatch", attempt="case-0", request_hash=manifest()["attempts"][0]["request_hash"]
        )


def test_billing_totals_do_not_inherit_exponent_limits_or_traps(tmp_path):
    from decimal import Inexact, localcontext

    from genesis.eval.qualification.accounting import Journal

    with localcontext() as context:
        context.Emin, context.Emax, context.prec = 0, 1, 1
        context.clamp = 1
        context.traps[Inexact] = False
        tiny = "0." + "0" * 39 + "1"
        budget = "0." + "0" * 39 + "2"
        frozen = manifest(budget=budget, maximum=tiny)
        with Journal(tmp_path / "campaign", frozen) as campaign:
            for i in range(2):
                dispatch(campaign, f"case-{i}")
                observe(campaign, f"case-{i}", usage={"cost": tiny})
                settle(campaign, f"case-{i}", charge=tiny)
            assert campaign.state.settled == Decimal(budget)
            with pytest.raises(Incomplete, match="budget"):
                campaign.reserve("case-2")


def test_same_writer_threads_cannot_both_reserve_one_attempt(tmp_path, monkeypatch):
    import threading

    entered, release, second_started = (threading.Event() for _ in range(3))
    original_write = os.write
    outcomes = []

    def paused_write(fd, record):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original_write(fd, record)

    def reserve(campaign, second=False):
        if second:
            second_started.set()
        try:
            campaign.reserve("case-0")
            outcomes.append("reserved")
        except Incomplete:
            outcomes.append("stopped")

    with journal(tmp_path) as campaign:
        monkeypatch.setattr(os, "write", paused_write)
        first = threading.Thread(target=reserve, args=(campaign,))
        second = threading.Thread(target=reserve, args=(campaign, True))
        first.start()
        try:
            assert entered.wait(5)
            second.start()
            assert second_started.wait(5)
            second.join(0.5)
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert sorted(outcomes) == ["reserved", "stopped"]
    recovered = type(campaign).read(tmp_path / "campaign")
    assert recovered.state.reserved == Decimal("0.1")


@pytest.mark.parametrize("writer", ["campaign", "journal"])
def test_same_object_reentry_preserves_outer_writer_and_releases_lock(tmp_path, writer):
    campaign = Campaign(tmp_path / "campaign") if writer == "campaign" else journal(tmp_path)
    with campaign:
        original_file, original_lock = campaign._file, campaign._lock
        with pytest.raises(Incomplete), campaign:
            pass
        assert (campaign._file, campaign._lock) == (original_file, original_lock)
        if writer == "campaign":
            campaign.append("answer", content="synthetic")
        else:
            campaign.reserve("case-0")
    with Campaign(tmp_path / "campaign"):
        pass
