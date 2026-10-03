"""External review runner — policy, dedup and the fail directions.

INSTALL-AGNOSTIC: no network, no `gh`, no orchestrator binary, no live config.
Every external command goes through the injectable ``runner`` seam, and the one
test that needs a real dispatch decision stubs the spawn rather than performing one.

The dedup fixture is the SHAPE of a real orchestrator report captured from a live
run (2026-09-14), not a stylised approximation — a hand-written one is exactly what
hid the newline-shredding defect this suite now pins: the first version of the fetch
split bodies on newlines, so the report marker and the head SHA landed in different
strings and an already-reviewed pull request read as never reviewed. A fixture that
passes a whole comment as one string cannot see that; the fetch test below drives
the real output shape.
"""

from __future__ import annotations

import base64
import json

import pytest

from genesis.session_awareness import external_review as er
from genesis.session_awareness import external_review_config as cfgmod

HEAD = "cc2ac04d812ba836f3b96c09f5d8442c56ba4ebb"
OTHER_HEAD = "b" * 40
MARKER = "<!-- reviewer-report -->"
WORKFLOW = "review"

REAL_REPORT = f"""{MARKER}
# Review Report — PR #1932

## Verdict

**ready: true** — all four required lenses reported zero findings.

## Reviewed Head SHA

`{HEAD}`

## Findings

None.
"""


def _cfg(**over):
    """A fully-configured install, so tests exercise policy rather than preflight."""
    cfg = {
        "mode": "dry_run",
        "orchestrator": {
            "command": "true",  # a real, always-present executable
            "argv": ["{workflow}", "{pr}", "{head}"],
            "workflow": WORKFLOW,
            "allow_workflows": [WORKFLOW],
            "report_marker": MARKER,
        },
        "max_dispatches_per_scan": 1,
        "skip_drafts": True,
        "require_ci_green": True,
    }
    cfg.update(over)
    return cfg


def _runner_returning(mapping: dict[str, tuple[int, str, str]]):
    """An injectable runner keyed on a substring of the command."""

    def _run(argv, *, timeout: int = 60):  # noqa: ARG001 — signature parity
        joined = " ".join(argv)
        for needle, result in mapping.items():
            if needle in joined:
                return result
        return 1, "", f"unexpected command: {joined}"

    return _run


class TestReportMentionsHead:
    """Dedup is a containment check, not a parse — see the function's docstring."""

    def test_report_naming_the_head_is_a_match(self):
        assert er.report_mentions_head([REAL_REPORT], marker=MARKER, head=HEAD)

    def test_report_for_a_different_head_is_not(self):
        assert not er.report_mentions_head([REAL_REPORT], marker=MARKER, head=OTHER_HEAD)

    def test_a_comment_without_the_marker_never_counts(self):
        """Another bot quoting the sha must not read as our orchestrator's report."""
        assert not er.report_mentions_head(
            [f"some other bot says {HEAD}"], marker=MARKER, head=HEAD
        )

    def test_no_marker_configured_matches_nothing(self):
        """An unset marker must not silently match — the runner refuses instead."""
        assert not er.report_mentions_head([REAL_REPORT], marker="", head=HEAD)

    def test_no_comments_is_not_a_review(self):
        assert not er.report_mentions_head([], marker=MARKER, head=HEAD)


class TestCommentFetch:
    """The producer side of dedup — drives the real `gh` output SHAPE."""

    def test_multiline_body_survives_as_one_body(self):
        """VERIFY-RED ANCHOR: the defect this pins.

        `gh -q '.[].body'` emits a multi-line comment as many lines. Encoding each
        body keeps one comment on one line; without that, the marker and the SHA end
        up in different strings and dedup silently fails open.
        """
        encoded = base64.b64encode(REAL_REPORT.encode()).decode()
        runner = _runner_returning({
            "issues/1932/comments": (0, encoded + "\n", ""),
            "pulls/1932/reviews": (0, "", ""),
            "pulls/1932/comments": (0, "", ""),
        })
        bodies = er._pr_comment_bodies(1932, "o/r", runner)
        assert bodies is not None
        assert len(bodies) == 1, "one comment must decode to exactly one body"
        assert er.report_mentions_head(bodies, marker=MARKER, head=HEAD)

    def test_failed_read_is_none_not_empty(self):
        """None means 'could not tell'; [] means 'no comments'. Only [] is safe."""
        runner = _runner_returning({"comments": (1, "", "boom"), "reviews": (1, "", "boom")})
        assert er._pr_comment_bodies(1932, "o/r", runner) is None

    def test_undecodable_comment_fails_the_whole_read(self):
        """One bad comment must not read as 'no comments' — that means re-dispatch."""
        runner = _runner_returning({"comments": (0, "!!!not-base64!!!\n", ""), "reviews": (0, "", "")})
        assert er._pr_comment_bodies(1932, "o/r", runner) is None


class TestCiIsGreen:
    @pytest.mark.parametrize(
        ("rollup", "expected"),
        [
            ([{"conclusion": "SUCCESS"}], True),
            ([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}], True),
            ([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}], False),
            ([{"status": "IN_PROGRESS", "conclusion": None}], False),
            ([], None),
        ],
    )
    def test_states(self, rollup, expected):
        assert er.ci_is_green({"statusCheckRollup": rollup}) is expected

    def test_absent_rollup_is_unknown_not_green(self):
        """A conflicting branch produces no rollup; unknown must never read as pass."""
        assert er.ci_is_green({}) is None


