"""Tests for scripts/lib/code_intel_index.sh — the single code-intel index entrypoint.

Three concurrent uncapped ``codebase-memory-mcp cli index_repository`` jobs on
one worktree once saturated the container's disk-write throttle and wedged the
whole container in a D-state I/O storm. The entrypoint enforces, in order:
worktree skip, per-repo single-flight flock, and resource-capped execution
(systemd scope, with a nice/ionice + rlimit fallback).

These tests are binary- and environment-independent: the indexer binaries and
``systemd-run`` are faked via PATH injection, so they run in CI runners with
no systemd user manager and no code-intel tools installed. The guardrail test
at the bottom is the enforcement mechanism: it fails the build on any NEW raw
index spawn outside the entrypoint.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _REPO_ROOT / "scripts" / "lib" / "code_intel_index.sh"

_SYSTEM_PATH = "/usr/bin:/bin"  # real bash/flock/sha1sum for harnesses


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_repo(tmp_path: Path, *, worktree: bool = False) -> Path:
    """A fake repo dir; ``worktree=True`` makes .git a FILE (gitdir pointer)."""
    repo = Path(os.path.realpath(tmp_path / "repo"))
    repo.mkdir(exist_ok=True)
    if worktree:
        (repo / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
    else:
        (repo / ".git").mkdir(exist_ok=True)
    return repo


def _fake_tools(bindir: Path, log: Path, *, sleep: float = 0) -> None:
    """Fake codebase-memory-mcp + gitnexus that record args and ulimit -v."""
    bindir.mkdir(exist_ok=True)
    for name in ("codebase-memory-mcp", "gitnexus"):
        _write_exec(
            bindir / name,
            "#!/usr/bin/env bash\n"
            f'echo "{name} ARGS:$*" >> "{log}"\n'
            f'echo "{name} ULIMIT_V:$(ulimit -v)" >> "{log}"\n'
            + (f"sleep {sleep}\n" if sleep else ""),
        )


def _fake_systemd_run(bindir: Path, log: Path, *, probe_ok: bool = True) -> None:
    """Fake systemd-run: logs argv, then execs the command after ``--``."""
    bindir.mkdir(exist_ok=True)
    if probe_ok:
        body = (
            "#!/usr/bin/env bash\n"
            f'echo "$*" >> "{log}"\n'
            'while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
            "shift\n"
            'exec "$@"\n'
        )
    else:
        body = f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 1\n'
    _write_exec(bindir / "systemd-run", body)


def _run_entry(tmp_path: Path, *args, path: str, env_extra=None, **popen_kw):
    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        **(env_extra or {}),
    }
    return subprocess.run(
        ["bash", str(_ENTRYPOINT), *[str(a) for a in args]],
        env=env, capture_output=True, text=True, timeout=60, **popen_kw,
    )


def _minimal_path(tmp_path: Path, *extra_tools: str) -> Path:
    """A PATH dir with only what the entrypoint needs — no systemd-run.

    ``/usr/bin:/bin`` contains the real systemd-run on most hosts, so using it
    would exercise the probe path, not the absent-binary fallback.
    """
    d = tmp_path / "minbin"
    d.mkdir(exist_ok=True)
    for tool in ("bash", "sh", "env", "mkdir", "sha1sum", "cut", "flock",
                 "nice", *extra_tools):
        src = Path("/usr/bin") / tool
        if not src.exists():
            src = Path("/bin") / tool
        target = d / tool
        if not target.exists():
            target.symlink_to(src)
    return d


# ── argument validation ───────────────────────────────────────────────────


def test_missing_repo_path_errors(tmp_path):
    res = _run_entry(tmp_path, path=_SYSTEM_PATH)
    assert res.returncode == 1
    assert "repo path missing" in res.stdout


def test_nonexistent_repo_errors(tmp_path):
    res = _run_entry(tmp_path, tmp_path / "nope", path=_SYSTEM_PATH)
    assert res.returncode == 1


def test_bad_tool_arg_errors(tmp_path):
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "everything", path=_SYSTEM_PATH)
    assert res.returncode == 1
    assert "cbm|gitnexus|both" in res.stdout


def test_disable_env_skips_everything(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, path=f"{fakebin}:{_SYSTEM_PATH}",
                     env_extra={"CODE_INTEL_INDEX_DISABLE": "1"})
    assert res.returncode == 0
    assert "disabled" in res.stdout
    assert not log.exists()


# ── 1. worktree skip ──────────────────────────────────────────────────────


def test_worktree_git_file_never_indexed(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path, worktree=True)
    res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    assert "worktree" in res.stdout
    assert not log.exists()  # zero index processes spawned — the core proof


def test_main_repo_runs_both_tools(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    out = log.read_text()
    assert "codebase-memory-mcp ARGS:cli" in out
    assert f'"repo_path": "{repo}"' in out
    assert re.search(r"gitnexus ARGS:analyze --quiet", out)


def test_tool_selection_cbm_only(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    out = log.read_text()
    assert "codebase-memory-mcp ARGS:" in out
    assert "gitnexus ARGS:" not in out


def test_tool_selection_gitnexus_only(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "gitnexus", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    out = log.read_text()
    assert "gitnexus ARGS:analyze --quiet" in out
    assert "codebase-memory-mcp ARGS:" not in out


def test_missing_tools_skip_cleanly(tmp_path):
    # No indexer binaries anywhere on PATH → informative skip, exit 0.
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "both", path=str(_minimal_path(tmp_path)))
    assert res.returncode == 0, res.stderr
    assert "codebase-memory-mcp not on PATH" in res.stdout
    assert "gitnexus not available" in res.stdout


# ── 2. single-flight lock ─────────────────────────────────────────────────


def _lock_file_for(tmp_path: Path, repo: Path) -> Path:
    digest = hashlib.sha1(str(repo).encode()).hexdigest()[:16]
    lock_dir = tmp_path / ".genesis" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"code-intel-{digest}.lock"


def test_lock_held_skips_without_running(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    lock_file = _lock_file_for(tmp_path, repo)
    with open(lock_file, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    assert "already running" in res.stdout
    assert not log.exists()


def test_parallel_double_invocation_exactly_one_runs(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log, sleep=2)
    repo = _make_repo(tmp_path)
    env = {"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(tmp_path),
           "GENESIS_HOME": str(tmp_path / ".genesis")}
    procs = [
        subprocess.Popen(
            ["bash", str(_ENTRYPOINT), str(repo), "cbm"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs)
    runs = log.read_text().count("codebase-memory-mcp ARGS:")
    assert runs == 1, f"expected exactly 1 index run, got {runs}: {outs}"
    assert sum("already running" in o for o in outs) == 1


def test_lock_released_after_completion(tmp_path):
    # Sequential runs must NOT dedup — the lock lives only for the run.
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    for _ in range(2):
        res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
        assert res.returncode == 0, res.stderr
    assert log.read_text().count("codebase-memory-mcp ARGS:") == 2


def test_no_flock_degrades_to_unlocked_run(tmp_path):
    # Missing flock must degrade to "no dedup", never "silently skip".
    minbin = _minimal_path(tmp_path)
    (minbin / "flock").unlink()
    log = tmp_path / "tools.log"
    _fake_tools(minbin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=str(minbin))
    assert res.returncode == 0, res.stderr
    assert "UNLOCKED" in res.stdout
    assert "codebase-memory-mcp ARGS:" in log.read_text()


# ── 3. resource caps ──────────────────────────────────────────────────────


def test_scope_path_passes_all_properties(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    calls = slog.read_text()
    assert "MemoryMax=2G" in calls
    assert "MemorySwapMax=0" in calls
    assert "IOWeight=20" in calls
    assert "CPUQuota=200%" in calls
    assert "--scope" in calls
    assert "codebase-memory-mcp ARGS:" in log.read_text()  # tool actually ran


def test_env_overrides_reach_scope(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_MEMORY_MAX": "512M",
                   "CODE_INTEL_INDEX_IO_WEIGHT": "5",
                   "CODE_INTEL_INDEX_CPU_QUOTA": "100%"},
    )
    assert res.returncode == 0, res.stderr
    calls = slog.read_text()
    assert "MemoryMax=512M" in calls
    assert "IOWeight=5" in calls
    assert "CPUQuota=100%" in calls


def test_probe_failure_falls_back_to_rlimit(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=False)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    assert slog.read_text().count("\n") == 1  # probe attempted exactly once
    assert "ULIMIT_V:2097152" in log.read_text()  # 2G in KB


def test_no_systemd_fallback_applies_rlimit(tmp_path):
    minbin = _minimal_path(tmp_path)
    log = tmp_path / "tools.log"
    _fake_tools(minbin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=str(minbin))
    assert res.returncode == 0, res.stderr
    assert "ULIMIT_V:2097152" in log.read_text()


# ── guardrail: no raw index spawns outside the entrypoint ─────────────────

_ALLOWED = {
    Path("scripts/lib/code_intel_index.sh"),
    Path("tests/test_scripts/test_code_intel_index.py"),
}
_SCAN_DIRS = ("scripts", "src", "tests", "config", ".claude")
_CODE_SUFFIXES = {
    ".py", ".sh", ".bash", ".js", ".ts", ".json", ".yaml", ".yml",
    ".service", ".timer", ".template",
}


def _scannable_files():
    for d in _SCAN_DIRS:
        base = _REPO_ROOT / d
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or "node_modules" in f.parts:
                continue
            if f.suffix in _CODE_SUFFIXES or (
                not f.suffix and os.access(f, os.X_OK)
            ):
                yield f


def test_no_raw_index_spawns_outside_entrypoint():
    """THE enforcement mechanism: every code-intel index spawn must route
    through scripts/lib/code_intel_index.sh. A raw ``cli index_repository``
    or ``gitnexus analyze`` spawn re-creates the D-state I/O-storm incident
    (three concurrent uncapped indexers → container wedged)."""
    violations = []
    for f in _scannable_files():
        rel = f.relative_to(_REPO_ROOT)
        if rel in _ALLOWED:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # comments are fine
            if "``" in stripped and '"' not in stripped and "'" not in stripped:
                continue  # docstring prose citing commands (no quoted argv)
            if "index_repository" in line:
                violations.append(f"{rel}:{lineno}: raw index_repository spawn")
            elif "gitnexus" in line and "analyze" in line:
                violations.append(f"{rel}:{lineno}: raw gitnexus analyze spawn")
    assert not violations, (
        "Raw code-intel index spawn(s) found — route them through "
        "scripts/lib/code_intel_index.sh (worktree-skip + flock + resource "
        "caps). Offenders:\n" + "\n".join(violations)
    )


def test_entrypoint_exists_and_is_executable():
    assert _ENTRYPOINT.is_file()
    assert os.access(_ENTRYPOINT, os.X_OK)


def test_all_wired_call_sites_reference_entrypoint():
    """The five known spawn sites must reference the entrypoint (coverage
    guardrail: don't trust the design's call-site list)."""
    for rel in (
        "scripts/setup_claude_config.py",
        "scripts/hooks/post-commit",
        "scripts/install.sh",
        "scripts/bootstrap.sh",
        "src/genesis/surplus/jobs/gitnexus.py",
    ):
        assert "code_intel_index.sh" in (_REPO_ROOT / rel).read_text(), rel
