"""Regression probes for exact billing, durability, privacy and archived evidence."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from decimal import Decimal, localcontext
from unittest.mock import patch

import httpx
import litellm
import pytest

from genesis.eval.qualification import contracts, evidence, manifest, runner, transport
from genesis.eval.qualification.evidence import Incomplete, Journal, currency_sum
from genesis.eval.rubrics import _RUBRICS
from tests.test_eval.test_qualification import call, first, priced, response, synthetic_spec


@pytest.fixture(scope="module")
async def frozen(tmp_path_factory):
    return await manifest.prepare(
        synthetic_spec(), temp_root=tmp_path_factory.mktemp("accounting") / "scratch"
    )


@pytest.mark.parametrize("where", ["response", "generation"])
async def test_numeric_billing_above_reservation_is_not_rounded(
    frozen, tmp_path, monkeypatch, where
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    key = first(data)
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        raw = response(cost="0.050000000000000001" if where == "response" else None)
        wire = json.dumps(raw).replace('"0.050000000000000001"', "0.050000000000000001")
        with pytest.raises(Incomplete):
            await call(journal, key, lambda r: httpx.Response(200, content=wire))
        if where == "generation":
            bill = '{{"data":{{"id":"gen-synthetic","model":"{}","total_cost":0.050000000000000001}}}}'.format(
                data["model_id"]
            )
            with pytest.raises(Incomplete, match="exceeds"):
                await transport.reconcile(
                    journal,
                    transport=httpx.MockTransport(lambda r: httpx.Response(200, content=bill)),
                )
        else:
            assert journal.attempts[key]["observation"]["usage"]["cost"] == "0.050000000000000001"
        assert "charge" not in journal.attempts[key]
        assert journal.committed == Decimal("0.05")


def test_currency_math_ignores_decimal_context(frozen, tmp_path):
    tiny = "0.00000000000000000000000000001"
    with localcontext() as context:
        context.prec = 2
        assert currency_sum(("5", tiny)) > Decimal(5)
        assert manifest.maximum_charge(
            {
                "request_fee": "0",
                "max_input_tokens": 3,
                "input_per_million": "0.12345678901234567890123456789",
                "output_per_million": "0",
            },
            {"max_tokens": 1},
        ) == Decimal("0.00000037037036703703703670370370367")
        data = priced(frozen)
        keys = list(data["schedule"])
        data["schedule"][keys[0]]["maximum_usd"] = "5"
        data["schedule"][keys[1]]["maximum_usd"] = tiny
        with Journal(tmp_path / "campaign") as journal:
            journal.initialize(data)
            journal.append("reserve", attempt=keys[0], reservation="5")
            with pytest.raises(Incomplete, match="funds"):
                journal.append("reserve", attempt=keys[1], reservation=tiny)


async def test_null_cost_can_be_reconciled(frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    raw = response(cost=None)
    raw["usage"]["cost"] = None
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        key = first(frozen)
        with pytest.raises(Incomplete):
            await call(journal, key, lambda r: httpx.Response(200, json=raw))
        await transport.reconcile(
            journal,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    content='{{"data":{{"id":"gen-synthetic","model":"{}","total_cost":0.01}}}}'.format(
                        frozen["model_id"]
                    ),
                )
            ),
        )
        assert journal.attempts[key]["charge"] == "0.01"


async def test_expired_bound_blocks_actual_http(frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    data["pricing"]["valid_until"] = "2000-01-01T00:00:00+00:00"
    seen = []
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        with pytest.raises(Incomplete):
            await call(journal, first(data), lambda r: seen.append(r))
        assert not seen


async def test_global_observers_block_without_mutation(frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    observer = object()
    with Journal(tmp_path / "campaign") as journal, patch.object(litellm, "callbacks", [observer]):
        journal.initialize(priced(frozen))
        with patch.object(transport.LiteLLMDelegate, "call") as submit:
            with pytest.raises(Incomplete, match="global"):
                await call(journal, first(frozen), lambda r: None)
            submit.assert_not_called()
        assert litellm.callbacks == [observer]
        assert "dispatch" not in journal.attempts[first(frozen)]


async def test_all_provider_metadata_redacted(frozen, tmp_path, monkeypatch):
    secret = "synthetic-sensitive-credential"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    raw = response(generation="gen-" + secret, content=secret)
    raw["usage"]["cost_details"] = {secret: {"echo": [secret]}}
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        key = first(frozen)
        with pytest.raises(Incomplete, match="identity"):
            await call(journal, key, lambda r: httpx.Response(200, json=raw))
        assert secret.encode() not in (tmp_path / "campaign" / "events.jsonl").read_bytes()
        with pytest.raises(Incomplete, match="redacted"):
            await transport.reconcile(
                journal, transport=httpx.MockTransport(lambda r: pytest.fail("GET"))
            )


def test_historical_report_survives_registry_changes(frozen, tmp_path):
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(frozen)
        before = runner.report(journal)
    name = next(iter(_RUBRICS))
    changed = replace(_RUBRICS[name], version="future-version")
    with (
        patch.dict(
            _RUBRICS, {name: changed, "future_rubric": replace(changed, name="future_rubric")}
        ),
        Journal(tmp_path / "campaign") as journal,
    ):
        assert runner.report(journal) == before
        with pytest.raises(ValueError):
            manifest.preflight(journal.manifest)


@pytest.mark.parametrize("fault", ["write", "fsync", "short"])
async def test_failed_reservation_durability_never_dispatches(frozen, tmp_path, fault):
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        target = "os.write" if fault in ("write", "short") else "os.fsync"
        options = (
            {"return_value": 1} if fault == "short" else {"side_effect": OSError("synthetic crash")}
        )
        with patch(target, **options), patch.object(transport.LiteLLMDelegate, "call") as submit:
            with pytest.raises(OSError):
                await call(journal, first(frozen), lambda r: None)
            submit.assert_not_called()
        assert journal._file is None


def test_directory_entries_synced_and_evidence_private(frozen, tmp_path):
    seen = []
    original = evidence.sync_directory

    def sync(path):
        seen.append(path)
        original(path)

    with (
        patch.object(evidence, "sync_directory", side_effect=sync),
        Journal(tmp_path / "campaign") as journal,
    ):
        journal.initialize(frozen)
    assert seen == [tmp_path, tmp_path / "campaign"]
    for name in ("writer.lock", "events.jsonl"):
        assert stat.S_IMODE((tmp_path / "campaign" / name).stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public"])
def test_unsafe_evidence_files_rejected(tmp_path, kind):
    path = tmp_path / "campaign"
    path.mkdir(mode=0o700)
    target = path / "events.jsonl"
    if kind in ("symlink", "hardlink"):
        source = tmp_path / "source"
        source.touch(mode=0o600)
        target.symlink_to(source) if kind == "symlink" else os.link(source, target)
    elif kind == "fifo":
        os.mkfifo(target, mode=0o600)
    else:
        target.touch(mode=0o644)
    with pytest.raises((Incomplete, OSError)), Journal(path):
        pass


async def test_complete_mocked_http_execution_and_restart(frozen, tmp_path, monkeypatch):
    """Exercise the whole runner on 24 synthetic requests; coverage stays incomplete."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    data = priced(frozen)
    dispatched = []

    def mock(request):
        key = data["order"][len(dispatched)]
        task = data["schedule"][key]
        assert json.loads(request.content)["messages"] == task["messages"]
        dispatched.append(key)
        content = (
            '{"redundant_with":1}'
            if task["contract"] == contracts.NOVELTY
            else '{"relevance":1}'
            if task["contract"] == contracts.RELEVANCE
            else '{"score":1}'
        )
        return httpx.Response(
            200,
            json=response(
                content=content, generation=f"gen-synthetic-{len(dispatched)}", cost="0.001"
            ),
        )

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(data)
        # Tiny public controls lack qualification coverage/approval. Bypass ONLY
        # the execution preflight here to test orchestration; real gate tests
        # separately use all 750 synthetic cases and never bypass preflight.
        with patch.object(runner, "preflight", return_value=[]):
            await runner.execute(
                journal, temp_root=tmp_path / "scratch", transport=httpx.MockTransport(mock)
            )
        assert dispatched == data["order"]
        assert all(a["score"]["agreement"] for a in journal.attempts.values())
        assert runner.report(journal)["status"] == "incomplete"
        assert journal.committed == Decimal("0.024")
    with (
        Journal(tmp_path / "campaign") as journal,
        patch.object(runner, "preflight", return_value=[]),
    ):
        await runner.execute(
            journal,
            temp_root=tmp_path / "scratch",
            transport=httpx.MockTransport(lambda r: pytest.fail("resend")),
        )
        assert len(journal.attempts) == 24