class TestEligibility:
    def _pr(self, **over):
        base = {
            "number": 1,
            "headRefOid": HEAD,
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        base.update(over)
        return base

    def _call(self, pr, **over):
        kwargs = {
            "already_reviewed": False,
            "comments_readable": True,
            "skip_drafts": True,
            "require_ci_green": True,
        }
        kwargs.update(over)
        return er.eligibility(pr, **kwargs)

    def test_unreviewed_head_is_eligible(self):
        ok, reason = self._call(self._pr())
        assert ok and "no report for head" in reason

    def test_reviewed_head_is_skipped(self):
        ok, reason = self._call(self._pr(), already_reviewed=True)
        assert not ok and "already reviewed" in reason

    def test_unreadable_comments_skip_rather_than_risk_a_duplicate(self):
        """The fail direction: a repeat review spends the shared subscription."""
        ok, reason = self._call(self._pr(), comments_readable=False)
        assert not ok and "cannot rule out" in reason

    def test_draft_is_skipped_when_configured(self):
        ok, _ = self._call(self._pr(isDraft=True))
        assert not ok

    def test_draft_is_allowed_when_not_configured(self):
        ok, _ = self._call(self._pr(isDraft=True), skip_drafts=False)
        assert ok

    def test_red_ci_is_skipped(self):
        ok, reason = self._call(self._pr(statusCheckRollup=[{"conclusion": "FAILURE"}]))
        assert not ok and "CI not green" in reason

    def test_unknown_ci_is_skipped(self):
        ok, reason = self._call(self._pr(statusCheckRollup=[]))
        assert not ok and "unknown" in reason

    def test_malformed_head_is_skipped(self):
        ok, reason = self._call(self._pr(headRefOid="abc"))
        assert not ok and "malformed" in reason


class TestDispatchScope:
    """The allowlist is what stands in for the approval gate."""

    def test_workflow_outside_the_allowlist_is_refused_even_when_passed_directly(self):
        block = _cfg()["orchestrator"]
        decision, detail = er.dispatch(1, workflow="writes-code", block=block)
        assert decision == er.DECISION_FAILED
        assert "not in allow_workflows" in detail

    def test_empty_allowlist_permits_nothing(self):
        block = dict(_cfg()["orchestrator"], allow_workflows=[])
        decision, _ = er.dispatch(1, workflow=WORKFLOW, block=block)
        assert decision == er.DECISION_FAILED

    def test_dry_run_never_spawns(self, monkeypatch):
        def _explode(*a, **k):
            raise AssertionError("dry run must not spawn a process")

        monkeypatch.setattr(er.subprocess, "Popen", _explode)
        decision, _ = er.dispatch(1, workflow=WORKFLOW, block=_cfg()["orchestrator"], dry_run=True)
        assert decision == er.DECISION_DRY_RUN

    def test_missing_binary_is_reported_not_raised(self):
        block = dict(_cfg()["orchestrator"], command="definitely-not-installed-xyz")
        decision, detail = er.dispatch(1, workflow=WORKFLOW, block=block)
        assert decision == er.DECISION_FAILED and "not installed" in detail


class TestBuildArgv:
    def test_substitutes_workflow_and_pr(self):
        block = _cfg()["orchestrator"]
        argv = er.build_argv(block, workflow=WORKFLOW, pr=42, head=HEAD)
        assert argv is not None
        assert argv[1:] == [WORKFLOW, "42", HEAD]

    def test_non_list_argv_is_refused(self):
        block = dict(_cfg()["orchestrator"], argv="run {pr}")
        assert er.build_argv(block, workflow=WORKFLOW, pr=1) is None

    def test_absent_command_is_refused(self):
        block = dict(_cfg()["orchestrator"], command="")
        assert er.build_argv(block, workflow=WORKFLOW, pr=1) is None


class TestCompressRows:
    def test_acted_rows_survive_whole(self):
        rows = [
            {"pr": 1, "decision": er.DECISION_DISPATCHED, "reason": "new"},
            {"pr": 2, "decision": er.DECISION_SKIPPED, "reason": "CI not green"},
        ]
        out = er.compress_rows(rows)
        acted = [r for r in out if r.get("decision") == er.DECISION_DISPATCHED]
        assert acted == [rows[0]]

    def test_skips_become_one_counted_summary_with_its_denominator(self):
        rows = [
            {"pr": n, "decision": er.DECISION_SKIPPED, "reason": "CI not green"} for n in range(70)
        ]
        out = er.compress_rows(rows)
        assert len(out) == 1
        summary = out[0]
        assert summary["skipped_total"] == 70
        assert summary["skipped_by_reason"] == {"CI not green": 70}
        assert len(summary["prs"]) == 70, "the skipped population is not dropped"

    def test_stays_under_the_audit_writers_row_cap(self):
        """The defect this exists for: an over-cap batch is refused OUTRIGHT, so an
        uncompressed scan wrote no audit at all."""
        rows = [
            {"pr": n, "decision": er.DECISION_SKIPPED, "reason": f"reason {n % 3}"}
            for n in range(500)
        ]
        assert len(er.compress_rows(rows)) < 64

    def test_failures_fold_too(self):
        """VERIFY-RED ANCHOR. Folding only SKIPPED left the one other population that
        can reach every PR in the queue — a dispatch failing for all of them —
        passing through whole, over the writer's cap, and refused outright: the exact
        silent-empty-audit defect this function exists to prevent."""
        rows = [
            {"pr": n, "decision": er.DECISION_FAILED, "reason": "argv malformed"} for n in range(80)
        ]
        out = er.compress_rows(rows)
        assert len(out) == 1
        assert out[0]["failed_total"] == 80
        assert out[0]["failed_by_reason"] == {"argv malformed": 80}

    def test_a_mixed_scan_stays_bounded(self):
        rows = (
            [{"pr": n, "decision": er.DECISION_FAILED, "reason": "boom"} for n in range(40)]
            + [{"pr": n, "decision": er.DECISION_SKIPPED, "reason": "draft"} for n in range(40)]
            + [{"pr": 999, "decision": er.DECISION_DISPATCHED, "reason": "new"}]
        )
        assert len(er.compress_rows(rows)) == 3  # dispatched + failed fold + skipped fold


class TestScanBudget:
    def _scan_with(self, pr_count, monkeypatch, budget=1):
        prs = [
            {
                "number": 100 + i,
                "headRefOid": HEAD,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            }
            for i in range(pr_count)
        ]
        runner = _runner_returning(
            {
                "repo view": (0, "o/r\n", ""),
                "pr list": (0, json.dumps(prs), ""),
                "comments": (0, "", ""),
                "reviews": (0, "", ""),
            }
        )
        monkeypatch.setattr(er, "record", lambda rows: None)
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        return er.scan(runner=runner, cfg=_cfg(max_dispatches_per_scan=budget))

    def test_failed_dispatches_consume_the_budget(self, monkeypatch):
        """VERIFY-RED ANCHOR. Counting only SUCCESSES means a cap that never binds
        when every dispatch fails — the whole queue is attempted, one row per PR, and
        the audit writer then refuses the oversized batch and records nothing."""
        prs = [
            {
                "number": 100 + i,
                "headRefOid": HEAD,
                "isDraft": False,
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            }
            for i in range(20)
        ]
        runner = _runner_returning(
            {
                "repo view": (0, "o/r\n", ""),
                "pr list": (0, json.dumps(prs), ""),
                # live mode re-reads the head immediately before spawning
                "pr view": (0, json.dumps(prs[0]), ""),
                "comments": (0, "", ""),
                "reviews": (0, "", ""),
            }
        )
        monkeypatch.setattr(er, "record", lambda rows: "/tmp/claim.jsonl")
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        # The SPAWN fails (not the config — a malformed template is now caught in
        # preflight, which is a different defect). This isolates the budget property:
        # an attempt that fails has still consumed a decision.
        monkeypatch.setattr(er, "claim_dispatch", lambda *a, **k: True)
        monkeypatch.setattr(er, "release_claim", lambda *a, **k: None)
        monkeypatch.setattr(
            er, "dispatch", lambda *a, **k: (er.DECISION_FAILED, "spawn failed: boom")
        )
        summary = er.scan(runner=runner, cfg=_cfg(mode="live"))
        failed = [d for d in summary["decisions"] if d[1] == er.DECISION_FAILED]
        assert len(failed) == 1, "the budget must bind on ATTEMPTS, not on successes"
        assert summary["failure"], "an infrastructure failure must be reported upward"

    def test_budget_is_a_hard_cap(self, monkeypatch):
        summary = self._scan_with(10, monkeypatch, budget=1)
        assert summary["dispatched"] == 1
        assert summary["considered"] == 10

    def test_budget_zero_dispatches_nothing(self, monkeypatch):
        summary = self._scan_with(5, monkeypatch, budget=0)
        assert summary["dispatched"] == 0

    def test_mode_off_short_circuits_before_any_command(self):
        def _explode(*a, **k):
            raise AssertionError("mode=off must not touch the network")

        summary = er.scan(runner=_explode, cfg=_cfg(mode="off"))
        assert summary["dispatched"] == 0 and summary["mode"] == "off"

    def test_unconfigured_install_dispatches_nothing_and_says_so(self):
        """A fresh clone ships an empty allowlist — it must no-op, not fail per-PR."""

        def _explode(*a, **k):
            raise AssertionError("an unconfigured install must not touch the network")

        cfg = _cfg()
        cfg["orchestrator"] = dict(cfg["orchestrator"], allow_workflows=[], workflow="")
        summary = er.scan(runner=_explode, cfg=cfg)
        assert summary["dispatched"] == 0
        assert "no permitted workflow" in summary["detail"]
        assert "failure" not in summary, "an unconfigured install is the designed no-op"

    def test_live_without_a_report_marker_refuses(self):
        """No marker means no dedup, which would re-review everything every tick."""

        def _explode(*a, **k):
            raise AssertionError("must refuse before touching the network")

        cfg = _cfg(mode="live")
        cfg["orchestrator"] = dict(cfg["orchestrator"], report_marker="")
        summary = er.scan(runner=_explode, cfg=cfg)
        assert summary["dispatched"] == 0 and "report_marker" in summary["detail"]

    def test_review_one_honours_a_zero_budget(self, monkeypatch):
        """VERIFY-RED ANCHOR. The budget caps spend against a shared subscription, so
        it cannot be true on one entry point and false on the other."""
        runner = _runner_returning(
            {
                "repo view": (0, "o/r\n", ""),
                "pr view": (
                    0,
                    json.dumps(
                        {
                            "number": 7,
                            "headRefOid": HEAD,
                            "isDraft": False,
                            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                        }
                    ),
                    "",
                ),
                "comments": (0, "", ""),
                "reviews": (0, "", ""),
            }
        )
        monkeypatch.setattr(er, "record", lambda rows: None)
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        summary = er.review_one(7, runner=runner, cfg=_cfg(max_dispatches_per_scan=0))
        assert summary["dispatched"] == 0

    def test_review_one_respects_an_in_flight_dispatch(self, monkeypatch):
        runner = _runner_returning(
            {
                "repo view": (0, "o/r\n", ""),
                "pr view": (
                    0,
                    json.dumps(
                        {
                            "number": 7,
                            "headRefOid": HEAD,
                            "isDraft": False,
                            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                        }
                    ),
                    "",
                ),
                "comments": (0, "", ""),
                "reviews": (0, "", ""),
            }
        )
        monkeypatch.setattr(er, "record", lambda rows: None)
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: {("o/r", 7, HEAD)})
        summary = er.review_one(7, runner=runner, cfg=_cfg())
        assert summary["dispatched"] == 0

    def test_unresolvable_repo_does_not_dispatch(self, monkeypatch):
        runner = _runner_returning({"repo view": (1, "", "no auth")})
        monkeypatch.setattr(er, "record", lambda rows: None)
        summary = er.scan(runner=runner, cfg=_cfg())
        assert summary["dispatched"] == 0 and "unresolved" in summary["detail"]

    def test_unreadable_pr_list_does_not_dispatch(self, monkeypatch):
        runner = _runner_returning({"repo view": (0, "o/r\n", ""), "pr list": (1, "", "boom")})
        monkeypatch.setattr(er, "record", lambda rows: None)
        summary = er.scan(runner=runner, cfg=_cfg())
        assert summary["dispatched"] == 0 and "unreadable" in summary["detail"]


