"""The issue premise gate: labels follow the answers, and devin-ready never stands without them.

``decide`` is pure (event payload in, label and comment changes out), so the rules
are tested directly. ``apply`` and ``main`` run with an injected ``gh`` runner,
so no test touches GitHub.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml

from tests.conftest import private_module

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "premise_gate.py"
gate = private_module("premise_gate_under_test", _SCRIPT)

_COMPLETE = """## Summary
Do the thing in `scripts/x.py`.

## Premise checks
1. n/a — no outside behaviour.
2. Searched scripts/ for x; nothing.
3. Scope reuses the shared reader.
4. Caller: the CI job, tracked by #42.
5. No numbers.
6. About 100 counted lines, one PR.
"""

_FORM_EMPTY = "### Summary\n\nDo the thing.\n\n### Premise checks\n\n_No response_\n"
_NEW = "2026-11-01T00:00:00Z"
_OLD = "2026-10-01T00:00:00Z"


def _event(action: str, body: str, *, labels=(), label=None, created=_NEW, number=7) -> dict:
    ev = {
        "action": action,
        "issue": {
            "number": number,
            "body": body,
            "created_at": created,
            "labels": [{"name": n} for n in labels],
        },
    }
    if label:
        ev["label"] = {"name": label}
    return ev


# -- job 1: the premises-missing label ---------------------------------------


@pytest.mark.parametrize("action", ["opened", "edited", "reopened"])
def test_an_issue_without_answers_gets_the_label(action: str) -> None:
    plan = gate.decide(_event(action, "Just a bug report."))
    assert plan.add == ["premises-missing"] and not plan.remove and not plan.comment


def test_the_empty_issue_form_field_counts_as_missing() -> None:
    assert gate.decide(_event("opened", _FORM_EMPTY)).add == ["premises-missing"]


def test_a_complete_issue_gets_nothing() -> None:
    plan = gate.decide(_event("opened", _COMPLETE))
    assert not plan.add and not plan.remove and not plan.comment


def test_completing_the_answers_removes_the_label() -> None:
    plan = gate.decide(_event("edited", _COMPLETE, labels=["premises-missing"]))
    assert plan.remove == ["premises-missing"] and not plan.add


def test_an_already_labelled_issue_is_not_relabelled() -> None:
    plan = gate.decide(_event("edited", "still nothing", labels=["premises-missing"]))
    assert not plan.add and not plan.remove


def test_an_issue_from_before_the_gate_is_never_newly_labelled() -> None:
    plan = gate.decide(_event("edited", "old issue", created=_OLD))
    assert not plan.add


def test_a_grandfathered_issue_still_loses_the_label_once_complete() -> None:
    plan = gate.decide(_event("edited", _COMPLETE, labels=["premises-missing"], created=_OLD))
    assert plan.remove == ["premises-missing"]


def test_one_empty_answer_is_enough_to_label() -> None:
    body = _COMPLETE.replace("5. No numbers.", "5. TBD")
    assert gate.decide(_event("opened", body)).add == ["premises-missing"]


def test_other_label_events_do_nothing() -> None:
    plan = gate.decide(_event("labeled", "nothing here", label="enhancement"))
    assert not plan.add and not plan.remove and not plan.comment


def test_a_pull_request_payload_is_ignored() -> None:
    ev = _event("opened", "x")
    ev["issue"]["pull_request"] = {"url": "u"}
    assert not gate.decide(ev).add


# -- job 2: the devin-ready guard --------------------------------------------


def test_devin_ready_without_answers_is_removed_with_an_aside_comment() -> None:
    plan = gate.decide(_event("labeled", "bare spec", labels=["devin-ready"], label="devin-ready"))
    assert plan.remove == ["devin-ready"]
    assert plan.add == ["premises-missing"]
    assert plan.comment.startswith("(aside)\n")
    assert "6 of 6" in plan.comment


def test_the_comment_names_only_the_missing_answers() -> None:
    body = _COMPLETE.replace("3. Scope reuses the shared reader.", "3. n/a")
    plan = gate.decide(_event("labeled", body, labels=["devin-ready"], label="devin-ready"))
    assert "1 of 6" in plan.comment and "3. scope limits" in plan.comment
    assert "1. outside-behaviour" not in plan.comment


def test_devin_ready_on_a_complete_issue_stays() -> None:
    plan = gate.decide(_event("labeled", _COMPLETE, labels=["devin-ready"], label="devin-ready"))
    assert not plan.add and not plan.remove and not plan.comment


def test_the_guard_ignores_grandfathering() -> None:
    plan = gate.decide(
        _event(
            "labeled", "old bare spec", labels=["devin-ready"], label="devin-ready", created=_OLD
        )
    )
    assert plan.remove == ["devin-ready"]


def test_the_guard_does_not_re_add_an_existing_missing_label() -> None:
    plan = gate.decide(
        _event("labeled", "x", labels=["devin-ready", "premises-missing"], label="devin-ready")
    )
    assert plan.remove == ["devin-ready"] and plan.add == []


# -- apply / main with a fake gh ----------------------------------------------


class _FakeGh:
    def __init__(self, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self.rc = rc

    def __call__(self, args: list[str]) -> int:
        self.calls.append(args)
        if "--body-file" in args:
            self.bodies.append(
                Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
            )
        return self.rc


def _no_view(_number: int) -> None:
    """A viewer whose read fails, so main() falls back to the event snapshot and
    no test ever reaches the real GitHub API."""
    return None


class _FailOn(_FakeGh):
    """A gh that fails only the calls whose args start with ``prefix``."""

    def __init__(self, prefix: list[str]) -> None:
        super().__init__()
        self.prefix = prefix

    def __call__(self, args: list[str]) -> int:
        super().__call__(args)
        return 1 if args[: len(self.prefix)] == self.prefix else 0


def test_apply_removes_devin_ready_first_and_on_its_own() -> None:
    gh = _FakeGh()
    plan = gate.Plan(add=["premises-missing"], remove=["devin-ready"], comment="(aside)\n\nhi\n")
    assert gate.apply(plan, 9, gh) == 0
    assert gh.calls[0] == ["issue", "edit", "9", "--remove-label", "devin-ready"]
    assert gh.calls[1][:3] == ["label", "create", "premises-missing"]
    assert "--force" not in gh.calls[1]
    assert gh.calls[2] == ["issue", "edit", "9", "--add-label", "premises-missing"]
    assert gh.calls[3][:3] == ["issue", "comment", "9"]
    assert gh.bodies == ["(aside)\n\nhi\n"]


def test_a_failed_label_create_never_blocks_the_rest() -> None:
    gh = _FailOn(["label", "create"])
    plan = gate.Plan(add=["premises-missing"], remove=["devin-ready"], comment="c")
    assert gate.apply(plan, 9, gh) == 0  # create fails when the label exists: ignored
    assert [c[:2] for c in gh.calls] == [
        ["issue", "edit"],
        ["label", "create"],
        ["issue", "edit"],
        ["issue", "comment"],
    ]


def test_every_step_is_attempted_and_the_worst_exit_is_returned() -> None:
    gh = _FailOn(["issue", "edit", "9", "--remove-label", "devin-ready"])
    plan = gate.Plan(add=["premises-missing"], remove=["devin-ready"], comment="c")
    assert gate.apply(plan, 9, gh) == 1
    assert len(gh.calls) == 4


def test_a_failure_in_the_last_step_is_still_reported() -> None:
    gh = _FailOn(["issue", "comment"])
    plan = gate.Plan(add=["premises-missing"], remove=["devin-ready"], comment="c")
    assert gate.apply(plan, 9, gh) == 1


def test_main_reads_the_event_file_and_makes_no_call_when_nothing_changes(tmp_path: Path) -> None:
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(_event("opened", _COMPLETE)))
    gh = _FakeGh()
    assert gate.main([str(ev)], gh, _no_view) == 0
    assert gh.calls == []


def test_main_carries_out_the_guard(tmp_path: Path) -> None:
    ev = tmp_path / "event.json"
    ev.write_text(
        json.dumps(_event("labeled", "bare", labels=["devin-ready"], label="devin-ready"))
    )
    gh = _FakeGh()
    assert gate.main([str(ev)], gh, _no_view) == 0
    assert gh.calls[0] == ["issue", "edit", "7", "--remove-label", "devin-ready"]
    assert gh.bodies and gh.bodies[0].startswith("(aside)")


def test_main_acts_on_the_issue_as_it_is_now(tmp_path: Path) -> None:
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(_event("edited", "old bare body", labels=[])))
    gh = _FakeGh()
    current = {"body": _COMPLETE, "labels": [{"name": "premises-missing"}], "createdAt": _NEW}
    assert gate.main([str(ev)], gh, lambda n: current) == 0
    assert gh.calls == [["issue", "edit", "7", "--remove-label", "premises-missing"]]


def test_live_falls_back_to_the_snapshot_when_the_read_fails() -> None:
    ev = _event("edited", "snapshot body")
    assert gate.live(ev, _no_view) == ev


def test_main_without_an_event_file_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert gate.main([], _FakeGh(), _no_view) == 2


def test_another_label_event_does_nothing_even_with_devin_ready_present() -> None:
    plan = gate.decide(_event("labeled", "bare", labels=["devin-ready"], label="needs-triage"))
    assert not plan.add and not plan.remove and not plan.comment


def test_the_guard_clears_a_stale_missing_label_on_a_complete_issue() -> None:
    plan = gate.decide(
        _event(
            "labeled", _COMPLETE, labels=["devin-ready", "premises-missing"], label="devin-ready"
        )
    )
    assert plan.remove == ["premises-missing"] and not plan.comment


def test_a_hostile_body_never_reaches_argv() -> None:
    body = "$(rm -rf /); `id` --remove-label everything\n## Premise checks\n1. a"
    plan = gate.decide(_event("labeled", body, labels=["devin-ready"], label="devin-ready"))
    gh = _FakeGh()
    gate.apply(plan, 9, gh)
    assert all("rm -rf" not in arg for call in gh.calls for arg in call)


# -- the bug-report form exemption (owner ruling 2026-10-28) -------------------


def _bug_form_body(drop: str | None = None) -> str:
    headings = gate._bug_form_headings()
    return "".join(f"{h}\n\nsomething\n\n" for h in headings if h != drop)


def test_the_bug_form_headings_are_read_from_the_form_itself() -> None:
    headings = gate._bug_form_headings()
    assert "### Description" in headings and "### Steps to reproduce" in headings


def test_a_bug_form_report_is_never_labelled() -> None:
    assert gate.decide(_event("opened", _bug_form_body())).add == []


def test_a_body_missing_one_form_heading_is_not_a_form_report() -> None:
    body = _bug_form_body(drop="### Steps to reproduce")
    assert gate.decide(_event("opened", body)).add == ["premises-missing"]


def test_an_unreadable_form_exempts_nothing(tmp_path: Path) -> None:
    assert gate._bug_form_headings(tmp_path / "missing.yml") == []
    assert not gate.is_bug_form(_bug_form_body(), headings=[])


def test_the_devin_guard_still_covers_a_bug_form_report() -> None:
    plan = gate.decide(
        _event("labeled", _bug_form_body(), labels=["devin-ready"], label="devin-ready")
    )
    assert plan.remove == ["devin-ready"]


# -- bug reports answer the verification questions (owner ruling 2026-10-28) --

_VERIFIED = """The health page errors.

