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
    """Fake codebase-memory-mcp + gitnexus recording args and ulimit -v."""
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


_FAKE_CONTAINER_BYTES = 32 * 1024**3  # 32 GiB — big enough that the target wins


def _fake_cgroup_tree(tmp_path: Path, *levels: int) -> tuple[Path, Path]:
    """Build a fake cgroup v2 tree and return ``(root, self_cgroup_file)``.

    ``levels`` are memory.max byte values from the ROOT downward; 0 means "max"
    (uncapped) at that level. The leaf is the deepest level, which is what
    /proc/self/cgroup points at.

    A TREE, not a single file, because the cap is derived by walking this
    process's whole cgroup chain and taking the smallest finite limit — nested
    v2 limits only restrict further, so a constrained ANCESTOR binds before the
    root. Pinning only the root could not express that case at all.

    Named by the level values: a single shared path collides, because _run_entry
    builds its default env AFTER a test has written its own smaller tree and
    would silently overwrite it — the test would then exercise the default while
    believing it had set something else.
    """
    tag = "-".join(str(x) for x in levels) or "default"
    root = tmp_path / f"cg-{tag}"
    rel_parts = [f"level{i}" for i in range(1, len(levels))]
    d = root
    for i, val in enumerate(levels):
        if i:
            d = d / rel_parts[i - 1]
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.max").write_text(f"{val}\n" if val else "max\n", encoding="utf-8")
    selfcg = tmp_path / f"selfcgroup-{tag}"
    selfcg.write_text("0::/" + "/".join(rel_parts) + "\n", encoding="utf-8")
    return root, selfcg


def _fake_cgroup_env(tmp_path: Path, *levels: int) -> dict[str, str]:
    """The two env vars pinning cap derivation to a fake tree."""
    root, selfcg = _fake_cgroup_tree(tmp_path, *(levels or (_FAKE_CONTAINER_BYTES,)))
    return {
        "CODE_INTEL_FAKE_CGROUP_ROOT": str(root),
        "CODE_INTEL_FAKE_CGROUP_SELF": str(selfcg),
    }


def _run_entry(tmp_path: Path, *args, path: str, env_extra=None, **popen_kw):
    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        # Pin the cgroup chain so cap derivation is deterministic; individual
        # tests override it to exercise the bound.
        **_fake_cgroup_env(tmp_path),
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
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra=_fake_cgroup_env(tmp_path, 4 * 1024**3),
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
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra=_fake_cgroup_env(tmp_path, 1 * 1024**3),
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
    res = _run_entry(
        tmp_path, repo, "cbm", path=str(minbin),
        env_extra=_fake_cgroup_env(tmp_path, 4 * 1024**3),
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


def _derive_mem_max(limit_bytes: str | None, tmp_path, *ancestors: str) -> str:
    """Run the script's own _derive_mem_max against a faked cgroup chain.

    Sources the real function rather than restating its arithmetic — a test that
    reimplements the code under test passes while production stays broken.

    ``limit_bytes`` is the ROOT level; ``ancestors`` are further levels below it,
    innermost last, so a constrained intermediate slice can be expressed. ``None``
    at the root means the file is absent entirely.
    """
    src = _ENTRYPOINT.read_text()
    start = src.index("_CI_MEM_TARGET_MB=")
    end = src.index("MEM_MAX=", start)
    body = src[start:end]

    root = tmp_path / "cg"
    root.mkdir(exist_ok=True)
    if limit_bytes is not None:
        (root / "memory.max").write_text(limit_bytes)
    d, rel = root, []
    for i, val in enumerate(ancestors, 1):
        d = d / f"level{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.max").write_text(val)
        rel.append(f"level{i}")
    selfcg = tmp_path / "selfcg"
    selfcg.write_text("0::/" + "/".join(rel) + "\n")

    res = subprocess.run(
        ["bash", "-c", body + "\n_derive_mem_max"],
        capture_output=True, text=True, timeout=30,
        env={
            "PATH": _SYSTEM_PATH,
            "CODE_INTEL_FAKE_CGROUP_ROOT": str(root),
            "CODE_INTEL_FAKE_CGROUP_SELF": str(selfcg),
        },
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_cap_is_bounded_by_the_smallest_finite_limit_on_the_chain():
    """A constrained ANCESTOR binds before the container root does.

    cgroup v2 nested limits only restrict further and are enforced across the
    subtree, so a 3 GiB user slice inside a 32 GiB container is the real ceiling.
    Reading only /sys/fs/cgroup/memory.max derived 4096M for a scope that could
    never fire before its own slice OOMed — strictly worse than the 2 GiB scope it
    replaced, which at least isolated. This repo already walks the chain the same
    way for pids.max (_collect_pid_budget in observability/snapshots/infrastructure).
    """
    import tempfile
    gib = 1024 * 1024 * 1024
    with tempfile.TemporaryDirectory(dir="/home/ubuntu/tmp") as td:
        # Root is generous; an intermediate slice is not. 3 GiB - 2 GiB reserve.
        assert _derive_mem_max(str(32 * gib), Path(td), str(3 * gib)) == "1024M"
    with tempfile.TemporaryDirectory(dir="/home/ubuntu/tmp") as td:
        # An uncapped intermediate level must not mask the root's real limit.
        assert _derive_mem_max(str(4 * gib), Path(td), "max") == "2048M"
    with tempfile.TemporaryDirectory(dir="/home/ubuntu/tmp") as td:
        # Deepest level binding, several levels down.
        assert _derive_mem_max(str(32 * gib), Path(td), "max", str(5 * gib)) == "3072M"


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