class TestUnitTemplateContract:
    """The unit file is executable configuration; a wrong value here is silent."""

    def test_service_sets_killmode_process(self):
        """VERIFY-RED ANCHOR for a measured BLOCKER.

        The runner DETACHES the orchestrator and returns in seconds. A Type=oneshot
        unit goes inactive when ExecStart returns, and systemd's DEFAULT
        KillMode=control-group then reaps everything left in the cgroup —
        `start_new_session=True` leaves the process group, not the cgroup. MEASURED
        with a control: default KillMode killed the detached child, KillMode=process
        did not. Without this line every timer-fired dispatch is killed instantly
        while the audit row still reads "dispatched".
        """
        from pathlib import Path

        template = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "systemd"
            / "genesis-external-review.service.template"
        )
        # Assert the DIRECTIVE, not the string. A substring check passes on the
        # explanatory comment above it — MEASURED: deleting the directive and leaving
        # the comment kept this test green, which is the textbook vacuous test (it
        # would pass with the mechanism removed). Parse directive lines instead:
        # strip comments, keep `key=value`, and look for the real setting.
        directives = {
            line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
            for line in template.read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        assert directives.get("Type") == "oneshot"
        assert directives.get("KillMode") == "process", (
            "a detached dispatch is reaped when ExecStart returns without this "
            "directive; a comment mentioning it is not the setting"
        )

    @pytest.mark.parametrize("installer", ["bootstrap.sh", "install.sh"])
    def test_timer_is_not_auto_enabled_by_any_installer(self, installer):
        """Autonomous, subscription-spending review must be opt-in on every clone —
        and the exclusion means nothing unless EVERY enable path carries it. There
        are TWO installers; the first version of this change covered one. The first
        version of this TEST would also have passed on a commented-out guard, since
        a substring check matches a comment, so it asserts over executable lines."""
        from pathlib import Path

        text = (Path(__file__).resolve().parents[2] / "scripts" / installer).read_text()
        executable = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert "genesis-external-review.timer) continue ;;" in executable, (
            f"{installer} would auto-enable the timer"
        )


class TestRecentDispatchHeads:
    """The half of dedup that cannot live on the pull request."""

    def _write(self, tmp_path, rows):
        import json as _json

        (tmp_path / "a.jsonl").write_text("\n".join(_json.dumps(r) for r in rows))

    def test_a_recorded_dispatch_is_returned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        self._write(tmp_path, [{"repo": "o/r", "pr": 7, "head": HEAD, "decision": er.DECISION_DISPATCHED}])
        assert ("o/r", 7, HEAD) in er.recent_dispatch_heads()

    def test_a_skip_is_not_a_dispatch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        self._write(tmp_path, [{"repo": "o/r", "pr": 7, "head": HEAD, "decision": er.DECISION_SKIPPED}])
        assert er.recent_dispatch_heads() == set()

    def test_absent_store_degrades_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path / "nope"))
        assert er.recent_dispatch_heads() == set()

    def test_malformed_rows_do_not_blind_the_check(self, tmp_path, monkeypatch):
        """One bad file must narrow less, never fail the whole run."""
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        (tmp_path / "bad.jsonl").write_text("{not json\n")
        self._write(tmp_path, [{"repo": "o/r", "pr": 7, "head": HEAD, "decision": er.DECISION_DISPATCHED}])
        assert ("o/r", 7, HEAD) in er.recent_dispatch_heads()

    def test_cooloff_excludes_an_old_file(self, tmp_path, monkeypatch):
        import os as _os
        import time as _time

        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        self._write(tmp_path, [{"repo": "o/r", "pr": 7, "head": HEAD, "decision": er.DECISION_DISPATCHED}])
        old = _time.time() - 60
        _os.utime(tmp_path / "a.jsonl", (old, old))
        assert er.recent_dispatch_heads(within_s=10) == set()


