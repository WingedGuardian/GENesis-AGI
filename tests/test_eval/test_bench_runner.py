"""Integration tests for the bench runner — mocked CC spawns, no live claude.

The _FakeInvoker distinguishes arms by ``inv.safe_mode`` (bare=True) and can
simulate the memory server's snapshot writes (the positive-control seam).
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from genesis.cc.exceptions import CCProcessError
from genesis.cc.types import CCModel, CCOutput, EffortLevel
from genesis.eval.bench.runner import BenchBusyError, run_bench

FIXTURES = pathlib.Path(__file__).parent / "bench_fixtures" / "synthetic_tasks.jsonl"


def _cc_output(text: str, downgraded: bool = False) -> CCOutput:
    return CCOutput(
        session_id="s", text=text,
        model_used="haiku" if downgraded else "sonnet",
        cost_usd=0.01, input_tokens=100, output_tokens=50,
        duration_ms=2000, exit_code=0,
        model_requested="sonnet", downgraded=downgraded,
    )


class _FakeInvoker:
    """Arm-aware fake: bare arms detected via safe_mode=True."""

    def __init__(self, behavior=None, snapshot_writer=None):
        self._behavior = behavior
        self._snapshot_writer = snapshot_writer
        self.invocations = []

    async def run(self, inv):
        self.invocations.append(inv)
        arm = "bare" if inv.safe_mode else "genesis"
        if arm == "genesis" and self._snapshot_writer:
            self._snapshot_writer(inv)
        if self._behavior:
            return await self._behavior(inv, arm)
        return _cc_output(f"{arm} answer")


class _FakeRouter:
    """Judge shim returning a canned per-text score."""

    def __init__(self, scores: dict[str, float] | None = None, fail: bool = False):
        self._scores = scores or {}
        self._fail = fail

    async def route_call(self, call_site_id, messages, **kwargs):
        class R:
            pass

        r = R()
        if self._fail:
            r.success, r.content, r.model_id, r.provider_used, r.error = (
                False, None, None, None, "boom",
            )
            return r
        prompt = messages[0]["content"]
        score = 0.5
        for key, val in self._scores.items():
            if key in prompt:
                score = val
                break
        r.success = True
        r.content = json.dumps({"score": score, "rationale": "canned"})
        r.model_id = "fake-judge"
        r.provider_used = "fake"
        r.error = None
        return r


@pytest.fixture
async def eval_db(db):
    """The shared in-memory db + the eval-tables migration chain
    (eval_runs/eval_results come from migrations, not create_all_tables —
    same pattern as test_j9_eval.py's insert_run test)."""
    import importlib

    for _mig in (
        "0002_add_eval_tables",
        "0003_eval_results_skipped",
        "0014_eval_results_metadata",
    ):
        await importlib.import_module(f"genesis.db.migrations.{_mig}").up(db)
    await db.commit()
    return db


@pytest.fixture
def bench_env(tmp_path, monkeypatch):
    """Fake prod DB (GENESIS_DB_PATH), fake home (lock + credentials),
    fake GENESIS_HOME (report output)."""
    prod = tmp_path / "prod.db"
    conn = sqlite3.connect(prod)
    conn.execute("CREATE TABLE eval_events (id INTEGER PRIMARY KEY, x TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("GENESIS_DB_PATH", str(prod))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "dot-genesis"))

    home = tmp_path / "home"
    creds = home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}")
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)
    return tmp_path


def _snapshot_writer(inv):
    """Simulate the redirected memory server: write a J-9 event into the
    snapshot the arm's MCP config points at."""
    cfg = json.loads(pathlib.Path(inv.mcp_config).read_text())
    db_path = cfg["mcpServers"]["genesis-memory"]["env"]["GENESIS_DB_PATH"]
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO eval_events (x) VALUES ('recall')")
    conn.commit()
    conn.close()


