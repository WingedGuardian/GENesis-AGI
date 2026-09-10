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
    """Fake codebase-memory-mcp + gitnexus recording args, ulimit -v, oom adj.

    The tool logs its OWN inherited ``oom_score_adj`` because that is the actual
    invariant for the kill-order change: the script self-writes the value, and
    what has to be true is that the INDEXER inherits it (through the scope), not
    merely that the script wrote something.
    """
    bindir.mkdir(exist_ok=True)
    for name in ("codebase-memory-mcp", "gitnexus"):
        _write_exec(
            bindir / name,
            "#!/usr/bin/env bash\n"
            f'echo "{name} ARGS:$*" >> "{log}"\n'
            f'echo "{name} ULIMIT_V:$(ulimit -v)" >> "{log}"\n'
            f'echo "{name} OOM_ADJ:$(cat /proc/self/oom_score_adj 2>/dev/null || echo NA)" >> "{log}"\n'
            + (f"sleep {sleep}\n" if sleep else ""),
        )


def _inherited_oom_adj() -> str:
    """This process's own oom_score_adj — what a child INHERITS absent a write.

    Asserting a literal "0" was wrong: it is true on a developer box and NOT on a
    GitHub runner, which is the reference "different install" the install-agnostic
    test rule points at (it failed there, 2 cells, while the mechanism itself
    passed). The invariant is "the value did not CHANGE", not "the value is zero".
    """
    return Path("/proc/self/oom_score_adj").read_text().strip()


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


_FAKE_CONTAINER_BYTES = 32 * 1024**3  # 32 GiB — big enough that the target wins


def _fake_cgroup_limit(tmp_path: Path, total_bytes: int = _FAKE_CONTAINER_BYTES) -> Path:
    """A stand-in for /sys/fs/cgroup/memory.max.

    Required for determinism, not convenience. The cap is now derived as
    min(measured target, a fraction of the container, container - reserve), so a
    bare assertion of "4096M" silently depends on the size of whatever machine
    runs the suite: on a container below ~6.8 GiB production correctly derives
    LESS, and the test would fail while the code was right. Pinning the input is
    what makes these cells environment-independent, which the module docstring
    already claims of the rest of the file.
    """
    # Named by SIZE. A single shared filename collides: _run_entry builds its
    # default env (writing the 32 GiB value) AFTER a test has written its own
    # smaller one, silently overwriting it — so the test would exercise 32 GiB
    # while believing it had set 4 GiB, and pass or fail for the wrong reason.
    p = tmp_path / f"fake_memory_max_{total_bytes}"
    p.write_text(f"{total_bytes}\n", encoding="utf-8")
    return p


def _run_entry(tmp_path: Path, *args, path: str, env_extra=None, **popen_kw):
    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        # Pin the container limit so cap derivation is deterministic; individual
        # tests override it to exercise the bound.
        "CODE_INTEL_FAKE_CGROUP_LIMIT_FILE": str(_fake_cgroup_limit(tmp_path)),
        # Fast, never-pausing watchdog by default so a fast fake tool doesn't
        # idle on the real 15s sample gap; watchdog tests override these.
        "CODE_INTEL_WATCHDOG_INTERVAL": "1",
        "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
        "CODE_INTEL_FAKE_LOADAVG": "0",
        "CODE_INTEL_FAKE_IOWAIT": "0",
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
    assert f"--repo-path {repo}" in out
    assert "--mode fast" in out  # default mode is fast
    assert "--persistence true" in out
    assert re.search(r"gitnexus ARGS:analyze\b", out)
    assert "--quiet" not in out  # #910's bogus flag removed (gitnexus 1.6 has none)


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
    assert "gitnexus ARGS:analyze" in out
    assert "--quiet" not in out
    assert "codebase-memory-mcp ARGS:" not in out


def test_missing_requested_tools_return_rc3(tmp_path):
    # A REQUESTED tool absent from PATH must be rc 3 — never a false success, or
    # the idle runner consumes the marker + stamps a fresh full-index timestamp,
    # silently disabling indexing until someone notices the graph is stale.
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "both", path=str(_minimal_path(tmp_path)))
    assert res.returncode == 3, res.stderr
    assert "codebase-memory-mcp not on PATH" in res.stdout
    assert "gitnexus not available" in res.stdout
    assert "missing from PATH" in res.stdout