class TestPrefilter:
    """Cheap checks run BEFORE the paginated comment read."""

    def _pr(self, **over):
        base = {
            "number": 7,
            "headRefOid": HEAD,
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        base.update(over)
        return base

    def _call(self, pr, **over):
        kwargs = {
            "skip_drafts": True,
            "require_ci_green": True,
            "recently_dispatched": set(),
            "slug": "o/r",
        }
        kwargs.update(over)
        return er.prefilter(pr, **kwargs)

    def test_clean_pr_passes(self):
        ok, _ = self._call(self._pr())
        assert ok

    def test_recently_dispatched_head_is_suppressed(self):
        """VERIFY-RED ANCHOR: without this the same head re-dispatches every tick."""
        ok, reason = self._call(self._pr(), recently_dispatched={("o/r", 7, HEAD)})
        assert not ok and "may still be in flight" in reason

    def test_a_different_head_is_not_suppressed(self):
        ok, _ = self._call(self._pr(), recently_dispatched={("o/r", 7, OTHER_HEAD)})
        assert ok

    def test_draft_and_ci_are_decided_without_comments(self):
        assert not self._call(self._pr(isDraft=True))[0]
        assert not self._call(self._pr(statusCheckRollup=[]))[0]

    def test_scan_reads_no_comments_for_a_filtered_pr(self, monkeypatch):
        """The COST property: a draft/red PR must not cost a paginated call."""
        calls = []

        def _runner(argv, *, timeout=60):  # noqa: ARG001
            joined = " ".join(argv)
            if "comments" in joined:
                calls.append(joined)
            if "repo view" in joined:
                return 0, "o/r\n", ""
            if "pr list" in joined:
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "number": 7,
                                "headRefOid": HEAD,
                                "isDraft": True,
                                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                            }
                        ]
                    ),
                    "",
                )
            return 0, "", ""

        monkeypatch.setattr(er, "record", lambda rows: None)
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        er.scan(runner=_runner, cfg=_cfg())
        assert calls == [], "a filtered PR must cost no comment read"