async def _run(bench_env, **kwargs):
    defaults = dict(
        tasks_path=FIXTURES,
        allow_repo_tasks=True,
        model=CCModel.SONNET,
        effort=EffortLevel.MEDIUM,
        verify_prod=False,
        run_root=bench_env / "bench-runs",
        invoker=_FakeInvoker(snapshot_writer=_snapshot_writer),
        router=_FakeRouter({"genesis answer": 0.9, "bare answer": 0.4}),
    )
    defaults.update(kwargs)
    return await run_bench(**defaults)


class TestRunBench:
    async def test_happy_path_pairs_and_stats(self, bench_env):
        report = await _run(bench_env)
        assert len(report.pairs) == 3
        assert all(not p.skipped for p in report.pairs)
        assert report.judge_calibrated is False
        assert report.rubric_version == "1.0.0"
        # genesis (0.9) beats bare (0.4) on every pair.
        assert report.score_winrate["n_treatment_wins"] == 3
        assert report.score_winrate["n_control_wins"] == 0
        # N=3 → exact McNemar has no power; honesty label preserved.
        assert report.score_winrate["recommendation"] == "insufficient_data"
        # Positive control satisfied (fake server wrote the snapshot).
        assert not any("INVALID RUN" in n for n in report.notes)

    async def test_report_written_to_output_dir(self, bench_env):
        report = await _run(bench_env)
        out = (
            bench_env / "dot-genesis" / "output" / f"bench_report_{report.run_id}.json"
        )
        assert out.exists()
        payload = json.loads(out.read_text())
        assert payload["judge_calibrated"] is False
        assert payload["task_file_sha256"] == report.task_file_sha256

    async def test_workdir_cleanup_and_keep(self, bench_env):
        root = bench_env / "bench-runs"
        report = await _run(bench_env)
        assert not (root / report.run_id).exists()
        report2 = await _run(bench_env, keep_workdir=True)
        assert (root / report2.run_id).exists()

    async def test_cc_error_skips_whole_pair(self, bench_env):
        async def behavior(inv, arm):
            if arm == "genesis" and ":recall_synth_1:" in (inv.session_key or ""):
                raise CCProcessError("boom")
            return _cc_output(f"{arm} answer")

        report = await _run(
            bench_env,
            invoker=_FakeInvoker(behavior=behavior, snapshot_writer=_snapshot_writer),
        )
        by_id = {p.task.id: p for p in report.pairs}
        pair = by_id["recall_synth_1"]
        assert pair.skipped
        assert pair.genesis.skipped and "CCProcessError" in pair.genesis.skip_reason
        assert not pair.bare.skipped  # the healthy arm's outcome is retained
        # Stats computed over the 2 complete pairs only.
        assert report.score_winrate["n_cases"] == 2

    async def test_empty_output_is_infra_skip(self, bench_env):
        async def behavior(inv, arm):
            if arm == "bare":
                return _cc_output("   ")
            return _cc_output("genesis answer")

        report = await _run(
            bench_env,
            invoker=_FakeInvoker(behavior=behavior, snapshot_writer=_snapshot_writer),
        )
        assert all(p.bare.skipped for p in report.pairs)
        assert all("empty output" in p.bare.skip_reason for p in report.pairs)

    async def test_judge_failure_skips_pair(self, bench_env):
        report = await _run(bench_env, router=_FakeRouter(fail=True))
        assert all(p.skipped for p in report.pairs)
        assert any("no complete pairs" in n for n in report.notes)

    async def test_model_downgrade_skips_pair(self, bench_env):
        """A quota-downgraded arm breaks fairness — infra skip, not a score."""
        async def behavior(inv, arm):
            return _cc_output(f"{arm} answer", downgraded=(arm == "bare"))

        report = await _run(
            bench_env,
            invoker=_FakeInvoker(behavior=behavior, snapshot_writer=_snapshot_writer),
        )
        assert all(p.bare.skipped for p in report.pairs)
        assert all("model downgrade" in p.bare.skip_reason for p in report.pairs)

    async def test_arm_degraded_positive_control(self, bench_env):
        """Genesis arm 'runs' but nothing writes the snapshot → INVALID."""
        report = await _run(
            bench_env, invoker=_FakeInvoker(snapshot_writer=None),
        )
        assert any("INVALID RUN (arm_degraded)" in n for n in report.notes)

    async def test_arm_degraded_not_masked_by_judge_failure(self, bench_env):
        """Regression (shakedown 2026-07-09): a judge outage skipped every
        pair AFTER the arms ran, which set genesis.skipped=True and silently
        disarmed the positive control. The pre-judge ran-state must keep it
        armed: judge dead + no snapshot writes → still INVALID."""
        report = await _run(
            bench_env,
            invoker=_FakeInvoker(snapshot_writer=None),
            router=_FakeRouter(fail=True),
        )
        assert any("INVALID RUN (arm_degraded)" in n for n in report.notes)

    async def test_persistence_pairs_runs(self, bench_env, eval_db):
        db = eval_db
        report = await _run(bench_env, db=db)
        assert report.control_run_id and report.treatment_run_id

        rows = await db.execute_fetchall(
            "SELECT id, model_profile, comparison_run_id, metadata_json "
            "FROM eval_runs ORDER BY model_profile",
        )
        by_profile = {r[1]: r for r in rows}
        control = by_profile["bench:bare"]
        treatment = by_profile["bench:genesis"]
        assert treatment[2] == control[0]  # treatment → control link
        meta = json.loads(treatment[3])
        assert meta["judge_calibrated"] is False
        assert meta["rubric"] == "bench_task_success"
        assert meta["paired_run_id"] == control[0]
        assert meta["invalid"] is False

        results = await db.execute_fetchall(
            "SELECT run_id, case_id, scorer_type, scorer_detail FROM eval_results",
        )
        assert len(results) == 6  # 3 tasks × 2 arms
        detail = json.loads(results[0][3])
        assert detail["rubric_version"] == "1.0.0"

    async def test_persistence_failure_is_nonfatal(self, bench_env):
        class _BrokenDB:
            async def execute(self, *a, **kw):
                raise sqlite3.OperationalError("database is locked")

            async def commit(self):
                raise AssertionError("unreachable")

        report = await _run(bench_env, db=_BrokenDB())
        assert any("PERSISTENCE FAILED" in n for n in report.notes)
        assert report.pairs  # run outcome intact

    async def test_task_id_filter_and_limit(self, bench_env):
        report = await _run(bench_env, task_id="drafting_synth_1")
        assert [p.task.id for p in report.pairs] == ["drafting_synth_1"]
        report2 = await _run(bench_env, limit=2)
        assert len(report2.pairs) == 2

    async def test_lock_excludes_concurrent_runs(self, bench_env):
        from genesis.eval.bench import runner as runner_mod

        fh = runner_mod._acquire_lock()
        try:
            with pytest.raises(BenchBusyError):
                await _run(bench_env)
        finally:
            runner_mod._release_lock(fh)

    async def test_console_render_smoke(self, bench_env):
        from genesis.eval.bench.report import render_console

        report = await _run(bench_env)
        text = render_console(report)
        assert "judge_calibrated: false" in text
        assert "insufficient_data" in text
        assert "PILOT" in text


class TestCli:
    def _parse(self, argv):
        import argparse

        from genesis.eval.cli import add_parser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_parser(sub)
        return parser.parse_args(argv)

    def test_bench_subcommand_registered(self):
        args = self._parse(["eval", "bench", "--model", "opus", "--limit", "2"])
        assert args.eval_command == "bench"
        assert args.model == "opus"
        assert args.limit == 2
        assert args.no_db is False

    def test_benchmark_subcommand_untouched(self):
        args = self._parse(["eval", "benchmark", "--include-paid"])
        assert args.eval_command == "benchmark"

    async def test_cmd_bench_rejects_bad_model(self, capsys):
        import argparse

        from genesis.eval.cli import _cmd_bench

        args = argparse.Namespace(
            model="gpt-99", effort="medium", tasks=None, limit=None,
            task_id=None, epsilon=0.05, no_db=True, keep_workdir=False,
            no_verify_prod=True,
        )
        assert await _cmd_bench(args) == 2
