"""Regression: the review marker hash must be WIDTH-INDEPENDENT and CONTENT-COMPLETE.

``get_current_diff_hash`` previously hashed ``git diff --cached --stat`` output,
which git renders differently by ``COLUMNS``/terminal width (path truncation +
change-bar scaling). So the SAME staged content hashed differently when ``mark``
(one width) and the commit gate (another width) ran — a systematic false "code
changes without review" block on a genuinely-reviewed commit. A first fix used
``--numstat`` (width-independent), but that collapses every binary change to
``-\t-\t<path>``, letting a same-path binary SWAP read as already-reviewed. The
shipped fix uses ``git diff --cached --raw --no-abbrev`` — width-independent AND
content-complete (the destination blob OID changes on ANY content change, binary
included). These tests pin both properties across the staged-change class.

Synthetic tmp_path repos (the long-path case ``--stat`` truncates; binary/mode/rename).
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


def test_binary_replacement_changes_hash(tmp_path):
    # A same-path binary SWAP must change the hash — else a reviewed binary could be
    # replaced with different bytes and read as already-reviewed (a review bypass).
    # BOTH payloads must be NUL-bearing (so git classifies them BINARY) and the SAME
    # length, so `git diff --cached --numstat` collapses BOTH to `-\t-\tasset.bin`
    # (byte-identical) — only `--raw` (blob OID) distinguishes them. (With NUL-free or
    # different-length bytes, numstat would differ and the test would green for the
    # wrong reason, not actually guarding the bypass.)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    blob = repo / "asset.bin"
    blob.write_bytes(bytes(range(32)))  # NUL-bearing → binary
    _git(repo, "add", "-A")
    h1 = _rs.get_current_diff_hash(cwd=str(repo))
    blob.write_bytes(bytes(reversed(range(32))))  # same length, still binary, different bytes
    _git(repo, "add", "-A")
    h2 = _rs.get_current_diff_hash(cwd=str(repo))
    assert h1 not in ("clean", "unknown")
    assert h1 != h2


def test_mode_change_changes_hash(tmp_path):
    # chmod +x with identical content must change the hash (the --raw mode field flips).
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    f = repo / "s.sh"
    f.write_text("echo hi\n")
    _git(repo, "add", "-A")
    (repo / "seed").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "add", "-A")
    h1 = _rs.get_current_diff_hash(cwd=str(repo))
    _git(repo, "update-index", "--chmod=+x", "s.sh")
    h2 = _rs.get_current_diff_hash(cwd=str(repo))
    assert h1 != h2


def test_rename_changes_hash(tmp_path):
    # A staged rename must change the hash vs the committed baseline.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "old.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    h_clean = _rs.get_current_diff_hash(cwd=str(repo))
    _git(repo, "mv", "old.py", "new.py")
    h_renamed = _rs.get_current_diff_hash(cwd=str(repo))
    assert h_clean == "clean"
    assert h_renamed not in ("clean", "unknown")


def test_clean_and_unknown(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    assert _rs.get_current_diff_hash(cwd=str(repo)) == "clean"  # nothing staged
    assert (
        _rs.get_current_diff_hash(cwd=str(tmp_path / "does_not_exist")) == "unknown"
    )  # git errors


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
