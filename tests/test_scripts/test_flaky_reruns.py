"""scripts/ci/flaky_reruns.py and scripts/ci/file_flaky_issues.py (#5024).

Both run as real subprocesses, the way the workflow runs them. The filer's ``gh``
is a fake on ``PATH`` that logs every argv it receives, so the tests read the
exact calls a main push would make.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
REPORT = REPO / "scripts" / "ci" / "flaky_reruns.py"
FILER = REPO / "scripts" / "ci" / "file_flaky_issues.py"


def _ledger(tmp_path: Path, **over) -> Path:
    data = {
        "collected": 3,
        "with_outcome": 3,
        "missing": 0,
        "missing_ids": [],
        "rerun_passed": [],
        "rerun_failed": [],
    }
    data.update(over)
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(data))
    return path


def _report(*args: str):
    return subprocess.run(
        [sys.executable, str(REPORT), *args], capture_output=True, text=True, timeout=60
    )


def test_a_retried_test_is_listed_and_the_run_passes(tmp_path):
    out = _report(str(_ledger(tmp_path, rerun_passed=["tests/test_a.py::test_x"])))
    assert out.returncode == 0, out.stdout
    assert "1 test(s) passed only on their retry" in out.stdout
    assert "- `tests/test_a.py::test_x`" in out.stdout


def test_a_test_with_no_outcome_fails_and_is_named(tmp_path):
    led = _ledger(
        tmp_path, missing=1, with_outcome=2, missing_ids=["tests/test_a.py::test_dropped"]
    )
    out = _report(str(led))
    assert out.returncode == 1, out.stdout
    assert "1 of 3 collected tests reached no outcome" in out.stdout
    assert "- `tests/test_a.py::test_dropped`" in out.stdout


def test_a_missing_ledger_fails_and_a_malformed_one_refuses(tmp_path):
    assert _report(str(tmp_path / "absent.json")).returncode == 1
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"collected": "3"}))
    assert _report(str(bad)).returncode == 2
    bad.write_text("[]")
    assert _report(str(bad)).returncode == 2


def test_markdown_in_an_id_cannot_break_the_summary(tmp_path):
    out = _report(str(_ledger(tmp_path, rerun_passed=["tests/test_a.py::t[`x`|y]"])))
    assert "- `tests/test_a.py::t['x'\\|y]`" in out.stdout


# The workflow's "List the change's test files" step, executed for real against
# throwaway git repositories, so the bash that feeds --rerun-exclude-path is
# tested and not only its text.
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("x = 1\n")
    (repo / "tests" / "gone.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_new.py").write_text("x = 2\n")
    (repo / "tests" / "test_old.py").write_text("x = 3\n")
    (repo / "notes.md").write_text("prose\n")
    _git(repo, "rm", "-q", "tests/gone.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return repo, base


def _list_step(
    repo: Path, tmp_path: Path, event: str, before: str = ""
) -> tuple[subprocess.CompletedProcess, Path]:
    steps = {s.get("name"): s for s in _workflow()["jobs"]["test"]["steps"]}
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        **os.environ,
        "EVENT_NAME": event,
        "PUSH_BEFORE_SHA": before,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
    }
    run = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", steps["List the change's test files"]["run"]],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return run, tmp_path / "rerun-exclude.txt"


def test_a_push_lists_the_changed_python_files_and_skips_deleted_ones(tmp_path):
    repo, base = _repo(tmp_path)
    run, listing = _list_step(repo, tmp_path, "push", base)
    assert run.returncode == 0, run.stderr
    assert sorted(listing.read_bytes().split(b"\0")[:-1]) == [
        b"tests/test_new.py",
        b"tests/test_old.py",
    ]


def test_an_unreadable_push_tip_stops_the_job(tmp_path):
    repo, _ = _repo(tmp_path)
    for before in ("", "0" * 40, "f" * 40):
        run, _listing = _list_step(repo, tmp_path, "push", before)
        assert run.returncode == 2, (before, run.stdout, run.stderr)


def test_a_pr_checkout_that_is_not_a_merge_commit_stops_the_job(tmp_path):
    repo, _ = _repo(tmp_path)
    run, _listing = _list_step(repo, tmp_path, "pull_request")
    assert run.returncode == 2
    assert "expected a 2-parent merge commit" in run.stdout


def test_a_pr_merge_commit_lists_against_its_first_parent(tmp_path):
    repo, base = _repo(tmp_path)
    change = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "merge", base)
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", change)
    run, listing = _list_step(repo, tmp_path, "pull_request")
    assert run.returncode == 0, run.stderr
    assert sorted(listing.read_bytes().split(b"\0")[:-1]) == [
        b"tests/test_new.py",
        b"tests/test_old.py",
    ]


def test_a_changed_conftest_excludes_its_whole_directory(tmp_path):
    repo, _ = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "sub dir ü").mkdir()
    (repo / "tests" / "sub dir ü" / "conftest.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "conftest")
    run, listing = _list_step(repo, tmp_path, "push", base)
    assert run.returncode == 0, run.stderr
    assert sorted(listing.read_bytes().split(b"\0")[:-1]) == [
        "tests/sub dir ü".encode(),
        "tests/sub dir ü/conftest.py".encode(),
    ]


def test_a_dispatch_excludes_nothing(tmp_path):
    repo, _ = _repo(tmp_path)
    run, listing = _list_step(repo, tmp_path, "workflow_dispatch")
    assert run.returncode == 0, run.stderr
    assert listing.read_bytes() == b""
    assert f"list={listing}" in (tmp_path / "github_output").read_text()


def _fake_gh(
    tmp_path: Path, issues: list[dict], fail: str = "", label_exists: bool = True
) -> tuple[dict, Path]:
    log = tmp_path / "gh.log"
    script = tmp_path / "bin" / "gh"
    script.parent.mkdir()
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'api' and sys.argv[2].endswith('/labels/flaky'):\n"
        f"    sys.exit(0 if {label_exists!r} else 1)\n"
        f"elif sys.argv[1:3] == ['label', 'create'] and {fail!r} == 'label':\n"
        "    sys.stderr.write('no permission'); sys.exit(1)\n"
        "elif sys.argv[1:3] == ['issue', 'list']:\n"
        f"    print(json.dumps({issues!r}))\n"
        f"elif {fail!r} and sys.argv[2] == {fail!r}:\n"
        "    sys.stderr.write('boom'); sys.exit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{script.parent}{os.pathsep}{os.environ['PATH']}"}
    return env, log


def _file(tmp_path: Path, ledger: Path, env: dict, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            str(FILER),
            str(ledger),
            "--repo",
            "o/r",
            "--run-url",
            "https://example.invalid/run/1",
            "--sha",
            "a" * 40,
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _all_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _calls(log: Path) -> list[list[str]]:
    """The issue calls only; the label probe is asserted by its own tests."""
    return [c for c in _all_calls(log) if c[0] == "issue"]


def test_a_missing_label_is_created_once_before_filing(tmp_path):
    env, log = _fake_gh(tmp_path, [], label_exists=False)
    out = _file(tmp_path, _ledger(tmp_path, rerun_passed=["tests/test_a.py::t"]), env)
    assert out.returncode == 0, out.stdout + out.stderr
    calls = _all_calls(log)
    assert calls[0][:2] == ["api", "repos/o/r/labels/flaky"]
    assert calls[1][:3] == ["label", "create", "flaky"] and "--force" not in calls[1]
    assert calls[-1][:2] == ["issue", "create"]


def test_an_existing_label_is_never_rewritten(tmp_path):
    env, log = _fake_gh(tmp_path, [])
    _file(tmp_path, _ledger(tmp_path, rerun_passed=["tests/test_a.py::t"]), env)
    assert not [c for c in _all_calls(log) if c[0] == "label"]


def test_a_label_that_cannot_be_created_refuses_before_any_issue_call(tmp_path):
    env, log = _fake_gh(tmp_path, [], fail="label", label_exists=False)
    out = _file(tmp_path, _ledger(tmp_path, rerun_passed=["tests/test_a.py::t"]), env)
    assert out.returncode == 2
    assert "could not be created" in out.stdout
    assert _calls(log) == []


def test_a_known_flake_is_commented_and_a_new_one_opened(tmp_path):
    env, log = _fake_gh(tmp_path, [{"number": 7, "title": "Flaky test: tests/test_a.py::test_x"}])
    led = _ledger(tmp_path, rerun_passed=["tests/test_a.py::test_x", "tests/test_b.py::test_y"])
    out = _file(tmp_path, led, env)
    assert out.returncode == 0, out.stdout + out.stderr
    calls = _calls(log)
    assert calls[0][:2] == ["issue", "list"] and "--label" in calls[0] and "flaky" in calls[0]
    assert calls[1][:3] == ["issue", "comment", "7"]
    assert calls[2][:2] == ["issue", "create"]
    assert calls[2][calls[2].index("--title") + 1] == "Flaky test: tests/test_b.py::test_y"
    assert calls[2][calls[2].index("--label") + 1] == "flaky"


def test_a_dry_run_lists_only(tmp_path):
    env, log = _fake_gh(tmp_path, [])
    out = _file(tmp_path, _ledger(tmp_path, rerun_passed=["tests/test_a.py::t"]), env, "--dry-run")
    assert out.returncode == 0
    assert [c[:2] for c in _calls(log)] == [["issue", "list"]]
    assert "dry run: would open" in out.stdout


def test_an_id_that_is_not_a_node_id_is_skipped(tmp_path):
    env, log = _fake_gh(tmp_path, [])
    led = _ledger(tmp_path, rerun_passed=["$(touch pwned)", "tests/x.py::t\nInjected: line"])
    out = _file(tmp_path, led, env)
    assert out.returncode == 0
    assert [c[:2] for c in _calls(log)] == [["issue", "list"]]
    assert out.stdout.count("::warning::skipped") == 2


def test_a_long_id_gets_a_stable_hashed_title_within_the_limit(tmp_path):
    env, log = _fake_gh(tmp_path, [])
    long_id = "tests/test_a.py::test_x[" + "p" * 300 + "]"
    _file(tmp_path, _ledger(tmp_path, rerun_passed=[long_id]), env)
    create = _calls(log)[1]
    title = create[create.index("--title") + 1]
    assert len(title) <= 256 and title.startswith("Flaky test: tests/test_a.py:: <id omitted")
    assert long_id in create[create.index("--body") + 1]


def test_a_saturated_listing_refuses_rather_than_risking_a_twin(tmp_path):
    env, log = _fake_gh(tmp_path, [{"number": i, "title": f"t{i}"} for i in range(1000)])
    out = _file(tmp_path, _ledger(tmp_path, rerun_passed=["tests/test_a.py::t"]), env)
    assert out.returncode == 2
    assert len(_calls(log)) == 1


def test_a_failed_write_is_reported_and_exits_1(tmp_path):
    env, _ = _fake_gh(tmp_path, [], fail="create")
    out = _file(tmp_path, _ledger(tmp_path, rerun_passed=["tests/test_a.py::t"]), env)
    assert out.returncode == 1
    assert "::warning::could not open" in out.stdout


def _workflow() -> dict:
    return yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())


def test_the_test_job_retries_once_and_arms_the_ledger_on_a_fixed_path():
    steps = {s.get("name"): s for s in _workflow()["jobs"]["test"]["steps"]}
    run = steps["Run tests"]
    assert "--reruns=1" in shlex.split(
        run["run"], comments=True
    )  # the flag, not a comment naming it
    # Never derived from census_prepare, which is advisory and may fail.
    assert run["env"]["GENESIS_RERUN_LEDGER"] == "${{ runner.temp }}/rerun-ledger.json"
    assert run["env"]["RERUN_EXCLUDE_LIST"] == "${{ steps.change_files.outputs.list }}"
    assert '"${excludes[@]}"' in run["run"] and "--rerun-exclude-path=$path" in run["run"]
    names = list(steps)
    assert names.index("List the change's test files") < names.index("Run tests")
    assert "if" not in steps["List the change's test files"]  # its failure must stop the tests
    account = steps["Account for retried tests"]
    assert account["env"]["GENESIS_RERUN_LEDGER"] == run["env"]["GENESIS_RERUN_LEDGER"]
    assert "scripts/ci/flaky_reruns.py" in account["run"]
    assert " ".join(account["if"].split()) == (
        "${{ !cancelled() && (steps.tests.outcome == 'success' || steps.tests.outcome == 'failure') }}"
    )
    assert "github.run_attempt" in steps["Preserve the retry ledger"]["with"]["name"]


def test_only_the_report_job_can_write_issues_and_it_can_be_switched_off():
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert "permissions" not in wf["jobs"]["test"]
    report = wf["jobs"]["flaky-report"]
    assert report["permissions"] == {"contents": "read", "issues": "write"}
    assert report["needs"] == "test"
    assert report["continue-on-error"] is True  # a filing failure never turns main red
    guard = " ".join(report["if"].split())
    assert "!cancelled()" in guard and "always()" not in guard
    assert "vars.FLAKY_AUTOFILE != 'off'" in guard
    assert "github.ref == 'refs/heads/main'" in guard and "pull_request" not in guard
    download = next(s for s in report["steps"] if s.get("id") == "ledger")
    assert "github.run_attempt" in download["with"]["name"]


def test_an_id_with_a_newline_cannot_forge_a_workflow_command(tmp_path):
    hostile = "tests/a.py::t\n::error file=README.md,line=1::pwned\n::add-mask::x"
    out = _report(str(_ledger(tmp_path, rerun_passed=[hostile])))
    assert not [line for line in out.stdout.splitlines() if line.startswith("::")], out.stdout
    assert "\\n::error" in out.stdout  # escaped, still visible


def test_a_bidi_or_format_character_in_an_id_is_refused(tmp_path):
    env, log = _fake_gh(tmp_path, [])
    led = _ledger(tmp_path, rerun_passed=["tests/a.py::t\u202eevil", "tests/a.py::t\u200dx"])
    out = _file(tmp_path, led, env)
    assert out.returncode == 0
    assert _calls(log) == [
        [
            "issue",
            "list",
            "--repo",
            "o/r",
            "--label",
            "flaky",
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title",
        ]
    ]


def test_one_run_files_at_most_the_cap_and_names_the_rest(tmp_path):
    env, log = _fake_gh(tmp_path, [])
    ids = [f"tests/test_a.py::t{i}" for i in range(25)]
    out = _file(tmp_path, _ledger(tmp_path, rerun_passed=ids), env)
    assert out.returncode == 0
    assert len([c for c in _calls(log) if c[:2] == ["issue", "create"]]) == 20
    assert "5 not filed this run" in out.stdout
    assert "not filed this run: 'tests/test_a.py::t24'" in out.stdout


def test_a_dispatch_off_main_is_always_a_dry_run_and_the_checkout_keeps_no_token():
    report = _workflow()["jobs"]["flaky-report"]
    file_step = next(s for s in report["steps"] if s.get("name") == "File the retried tests")
    assert " ".join(file_step["env"]["DRY_RUN"].split()) == (
        "${{ github.event_name == 'workflow_dispatch' && "
        "(inputs.flaky_dry_run != false || github.ref != 'refs/heads/main') }}"
    )
    checkout = next(
        s for s in report["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["persist-credentials"] is False
