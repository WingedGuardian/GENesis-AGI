"""The collected-floor check must catch tests that silently stop being collected.

A test that drops out of collection does not turn the suite red; it makes it
smaller. ``scripts/ci/collected_floor.py`` compares the node ids collected on a
change's base and on the change, and fails on ids that vanished from files the
change did not touch. Every test below drives the real script (or, for the
plugin, a real ``pytest --collect-only``) rather than a copy of its logic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci" / "collected_floor.py"
PLUGIN_DIR = REPO / "scripts" / "ci"
CI = REPO / ".github" / "workflows" / "ci.yml"

OK, FAIL, REFUSED = 0, 1, 2
TAG = "COLLECTED-FLOOR"


def tree(path: Path, ids, errors=(), skipped=(), paths=None) -> Path:
    # Synthetic ids here never put `::` inside a file name, so their file is
    # the text before the first `::`; tests that need otherwise pass `paths`.
    files = {i: i.split("::", 1)[0] for i in ids}
    files.update(paths or {})
    body = {"ids": list(ids), "paths": files, "errors": list(errors), "skipped": list(skipped)}
    path.write_text(json.dumps(body))
    return path


def run(tmp_path: Path, base, pr, changed=(), body=None, mode=None, summary=True):
    b = base if isinstance(base, Path) else tree(tmp_path / "base.json", base)
    p = pr if isinstance(pr, Path) else tree(tmp_path / "pr.json", pr)
    ch = tmp_path / "changed.txt"
    # `git diff -z` shape: NUL after every path.
    ch.write_bytes(b"".join(os.fsencode(c) + b"\0" for c in changed))
    args = [sys.executable, str(SCRIPT), str(b), str(p), "--changed", str(ch)]
    if body is not None:
        bf = tmp_path / "body.txt"
        bf.write_text(body)
        args += ["--body-file", str(bf)]
    else:
        args.append(mode or "--no-body")
    out = tmp_path / "summary.md"
    if summary:
        args += ["--summary", str(out)]
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    r.summary = out.read_text() if out.exists() else ""
    return r


BASE = ["tests/a/test_x.py::test_1", "tests/a/test_x.py::test_2", "tests/b/test_y.py::test_3"]


class TestTheRule:
    def test_no_change_is_ok(self, tmp_path):
        r = run(tmp_path, BASE, BASE)
        assert r.returncode == OK, r.stdout + r.stderr
        assert "-0 vanished" in r.stdout

    def test_added_tests_are_ok_and_counted(self, tmp_path):
        r = run(
            tmp_path, BASE, [*BASE, "tests/c/test_z.py::test_new"], changed=["tests/c/test_z.py"]
        )
        assert r.returncode == OK
        assert "+1 added" in r.stdout

    def test_removal_from_a_changed_file_is_explained(self, tmp_path):
        r = run(tmp_path, BASE, BASE[1:], changed=["tests/a/test_x.py"])
        assert r.returncode == OK, r.stdout
        assert "1 explained" in r.stdout and "0 unexplained" in r.stdout

    def test_removal_from_an_untouched_file_fails(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], changed=["src/pkg/mod.py"])
        assert r.returncode == FAIL, r.stdout
        assert "::error::" in r.stdout and "tests/b/test_y.py::test_3" in r.stdout

    def test_a_config_change_does_not_explain_a_drop(self, tmp_path):
        """The real failure mode: a non-test file drops a directory."""
        r = run(tmp_path, BASE, BASE[:2], changed=["pyproject.toml"])
        assert r.returncode == FAIL

    def test_deleted_or_renamed_file_explains_its_ids(self, tmp_path):
        # --no-renames lists a rename as delete + add, so the old path is present.
        pr = [*BASE[:2], "tests/b/test_renamed.py::test_3"]
        r = run(tmp_path, BASE, pr, changed=["tests/b/test_y.py", "tests/b/test_renamed.py"])
        assert r.returncode == OK, r.stdout

    def test_param_ids_containing_double_colon_map_to_their_file(self, tmp_path):
        base = ["tests/a/test_x.py::test_ip[2001:db8::1]", *BASE]
        r = run(tmp_path, base, BASE, changed=["tests/a/test_x.py"])
        assert r.returncode == OK, r.stdout


class TestConftest:
    """A conftest edit never explains a drop: tests/conftest.py sits above the
    whole suite, so letting it explain would switch the check off for every PR
    that edits it. It is named as the likely cause instead."""

    def test_same_directory_conftest_fails_and_is_named(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], changed=["tests/b/conftest.py"])
        assert r.returncode == FAIL, r.stdout
        assert "sit under a conftest.py this change edits (tests/b)" in r.stdout

    def test_suite_wide_conftest_does_not_waive_the_suite(self, tmp_path):
        base = [f"tests/m{i}/test_m.py::test_{i}" for i in range(100)]
        r = run(tmp_path, base, base[:10], changed=["tests/conftest.py"])
        assert r.returncode == FAIL, r.stdout
        assert "90 test(s)" in r.stdout and "conftest.py this change edits (tests)" in r.stdout

    def test_root_conftest_is_named_as_dot(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], changed=["conftest.py"])
        assert r.returncode == FAIL
        assert "this change edits (.)" in r.stdout

    def test_sibling_directory_conftest_is_not_named(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], changed=["tests/a/conftest.py"])
        assert r.returncode == FAIL, r.stdout
        assert "conftest.py this change edits" not in r.stdout

    def test_prefix_named_directory_is_not_under_it(self, tmp_path):
        # tests/b_extra is not inside tests/b.
        base = [*BASE[:2], "tests/b_extra/test_q.py::test_9"]
        r = run(tmp_path, base, BASE[:2], changed=["tests/b/conftest.py"])
        assert r.returncode == FAIL, r.stdout
        assert "conftest.py this change edits" not in r.stdout

    def test_escape_still_accepts_a_deliberate_conftest_drop(self, tmp_path):
        r = run(
            tmp_path,
            BASE,
            BASE[:2],
            changed=["tests/b/conftest.py"],
            body="Collected-drop: tests/b now opt-in\n",
        )
        assert r.returncode == OK, r.stdout


class TestPathIdentity:
    """A vanished id is matched to its file by the path the plugin recorded,
    and the changed list is raw `git diff -z` bytes: no name may be cut,
    stripped or refused on the way."""

    def _check(self, tmp_path, name: str, raw_changed: bytes):
        nodeid = f"{name}::test_x"
        b = tree(tmp_path / "base.json", [*BASE, nodeid], paths={nodeid: name})
        p = tree(tmp_path / "pr.json", BASE)
        ch = tmp_path / "changed.txt"
        ch.write_bytes(b"src/x.py\0" + raw_changed + b"\0")
        summary = tmp_path / "summary.md"
        # Scrubbed: CI's test job sets GITHUB_STEP_SUMMARY, and the script must
        # neither write into that job's summary nor pass here only because the
        # summary path never ran. The summary is exercised explicitly instead.
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
        r = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(b),
                str(p),
                "--changed",
                str(ch),
                "--no-body",
                "--summary",
                str(summary),
            ],
            capture_output=True,
            text=True,
            # Lenient here so a raw undecodable byte reaches the assertion
            # below instead of failing the harness.
            errors="replace",
            env=env,
        )
        assert "Traceback" not in r.stderr, r.stderr
        assert summary.exists(), "the summary path did not run"
        return r

    def test_non_ascii_name(self, tmp_path):
        name = "tests/ünï/test_ä.py"
        r = self._check(tmp_path, name, name.encode())
        assert r.returncode == OK, r.stdout + r.stderr

    def test_double_colon_inside_a_file_name(self, tmp_path):
        name = "tests/test_a::b.py"
        r = self._check(tmp_path, name, name.encode())
        assert r.returncode == OK, r.stdout + r.stderr

    def test_newline_inside_a_file_name(self, tmp_path):
        name = "tests/test_a\nb.py"
        r = self._check(tmp_path, name, name.encode())
        assert r.returncode == OK, r.stdout + r.stderr

    def test_surrounding_spaces_are_part_of_the_name(self, tmp_path):
        name = " tests/test_sp.py "
        r = self._check(tmp_path, name, name.encode())
        assert r.returncode == OK, r.stdout + r.stderr

    def test_non_utf8_bytes_round_trip_instead_of_crashing(self, tmp_path):
        raw = b"tests/test_\xff.py"
        r = self._check(tmp_path, os.fsdecode(raw), raw)
        assert r.returncode == OK, r.stdout + r.stderr

    def test_unexplained_non_utf8_name_is_reported_not_crashed(self, tmp_path):
        name = os.fsdecode(b"tests/test_\xfe.py")
        r = self._check(tmp_path, name, b"src/other.py")
        assert r.returncode == FAIL, r.stdout + r.stderr
        assert "Traceback" not in r.stderr
        assert "test_\\udcfe.py" in r.stdout

    def test_a_missing_path_map_refuses(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"ids": BASE, "errors": [], "skipped": []}))
        r = run(tmp_path, bad, BASE)
        assert r.returncode == REFUSED and "map every id" in r.stdout


class TestEscapeLine:
    def test_visible_line_accepts_the_drop_loudly(self, tmp_path):
        r = run(
            tmp_path, BASE, BASE[:2], body="Summary.\n\nCollected-drop: moved behind a marker\n"
        )
        assert r.returncode == OK, r.stdout
        assert "::warning::" in r.stdout and "moved behind a marker" in r.stdout
        assert "drop accepted" in r.summary

    def test_case_and_leading_space_are_tolerated(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], body="  collected-DROP:   deliberate\n")
        assert r.returncode == OK, r.stdout

    def test_line_inside_a_fence_is_not_an_escape(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], body="```\nCollected-drop: example only\n```\n")
        assert r.returncode == FAIL, r.stdout

    def test_line_inside_an_html_comment_is_not_an_escape(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], body="<!-- Collected-drop: hidden -->\n")
        assert r.returncode == FAIL, r.stdout

    def test_empty_reason_is_not_an_escape(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], body="Collected-drop:\n")
        assert r.returncode == FAIL, r.stdout

    def test_unreadable_body_refuses_only_when_it_matters(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], mode="--body-unreadable")
        assert r.returncode == REFUSED and TAG in r.stdout + r.stderr
        clean = run(tmp_path, BASE, BASE, mode="--body-unreadable")
        assert clean.returncode == OK, clean.stdout

    def test_no_pr_means_no_escape(self, tmp_path):
        r = run(tmp_path, BASE, BASE[:2], mode="--no-body")
        assert r.returncode == FAIL


class TestCollectionErrors:
    def test_new_error_on_the_pr_tree_fails_even_with_an_escape(self, tmp_path):
        pr = tree(tmp_path / "pr.json", BASE, errors=["tests/b/test_broken.py"])
        r = run(tmp_path, BASE, pr, body="Collected-drop: anything\n")
        assert r.returncode == FAIL
        assert "new collection error" in r.stdout

    def test_error_present_on_both_trees_is_not_new(self, tmp_path):
        base = tree(tmp_path / "base.json", BASE, errors=["tests/b/test_broken.py"])
        pr = tree(tmp_path / "pr.json", BASE, errors=["tests/b/test_broken.py"])
        assert run(tmp_path, base, pr).returncode == OK

    def test_base_only_error_is_reported_not_failed(self, tmp_path):
        base = tree(tmp_path / "base.json", BASE, errors=["tests/b/test_broken.py"])
        r = run(tmp_path, base, BASE)
        assert r.returncode == OK
        assert "base-only collection error" in r.stdout

    def test_newly_skipped_module_is_named(self, tmp_path):
        pr = tree(tmp_path / "pr.json", BASE[:2], skipped=["tests/b/test_y.py"])
        r = run(tmp_path, BASE, pr)
        assert r.returncode == FAIL
        assert "now skipped at import" in r.stdout


class TestRefusals:
    def test_missing_output_refuses(self, tmp_path):
        r = run(tmp_path, tmp_path / "absent.json", BASE)
        assert r.returncode == REFUSED and TAG in r.stdout

    def test_malformed_json_refuses(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert run(tmp_path, bad, BASE).returncode == REFUSED

    def test_missing_key_refuses(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"ids": BASE}))
        assert run(tmp_path, bad, BASE).returncode == REFUSED

    def test_empty_tree_refuses_rather_than_passing(self, tmp_path):
        r = run(tmp_path, BASE, [])
        assert r.returncode == REFUSED and "0 tests" in r.stdout


class TestSummary:
    def test_lists_ids_whole_and_states_what_it_omits(self, tmp_path):
        long = [f"tests/big/test_{i}.py::test_{'x' * 180}" for i in range(6000)]
        r = run(tmp_path, long, [long[0]], changed=[])
        assert r.returncode == FAIL
        size = len(r.summary.encode())
        assert size < 1024 * 1024, size
        assert "not listed (summary budget spent)" in r.summary
        listed = [line for line in r.summary.splitlines() if line.startswith("tests/big/")]
        assert listed and all(line in long for line in listed), "an id was cut"

    def test_fence_outgrows_backticks_in_an_id(self, tmp_path):
        base = [*BASE, "tests/b/test_y.py::test_q[```]"]
        r = run(tmp_path, base, BASE)
        assert "````\n" in r.summary


class TestRunsTheWayCIRunsIt:
    def test_bare_interpreter_stdlib_only_with_the_shared_body_scanner(self, tmp_path):
        """CI runs `python3` on the script; the escape path loads the sibling
        body scanner, so prove it loads with no site packages."""
        for name, ids in (("base.json", BASE), ("pr.json", BASE[:2])):
            tree(tmp_path / name, ids)
        (tmp_path / "changed.txt").write_text("\n")
        (tmp_path / "body.txt").write_text("Collected-drop: deliberate\n")
        r = subprocess.run(
            [
                "python3",
                "-E",
                "-S",
                "scripts/ci/collected_floor.py",
                str(tmp_path / "base.json"),
                str(tmp_path / "pr.json"),
                "--changed",
                str(tmp_path / "changed.txt"),
                "--body-file",
                str(tmp_path / "body.txt"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        assert r.returncode == OK, r.stdout + r.stderr
        assert "accepted by the PR's Collected-drop line" in r.stdout


class TestPlugin:
    def test_records_ids_errors_and_import_skips_exactly(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "tests" / "sub").mkdir(parents=True)
        (proj / "tests" / "test_ok.py").write_text(
            "import pytest\n"
            "@pytest.mark.parametrize('x', [1, 'a b', '2001:db8::1'])\n"
            "def test_p(x): pass\n"
        )
        (proj / "tests" / "test_err.py").write_text("import nosuchmodule_collected_floor\n")
        (proj / "tests" / "sub" / "test_skip.py").write_text(
            "import pytest\npytest.importorskip('nosuchmodule_collected_floor')\ndef test_s(): pass\n"
        )
        out = tmp_path / "ids.json"
        env = {**os.environ, "COLLECTED_FLOOR_OUT": str(out), "PYTHONPATH": str(PLUGIN_DIR)}
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "collect_ids_plugin",
                "-p",
                "no:cacheprovider",
                "--continue-on-collection-errors",
                "tests",
            ],
            capture_output=True,
            text=True,
            cwd=str(proj),
            env=env,
        )
        assert r.returncode == 1, r.stdout + r.stderr  # collection errors, continued
        data = json.loads(out.read_text())
        assert data["ids"] == [
            "tests/test_ok.py::test_p[1]",
            "tests/test_ok.py::test_p[a b]",
            "tests/test_ok.py::test_p[2001:db8::1]",
        ]
        assert set(data["paths"].values()) == {"tests/test_ok.py"}
        assert set(data["paths"]) == set(data["ids"])
        assert data["errors"] == ["tests/test_err.py"]
        assert data["skipped"] == ["tests/sub/test_skip.py"]


class TestWorkflowContract:
    def _jobs(self):
        return yaml.safe_load(CI.read_text())["jobs"]

    @staticmethod
    def _flags(job) -> list[str]:
        import re

        text = "\n".join(str(s.get("run", "")) for s in job["steps"])
        return sorted(re.findall(r"--ignore(?:-glob)?=\S+", text))

    def test_selection_flags_match_the_test_job(self):
        jobs = self._jobs()
        test_flags = self._flags(jobs["test"])
        assert test_flags, "precondition: the test job's selection flags were found"
        assert self._flags(jobs["collected-floor"]) == test_flags

    def test_job_uses_the_shipped_plugin_and_script(self):
        text = "\n".join(str(s.get("run", "")) for s in self._jobs()["collected-floor"]["steps"])
        assert "scripts/ci/collect_ids_plugin.py" in text and "-p collect_ids_plugin" in text
        assert "scripts/ci/collected_floor.py" in text
        assert (REPO / "scripts/ci/collect_ids_plugin.py").is_file()

    def test_base_collects_under_its_own_dependencies(self):
        (step,) = [
            s
            for s in self._jobs()["collected-floor"]["steps"]
            if s.get("name") == "Collect test ids on both trees"
        ]
        run = step["run"]
        # The dependency diff is taken BEFORE the switch, the fresh venv is
        # built and installed AFTER it (so it installs the base's metadata),
        # and the base is collected with that venv's interpreter.
        diff = run.index('git diff --quiet "$BASE" "$HEAD_SHA" -- pyproject.toml')
        checkout = run.index('git checkout --quiet --force --detach "$BASE"')
        venv = run.index('python -m venv "$RUNNER_TEMP/venv-base"', checkout)
        install = run.index('"$base_py" -m pip install --quiet -e ".[test]"', venv)
        assert diff < checkout < venv < install < run.index('collect base "$base_py"')
        assert "collect pr python" in run and '"$2" -m pytest' in run

    def test_job_runs_on_pull_requests_and_pushes_only(self):
        cond = self._jobs()["collected-floor"]["if"]
        assert "pull_request" in cond and "push" in cond
        assert "schedule" not in cond and "workflow_dispatch" not in cond

    def test_the_hand_maintained_floor_is_gone(self):
        guard = self._jobs()["test"]["steps"][-1]
        assert guard["name"] == "Suite dormancy guard"
        assert "--min-collected" not in guard["run"]
        assert "--ceiling" in guard["run"]
