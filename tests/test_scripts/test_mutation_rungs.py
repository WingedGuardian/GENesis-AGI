"""The defect corpus's report: which rung of a gate's tests notices a deliberate
defect. Each row of the plan's semantics table is a test here, on real pytest
children where the outcome comes from a run."""

from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent.parent
_CI = _REPO / "scripts" / "ci"
_SWEEP = _CI / "mutation_sweep.py"
# The harness imports its siblings by bare name, as it does when run as a script.
sys.path.insert(0, str(_CI))
mr = importlib.import_module("mutation_rungs")

_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=_ENV,
    )


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c", "--allow-empty")


@pytest.fixture
def project(tmp_path):
    """Two rungs over one guard: `pins` notices a broken guard, `blind` does not,
    and `elsewhere` never imports it."""
    (tmp_path / "guard.py").write_text(
        textwrap.dedent(
            """
            def is_allowed(name):
                if name.startswith("danger"):
                    return False
                return True
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_pins.py").write_text(
        'import guard\ndef test_blocks():\n    assert guard.is_allowed("dangerous") is False\n',
        encoding="utf-8",
    )
    (tmp_path / "test_blind.py").write_text(
        'import guard\ndef test_allows():\n    assert guard.is_allowed("ordinary") is True\n',
        encoding="utf-8",
    )
    (tmp_path / "test_elsewhere.py").write_text(
        "def test_nothing_to_do_with_it():\n    assert True\n", encoding="utf-8"
    )
    _git(tmp_path, "init", "-q")
    _commit_all(tmp_path)
    return tmp_path


def _defect(**kw):
    base = {
        "label": "guard removed",
        "why": "the guard must refuse a dangerous name",
        "aim": "pins",
        "path": "guard.py",
        "validator": "python",
        "anchor": 'if name.startswith("danger"):',
        "replacement": "if False:",
    }
    base.update(kw)
    return base


def _doc(*defects, rungs=None):
    return {
        "rungs": rungs or {"pins": ["test_pins.py"], "blind": ["test_blind.py"]},
        "cases": list(defects),
    }


def _run(project, doc, *flags, env=None):
    manifest = project / "defects.json"
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    _commit_all(project)
    return subprocess.run(
        [sys.executable, str(_SWEEP), str(manifest), "--repo", str(project), *flags],
        capture_output=True,
        text=True,
        env={**_ENV, **(env or {})},
        timeout=300,
    )


def _report(project, *defects, rungs=None, env=None):
    return _run(project, _doc(*defects, rungs=rungs), "--rung-report", env=env)


# ── The manifest ─────────────────────────────────────────────────────────────


def test_a_defect_expands_to_one_case_per_rung(project):
    cases, report = mr.rung_cases(_doc(_defect()), project)
    assert [c.label for c in cases] == ["guard removed [pins]", "guard removed [blind]"]
    assert [c.test for c in cases] == ["test_pins.py", "test_blind.py"]
    assert report is not None


def test_a_rung_of_several_files_passes_the_rest_as_arguments(project):
    cases, _ = mr.rung_cases(
        _doc(_defect(), rungs={"pins": ["test_pins.py", "test_blind.py"]}), project
    )
    assert cases[0].test == "test_pins.py"
    assert cases[0].pytest_args == ("test_blind.py",)


def test_a_case_may_narrow_a_rung_to_a_selector_and_skip_the_others(project):
    defect = _defect(tests={"pins": "test_pins.py::test_blocks"})
    cases, _ = mr.rung_cases(_doc(defect), project)
    assert [(c.label, c.test) for c in cases] == [
        ("guard removed [pins]", "test_pins.py::test_blocks")
    ]


@pytest.mark.parametrize(
    "doc,needle",
    [
        ({"cases": [_defect()]}, "rungs"),
        (_doc(), "no cases"),
        (_doc(_defect(), rungs={"pins": []}), "pins"),
        (_doc(_defect(aim="nonsense")), "aim"),
        (_doc({k: v for k, v in _defect().items() if k != "aim"}), "aim"),
        (_doc(_defect(), _defect()), "guard removed"),
        (_doc(_defect(surprise=1)), "surprise"),
        (_doc(_defect(test="test_pins.py")), "test"),
        (_doc(_defect(tests={"nonsense": "x.py"})), "nonsense"),
        (_doc(_defect(aim="blind", tests={"pins": "test_pins.py"})), "aim"),
        (_doc({k: v for k, v in _defect().items() if k != "label"}), "label"),
        (_doc(_defect(label=5)), "label"),
        (_doc(_defect(label="  ")), "label"),
        (_doc(_defect(pytest_args="-x")), "pytest_args"),
        (_doc(_defect(), rungs={"pins": ["test_pins.py", "/abs/test_x.py"]}), "inside"),
        (_doc(_defect(), rungs={"pins": ["test_pins.py", "../test_x.py"]}), "inside"),
        (_doc(_defect(), rungs={"pins": ["test_pins.py", "-x"]}), "inside"),
    ],
    ids=[
        "no-rungs",
        "empty-cases-read-as-clean",
        "empty-rung",
        "unknown-aim",
        "missing-aim",
        "duplicate-label",
        "unknown-key",
        "a-test-of-its-own",
        "narrowed-to-an-unknown-rung",
        "aim-outside-the-narrowing",
        "no-label",
        "label-not-a-string",
        "blank-label",
        "args-a-bare-string",
        "later-rung-file-absolute",
        "later-rung-file-climbs",
        "later-rung-file-an-option",
    ],
)
def test_a_malformed_manifest_is_refused_naming_the_field(project, doc, needle):
    with pytest.raises(ValueError, match=needle):
        mr.rung_cases(doc, project)


# ── The report, one row of the table per test ────────────────────────────────
#
# Results are built by hand here: each real run costs seconds, and this job's
# suite is already long. The real runs are in the next section.


def _line(out: str, label: str = "guard removed") -> str:
    return next(line for line in out.splitlines() if line.startswith(label) and "(aim:" in line)


def _judged(project, outcomes, *defects, rungs=None):
    """A report given each (defect label, rung)'s result. A pair left out never ran."""
    cases, report = mr.rung_cases(_doc(*(defects or [_defect()]), rungs=rungs), project)
    for case in cases:
        owner = report.owners[id(case)]
        if owner in outcomes:
            report.add(outcomes[owner](case))
    return report


def _bit(case):
    return mr.Result(
        case,
        mr.BIT,
        "",
        "FAILED captured.py::in_output - a test printed this\n"
        "=== short test summary info ===\n"
        "FAILED test_pins.py::test_blocks - assert\n"
        "1 failed in 0.1s\n",
    )


def _survived(case):
    return mr.Result(case, "SURVIVED")


def _never_opened(case):
    return mr.Result(case, mr.ABORTED, "never opened", never_opened=True)


def _aborted(case):
    return mr.Result(case, mr.ABORTED, "timed out after 8s")


G = "guard removed"


def test_a_catch_on_the_aimed_rung_names_it_and_its_test(project):
    report = _judged(project, {(G, "pins"): _bit, (G, "blind"): _survived})
    line = _line(report.table())
    assert "caught_by: pins" in line and "off aim" not in line and "blind" not in line
    assert "pins: test_pins.py::test_blocks" in report.table()
    assert "captured.py" not in report.table(), "only pytest's own summary names a catch"
    assert report.exit_code() == 0


def test_a_catch_elsewhere_only_is_marked_off_aim(project):
    report = _judged(project, {(G, "pins"): _bit, (G, "blind"): _survived}, _defect(aim="blind"))
    line = _line(report.table())
    assert "caught_by: pins" in line and "off aim" in line
    assert report.exit_code() == 0


def test_an_escape_is_reported_and_exits_zero(project):
    report = _judged(project, {(G, "pins"): _survived, (G, "blind"): _survived})
    assert "ESCAPED" in _line(report.table())
    assert "PARTIAL" not in report.table()
    assert report.exit_code() == 0


def test_a_rung_that_never_opened_the_target_says_so_and_exits_zero(project):
    report = _judged(project, {(G, "pins"): _bit, (G, "blind"): _never_opened})
    line = _line(report.table())
    assert "caught_by: pins" in line and "never opened: blind" in line
    assert report.exit_code() == 0


def test_nothing_but_never_opened_is_an_escape(project):
    report = _judged(project, {(G, "pins"): _never_opened, (G, "blind"): _never_opened})
    line = _line(report.table())
    assert "ESCAPED" in line and "never opened: pins, blind" in line
    assert report.exit_code() == 0


def test_an_abort_with_nothing_caught_is_undetermined_not_an_escape(project):
    report = _judged(project, {(G, "pins"): _survived, (G, "blind"): _aborted})
    line = _line(report.table())
    assert "undetermined" in line and "ESCAPED" not in line and "aborted: blind" in line
    assert "blind: timed out after 8s" in report.table()
    assert report.exit_code() == 1


def test_an_abort_beside_a_catch_keeps_the_catch_and_still_exits_one(project):
    report = _judged(project, {(G, "pins"): _bit, (G, "blind"): _aborted})
    line = _line(report.table())
    assert "caught_by: pins" in line and "aborted: blind" in line
    assert report.exit_code() == 1


def test_a_run_cut_short_says_partial_and_claims_no_escape(project):
    second = _defect(label="second", anchor="return False", replacement="return True")
    report = _judged(project, {(G, "pins"): _survived, (G, "blind"): _survived}, _defect(), second)
    text = report.table()
    assert "PARTIAL" in text
    assert "not finished" in _line(text, "second")
    assert "ESCAPED" in _line(text), "this defect's own rungs all ran"
    assert "`: caught nothing" not in text


def test_a_defect_with_a_rung_still_to_run_is_not_an_escape(project):
    report = _judged(project, {(G, "pins"): _survived})
    assert "not finished" in _line(report.table())
    assert "ESCAPED" not in report.table()


def test_zero_catch_flag_names_the_idle_rung(project):
    second = _defect(label="second", anchor="return False", replacement="return True")
    outcomes = {
        (G, "pins"): _bit,
        (G, "blind"): _survived,
        ("second", "pins"): _bit,
        ("second", "blind"): _survived,
    }
    text = _judged(project, outcomes, _defect(), second).table()
    assert "rung `blind`: caught nothing" in text
    assert "rung `pins`: caught nothing" not in text


def test_a_rung_that_only_aborted_is_not_said_to_have_caught_nothing(project):
    second = _defect(label="second", anchor="return False", replacement="return True")
    outcomes = {
        (G, "pins"): _bit,
        (G, "blind"): _aborted,
        ("second", "pins"): _bit,
        ("second", "blind"): _aborted,
    }
    assert "`: caught nothing" not in _judged(project, outcomes, _defect(), second).table()


def test_a_rung_a_case_narrowed_away_says_not_run(project):
    defect = _defect(tests={"pins": "test_pins.py::test_blocks"})
    report = _judged(project, {(G, "pins"): _bit}, defect)
    line = _line(report.table())
    assert "caught_by: pins" in line and "not run: blind" in line
    assert "PARTIAL" not in report.table()


def test_a_lone_case_raises_no_zero_catch_flag(project):
    report = _judged(project, {(G, "pins"): _bit, (G, "blind"): _survived})
    assert "`: caught nothing" not in report.table()


def test_the_table_reaches_the_job_summary(project, tmp_path_factory, monkeypatch, capsys):
    summary = tmp_path_factory.mktemp("s") / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _judged(project, {(G, "pins"): _bit, (G, "blind"): _survived}).publish()
    assert "caught_by: pins" in summary.read_text(encoding="utf-8")
    assert "caught_by: pins" in capsys.readouterr().out


def test_a_summary_that_cannot_be_written_does_not_lose_the_table(project, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(project / "no" / "such" / "dir" / "s.md"))
    _judged(project, {(G, "pins"): _bit, (G, "blind"): _survived}).publish()
    assert "caught_by: pins" in capsys.readouterr().out


def test_the_report_states_what_opened_cannot_show(project):
    text = _judged(project, {(G, "pins"): _bit, (G, "blind"): _survived}).table()
    assert "Opened is not exercised" in text


# ── Real runs: what the table cannot show by hand ────────────────────────────


def test_a_real_report_end_to_end(project):
    before = {p: p.read_bytes() for p in project.glob("*.py")}
    r = _report(
        project,
        _defect(),
        rungs={
            "pins": ["test_pins.py"],
            "blind": ["test_blind.py"],
            "elsewhere": ["test_elsewhere.py"],
        },
    )
    assert r.returncode == 0, r.stdout + r.stderr
    line = _line(r.stdout)
    assert "caught_by: pins" in line and "never opened: elsewhere" in line
    assert "pins: test_pins.py::test_blocks" in r.stdout
    assert {p: p.read_bytes() for p in project.glob("*.py")} == before


def test_a_real_run_that_times_out_is_undetermined_and_exits_one(project):
    (project / "test_hangs.py").write_text(
        "import time, guard\n"
        "def test_hangs():\n"
        '    if not guard.is_allowed("dangerous"):\n'
        "        return\n"
        "    time.sleep(120)\n",
        encoding="utf-8",
    )
    r = _run(
        project,
        _doc(_defect(aim="hangs"), rungs={"hangs": ["test_hangs.py"]}),
        "--rung-report",
        "--timeout",
        "8",
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "undetermined" in _line(r.stdout)
    assert "timed out" in r.stdout


def test_an_anchor_that_stopped_matching_is_untrusted(project):
    r = _report(project, _defect(anchor="no such line"), rungs={"pins": ["test_pins.py"]})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "undetermined" in _line(r.stdout)
    assert "matched 0x" in r.stdout


def test_a_skip_in_a_rungs_later_file_names_the_whole_rung(project):
    (project / "test_skips.py").write_text(
        "import pytest\n@pytest.mark.skip\ndef test_skipped():\n    pass\n", encoding="utf-8"
    )
    r = _report(project, _defect(aim="sk"), rungs={"sk": ["test_pins.py", "test_skips.py"]})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Stopped at rung `sk`, files: test_pins.py, test_skips.py" in r.stdout


def test_a_skip_in_a_rung_is_refused_naming_it(project):
    (project / "test_skips.py").write_text(
        "import pytest, guard\n"
        "def test_runs():\n"
        '    assert guard.is_allowed("dangerous") is False\n'
        "@pytest.mark.skip\n"
        "def test_skipped():\n"
        "    pass\n",
        encoding="utf-8",
    )
    r = _report(project, _defect(aim="sk"), rungs={"sk": ["test_skips.py"]})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Stopped at rung `sk`, files: test_skips.py" in r.stdout
    assert "nothing skipped or xfailed" in r.stdout


def test_a_red_baseline_ends_non_zero_with_a_partial_table_and_no_escapes(project):
    (project / "test_red.py").write_text("def test_always():\n    assert False\n", encoding="utf-8")
    r = _report(
        project,
        _defect(aim="blind"),
        rungs={"blind": ["test_blind.py"], "red": ["test_red.py"]},
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "PARTIAL" in r.stdout
    assert "ESCAPED" not in r.stdout
    assert "test_red.py" in r.stderr


def test_a_stop_keeps_what_ran_and_claims_no_escape(project):
    """A stop has no basis for ESCAPED or a zero-catch line, and exits as a stop."""
    (project / "test_hangs.py").write_text(
        "import time, guard\n"
        "def test_hangs():\n"
        '    if not guard.is_allowed("dangerous"):\n'
        "        return\n"
        "    time.sleep(120)\n",
        encoding="utf-8",
    )
    manifest = project / "defects.json"
    manifest.write_text(
        json.dumps(
            _doc(_defect(), rungs={"pins": ["test_pins.py"], "hangs": ["test_hangs.py"]}),
        ),
        encoding="utf-8",
    )
    _commit_all(project)
    report = project.parent / "report.txt"
    with report.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, str(_SWEEP), str(manifest), "--repo", str(project), "--rung-report"],
            stdout=out,
            stderr=subprocess.DEVNULL,
            env=_ENV,
            start_new_session=True,
            preexec_fn=lambda: [
                signal.signal(each, signal.SIG_DFL) for each in (signal.SIGTERM, signal.SIGINT)
            ],
        )
        try:
            for _ in range(600):
                if "guard removed [pins]" in report.read_text(errors="replace"):
                    break
                assert proc.poll() is None, report.read_text(errors="replace")
                time.sleep(0.1)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=60) == 128 + signal.SIGTERM
        finally:
            if proc.poll() is None:
                proc.kill()
    text = report.read_text(errors="replace")
    assert "PARTIAL" in text
    assert "caught_by: pins" in text, "what it learned before the stop"
    assert "ESCAPED" not in text
    assert "`: caught nothing" not in text


def test_pass_fail_mode_is_unchanged_by_all_this(project):
    manifest = project / "plain.json"
    plain = {k: v for k, v in _defect().items() if k != "aim"}
    plain["test"] = "test_pins.py"
    manifest.write_text(json.dumps({"cases": [plain]}), encoding="utf-8")
    _commit_all(project)
    r = subprocess.run(
        [sys.executable, str(_SWEEP), str(manifest), "--repo", str(project)],
        capture_output=True,
        text=True,
        env=_ENV,
        timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "caught_by" not in r.stdout
    assert r.stdout.rstrip().endswith("1 bit, 0 SURVIVED, 0 ABORTED")


# ── The anchor check ─────────────────────────────────────────────────────────


def test_anchor_check_passes_on_a_sound_manifest_and_runs_no_tests(project):
    (project / "test_pins.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
    r = _run(project, _doc(_defect()), "--check-anchors")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize(
    "defect,needle",
    [
        (_defect(anchor="no such line"), "matched 0x"),
        (_defect(replacement="if False"), "guard removed"),
        (_defect(path="gone.py"), "gone.py"),
    ],
    ids=["moved-anchor", "mutation-does-not-compile", "target-gone"],
)
def test_anchor_check_fails_naming_the_case(project, defect, needle):
    r = _run(project, _doc(defect), "--check-anchors")
    assert r.returncode == 1, r.stdout + r.stderr
    assert needle in r.stdout


def test_anchor_check_does_not_read_an_argument_as_a_file(project):
    defect = _defect(pytest_args=["-k", "test"])
    r = _run(project, _doc(defect), "--check-anchors")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize(
    "files",
    [["test_renamed.py"], ["test_pins.py", "test_renamed.py"]],
    ids=["first-file", "later-file"],
)
def test_anchor_check_fails_on_a_rung_file_that_is_gone(project, files):
    r = _run(project, _doc(_defect(), rungs={"pins": files}), "--check-anchors")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "test_renamed.py" in r.stdout


def test_anchor_check_fails_on_a_narrowed_selector_whose_file_is_gone(project):
    defect = _defect(tests={"pins": "test_renamed.py::test_blocks"})
    r = _run(project, _doc(defect), "--check-anchors")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "test_renamed.py" in r.stdout


def test_the_two_modes_cannot_be_combined(project):
    r = _run(project, _doc(_defect()), "--check-anchors", "--rung-report")
    assert r.returncode == 2, r.stdout + r.stderr


# ── The real manifest and its jobs ───────────────────────────────────────────


def _manifest() -> dict:
    return json.loads((_REPO / "config" / "gate_defects.json").read_text(encoding="utf-8"))


def _workflow(name: str) -> dict:
    return yaml.safe_load((_REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    return doc.get("on", doc.get(True))


def test_a_change_to_a_cases_target_or_rung_triggers_the_anchor_check():
    """Without this the workflow's path list drifts from the manifest, and a moved
    anchor reaches main unseen."""
    paths = set(_on(_workflow("gate-corpus-anchors.yml"))["pull_request"]["paths"])
    doc = _manifest()
    named = {c["path"] for c in doc["cases"]}
    named |= {f for files in doc["rungs"].values() for f in files}
    named |= {"config/gate_defects.json"}
    assert named <= paths, named - paths


def test_the_full_report_runs_weekly_and_on_its_own_changes_only():
    on = _on(_workflow("gate-corpus.yml"))
    assert on["schedule"]
    paths = set(on["pull_request"]["paths"])
    assert "config/gate_defects.json" in paths and "scripts/ci/mutation_*.py" in paths
    assert "scripts/hooks/git_push_guard.py" not in paths, "the owner ruled against this"


def test_the_jobs_are_bounded_and_read_only():
    for name in ("gate-corpus.yml", "gate-corpus-anchors.yml"):
        doc = _workflow(name)
        assert doc["permissions"] == {"contents": "read"}, name
        assert _on(doc)["pull_request"]["branches"] == ["main"], name
        for job in doc["jobs"].values():
            assert job["timeout-minutes"], name
            checkout = next(s for s in job["steps"] if "checkout" in s.get("uses", ""))
            assert checkout["with"]["persist-credentials"] is False, name


def test_the_full_report_lets_the_sweep_hear_a_cancel():
    steps = next(iter(_workflow("gate-corpus.yml")["jobs"].values()))["steps"]
    run = next(s["run"] for s in steps if "--rung-report" in s.get("run", ""))
    assert run.startswith("exec ")
    assert "--timeout 1500" in run
