"""Regression: the review marker hash must be TERMINAL-WIDTH-INDEPENDENT.

``get_current_diff_hash`` previously hashed ``git diff --cached --stat`` output,
which git renders differently by ``COLUMNS``/terminal width (path truncation +
change-bar scaling). So the SAME staged content hashed differently when ``mark``
(one width) and the commit gate (another width) ran, producing a systematic false
"code changes without review" block on a genuinely-reviewed commit. The fix uses
``--numstat`` (machine format), which ignores width. This test pins that: the hash
of identical staged content is stable across wildly different ``COLUMNS`` values.

Synthetic tmp_path repo with a LONG file path (the case ``--stat`` truncates).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_rs = _load("review_state")


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _mk_repo_with_staged_long_path(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    # A deeply-nested long path is exactly what `git diff --stat` truncates with `.../`
    # at narrow widths — the trigger for the width-dependent hash.
    deep = repo / "src" / "genesis" / "observability" / "snapshots" / "a_rather_long_module_name"
    deep.mkdir(parents=True)
    f = deep / "infrastructure_pid_budget_collector.py"
    f.write_text("def f():\n" + "".join(f"    x{i} = {i}\n" for i in range(40)))
    _git(repo, "add", "-A")
    return repo


def test_hash_is_width_independent(tmp_path, monkeypatch):
    repo = _mk_repo_with_staged_long_path(tmp_path)

    monkeypatch.setenv("COLUMNS", "80")
    h80 = _rs.get_current_diff_hash(cwd=str(repo))
    monkeypatch.setenv("COLUMNS", "400")
    h400 = _rs.get_current_diff_hash(cwd=str(repo))
    monkeypatch.delenv("COLUMNS", raising=False)
    hnone = _rs.get_current_diff_hash(cwd=str(repo))

    assert h80 not in ("clean", "unknown")
    # The whole point: identical staged content → identical hash regardless of width.
    assert h80 == h400 == hnone


def test_hash_still_changes_with_content(tmp_path, monkeypatch):
    # Width-independence must not cost content-sensitivity: a different staged diff
    # must still produce a different hash (else the marker never goes stale).
    repo = _mk_repo_with_staged_long_path(tmp_path)
    monkeypatch.setenv("COLUMNS", "120")
    h1 = _rs.get_current_diff_hash(cwd=str(repo))
    (repo / "another.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    h2 = _rs.get_current_diff_hash(cwd=str(repo))
    assert h1 != h2