def test_mode_arg_reaches_cbm(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", "full", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    assert "--mode full" in log.read_text()


def test_mode_env_default_used_when_arg_absent(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
                     env_extra={"CODE_INTEL_INDEX_MODE": "moderate"})
    assert res.returncode == 0, res.stderr
    assert "--mode moderate" in log.read_text()


def test_bad_mode_errors(tmp_path):
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", "turbo", path=_SYSTEM_PATH)
    assert res.returncode == 1
    assert "fast|moderate|full" in res.stdout


def test_persistence_env_override(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
                     env_extra={"CODE_INTEL_INDEX_PERSISTENCE": "false"})
    assert res.returncode == 0, res.stderr
    assert "--persistence false" in log.read_text()


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


def test_lock_skip_rc_override(tmp_path):
    # The runner sets CODE_INTEL_INDEX_LOCK_SKIP_RC=75 so it can tell "lock held
    # / host-frozen — keep the marker" apart from a real success (rc 0). The lock
    # ACQUISITION is byte-unchanged, so the host freeze still neutralizes it.
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    lock_file = _lock_file_for(tmp_path, repo)
    with open(lock_file, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}",
                         env_extra={"CODE_INTEL_INDEX_LOCK_SKIP_RC": "75"})
    assert res.returncode == 75, res.stderr
    assert not log.exists()


def test_parallel_double_invocation_exactly_one_runs(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log, sleep=2)
    repo = _make_repo(tmp_path)
    env = {"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(tmp_path),
           "GENESIS_HOME": str(tmp_path / ".genesis"),
           # Quiet, non-pausing watchdog so the fake tool isn't paused by the
           # test host's real load (this test bypasses the _run_entry defaults).
           "CODE_INTEL_WATCHDOG_INTERVAL": "1",
           "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
           "CODE_INTEL_FAKE_LOADAVG": "0",
           "CODE_INTEL_FAKE_IOWAIT": "0"}
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
    assert "MemoryMax=4096M" in calls  # measured target, emitted as M (#1776)
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
    assert "ULIMIT_V:4194304" in log.read_text()  # 4G in KB (measured default)


def test_no_systemd_fallback_applies_rlimit(tmp_path):
    minbin = _minimal_path(tmp_path)
    log = tmp_path / "tools.log"
    _fake_tools(minbin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=str(minbin))
    assert res.returncode == 0, res.stderr
    assert "ULIMIT_V:4194304" in log.read_text()  # 4G in KB


# ── 3b. OOM kill-order preference ─────────────────────────────────────────
# The larger measured MemoryMax makes this job a bigger consumer, so it must
# also become the kernel's PREFERRED victim — otherwise a bigger cap makes a
# container-wide OOM more likely to take the server or a CC session instead.
# CORRECTED 2026-09-09 (this comment previously had it wrong): raising needs no
# privilege, and neither does LOWERING, as long as the target stays at or above
# oom_score_adj_min (0 here, inherited from init). MEASURED directly: 500 -> 321
# accepted, 321 -> 0 accepted, only -1 refused. So the kernel does NOT enforce a
# raise-only contract — the script has to, which is what
# test_oom_score_adj_keeps_a_higher_inherited_value pins.


def test_oom_score_adj_reaches_the_indexer(tmp_path):
    """The INDEXER must inherit the raised value, not merely the script.

    `-p OOMScoreAdjust=` is invalid on `systemd-run --scope` (a scope does not
    exec, so Exec properties do not apply), so the mechanism is a self-write
    plus inheritance. What has to hold is that the tool actually sees it.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:900" in log.read_text()  # ABOVE invoker.py's 500 for CC subprocesses
    # And NOT passed as a scope property, which systemd would reject outright.
    assert "OOMScoreAdjust" not in slog.read_text()


def test_oom_score_adj_override_reaches_the_indexer(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "321"},
    )
    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:321" in log.read_text()


def test_oom_score_adj_non_numeric_warns_and_still_indexes(tmp_path):
    """A bad lever value must never cost the INDEX — only the preference.

    The direction control for the cell above: without it, a guard that refused
    every value would pass that test's sibling and silently disable the feature.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "not-a-number"},
    )
    assert res.returncode == 0, res.stderr
    assert "ignoring non-numeric" in res.stdout
    assert "codebase-memory-mcp ARGS:" in log.read_text()  # index still ran
    # unchanged from what this process would pass down — not a literal 0
    assert f"OOM_ADJ:{_inherited_oom_adj()}" in log.read_text()


