"""Real pytest -q notices identify the source tree without changing imports."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
CASES = [
    ("main", True), ("linked", False), ("separate-main", True),
    ("separate-linked", False), ("lone", False), ("bogus-admin", False),
    ("newline", True), ("nonrepo", False), ("missing-git", False),
    ("failed-git", False), ("redirect-main", True), ("redirect-linked", False),
    ("warnings-error", True),
]


def _source_parts():
    source = (SOURCE / "tests/conftest.py").read_text()
    nodes = ast.parse(source).body
    imports = next(node for node in nodes if isinstance(node, ast.Import) and node.names[0].name == "contextlib")
    prefix = "\n".join(source.split("\n")[:imports.lineno - 1])
    functions = {node.name: ast.get_source_segment(source, node) for node in nodes if isinstance(node, ast.FunctionDef)}
    # Execute the real session-start function below. Only its unrelated home
    # bookkeeping and missing-timeout message are replaced in the owned child.
    return prefix, functions.get("_note_main_checkout", ""), functions["pytest_sessionstart"]


@pytest.mark.parametrize("case, expected", CASES)
def test_main_checkout_notice_and_actual_import_source(tmp_path, case, expected):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "GENESIS_", "CLAUDE_", "PYTHON", "PYTEST"))}
    env.update(HOME=str(tmp_path), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")

    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                              capture_output=True, timeout=10).stdout.removesuffix(b"\n")

    main = tmp_path / ("main\nsource" if case == "newline" else "main")
    main.mkdir()
    linked = tmp_path / "linked"
    if case != "nonrepo":
        separate = ["--separate-git-dir", str(tmp_path / "metadata")] if case.startswith("separate-") else []
        git(main, "-c", "init.templateDir=", "init", "-q", *separate)
        tree = git(main, "hash-object", "-t", "tree", os.devnull).decode()
        oid = git(main, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit-tree", tree, "-m", "owned fixture").decode()
        git(main, "update-ref", "HEAD", oid)
        if case not in ("lone", "bogus-admin"):
            git(main, "worktree", "add", "--detach", str(linked), oid)
        if case == "bogus-admin":
            (main / ".git/worktrees/not-a-registration").mkdir(parents=True)
    root = linked if case in ("linked", "separate-linked", "redirect-linked") else main
    if case in ("redirect-main", "redirect-linked"):
        other = tmp_path / "other"
        other.mkdir()
        git(other, "-c", "init.templateDir=", "init", "-q")
        env.update(GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other))
    if case in ("missing-git", "failed-git"):
        tools = tmp_path / "tools"
        tools.mkdir()
        if case == "failed-git":
            (tools / "git").write_text("#!/bin/sh\nexit 1\n")
            (tools / "git").chmod(0o755)
        env["PATH"] = str(tools)
    if case == "warnings-error":
        env["PYTHONWARNINGS"] = "error"

    owned = root / "src/genesis"
    decoy = tmp_path / "decoy/genesis"
    for path, identity in ((owned, "owned"), (decoy, "decoy")):
        path.mkdir(parents=True)
        (path / "__init__.py").write_text(f"ORIGIN = {identity!r}\n")
    env["PYTHONPATH"] = str(decoy.parent)
    testdir = root / "tests"
    testdir.mkdir()
    prefix, notice, sessionstart = _source_parts()
    (testdir / "conftest.py").write_text(prefix + "\nimport os\nimport subprocess\n" + notice
        + "\n\ndef _note_real_home_at_start(config):\n    pass\n_NO_TIMEOUT_WARNING = ''\n\n" + sessionstart + "\n")
    test = testdir / "test_source.py"
    test.write_text("from pathlib import Path\nimport genesis\ndef test_source():\n"
                    "    assert genesis.ORIGIN == 'owned'\n"
                    "    assert Path(genesis.__file__).resolve() == Path(__file__).parents[1] / 'src/genesis/__init__.py'\n")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", str(test)],
                            cwd=root, env=env, capture_output=True, timeout=15)
    output = (result.stdout + result.stderr).decode(errors="replace")
    assert result.returncode == 0, output
    assert ("MAIN checkout" in output) is expected, output