def _live_runner(head=HEAD, number=7):
    """A runner whose PR view and list both report one eligible, green PR."""
    body = {
        "number": number,
        "headRefOid": head,
        "isDraft": False,
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }
    return _runner_returning(
        {
            "repo view": (0, "o/r\n", ""),
            "pr list": (0, json.dumps([body]), ""),
            "pr view": (0, json.dumps(body), ""),
            "comments": (0, "", ""),
                "reviews": (0, "", ""),
        }
    )


class TestWorkOrderDispatch:
    """The dispatch boundary must CARRY and PERSIST the decision that authorised it.

    Four review findings shared this one generator: the head, the repo and the audit
    record all stopped at the boundary, and the log was created without the store's
    permissions. These pin the mechanism, not the four instances.
    """

    def test_argv_carries_repo_and_head(self):
        block = dict(_cfg()["orchestrator"], argv=["{workflow}", "{repo}", "{pr}", "{head}"])
        argv = er.build_argv(block, workflow=WORKFLOW, pr=42, repo="o/r", head=HEAD)
        assert argv[1:] == [WORKFLOW, "o/r", "42", HEAD]

    def test_claim_is_persisted_BEFORE_the_spawn(self, monkeypatch):
        """VERIFY-RED ANCHOR: ordering is the property. A claim written after the
        spawn means a failed write leaves a running review with no record of it."""
        order = []
        monkeypatch.setattr(er, "claim_dispatch", lambda *a, **k: order.append("claim") or True)
        monkeypatch.setattr(er, "release_claim", lambda *a, **k: None)
        monkeypatch.setattr(er, "record", lambda rows: order.append("record") or "/tmp/x")
        monkeypatch.setattr(
            er,
            "dispatch",
            lambda *a, **k: (order.append("dispatch"), (er.DECISION_DISPATCHED, "ok"))[1],
        )
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        er.review_one(7, runner=_live_runner(), cfg=_cfg(mode="live"))
        assert order[:3] == ["claim", "record", "dispatch"], (
            f"interlock must precede the audit claim, which must precede spawn, got {order}"
        )

    def test_unpersistable_claim_refuses_to_spend(self, monkeypatch):
        """If the audit cannot be written, the dispatch does not happen — the trail
        that stands in for the approval gate is not optional."""
        monkeypatch.setattr(er, "record", lambda rows: None)  # write_batch failure
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        monkeypatch.setattr(er, "claim_dispatch", lambda *a, **k: True)
        monkeypatch.setattr(er, "release_claim", lambda *a, **k: None)

        def _explode(*a, **k):
            raise AssertionError("must not spawn when the claim cannot be persisted")

        monkeypatch.setattr(er, "dispatch", _explode)
        summary = er.review_one(7, runner=_live_runner(), cfg=_cfg(mode="live"))
        assert summary["dispatched"] == 0
        assert "could not be persisted" in summary["decisions"][0][2]

    def test_head_moving_between_checks_and_spawn_skips(self, monkeypatch):
        """VERIFY-RED ANCHOR: every check was made for one commit; a push in the gap
        would review code whose CI nobody has seen."""
        listed = {
            "number": 7,
            "headRefOid": HEAD,
            "isDraft": False,
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        moved = dict(listed, headRefOid=OTHER_HEAD)
        runner = _runner_returning(
            {
                "repo view": (0, "o/r\n", ""),
                "pr list": (0, json.dumps([listed]), ""),
                "pr view": (0, json.dumps(moved), ""),  # head moved before the spawn
                "comments": (0, "", ""),
                "reviews": (0, "", ""),
            }
        )
        monkeypatch.setattr(er, "record", lambda rows: "/tmp/x")
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())

        def _explode(*a, **k):
            raise AssertionError("must not dispatch against a moved head")

        monkeypatch.setattr(er, "dispatch", _explode)
        summary = er.scan(runner=runner, cfg=_cfg(mode="live"))
        assert summary["dispatched"] == 0
        assert "head moved" in summary["decisions"][0][2]

    def test_live_requires_workflow_in_the_template(self):
        """The allowlist cannot bind a child that is never told which workflow."""
        cfg = _cfg(mode="live")
        cfg["orchestrator"] = dict(cfg["orchestrator"], argv=["run", "{pr}"])
        assert "{workflow}" in er._preflight(cfg, "live")[0]

    def test_live_requires_pr_and_head_in_the_template(self):
        """Without {pr} the same untargeted command fires for every pull request;
        without {head} the child re-resolves HEAD after the decision that
        authorised a specific commit."""
        cfg = _cfg(mode="live")
        for argv, needle in (
            (["{workflow}", "{head}"], "{pr}"),
            (["{workflow}", "{pr}"], "{head}"),
        ):
            cfg["orchestrator"] = dict(cfg["orchestrator"], argv=argv)
            assert needle in er._preflight(cfg, "live")[0]

    def test_repo_override_requires_repo_in_the_template(self):
        """A --repo override with no {repo} would review the same-numbered PR in the
        working-directory repository instead of the selected one."""
        cfg = _cfg(mode="live")
        assert er._preflight(cfg, "live", repo_override="other/repo")[0] is not None
        assert er._preflight(cfg, "live", repo_override=None) == (None, False)

    def test_run_log_is_owner_only(self, tmp_path, monkeypatch):
        """The log holds a review agent's full transcript of a private repository."""
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path / "store"))
        monkeypatch.setattr(er, "log_dir", lambda: str(tmp_path / "store" / "logs"))
        monkeypatch.setattr(er, "orchestrator_binary", lambda cmd: "/bin/true")
        monkeypatch.setattr(er.subprocess, "Popen", lambda *a, **k: None)
        decision, _ = er.dispatch(
            7, workflow=WORKFLOW, block=_cfg()["orchestrator"], repo="o/r", head=HEAD
        )
        assert decision == er.DECISION_DISPATCHED
        logs = tmp_path / "store" / "logs"
        assert oct(logs.stat().st_mode)[-3:] == "700"
        written = list(logs.glob("*.log"))
        assert written and oct(written[0].stat().st_mode)[-3:] == "600"