def test_oom_score_adj_negative_is_refused_not_attempted(tmp_path):
    """A negative value is unachievable from a user manager anyway (measured:
    -1 and -500 both land on the manager's own value), so the guard rejects it
    at the lever rather than writing and failing.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "-500"},
    )
    assert res.returncode == 0, res.stderr
    assert "ignoring non-numeric" in res.stdout
    assert f"OOM_ADJ:{_inherited_oom_adj()}" in log.read_text()  # unchanged


def test_oom_score_adj_keeps_a_higher_inherited_value(tmp_path):
    """Raise-only must be enforced in code, because the kernel does not enforce it.

    MEASURED: an unprivileged task may LOWER oom_score_adj freely as long as it
    stays >= oom_score_adj_min (0 here) — 500 -> 321 and 321 -> 0 both succeed.
    So an unconditional write of the 900 default would quietly UNDO a parent that
    had deliberately raised this job to 1000, making it less likely to be chosen
    than the parent intended. The script reads the current value and keeps it when
    it is already at least what was requested.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    # Raise this process first; the entrypoint inherits it.
    Path("/proc/self/oom_score_adj").write_text("1000\n")
    try:
        res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
    finally:
        Path("/proc/self/oom_score_adj").write_text("0\n")
    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:1000" in log.read_text(), (
        "the inherited 1000 was lowered to the 900 default — raise-only broken"
    )
    assert "inherited" in res.stdout and "raise-only" in res.stdout


def test_oom_score_adj_oversized_value_is_rejected_not_wrapped(tmp_path):
    """All-digit is not in-range. $((10#$v)) WRAPS past bash's signed 64-bit
    range, so this value evaluates to 0, would sail through a `> 1000` check, and
    would be written and logged as accepted — a silent downgrade to the least
    preferred setting, from an input that looks like an obvious typo.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "18446744073709551616"},
    )
    assert res.returncode == 0, res.stderr
    assert "exceeds the kernel maximum" in res.stdout
    assert f"OOM_ADJ:{_inherited_oom_adj()}" in log.read_text()  # untouched
    assert "codebase-memory-mcp ARGS:" in log.read_text()  # index still ran


def test_success_log_does_not_quote_other_units_oom_scores(tmp_path):
    """The log line must not name scores this script does not own.

    It used to read "the server at 100" while the shipped unit template declared
    -500 — a number that existed nowhere, presenting a kill ordering that was not
    real. An operational log that invents its own facts is worse than a terse one,
    because OOM diagnosis is exactly when someone trusts it.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    assert "oom_score_adj=900" in res.stdout
    for claim in ("the server at 100", "at 500", "at 0)"):
        assert claim not in res.stdout, f"log still asserts a foreign score: {claim!r}"


# ── 3c. the cap is bounded by the container ────────────────────────────────
# A cap equal to the container limit is not a cap: the parent cgroup reaches its
# own OOM before the scope boundary is ever hit, taking the server or a session
# with it. host-setup.sh floors an install at 4 GiB, so an absolute 4G default
# was exactly that on a minimum install.


def test_cap_leaves_a_reserve_on_a_small_container(tmp_path):
    """On a 4 GiB install the cap must stay well below the limit.

    The container fraction alone was not sufficient: 60% of 4 GiB is 2,457M,
    leaving 1,639M for genesis-server + Qdrant + a session together. The reserve
    states the invariant directly, and lands this install on 2048M — no worse
    than the pre-existing 2G default, so nothing regresses for small hosts.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)
    limit = _fake_cgroup_limit(tmp_path, 4 * 1024**3)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_FAKE_CGROUP_LIMIT_FILE": str(limit)},
    )
    assert res.returncode == 0, res.stderr
    assert "MemoryMax=2048M" in slog.read_text(), slog.read_text()


def test_cap_never_emits_a_nonpositive_value(tmp_path):
    """A container smaller than the reserve must not produce "0M" or a negative.

    systemd would reject a malformed value and the scope would then carry NO cap
    at all — failing open to something strictly worse than the bug. A small
    positive floor keeps the scope real; such a host cannot run this index
    regardless, and being killed at its own scope is the correct outcome.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)
    limit = _fake_cgroup_limit(tmp_path, 1 * 1024**3)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_FAKE_CGROUP_LIMIT_FILE": str(limit)},
    )
    assert res.returncode == 0, res.stderr
    calls = slog.read_text()
    assert "MemoryMax=256M" in calls, calls
    assert "MemoryMax=0M" not in calls and "MemoryMax=-" not in calls


