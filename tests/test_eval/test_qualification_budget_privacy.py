"""Frozen campaign caps and overlapping credential containment, without paid calls."""

from __future__ import annotations

import logging
from contextlib import suppress
from decimal import Decimal, localcontext
from unittest.mock import patch

import httpx
import pytest

from genesis.eval.qualification import manifest, runner, transport
from genesis.eval.qualification.evidence import Incomplete, Journal
from tests.test_eval.test_qualification import call, first, priced, response, synthetic_spec


@pytest.fixture(scope="module")
async def frozen(tmp_path_factory):
    spec = synthetic_spec()
    spec["ceiling_usd"] = "8.50"
    return await manifest.prepare(spec, temp_root=tmp_path_factory.mktemp("caps") / "scratch")


def test_explicit_cap_is_frozen_reported_and_cannot_change_on_restart(frozen, tmp_path):
    assert frozen["ceiling_usd"] == "8.50"
    data = priced(frozen, maximum="4.25")
    keys = data["order"]
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        journal.append("reserve", attempt=keys[0], reservation="4.25")
    with Journal(tmp_path / "campaign") as journal:
        assert runner.report(journal)["ceiling_usd"] == "8.50"
        journal.append("reserve", attempt=keys[1], reservation="4.25")
        assert journal.committed == Decimal("8.50")
        with pytest.raises(Incomplete, match="funds"):
            journal.append("reserve", attempt=keys[2], reservation="4.25")
        with pytest.raises(Incomplete, match="conflicting manifest"):
            journal.initialize({**data, "ceiling_usd": "50"})
    with Journal(tmp_path / "campaign") as journal:
        assert journal.manifest["ceiling_usd"] == "8.50"
        assert len(journal.attempts) == 2


def test_campaign_preflight_compares_complete_bound_with_frozen_cap(frozen):
    data = priced(frozen, maximum="0.25")  # 24 scheduled requests = $6.
    assert not any("exceeds" in issue for issue in manifest.frozen_issues(data))
    data["ceiling_usd"] = "5"
    assert any("exceeds $5" in issue for issue in manifest.frozen_issues(data))


def test_tiny_excess_at_configured_cap_is_retained(frozen, tmp_path):
    data = priced(frozen, maximum="8.50")
    first, second = data["order"][:2]
    data["schedule"][second]["maximum_usd"] = "0.00000000000000000000000000001"
    with localcontext() as context, Journal(tmp_path / "campaign") as journal:
        context.prec = 2
        journal.initialize(data)
        journal.append("reserve", attempt=first, reservation="8.50")
        with pytest.raises(Incomplete, match="funds"):
            journal.append(
                "reserve", attempt=second, reservation=data["schedule"][second]["maximum_usd"]
            )


@pytest.mark.parametrize(
    "value", [None, False, True, 0, -1, 8.5, [], {}, "NaN", "Infinity", "1e1001"]
)
async def test_invalid_cap_cannot_prepare_or_dispatch(tmp_path, value):
    spec = {**synthetic_spec(), "ceiling_usd": value}
    with (
        patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("HTTP")),
        pytest.raises(Incomplete),
    ):
        await manifest.prepare(spec, temp_root=tmp_path / "scratch")
    assert not (tmp_path / "scratch").exists()


@pytest.mark.parametrize("long_first", [False, True])
def test_overlapping_credentials_redacted_in_evidence_and_formatted_logs(
    monkeypatch, caplog, long_first
):
    short = "synthetic-key-prefix"
    long = short + "-private-suffix"
    entries = [("TEST_SHORT_KEY", short), ("TEST_LONG_TOKEN", long)]
    if long_first:
        entries.reverse()
    for key, secret in entries:
        monkeypatch.setenv(key, secret)
    monkeypatch.setenv("TEST_DUPLICATE_SECRET", long)
    raw = {long: [{"echo": long}, short]}
    assert transport.safe_evidence(raw) == {"[REDACTED]": [{"echo": "[REDACTED]"}, "[REDACTED]"]}
    assert transport.safe_text(long + " " + short) == "[REDACTED] [REDACTED]"
    original = logging.getLogRecordFactory()
    with pytest.raises(RuntimeError), transport.private_logs():
        try:
            raise ValueError(long)
        except ValueError:
            logging.getLogger(__name__).exception("echo %s", long, stack_info=True)
        raise RuntimeError("exit")
    assert short not in caplog.text
    assert "private-suffix" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert logging.getLogRecordFactory() is original


@pytest.mark.parametrize(
    "choices",
    [
        {"unexpected": 1},
        "bad",
        True,
        [None],
        [{"message": None}],
        [{"message": "bad"}],
        [{"message": []}],
        [{"message": {"content": []}}],
    ],
)
@pytest.mark.parametrize("billing", ["inline", "reconcile"])
async def test_malformed_optional_answer_retains_identity_and_billing(
    frozen, tmp_path, monkeypatch, choices, billing
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    seen = []
    raw = response(cost="0.01" if billing == "inline" else None)
    raw["choices"] = choices

    def mock(request):
        seen.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json=raw)
        return httpx.Response(
            200,
            json={
                "data": {"id": "gen-synthetic", "model": frozen["model_id"], "total_cost": "0.01"}
            },
        )

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        key = first(frozen)
        # Missing inline billing requires GET; identity must survive.
        with suppress(Incomplete):
            await call(journal, key, mock)
        record = journal.attempts[key]
        assert record.get("observation", {}).get("generation_id") == "gen-synthetic"
        assert record["observation"]["content"] is None
        if billing == "reconcile":
            assert "charge" not in record
            await transport.reconcile(journal, transport=httpx.MockTransport(mock))
        assert record["charge"] == "0.01"
        await runner.score_record(journal, key, None)
        assert record["score"]["error"] == "MalformedJudgment"
        assert record["score"]["agreement"] is False
    with Journal(tmp_path / "campaign") as journal:
        assert journal.attempts[key]["charge"] == "0.01"
        assert journal.attempts[key]["score"]["error"] == "MalformedJudgment"
    assert seen == (["POST"] if billing == "inline" else ["POST", "GET"])