class TestBoundaryUnknowns:
    def test_non_object_audit_row_does_not_abort_the_scan(self, tmp_path, monkeypatch):
        """Valid JSON of the WRONG SHAPE escaped the handler and killed the run."""
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        (tmp_path / "a.jsonl").write_text(
            '[]\n"corrupt"\n' + json.dumps({"repo": "o/r", "pr": 7, "head": HEAD, "decision": "dispatched"})
        )
        assert ("o/r", 7, HEAD) in er.recent_dispatch_heads()

    def test_non_object_pr_entries_are_dropped(self):
        runner = _runner_returning({"pr list": (0, json.dumps([{"number": 1}, "junk", 5]), "")})
        rows = er.open_prs("o/r", runner)
        assert rows == [{"number": 1}]

    def test_pr_list_at_the_limit_is_reported_as_truncated(self, monkeypatch):
        """A count EQUAL to the limit is a truncated read, not a complete one."""
        prs = [
            {"number": n, "headRefOid": HEAD, "isDraft": True, "statusCheckRollup": []}
            for n in range(er._PR_LIST_LIMIT)
        ]
        runner = _runner_returning(
            {"repo view": (0, "o/r\n", ""), "pr list": (0, json.dumps(prs), "")}
        )
        monkeypatch.setattr(er, "record", lambda rows: None)
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        assert er.scan(runner=runner, cfg=_cfg())["truncated"] is True

    def test_kill_switch_short_circuits_before_any_config_read(self, monkeypatch):
        """VERIFY-RED ANCHOR: the emergency stop must work when config does not."""
        monkeypatch.setenv(cfgmod.DISABLE_ENV, "1")

        def _explode():
            raise AssertionError("kill switch must be checked before load_config")

        monkeypatch.setattr(cfgmod, "load_config", _explode)
        assert er.scan()["mode"] == "off"
        assert er.review_one(7)["mode"] == "off"

    def test_infrastructure_failure_is_flagged_for_a_nonzero_exit(self, monkeypatch):
        runner = _runner_returning({"repo view": (1, "", "gh auth expired")})
        monkeypatch.setattr(er, "record", lambda rows: None)
        assert er.scan(runner=runner, cfg=_cfg()).get("failure")

    def test_a_quiet_scan_is_not_a_failure(self, monkeypatch):
        """Nothing eligible is a SUCCESSFUL scan — the timer must not go red."""
        runner = _runner_returning(
            {"repo view": (0, "o/r\n", ""), "pr list": (0, json.dumps([]), "")}
        )
        monkeypatch.setattr(er, "record", lambda rows: None)
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())
        assert "failure" not in er.scan(runner=runner, cfg=_cfg())