## Verification
1. Reproduced on a fresh main (abc123) by loading the health page.
2. GET /health returns 500 with `KeyError: 'x'` (trace below).
3. Clean config, current install, valid credentials; not a typo.
4. No open issue matches; main has no fix.
5. Expected 200 with a status body; got 500.
"""


def test_a_bug_report_without_verification_is_labelled() -> None:
    assert gate.decide(_event("opened", "It errors.", labels=["bug"])).add == ["premises-missing"]


def test_a_verified_bug_report_is_not_labelled() -> None:
    plan = gate.decide(_event("opened", _VERIFIED, labels=["bug"]))
    assert not plan.add and not plan.remove


def test_a_bug_report_cannot_pass_on_premise_answers_alone() -> None:
    assert gate.decide(_event("opened", _COMPLETE, labels=["bug"])).add == ["premises-missing"]


def test_a_work_issue_cannot_pass_on_verification_alone() -> None:
    assert gate.decide(_event("opened", _VERIFIED)).add == ["premises-missing"]


def test_verifying_a_bug_report_clears_the_label() -> None:
    plan = gate.decide(_event("edited", _VERIFIED, labels=["bug", "premises-missing"]))
    assert plan.remove == ["premises-missing"]


def test_one_empty_verification_answer_is_enough_to_label() -> None:
    body = _VERIFIED.replace("4. No open issue matches; main has no fix.", "4. TBD")
    assert gate.decide(_event("opened", body, labels=["bug"])).add == ["premises-missing"]


def test_devin_ready_on_a_verified_bug_still_needs_the_six_premise_answers() -> None:
    plan = gate.decide(
        _event("labeled", _VERIFIED, labels=["bug", "devin-ready"], label="devin-ready")
    )
    assert plan.remove == ["devin-ready"] and "6 of 6" in plan.comment


# -- the workflow file itself ---------------------------------------------------

_ROOT = _SCRIPT.parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "premise-gate.yml"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _local_imports(path: Path, seen: set[Path]) -> set[Path]:
    """Repo files ``path`` needs at run time: itself plus every local module it
    imports, transitively (resolved against its own directory and scripts/hooks)."""
    if path in seen:
        return seen
    seen.add(path)
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            for base in (path.parent, _ROOT / "scripts" / "hooks"):
                candidate = base / (name.split(".")[0] + ".py")
                if candidate.exists():
                    _local_imports(candidate, seen)
    return seen


def test_each_job_checks_out_every_file_the_script_needs() -> None:
    needed = {str(p.relative_to(_ROOT)) for p in _local_imports(_SCRIPT, set())}
    needed.add(".github/ISSUE_TEMPLATE/bug_report.yml")
    for name, job in _workflow()["jobs"].items():
        checkout = next(
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
        )
        listed = set(checkout["with"]["sparse-checkout"].split())
        assert needed <= listed, (name, sorted(needed - listed))
        assert checkout["with"].get("persist-credentials") is False, name


def test_the_trigger_types() -> None:
    doc = _workflow()
    on = doc.get("on") or doc.get(True)
    assert sorted(on["issues"]["types"]) == ["edited", "labeled", "opened", "reopened", "unlabeled"]


def test_no_event_text_reaches_a_shell() -> None:
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            assert "github.event" not in str(step.get("run", ""))
            text = json.dumps(step.get("env") or {})
            assert "github.event.issue" not in text and "github.event.comment" not in text


def test_the_guard_never_queues_and_the_label_job_is_serialized() -> None:
    doc = _workflow()
    jobs = doc["jobs"]
    assert "concurrency" not in jobs["guard"] and "concurrency" not in doc
    assert "devin-ready" in jobs["guard"]["if"] and "'labeled'" in jobs["guard"]["if"]
    assert jobs["label"]["concurrency"]["cancel-in-progress"] is False
    assert "devin-ready" not in jobs["label"]["if"]


# -- second review: one meaning for premises-missing, bug relabels, hardening ---


def test_the_guard_keeps_the_label_when_a_bug_lacks_its_verification() -> None:
    plan = gate.decide(
        _event(
            "labeled",
            _COMPLETE,
            labels=["bug", "premises-missing", "devin-ready"],
            label="devin-ready",
        )
    )
    assert plan.remove == [] and plan.add == [] and not plan.comment


def test_the_guard_on_a_verified_bug_without_the_six_removes_devin_and_the_label() -> None:
    plan = gate.decide(
        _event(
            "labeled",
            _VERIFIED,
            labels=["bug", "premises-missing", "devin-ready"],
            label="devin-ready",
        )
    )
    assert plan.remove == ["devin-ready", "premises-missing"] and plan.comment


def test_adding_the_bug_label_rechecks_against_verification() -> None:
    plan = gate.decide(_event("labeled", _COMPLETE, labels=["bug"], label="bug"))
    assert plan.add == ["premises-missing"]


def test_removing_the_bug_label_rechecks_against_the_six() -> None:
    plan = gate.decide(_event("unlabeled", _VERIFIED, labels=["premises-missing"], label="bug"))
    assert not plan.add and not plan.remove  # still missing (the six), label already on


def test_removing_the_bug_label_clears_it_when_the_six_are_there() -> None:
    plan = gate.decide(_event("unlabeled", _COMPLETE, labels=["premises-missing"], label="bug"))
    assert plan.remove == ["premises-missing"]


def test_labels_given_as_plain_strings_are_read() -> None:
    ev = _event("opened", _VERIFIED)
    ev["issue"]["labels"] = ["bug"]
    assert not gate.decide(ev).add


def test_a_failed_devin_removal_says_so_in_the_comment() -> None:
    gh = _FailOn(["issue", "edit", "9", "--remove-label", "devin-ready"])
    plan = gate.decide(
        _event("labeled", "bare", labels=["devin-ready"], label="devin-ready", number=9)
    )
    assert gate.apply(plan, 9, gh) == 1
    assert "could NOT be removed" in gh.bodies[0] and "was removed" not in gh.bodies[0]


def test_a_signal_killed_step_counts_as_a_failure() -> None:
    def killed(args: list[str]) -> int:
        return -9 if args[:2] == ["issue", "edit"] and "devin-ready" in args else 0

    assert gate.apply(gate.Plan(remove=["devin-ready"]), 9, killed) == 9


def test_adding_a_bug_form_field_does_not_unexempt_older_reports() -> None:
    older = "### Description\n\nx\n\n### Steps to reproduce\n\ny\n\n### Environment\n\nz\n"
    assert gate.decide(_event("opened", older, labels=["bug"])).add == []


def test_the_label_queue_is_per_issue_and_both_parsers_are_installed() -> None:
    jobs = _workflow()["jobs"]
    assert "github.event.issue.number" in jobs["label"]["concurrency"]["group"]
    for name, job in jobs.items():
        install = " ".join(str(st.get("run", "")) for st in job["steps"])
        assert "pyyaml" in install and "markdown-it-py" in install, name


def test_the_label_job_runs_on_bug_relabels_but_never_on_devin_ready() -> None:
    cond = _workflow()["jobs"]["label"]["if"]
    assert "'bug'" in cond and "unlabeled" in cond and "devin-ready" not in cond