async def test_pricing_expires_between_requests(frozen, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    dispatched = []

    class Clock:
        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

        @staticmethod
        def now(tz):
            return datetime(2100 if dispatched else 2026, 1, 1, tzinfo=UTC)

    def mock(request):
        dispatched.append(request)
        return httpx.Response(200, json=response())

    with (
        Journal(tmp_path / "campaign") as journal,
        patch.object(runner, "preflight", return_value=[]),
        patch.object(manifest, "datetime", Clock),
    ):
        journal.initialize(priced(frozen))
        with pytest.raises(Incomplete, match="expired"):
            await runner.execute(
                journal, temp_root=tmp_path / "scratch", transport=httpx.MockTransport(mock)
            )
        assert len(dispatched) == 1
        assert len(journal.attempts) == 1
        assert "score" in next(iter(journal.attempts.values()))


@pytest.mark.parametrize("status", [401, 404, 429, 503])
async def test_delegate_error_logs_redacted(frozen, tmp_path, monkeypatch, caplog, capsys, status):
    import logging

    monkeypatch.setattr("genesis.routing.litellm_delegate._last_failure_log", {})
    secret = "synthetic-credential-error-echo"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    original = logging.getLogRecordFactory()
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        with pytest.raises(Incomplete):
            await call(
                journal,
                first(frozen),
                lambda r: httpx.Response(
                    status, json={"error": {"message": secret, "code": status}}
                ),
            )
        assert journal.attempts[first(frozen)]["failure"]
        assert secret not in caplog.text
        assert "[REDACTED]" in caplog.text
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err
        assert secret.encode() not in (tmp_path / "campaign" / "events.jsonl").read_bytes()
    assert logging.getLogRecordFactory() is original


def test_formatted_exception_logs_redacted_and_factory_restored(monkeypatch, caplog):
    import logging

    secret = "synthetic-secret-in-exception"
    monkeypatch.setenv("PRIVATE_TOKEN", secret)
    original = logging.getLogRecordFactory()
    with pytest.raises(RuntimeError), transport.private_logs():
        try:
            raise ValueError(secret)
        except ValueError:
            logging.getLogger(__name__).exception("failure %s", secret)
        raise RuntimeError("synthetic cancellation-like exit")
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert logging.getLogRecordFactory() is original


@pytest.mark.parametrize("prior", ["conflict", "wrong_id", "wrong_model", "missing", "invalid"])
async def test_generation_billing_history_cannot_be_silently_replaced(
    frozen, tmp_path, monkeypatch, prior
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-real-key")
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        key = first(frozen)
        with pytest.raises(Incomplete):
            await call(journal, key, lambda r: httpx.Response(200, json=response(cost=None)))
        original = {"id": "gen-synthetic", "model": frozen["model_id"], "total_cost": "0.06"}
        if prior == "wrong_id":
            original["id"] = "different-id"
        elif prior == "wrong_model":
            original["model"] = "different/model"
        elif prior == "missing":
            original["total_cost"] = None
        elif prior == "invalid":
            original["total_cost"] = "NaN"
        with pytest.raises(Incomplete):
            await transport.reconcile(
                journal,
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"data": original})
                ),
            )
        assert len(journal.attempts[key]["billing_observations"]) == 1
        good = {"data": {"id": "gen-synthetic", "model": frozen["model_id"], "total_cost": "0.01"}}
        if prior == "conflict":
            with pytest.raises(Incomplete, match="contradictory"):
                await transport.reconcile(
                    journal, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=good))
                )
            assert "charge" not in journal.attempts[key]
            assert journal.committed == Decimal("0.05")
        else:
            await transport.reconcile(
                journal, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=good))
            )
            assert journal.attempts[key]["charge"] == "0.01"


