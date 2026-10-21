"""The collected-floor check must catch tests that silently stop being collected.

A test that drops out of collection does not turn the suite red; it makes it
smaller. ``scripts/ci/collected_floor.py`` compares the node ids collected on a
change's base and on the change, and fails on ids that vanished from files the
change did not touch. Every test below drives the real script (or, for the
plugin, a real ``pytest --collect-only``) rather than a copy of its logic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml
from _pytest.fixtures import FixtureManager

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


def collect_project(project, output):
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "COLLECTED_FLOOR_OUT": str(output),
        "PYTHONPATH": str(PLUGIN_DIR),
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "collect_ids_plugin"],
        cwd=project, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(output.read_text())


MOVE_SOURCE = (
    "import pytest\n"
    "def test_plain(): assert True\n"
    "@pytest.mark.parametrize('x', [1, '2001:db8::1'])\n"
    "def test_param(x): assert x\n"
    "class TestA:\n"
    "    def test_same(self): assert True\n"
    "class TestB:\n"
    "    def test_same(self): assert True\n"
)


class TestStructuralMoves:
    @pytest.mark.parametrize(
        ("case", "expected"),
        [("pure", 5), ("body", 4), ("class_name", 4), ("class_header", 4),
         ("param_id", 4), ("duplicate_survivor", 0), ("ambiguous", 0)],
    )
    def test_real_collection_classifies_only_unique_unchanged_moves(self, tmp_path, case, expected):
        project = tmp_path / "project"
        project.mkdir()
        old = project / "test_old.py"
        old.write_text(MOVE_SOURCE)
        base = tmp_path / "base.json"
        base_data = collect_project(project, base)
        assert len(base_data.get("move_keys", {})) == 5
        source = MOVE_SOURCE
        if case == "body":
            source = source.replace("def test_plain(): assert True", "def test_plain(): assert False")
        elif case == "class_name":
            source = source.replace("class TestA:", "class TestRenamed:")
        elif case == "class_header":
            source = source.replace("class TestA:", "class TestA(object):")
        elif case == "param_id":
            # Same AST and parameter values, but a collection hook changes one node ID.
            (project / "conftest.py").write_text(
                "def pytest_collection_modifyitems(items):\n"
                "    for item in items:\n"
                "        item._nodeid = item.nodeid.replace('[1]', '[renamed]')\n"
            )
        old.unlink()
        if case == "duplicate_survivor":
            old.write_text(source.replace("class TestA:\n    def test_same(self): assert True\n", ""))
        else:
            (project / "test_new.py").write_text(source)
        if case == "ambiguous":
            (project / "test_second.py").write_text(source)
        pr = tmp_path / "pr.json"
        collect_project(project, pr)
        result = run(tmp_path, base, pr, changed=["test_old.py", "test_new.py", "test_second.py"])
        assert result.returncode == OK, result.stdout + result.stderr
        if expected:
            assert f"{expected} structural move(s)" in result.stdout
            assert "not semantic proof" in result.summary
        else:
            assert "Structural moves" not in result.summary
        # Classification never deletes the original disappearance evidence.
        vanished = set(base_data["ids"]) - set(json.loads(pr.read_text())["ids"])
        explained = result.summary.split("Explained: removed", 1)[1].split("Structural moves", 1)[0]
        assert all(nodeid in explained for nodeid in vanished)

    @pytest.mark.parametrize(
        ("method", "suffix"),
        [
            ("    def test_one(self): pass\n", "TestCase::test_one"),
            ("    @staticmethod\n    def test_one(): pass\n", "TestCase::test_one"),
            ("    @classmethod\n    def test_one(cls): pass\n", "TestCase::test_one"),
        ],
        ids=["instance", "static", "class"],
    )
    def test_shadowed_enclosing_class_does_not_create_a_move_pair(
        self, tmp_path, method, suffix
    ):
        project = tmp_path / "project"
        project.mkdir()
        old = project / "test_old.py"
        old.write_text(f"class TestCase:\n{method}")
        base = tmp_path / "base.json"
        base_data = collect_project(project, base)
        old_id = base_data["ids"][0]
        assert old_id.endswith(suffix)

        old.unlink()
        (project / "test_new.py").write_text(
            f"class TestCase:\n{method}\n"
            "_Base = TestCase\n"
            "class TestCase(_Base):\n"
            "    pass\n"
        )
        pr = tmp_path / "pr.json"
        pr_data = collect_project(project, pr)
        new_id = pr_data["ids"][0]
        assert new_id.endswith(suffix)
        assert new_id not in pr_data.get("move_keys", {})

        result = run(tmp_path, base, pr, changed=["test_old.py", "test_new.py"])
        assert result.returncode == OK, result.stdout + result.stderr
        assert old_id in result.summary
        assert "Structural moves" not in result.summary

    def test_shadowed_outer_class_does_not_create_a_nested_move_pair(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        old = project / "test_old.py"
        old.write_text(
            "class TestOuter:\n"
            "    class TestInner:\n"
            "        def test_one(self): pass\n"
        )
        base = tmp_path / "base.json"
        base_data = collect_project(project, base)
        old_id = base_data["ids"][0]
        assert old_id.endswith("TestOuter::TestInner::test_one")

        old.unlink()
        (project / "test_new.py").write_text(
            "class TestOuter:\n"
            "    class TestInner:\n"
            "        def test_one(self): pass\n"
            "_Base = TestOuter\n"
            "class TestOuter(_Base):\n"
            "    pass\n"
        )
        pr = tmp_path / "pr.json"
        pr_data = collect_project(project, pr)
        new_id = pr_data["ids"][0]
        assert new_id.endswith("TestOuter::TestInner::test_one")
        assert new_id not in pr_data.get("move_keys", {})

        result = run(tmp_path, base, pr, changed=["test_old.py", "test_new.py"])
        assert result.returncode == OK, result.stdout + result.stderr
        assert old_id in result.summary
        assert "Structural moves" not in result.summary

    def test_unshadowed_nested_static_and_class_methods_keep_move_keys(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "test_cases.py").write_text(
            "class TestOuter:\n"
            "    def test_instance(self): pass\n"
            "    @staticmethod\n"
            "    def test_static(): pass\n"
            "    @classmethod\n"
            "    def test_class(cls): pass\n"
            "    class TestInner:\n"
            "        def test_nested(self): pass\n"
        )
        data = collect_project(project, tmp_path / "ids.json")
        expected = {
            "test_cases.py::TestOuter::test_instance",
            "test_cases.py::TestOuter::test_static",
            "test_cases.py::TestOuter::test_class",
            "test_cases.py::TestOuter::TestInner::test_nested",
        }
        assert set(data["ids"]) == expected
        assert set(data.get("move_keys", {})) == expected

    @pytest.mark.parametrize("keys", [None, [], {"bad": []}, "invalid"])
    def test_missing_or_malformed_optional_keys_preserve_raw_loss(self, tmp_path, keys):
        base = tree(tmp_path / "base.json", ["old.py::test_one"])
        pr = tree(tmp_path / "pr.json", ["new.py::test_one"])
        for path in (base, pr):
            data = json.loads(path.read_text())
            data["move_keys"] = keys
            path.write_text(json.dumps(data))
        result = run(tmp_path, base, pr, changed=["old.py", "new.py"])
        assert result.returncode == OK
        assert "old.py::test_one" in result.summary
        assert "Structural moves" not in result.summary

    @pytest.mark.parametrize("mutation", ["none", "digest", "suffix", "shape", "untouched", "duplicate"])
    def test_key_validation_changed_paths_and_ambiguity(self, tmp_path, mutation):
        # The recorded path itself contains ::, as does the parameter value.
        old, new = "old::file.py", "new::file.py"
        suffix = "TestA::test_one[2001:db8::1]"
        base = tree(tmp_path / "base.json", [old + "::" + suffix], paths={old + "::" + suffix: old})
        pr = tree(tmp_path / "pr.json", [new + "::" + suffix], paths={new + "::" + suffix: new})
        for path in (base, pr):
            data = json.loads(path.read_text())
            key = [suffix, "a" * 64]
            if mutation == "digest":
                key[1] = "not-a-hash"
            elif mutation == "suffix":
                key[0] = "bare-name"
            elif mutation == "shape":
                key = {"suffix": suffix}
            data["move_keys"] = {data["ids"][0]: key}
            if mutation == "duplicate" and path == base:
                duplicate = "other.py::" + suffix
                data["ids"].append(duplicate)
                data["paths"][duplicate] = "other.py"
                data["move_keys"][duplicate] = key
            path.write_text(json.dumps(data))
        changed = [new] if mutation == "untouched" else [old, new, "other.py"]
        result = run(tmp_path, base, pr, changed=changed)
        assert result.returncode == (FAIL if mutation == "untouched" else OK)
        assert ("Structural moves" in result.summary) == (mutation == "none")
        assert old + "::" + suffix in result.summary

    def test_wrappers_generated_inherited_and_ambiguous_source_have_no_keys(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "test_cases.py").write_text(
            "import functools\n"
            "def decorate(fn):\n"
            "    @functools.wraps(fn)\n"
            "    def wrapper(): return fn()\n"
            "    return wrapper\n"
            "@decorate\n"
            "def test_wrapped(): pass\n"
            "exec('def test_generated(): pass')\n"
            "class TestParent:\n"
            "    def test_method(self): pass\n"
            "class TestChild(TestParent): pass\n"
            "def test_duplicate(): pass\n"
            "def test_duplicate(): pass\n"
        )
        (project / "test_encoded.py").write_bytes(
            b"# coding: latin-1\ndef test_encoded(): assert '\xe9'\n"
        )
        data = collect_project(project, tmp_path / "ids.json")
        assert len(data["ids"]) == 6
        assert list(data.get("move_keys", {})) == ["test_cases.py::TestParent::test_method"]

    def test_source_failure_and_parameter_cache(self, tmp_path, monkeypatch):
        spec = importlib.util.spec_from_file_location("collection_plugin", PLUGIN_DIR / "collect_ids_plugin.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        definitions = getattr(plugin, "_definitions", None)
        assert callable(definitions)
        path = tmp_path / "test_cases.py"
        path.write_text("def not_valid(:")
        assert definitions(path) == {}
        assert definitions(tmp_path / "missing.py") == {}
        # Compile the real function at this path, then use ordinary pytest Function items.
        path.write_text("def test_param(x): pass\n")
        module_code = compile(path.read_text(), str(path), "exec")
        function_code = next(
            constant for constant in module_code.co_consts if isinstance(constant, types.CodeType)
        )
        callobj = types.FunctionType(function_code, {})
        config = pytest.Config.fromdictargs({}, ["--rootdir", str(tmp_path)])
        session = pytest.Session.from_config(config)
        session._fixturemanager = FixtureManager(session)
        module = pytest.Module.from_parent(session, path=path)
        cache = {}
        calls = []
        original = definitions
        monkeypatch.setattr(plugin, "_definitions", lambda p: (calls.append(p), original(p))[1])
        for parameter in ["1", "2001:db8::1"]:
            item = pytest.Function.from_parent(
                module,
                name=f"test_param[{parameter}]",
                callobj=callobj,
                originalname="test_param",
            )
            item._nodeid = f"test_cases.py::test_param[{parameter}]"
            item._location = ("test_cases.py", 0, "test_param")
            assert plugin._move_key(item, tmp_path, "test_cases.py", cache) is not None
        assert calls == [path]
        assert plugin._move_key(item, tmp_path / "other-root", "test_cases.py", cache) is None


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
        install = run.index('"$base_py" -m pip install --quiet -e ".[', venv)
        assert diff < checkout < venv < install < run.index('collect base "$base_py"')
        assert "collect pr python" in run and '"$2" -m pytest' in run

    @staticmethod
    def _extras(job) -> list[str]:
        import re

        text = "\n".join(str(s.get("run", "")) for s in job["steps"])
        return re.findall(r'pip install (?:--quiet )?-e "\.\[([^\]]*)\]"', text)

    def test_both_trees_install_the_test_jobs_extras(self):
        # A module that imports an optional extra at collection time cannot be
        # collected without it, and a new collection error on the PR tree
        # fails this job. The PR tree gets the test job's extras; the base's
        # fresh venv gets them too, so a later PR that edits pyproject.toml
        # still collects the base's extra-dependent tests (pip only warns
        # about an extra the base tree does not declare).
        jobs = self._jobs()
        (test_extras,) = self._extras(jobs["test"])
        assert "test" in test_extras.split(","), "precondition: the test job's install was found"
        assert self._extras(jobs["collected-floor"]) == [test_extras, test_extras]

    def test_job_runs_on_pull_requests_and_pushes_only(self):
        cond = self._jobs()["collected-floor"]["if"]
        assert "pull_request" in cond and "push" in cond
        assert "schedule" not in cond and "workflow_dispatch" not in cond

    def test_the_hand_maintained_floor_is_gone(self):
        guard = self._jobs()["test"]["steps"][-1]
        assert guard["name"] == "Suite dormancy guard"
        assert "--min-collected" not in guard["run"]
        assert "--ceiling" in guard["run"]