def test_cap_is_derived_without_depending_on_PATH(tmp_path):
    """The limit is read with a shell BUILTIN, not `cat`.

    This entrypoint runs with a minimal PATH in the no-systemd fallback (the
    environment _minimal_path builds). With `cat` absent the read failed, emptied
    the value, and returned the UNBOUNDED target — restoring a cap equal to the
    parent limit on precisely the constrained install the bound protects. A
    builtin cannot go missing.
    """
    minbin = _minimal_path(tmp_path)
    log = tmp_path / "tools.log"
    _fake_tools(minbin, log)
    repo = _make_repo(tmp_path)
    limit = _fake_cgroup_limit(tmp_path, 4 * 1024**3)
    res = _run_entry(
        tmp_path, repo, "cbm", path=str(minbin),
        env_extra={"CODE_INTEL_FAKE_CGROUP_LIMIT_FILE": str(limit)},
    )
    assert res.returncode == 0, res.stderr
    # 2048M in KB — the BOUNDED value, proving the read succeeded without `cat`.
    assert "ULIMIT_V:2097152" in log.read_text(), log.read_text()


# ── 4. pressure watchdog ──────────────────────────────────────────────────


def test_watchdog_wall_cap_kills_stuck_index(tmp_path):
    """A tool that never finishes is SIGKILLed at the wall cap — a runaway index
    can't hold the box hostage (the incident's failure mode)."""
    import time
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_exec(fakebin / "codebase-memory-mcp", "#!/usr/bin/env bash\nsleep 60\n")
    repo = _make_repo(tmp_path)
    t0 = time.time()
    res = _run_entry(
        tmp_path, repo, "cbm", "fast", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_WATCHDOG_WALL_FAST": "2",
                   "CODE_INTEL_WATCHDOG_INTERVAL": "1",
                   "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
                   "CODE_INTEL_FAKE_LOADAVG": "0"},
    )
    elapsed = time.time() - t0
    assert res.returncode != 0, "a killed index must report failure"
    assert elapsed < 30, f"wall cap did not kill a 60s tool (took {elapsed:.0f}s)"
    assert "wall cap" in res.stdout


def test_watchdog_pauses_under_pressure(tmp_path):
    """High load pauses the running index (cgroup freeze / SIGSTOP) — the only
    working I/O throttle on this host."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_exec(fakebin / "codebase-memory-mcp", "#!/usr/bin/env bash\nsleep 20\n")
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", "fast", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_WATCHDOG_WALL_FAST": "4",
                   "CODE_INTEL_WATCHDOG_INTERVAL": "1",
                   "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
                   "CODE_INTEL_FAKE_LOADAVG": "99"},
    )
    assert "pausing index" in res.stdout


def test_watchdog_full_mode_uses_longer_wall_cap(tmp_path):
    """full mode reads the FULL wall cap, not the fast one — a legit full rebuild
    (throttled, duty-cycled) is given time the fast cap wouldn't allow."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_exec(fakebin / "codebase-memory-mcp", "#!/usr/bin/env bash\nsleep 3\n")
    repo = _make_repo(tmp_path)
    # A 1s FAST cap would kill a 3s tool; the 30s FULL cap lets it finish.
    res = _run_entry(
        tmp_path, repo, "cbm", "full", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_WATCHDOG_WALL_FAST": "1",
                   "CODE_INTEL_WATCHDOG_WALL_FULL": "30",
                   "CODE_INTEL_WATCHDOG_INTERVAL": "1",
                   "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
                   "CODE_INTEL_FAKE_LOADAVG": "0"},
    )
    assert res.returncode == 0, res.stdout
    assert "wall cap" not in res.stdout


# ── guardrail: no raw index spawns outside the entrypoint ─────────────────