@pytest.mark.parametrize(
    "alias", ["API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN"]
)
async def test_execute_and_reconcile_share_credential_policy(frozen, tmp_path, monkeypatch, alias):
    for name in ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    chosen = "synthetic-selected-account-key"
    monkeypatch.setenv(alias, chosen)
    if alias == "API_KEY_OPENROUTER":
        monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-other-account-key")
    seen = []

    def mock(request):
        seen.append((request.method, request.headers["Authorization"]))
        if request.method == "POST":
            return httpx.Response(200, json=response(cost=None))
        return httpx.Response(
            200,
            json={
                "data": {"id": "gen-synthetic", "model": frozen["model_id"], "total_cost": "0.01"}
            },
        )

    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        with pytest.raises(Incomplete):
            await call(journal, first(frozen), mock)
        await transport.reconcile(journal, transport=httpx.MockTransport(mock))
        assert seen == [("POST", "Bearer " + chosen), ("GET", "Bearer " + chosen)]
        assert journal.attempts[first(frozen)]["charge"] == "0.01"
        assert chosen.encode() not in (tmp_path / "campaign" / "events.jsonl").read_bytes()


async def test_serialized_credential_mismatch_has_zero_egress(frozen, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-selected-account-key")
    monkeypatch.setattr(
        "genesis.routing.litellm_delegate._resolve_api_key", lambda _: "different-account-key"
    )
    seen = []
    with Journal(tmp_path / "campaign") as journal:
        journal.initialize(priced(frozen))
        with pytest.raises(Incomplete):
            await call(journal, first(frozen), lambda r: seen.append(r))
        assert not seen
        assert "charge" not in journal.attempts[first(frozen)]


@pytest.mark.parametrize("command", ["prepare", "dry-run", "report"])
def test_offline_cli_in_fresh_process_performs_zero_http(frozen, tmp_path, command):
    import subprocess
    import sys

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(synthetic_spec()))
    campaign = tmp_path / "campaign"
    if command != "prepare":
        with Journal(campaign) as journal:
            journal.initialize(frozen)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "LITELLM_LOCAL_MODEL_COST_MAP",
            "API_KEY_OPENROUTER",
            "OPENROUTER_API_KEY",
            "OPENROUTER_API_TOKEN",
        )
    }
    env["PYTHONPATH"] = str(manifest.ROOT / "src")
    script = """
import httpx, json, os, sys
calls = []
def forbidden(*args, **kwargs):
    calls.append(1)
    raise AssertionError("offline HTTP attempted")
httpx.get = forbidden
httpx.Client.send = forbidden
httpx.AsyncClient.send = forbidden
from genesis.eval.qualification.__main__ import main
assert not calls and "litellm" not in sys.modules
previous = os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP")
assert main(sys.argv[1:]) == 2
assert not calls
assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == previous
"""
    args = [command, str(campaign)]
    if command == "prepare":
        args += ["--spec", str(spec), "--temp-root", str(tmp_path / "scratch")]
    result = subprocess.run(
        [sys.executable, "-c", script, *args], env=env, capture_output=True, text=True, timeout=40
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "incomplete"
    assert report["committed_usd"] == "0"