class TestConfig:
    def test_kill_switch_outranks_live_config(self, monkeypatch):
        monkeypatch.setenv(cfgmod.DISABLE_ENV, "1")
        assert cfgmod.effective_mode({"mode": "live"}) == "off"

    @pytest.mark.parametrize("spelling", ["1", "true", "TRUE", "yes", "on", " True "])
    def test_kill_switch_accepts_the_spellings_operators_use(self, monkeypatch, spelling):
        """A kill switch that fails OPEN on `true` is worse than none."""
        monkeypatch.setenv(cfgmod.DISABLE_ENV, spelling)
        assert cfgmod.effective_mode({"mode": "live"}) == "off"

    @pytest.mark.parametrize("spelling", ["0", "false", "no", ""])
    def test_kill_switch_stays_off_for_negatives(self, monkeypatch, spelling):
        monkeypatch.setenv(cfgmod.DISABLE_ENV, spelling)
        assert cfgmod.effective_mode({"mode": "live"}) == "live"

    def test_unknown_mode_degrades_to_dry_run_not_off(self):
        """Less authority than live, but never silently off — a runner that quietly
        stops reviewing looks exactly like a queue with nothing to review."""
        assert cfgmod.effective_mode({"mode": "banana"}) == "dry_run"

    @pytest.mark.parametrize("bad", ["lots", True, -5, None])
    def test_bad_budget_degrades_to_the_default(self, bad):
        assert cfgmod.max_dispatches_per_scan({"max_dispatches_per_scan": bad}) == 1

    def test_explicit_zero_is_honoured(self):
        assert cfgmod.max_dispatches_per_scan({"max_dispatches_per_scan": 0}) == 0

    def test_workflow_outside_the_allowlist_is_refused(self):
        """A config typo must only ever narrow the scope, never widen it."""
        cfg = _cfg()
        cfg["orchestrator"] = dict(cfg["orchestrator"], workflow="writes-code")
        assert cfgmod.workflow_name(cfg) is None

    def test_empty_allowlist_permits_nothing(self):
        cfg = _cfg()
        cfg["orchestrator"] = dict(cfg["orchestrator"], allow_workflows=[])
        assert cfgmod.workflow_name(cfg) is None

    def test_shipped_config_dispatches_nothing(self):
        """THE default-safety property: a fresh clone must be inert by construction,
        not merely because its timer happens to be disabled."""
        cfg = cfgmod.load_config()
        assert cfg["mode"] in cfgmod.MODES
        assert cfgmod.workflow_name(cfg) is None
        assert cfg["orchestrator"]["command"] == ""
        assert cfg["orchestrator"]["allow_workflows"] == []

    def test_overlay_merges_orchestrator_keys_without_blanking_siblings(self):
        """An overlay setting only `command` must not wipe the other keys."""
        merged = cfgmod.orchestrator({"orchestrator": {"command": "mytool"}})
        assert merged["command"] == "mytool"
        assert "report_marker" in merged and "allow_workflows" in merged


class TestSinglePassSubstitution:
    """VERIFY-RED ANCHOR: chained replaces re-scan freshly INSERTED values."""

    def test_a_value_containing_a_placeholder_is_not_resubstituted(self):
        """A workflow literally named `review-{pr}` is allowlisted as-is; sequential
        replacement would rewrite its embedded token and dispatch a workflow the
        allowlist never named."""
        block = dict(
            _cfg()["orchestrator"],
            argv=["{workflow}", "{pr}"],
            workflow="review-{pr}",
            allow_workflows=["review-{pr}"],
        )
        argv = er.build_argv(block, workflow="review-{pr}", pr=42, head=HEAD)
        assert argv[1] == "review-{pr}", "the substituted value must not be re-scanned"
        assert argv[2] == "42"

    def test_unknown_tokens_pass_through_verbatim(self):
        block = dict(_cfg()["orchestrator"], argv=["--flag={bogus}", "{pr}"])
        argv = er.build_argv(block, workflow=WORKFLOW, pr=9, head=HEAD)
        assert argv[1] == "--flag={bogus}"


class TestStrictFlags:
    """A malformed boolean must fail to the SAFE default, never silently off."""

    @pytest.mark.parametrize("bad", ["false", "no", 0, None, ""])
    def test_non_boolean_values_keep_the_guard_on(self, bad):
        assert cfgmod.flag({"skip_drafts": bad}, "skip_drafts") is True

    def test_literal_false_disarms(self):
        assert cfgmod.flag({"skip_drafts": False}, "skip_drafts") is False

    def test_literal_true_holds(self):
        assert cfgmod.flag({"require_ci_green": True}, "require_ci_green") is True


class TestStoreDirSandbox:
    """The env override must stay inside $HOME — the unit's ReadWritePaths is %h."""

    def test_an_absolute_path_inside_home_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GENESIS_EXTERNAL_REVIEW_DIR", str(tmp_path / "store"))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert er.store_dir() == str(tmp_path / "store")

    def test_a_path_outside_home_is_rejected(self, monkeypatch):
        monkeypatch.setenv("GENESIS_EXTERNAL_REVIEW_DIR", "/srv/external-review")
        monkeypatch.setenv("HOME", "/home/tester")
        assert er.store_dir() == "/home/tester/.genesis/external_review_runs"

    def test_a_relative_path_is_rejected(self, monkeypatch):
        monkeypatch.setenv("GENESIS_EXTERNAL_REVIEW_DIR", "relative/store")
        monkeypatch.setenv("HOME", "/home/tester")
        assert er.store_dir() == "/home/tester/.genesis/external_review_runs"