_ALLOWED = {
    Path("scripts/lib/code_intel_index.sh"),
    Path("scripts/code_intel_runner.sh"),  # the sole entrypoint caller (queue consumer)
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


def test_runner_is_the_sole_entrypoint_caller():
    """After the queue conversion the idle-gated runner is the ONLY thing that
    invokes the locked entrypoint; every former trigger just enqueues a marker."""
    runner = _REPO_ROOT / "scripts" / "code_intel_runner.sh"
    assert "code_intel_index.sh" in runner.read_text()


def test_triggers_enqueue_markers_and_do_not_spawn():
    """Coverage guardrail (don't trust the design's call-site list): every former
    spawn site now writes an index-request marker and must NOT invoke the
    entrypoint directly — a per-commit/setup spawn is what stormed the box."""
    for rel in (
        "scripts/setup_claude_config.py",
        "scripts/hooks/post-commit",
        "scripts/install.sh",
        "src/genesis/surplus/jobs/gitnexus.py",
    ):
        text = (_REPO_ROOT / rel).read_text()
        assert "index_marker" in text, f"{rel} must enqueue a marker"
        assert "code_intel_index.sh" not in text, (
            f"{rel} must NOT spawn the entrypoint directly — enqueue a marker"
        )


# ── 3c. cap bounded by the container, and adj normalisation ──────────────────
# Both from Codex P1/P2 on this PR. The cap was shipped as an absolute 4G, which
# EQUALS the container limit on a minimum install (host-setup.sh floors an
# install at 4 GiB) — a cap equal to the whole container isolates nothing.


def _derive_mem_max(limit_bytes: str | None, tmp_path) -> str:
    """Run the script's own _derive_mem_max against a faked cgroup limit.

    Sources the real function rather than restating its arithmetic — a test that
    reimplements the code under test passes while production stays broken.
    """
    src = _ENTRYPOINT.read_text()
    start = src.index("_CI_MEM_TARGET_MB=")
    end = src.index("MEM_MAX=", start)
    body = src[start:end]
    fake_cgroup = tmp_path / "cg"
    fake_cgroup.mkdir(exist_ok=True)
    if limit_bytes is not None:
        (fake_cgroup / "memory.max").write_text(limit_bytes)
    # Point the function's first candidate path at the fake.
    body = body.replace("/sys/fs/cgroup/memory.max", str(fake_cgroup / "memory.max"))
    body = body.replace("/sys/fs/cgroup/memory/memory.limit_in_bytes", str(fake_cgroup / "nope"))
    res = subprocess.run(
        ["bash", "-c", body + "\n_derive_mem_max"], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_cap_is_bounded_by_the_container_limit(tmp_path):
    """A minimum install must not get a cap equal to its whole container."""
    gib = 1024 * 1024 * 1024
    # 4 GiB minimum install: the absolute 4096M target would BE the container.
    # 2048M, not 60%-of-4GiB (2457M): the reserve is the binding constraint here.
    # 2457M would leave only 1,639M for genesis-server + Qdrant + a session
    # together, so the parent cgroup could still OOM before the scope boundary —
    # which is the whole failure this bound exists to prevent. See _CI_MEM_RESERVE_MB.
    assert _derive_mem_max(str(4 * gib), tmp_path) == "2048M"
    # 32 GiB: the measured target is well under the bound, so it stands.
    assert _derive_mem_max(str(32 * gib), tmp_path) == "4096M"
    # Uncapped container ("max") or unreadable: nothing bounds us, target stands.
    assert _derive_mem_max("max", tmp_path) == "4096M"
    assert _derive_mem_max(None, tmp_path) == "4096M"


def test_cap_is_emitted_as_M_so_the_rlimit_fallback_can_parse_it(tmp_path):
    """The bound must never be expressed as a percentage.

    The rlimit fallback parses only <int>[.frac]G|M; a '%' falls through to
    "running memory-uncapped", failing OPEN to something worse than the bug.
    """
    gib = 1024 * 1024 * 1024
    for limit in (str(4 * gib), str(32 * gib), "max", None):
        out = _derive_mem_max(limit, tmp_path)
        assert out.endswith("M"), out
        assert "%" not in out


def test_oom_score_adj_is_above_the_cc_subprocess_rung(tmp_path):
    """500 would TIE with CC subprocesses, which invoker.py already sets to 500.

    At equal adj the kernel falls back to memory charge, so a large session could
    be chosen over the indexer — defeating the ordering this feature exists for.
    """
    invoker = (_REPO_ROOT / "src/genesis/cc/invoker.py").read_text()
    assert "def set_oom_score_adj(pid: int, score: int = 500)" in invoker, (
        "invoker's CC-subprocess rung moved; the index adj must stay strictly above it"
    )
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 0, res.stderr
    logged = log.read_text()
    adj = int(re.search(r"OOM_ADJ:(\d+)", logged).group(1))
    assert adj > 500, f"index adj {adj} does not outrank CC subprocesses at 500"


def test_zero_padded_adj_is_not_applied_as_octal(tmp_path):
    """MEASURED: writing '0500' makes the kernel apply 320 (base autodetection).

    The old guard accepted zero-padded digits and passed the raw string through,
    so an operator's '0500' silently WEAKENED the preference while the log echoed
    '0500' back as if it had taken.
    """
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "0500"},
    )
    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:500" in log.read_text()  # 500, NOT octal 320
    assert "oom_score_adj=500" in res.stdout  # and the log reports what was written


def test_adj_above_the_kernel_maximum_is_refused(tmp_path):
    """Out-of-range must be rejected at the lever, not written and misapplied."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "5000"},
    )
    assert res.returncode == 0, res.stderr
    assert "exceeds the kernel maximum" in res.stdout
    assert "codebase-memory-mcp ARGS:" in log.read_text()  # index still ran
    assert f"OOM_ADJ:{_inherited_oom_adj()}" in log.read_text()  # unchanged