class TestDispatchClaim:
    """The cross-process interlock the audit row cannot provide."""

    def test_the_first_claim_wins_and_the_second_loses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        assert er.claim_dispatch("o/r", 7, HEAD) is True
        assert er.claim_dispatch("o/r", 7, HEAD) is False

    def test_release_frees_the_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        assert er.claim_dispatch("o/r", 7, HEAD) is True
        er.release_claim("o/r", 7, HEAD)
        assert er.claim_dispatch("o/r", 7, HEAD) is True

    def test_a_stale_claim_is_reclaimable(self, tmp_path, monkeypatch):
        import os as _os
        import time as _time

        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        assert er.claim_dispatch("o/r", 7, HEAD) is True
        claims = list(tmp_path.glob("claims/*.json"))
        assert claims
        old = _time.time() - er.DISPATCH_COOLOFF_S - 60
        _os.utime(claims[0], (old, old))
        assert er.claim_dispatch("o/r", 7, HEAD) is True

    def test_an_unwritable_store_degrades_permissive(self, tmp_path, monkeypatch):
        """Dedup narrows or stays silent; it must never crash-block a dispatch."""
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path / "missing" / "deep"))
        (tmp_path / "missing").mkdir(mode=0o500)
        try:
            assert er.claim_dispatch("o/r", 7, HEAD) is True
        finally:
            (tmp_path / "missing").chmod(0o700)


class TestDedupKeyScope:
    """(repo, pr, head) — forks share PR numbers and SHAs legitimately."""

    def test_the_same_pr_in_another_repo_is_not_suppressed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        rows = [{"repo": "a/x", "pr": 7, "head": HEAD, "decision": "dispatched"}]
        (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        heads = er.recent_dispatch_heads()
        assert ("a/x", 7, HEAD) in heads
        assert ("b/y", 7, HEAD) not in heads

    def test_a_failed_row_cancels_the_claim(self, tmp_path, monkeypatch):
        """A spawn that provably never started must not suppress its own retry for
        the whole cooloff — the corrective FAILED row frees the key."""
        import os as _os
        import time as _time

        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        (tmp_path / "claim.jsonl").write_text(
            json.dumps({"repo": "o/r", "pr": 7, "head": HEAD, "decision": "dispatched"})
        )
        (tmp_path / "fail.jsonl").write_text(
            json.dumps({"repo": "o/r", "pr": 7, "head": HEAD, "decision": "failed"})
        )
        now = _time.time()
        _os.utime(tmp_path / "claim.jsonl", (now - 10, now - 10))
        _os.utime(tmp_path / "fail.jsonl", (now, now))
        assert ("o/r", 7, HEAD) not in er.recent_dispatch_heads()

    def test_a_claim_file_suppresses_without_an_audit_row(self, tmp_path, monkeypatch):
        """The interlock half of dedup: a claim that outlives an unwritten audit."""
        monkeypatch.setattr(er, "store_dir", lambda: str(tmp_path))
        assert er.claim_dispatch("o/r", 7, HEAD) is True
        assert ("o/r", 7, HEAD) in er.recent_dispatch_heads()


class TestClosedEligibility:
    def test_a_merged_pr_is_ineligible_even_with_a_green_rollup(self):
        ok, reason = er.eligibility(
            {
                "number": 1,
                "headRefOid": HEAD,
                "isDraft": False,
                "state": "MERGED",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            },
            already_reviewed=False,
            comments_readable=True,
            skip_drafts=True,
            require_ci_green=True,
        )
        assert not ok and "merged" in reason

    def test_fresh_revalidation_catches_a_late_draft(self, monkeypatch):
        """The listing was clean; the pre-spawn re-read is not — do not spend."""
        listed = {
            "number": 7,
            "headRefOid": HEAD,
            "isDraft": False,
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        drafted = dict(listed, isDraft=True)
        runner = _runner_returning(
            {
                "repo view": (0, "o/r\n", ""),
                "pr list": (0, json.dumps([listed]), ""),
                "pr view": (0, json.dumps(drafted), ""),
                "comments": (0, "", ""),
                "reviews": (0, "", ""),
            }
        )
        monkeypatch.setattr(er, "record", lambda rows: "/tmp/x")
        monkeypatch.setattr(er, "recent_dispatch_heads", lambda *a, **k: set())

        def _explode(*a, **k):
            raise AssertionError("must not dispatch against a newly-drafted PR")

        monkeypatch.setattr(er, "dispatch", _explode)
        summary = er.scan(runner=runner, cfg=_cfg(mode="live"))
        assert summary["dispatched"] == 0
        assert "no longer eligible" in summary["decisions"][0][2]


    def test_unknown_draft_status_fails_closed(self):
        """A payload that cannot say `isDraft: false` is treated as a draft —
        the skip is cheap, the dispatch is not."""
        ok, _ = er.eligibility(
            {"number": 1, "headRefOid": HEAD, "isDraft": None,
             "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
            already_reviewed=False,
            comments_readable=True,
            skip_drafts=True,
            require_ci_green=True,
        )
        assert not ok


class TestPreflightArgvTypes:
    def test_a_non_string_element_is_reported_not_raised(self):
        """' '.join([None]) raises TypeError — out of the fail-safe boundary."""
        cfg = _cfg(mode="live")
        cfg["orchestrator"] = dict(cfg["orchestrator"], argv=["{workflow}", None, "{pr}", "{head}"])
        detail, is_failure = er._preflight(cfg, "live")
        assert is_failure and "list of strings" in detail
