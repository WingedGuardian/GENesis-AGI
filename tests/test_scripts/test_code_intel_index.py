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
    _write_exec(
        bindir / "node",
        '#!/usr/bin/env bash\necho "${FAKE_NODE_VERSION:-v22.22.2}"\n',
    )
    for name in ("codebase-memory-mcp", "gitnexus"):
        version_guard = (
            'if [ "${1:-}" = "--version" ]; then '
            'echo "${FAKE_GITNEXUS_VERSION:-1.6.12}"; exit 0; fi\n'
            if name == "gitnexus"
            else ""
        )
        _write_exec(
            bindir / name,
            "#!/usr/bin/env bash\n"
            + version_guard
            + f'echo "{name} ARGS:$*" >> "{log}"\n'
            f'echo "{name} ULIMIT_V:$(ulimit -v)" >> "{log}"\n'
            f'echo "{name} OOM_ADJ:$(</proc/self/oom_score_adj)" >> "{log}"\n'
            + (f"sleep {sleep}\n" if sleep else ""),
        )


def _inherited_oom_adj() -> str:
    """This process's own oom_score_adj — what a child INHERITS absent a write.

    Asserting a literal "0" is wrong: it is true on a developer box and NOT on a
    GitHub runner (measured: a runner starts at 500). The invariant is "the value
    did not CHANGE", not "the value is zero".
    """
    return Path("/proc/self/oom_score_adj").read_text().strip()


def _fake_systemd_run(bindir: Path, log: Path, *, probe_ok: bool = True) -> None:
    """Fake systemd-run: logs argv, then execs the command after ``--``."""
    bindir.mkdir(exist_ok=True)
    if probe_ok:
        body = (
            "#!/usr/bin/env bash\n"
            f'echo "$*" >> "{log}"\n'
            f'echo "SYSTEMD_RUN_OOM_ADJ:$(cat /proc/self/oom_score_adj)" >> "{log}"\n'
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
        # Fast, never-pausing watchdog by default so a fast fake tool doesn't
        # idle on the real 15s sample gap; watchdog tests override these.
        "CODE_INTEL_WATCHDOG_INTERVAL": "1",
        "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
        "CODE_INTEL_FAKE_LOADAVG": "0",
        "CODE_INTEL_FAKE_IOWAIT": "0",
        # Deterministic admission-control seams: without them the entrypoint
        # reads the TEST HOST's cgroup ceiling and live usage, so a small CI
        # container refuses gitnexus legs (rc 3) or trims caps differently than
        # the tests assert. Generous defaults; tests override via env_extra.
        "CODE_INTEL_MEM_CEILING_BYTES": str(64 * 1024**3),
        "CODE_INTEL_MEM_CURRENT_BYTES": str(512 * 1024**2),
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


def _fake_v2_cgroup(
    tmp_path: Path,
    *,
    membership: str,
    mount_root: str = "/",
    limits: dict[str, str | None],
) -> dict[str, str]:
    mount = tmp_path / "cgroup2"
    mount.mkdir()
    for rel, value in limits.items():
        directory = mount / rel.lstrip("/")
        directory.mkdir(parents=True, exist_ok=True)
        if value is not None:
            (directory / "memory.max").write_text(f"{value}\n")
    self_cgroup = tmp_path / "self.cgroup"
    self_cgroup.write_text(f"0::{membership}\n")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"35 24 0:31 {mount_root} {mount} rw,nosuid,nodev,noexec,relatime"
        " - cgroup2 cgroup rw\n"
    )
    return {
        "CODE_INTEL_MEM_CEILING_BYTES": "",
        "CODE_INTEL_CGROUP_SELF": str(self_cgroup),
        "CODE_INTEL_CGROUP_MOUNTINFO": str(mountinfo),
    }


def _fake_v1_cgroup(
    tmp_path: Path,
    *,
    membership: str,
    mount_root: str,
    limits: dict[str, str],
) -> dict[str, str]:
    mount = tmp_path / "memory-controller"
    mount.mkdir()
    for rel, value in limits.items():
        directory = mount / rel.lstrip("/")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "memory.limit_in_bytes").write_text(f"{value}\n")
    self_cgroup = tmp_path / "self.cgroup"
    self_cgroup.write_text(f"7:cpu,memory:{membership}\n")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"42 24 0:38 {mount_root} {mount} rw,nosuid,nodev,noexec,relatime"
        " - cgroup cgroup rw,cpu,memory\n"
    )
    return {
        "CODE_INTEL_MEM_CEILING_BYTES": "",
        "CODE_INTEL_CGROUP_SELF": str(self_cgroup),
        "CODE_INTEL_CGROUP_MOUNTINFO": str(mountinfo),
    }


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


def test_cbm_disable_sentinel_blocks_index_spawn(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    disable_file = tmp_path / "codebase-memory-mcp.disabled"
    disable_file.write_text("incident freeze\n")
    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODEBASE_MEMORY_MCP_DISABLE_FILE": str(disable_file)},
    )
    # rc 3, not 0: a requested-but-skipped leg is not a success — rc 0 would
    # let the runner consume the marker and stamp cbm's shared full clock for
    # work that never ran.
    assert res.returncode == 3, res.stderr
    assert f"disabled by {disable_file}" in res.stdout
    assert not log.exists()


def test_cbm_disable_does_not_poison_successful_gitnexus_leg(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    disable_file = tmp_path / "codebase-memory-mcp.disabled"
    disable_file.write_text("incident freeze\n")
    res = _run_entry(
        tmp_path,
        repo,
        "both",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODEBASE_MEMORY_MCP_DISABLE_FILE": str(disable_file)},
    )
    # rc 5: gitnexus leg done, cbm leg skipped — the runner must consume the
    # marker WITHOUT stamping cbm's shared full-success clock.
    assert res.returncode == 5, res.stderr
    out = log.read_text()
    assert "codebase-memory-mcp ARGS:" not in out
    assert "gitnexus ARGS:analyze" in out


def test_cbm_done_gitnexus_refused_is_partial_rc4(tmp_path):
    """cbm done + gitnexus refused must NOT be rc 3: the runner restores an
    rc-3 marker without penalty, which rebuilt the completed cbm leg on every
    idle tick forever. rc 4 says 'consume what ran'."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    (fakebin / "gitnexus").unlink()  # cbm present, gitnexus absent
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 4, res.stderr
    assert "codebase-memory-mcp ARGS:" in log.read_text()


def test_cbm_done_gitnexus_failed_is_partial_rc4(tmp_path):
    """cbm done + gitnexus leg FAILED (not merely skipped) is still rc 4:
    the completed leg must be consumable, not restored as a whole-request
    failure that rebuilds cbm on every retry."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    _write_exec(
        fakebin / "gitnexus",
        '#!/usr/bin/env bash\n'
        'if [ "${1:-}" = "--version" ]; then echo "1.6.12"; exit 0; fi\n'
        f'echo "gitnexus ARGS:$*" >> "{log}"\n'
        "exit 7\n",
    )
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 4, res.stderr
    assert "codebase-memory-mcp ARGS:" in log.read_text()
    assert "gitnexus ARGS:" in log.read_text()  # ran and failed — not missing


def test_cbm_failed_gitnexus_done_is_partial_rc5(tmp_path):
    """The inverse: cbm fails, gitnexus succeeds — rc 5, so gitnexus's work is
    consumed and cbm's shared full clock is not stamped for work that errored."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    _write_exec(
        fakebin / "codebase-memory-mcp",
        '#!/usr/bin/env bash\n'
        f'echo "codebase-memory-mcp ARGS:$*" >> "{log}"\n'
        "exit 9\n",
    )
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 5, res.stderr
    assert "gitnexus ARGS:analyze" in log.read_text()


def test_both_legs_failed_keeps_failure_rc(tmp_path):
    """When NO requested leg completed there is no partial outcome: the raw
    failure rc is preserved so the runner applies the attempts penalty."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    _write_exec(
        fakebin / "codebase-memory-mcp",
        '#!/usr/bin/env bash\n'
        f'echo "codebase-memory-mcp ARGS:$*" >> "{log}"\n'
        "exit 9\n",
    )
    _write_exec(
        fakebin / "gitnexus",
        '#!/usr/bin/env bash\n'
        'if [ "${1:-}" = "--version" ]; then echo "1.6.12"; exit 0; fi\n'
        f'echo "gitnexus ARGS:$*" >> "{log}"\n'
        "exit 7\n",
    )
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "both", path=f"{fakebin}:{_SYSTEM_PATH}")
    assert res.returncode == 7, res.stderr  # last leg's failure code, not 3/4/5


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
    assert "missing or refused" in res.stdout


def test_wrong_gitnexus_version_is_refused(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "gitnexus",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"FAKE_GITNEXUS_VERSION": "1.6.8"},
    )
    assert res.returncode == 3
    assert "expected pinned 1.6.12" in res.stdout
    assert not log.exists()


def test_unsupported_node_version_refuses_gitnexus(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "gitnexus",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"FAKE_NODE_VERSION": "v23.11.1"},
    )
    assert res.returncode == 3
    assert "does not support Node v23.11.1" in res.stdout
    assert not log.exists()


def test_gitnexus_runs_from_npm_prefix_when_prefix_is_not_on_path(tmp_path):
    path_bin = tmp_path / "path-bin"
    prefix_bin = tmp_path / "npm-prefix" / "bin"
    path_bin.mkdir()
    prefix_bin.mkdir(parents=True)
    log = tmp_path / "tools.log"
    _write_exec(path_bin / "node", "#!/bin/sh\necho v22.22.2\n")
    _write_exec(
        path_bin / "npm",
        f'#!/bin/sh\necho "{tmp_path / "npm-prefix"}"\n',
    )
    _write_exec(
        prefix_bin / "gitnexus",
        "#!/bin/sh\n"
        'if [ "${1:-}" = --version ]; then echo 1.6.12; exit 0; fi\n'
        f'echo "gitnexus ARGS:$*" > "{log}"\n',
    )
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "gitnexus",
        path=f"{path_bin}:{_SYSTEM_PATH}",
    )
    assert res.returncode == 0, res.stderr
    assert log.read_text() == "gitnexus ARGS:analyze\n"


def test_shadow_gitnexus_installations_conflict_is_refused(tmp_path):
    """Two installed copies at DIFFERENT versions must refuse, not pick one.

    The service's PATH and an interactive client's PATH can order copies
    differently — canonical-order resolution plus a shadow scan keeps a second
    install that already wrote a different storage format from being trusted.
    """
    path_bin = tmp_path / "path-bin"
    shadow_bin = tmp_path / ".npm-global" / "bin"
    path_bin.mkdir()
    shadow_bin.mkdir(parents=True)
    log = tmp_path / "tools.log"
    _write_exec(path_bin / "node", "#!/bin/sh\necho v22.22.2\n")
    body = (
        "#!/bin/sh\n"
        'if [ "${1:-}" = --version ]; then echo %s; exit 0; fi\n'
        f'echo "gitnexus ARGS:$*" >> "{log}"\n'
    )
    _write_exec(shadow_bin / "gitnexus", body % "1.6.8")
    _write_exec(path_bin / "gitnexus", body % "1.6.12")
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "gitnexus",
        path=f"{path_bin}:{_SYSTEM_PATH}",
    )
    assert res.returncode == 3, res.stderr
    assert "gitnexus not available" in res.stdout
    assert not log.exists()


def test_shadow_gitnexus_installations_same_version_resolves(tmp_path):
    """The control case: two copies at the SAME version are not a conflict —
    refusing those would break the common reinstall-into-second-prefix shape."""
    path_bin = tmp_path / "path-bin"
    shadow_bin = tmp_path / ".npm-global" / "bin"
    path_bin.mkdir()
    shadow_bin.mkdir(parents=True)
    log = tmp_path / "tools.log"
    _write_exec(path_bin / "node", "#!/bin/sh\necho v22.22.2\n")
    body = (
        "#!/bin/sh\n"
        'if [ "${1:-}" = --version ]; then echo 1.6.12; exit 0; fi\n'
        f'echo "gitnexus ARGS:$*" >> "{log}"\n'
    )
    _write_exec(shadow_bin / "gitnexus", body)
    _write_exec(path_bin / "gitnexus", body)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "gitnexus",
        path=f"{path_bin}:{_SYSTEM_PATH}",
    )
    assert res.returncode == 0, res.stderr
    assert "gitnexus ARGS:analyze" in log.read_text()


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
    poison = tmp_path / "does-not-exist"
    with open(lock_file, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = _run_entry(
            tmp_path,
            repo,
            "both",
            path=f"{fakebin}:{_SYSTEM_PATH}",
            env_extra={
                "CODE_INTEL_MEM_CEILING_BYTES": "",
                "CODE_INTEL_CGROUP_SELF": str(poison),
                "CODE_INTEL_CGROUP_MOUNTINFO": str(poison),
            },
        )
    assert res.returncode == 0, res.stderr
    assert "already running" in res.stdout
    assert "cgroup" not in res.stdout.lower()
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
    assert "MemoryMax=4G" in calls
    assert "MemorySwapMax=0" in calls
    assert "IOWeight=20" in calls
    assert "CPUQuota=200%" in calls
    assert "--scope" in calls
    assert "codebase-memory-mcp ARGS:" in log.read_text()  # tool actually ran


def test_gitnexus_scope_uses_measured_8g_cap(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "gitnexus",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        # Pin the ceiling AND the live-usage seam: the admission block reads
        # the host cgroup/MemTotal otherwise, so this assertion used to depend
        # on how much RAM the machine running the test happened to have.
        env_extra={
            "CODE_INTEL_MEM_CEILING_BYTES": str(64 * 1024**3),
            "CODE_INTEL_MEM_CURRENT_BYTES": str(512 * 1024**2),
        },
    )
    assert res.returncode == 0, res.stderr
    assert "MemoryMax=8G" in slog.read_text()
    assert "gitnexus ARGS:analyze" in log.read_text()


def test_env_overrides_reach_scope(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_MEMORY_MAX": "5G",
                   "CODE_INTEL_INDEX_IO_WEIGHT": "5",
                   "CODE_INTEL_INDEX_CPU_QUOTA": "100%"},
    )
    assert res.returncode == 0, res.stderr
    calls = slog.read_text()
    assert "MemoryMax=5G" in calls
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
    assert "ULIMIT_V:4194304" in log.read_text()  # 4G in KB


def test_no_systemd_fallback_applies_rlimit(tmp_path):
    minbin = _minimal_path(tmp_path)
    log = tmp_path / "tools.log"
    _fake_tools(minbin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(tmp_path, repo, "cbm", path=str(minbin))
    assert res.returncode == 0, res.stderr
    assert "ULIMIT_V:4194304" in log.read_text()


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


def test_installer_does_not_claim_queue_success_after_writer_failure():
    text = (_REPO_ROOT / "scripts/install.sh").read_text()
    queue = text.split("# Queue initial code intelligence indexing", 1)[1].split(
        "# ═", 1
    )[0]
    assert "index_marker.py\" write" in queue
    assert "|| true" not in queue
    assert "WARNING: could not queue initial code intelligence index" in queue


# ── Codebase Memory batch admission ───────────────────────────────────────


def test_cbm_default_uses_the_measured_four_gibibyte_target(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)

    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")

    assert res.returncode == 0, res.stdout + res.stderr
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert "MemoryMax=4G" in runs[0]


def test_cbm_cap_is_trimmed_to_nested_v2_headroom(tmp_path):
    gib = 1024**3
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v2_cgroup(
        tmp_path,
        membership="/tenant/session",
        limits={
            "tenant/session": str(7 * gib),
            "tenant": str(6 * gib),
            "": None,
        },
    )
    env["CODE_INTEL_MEM_CURRENT_BYTES"] = str(512 * 1024**2)

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    expected = 6 * gib - 512 * 1024**2 - 2 * gib
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert f"MemoryMax={expected}" in runs[0]


def test_cbm_equal_nested_limits_account_for_parent_usage(tmp_path):
    gib = 1024**3
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v2_cgroup(
        tmp_path,
        membership="/tenant/session",
        limits={
            "tenant/session": str(8 * gib),
            "tenant": str(8 * gib),
            "": None,
        },
    )
    env["CODE_INTEL_MEM_CURRENT_BYTES"] = ""
    cgroup = tmp_path / "cgroup2"
    (cgroup / "tenant" / "session" / "memory.current").write_text(f"{gib}\n")
    (cgroup / "tenant" / "memory.current").write_text(f"{3 * gib}\n")

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert f"MemoryMax={3 * gib}" in runs[0]


def test_cbm_accounts_for_pressure_at_every_finite_v2_ancestor(tmp_path):
    gib = 1024**3
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v2_cgroup(
        tmp_path,
        membership="/tenant/session",
        limits={
            "tenant/session": str(7 * gib),
            "tenant": str(8 * gib),
            "": None,
        },
    )
    env["CODE_INTEL_MEM_CURRENT_BYTES"] = ""
    cgroup = tmp_path / "cgroup2"
    (cgroup / "tenant" / "session" / "memory.current").write_text(f"{gib}\n")
    (cgroup / "tenant" / "memory.current").write_text(f"{5 * gib}\n")

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 3
    assert "below the measured" in res.stdout
    assert "codebase-memory-mcp cli index_repository" not in systemd_log.read_text()


def test_cbm_refuses_unreadable_usage_at_a_finite_v2_ancestor(tmp_path):
    gib = 1024**3
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v2_cgroup(
        tmp_path,
        membership="/tenant/session",
        limits={
            "tenant/session": str(7 * gib),
            "tenant": str(8 * gib),
            "": None,
        },
    )
    env["CODE_INTEL_MEM_CURRENT_BYTES"] = ""
    cgroup = tmp_path / "cgroup2"
    (cgroup / "tenant" / "session" / "memory.current").write_text(f"{gib}\n")

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 3
    assert "cannot read current memory usage" in res.stdout
    assert "codebase-memory-mcp cli index_repository" not in systemd_log.read_text()


def test_cbm_admission_discounts_only_clean_cache_above_reserve(tmp_path):
    gib = 1024**3
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v2_cgroup(
        tmp_path,
        membership="/tenant",
        limits={"tenant": str(9 * gib), "": None},
    )
    env["CODE_INTEL_MEM_CURRENT_BYTES"] = ""
    cgroup = tmp_path / "cgroup2" / "tenant"
    (cgroup / "memory.current").write_text(f"{5 * gib}\n")
    (cgroup / "memory.stat").write_text(
        f"inactive_file {2 * gib}\n"
        f"active_file {gib}\n"
        "file_dirty 0\n"
        "file_writeback 0\n"
    )

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert f"MemoryMax={3 * gib}" in runs[0]


def test_cbm_refuses_when_v2_hierarchy_is_not_mounted(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    self_cgroup = tmp_path / "self.cgroup"
    self_cgroup.write_text("0::/tenant/session\n")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("")

    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={
            "CODE_INTEL_MEM_CEILING_BYTES": "",
            "CODE_INTEL_CGROUP_SELF": str(self_cgroup),
            "CODE_INTEL_CGROUP_MOUNTINFO": str(mountinfo),
        },
    )

    assert res.returncode == 3
    assert "cannot resolve" in res.stdout
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


def test_cbm_accepts_authoritative_unlimited_v2_mount_root(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v2_cgroup(
        tmp_path,
        membership="/",
        mount_root="/tenant/session",
        limits={"": None},
    )

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert "MemoryMax=4G" in runs[0]


def test_cbm_refuses_missing_v2_limit_below_mount_root(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    env = _fake_v2_cgroup(
        tmp_path,
        membership="/tenant/session",
        limits={"tenant/session": None, "tenant": "max", "": None},
    )

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 3
    assert "memory.max" in res.stdout
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


def test_cbm_resolves_namespaced_comounted_v1_memory_controller(tmp_path):
    gib = 1024**3
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v1_cgroup(
        tmp_path,
        membership="/",
        mount_root="/docker/demo",
        limits={"": str(8 * gib)},
    )
    env["CODE_INTEL_MEM_CURRENT_BYTES"] = str(512 * 1024**2)

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert "MemoryMax=4G" in runs[0]


def test_cbm_maps_host_relative_v1_membership_against_mount_root(tmp_path):
    gib = 1024**3
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v1_cgroup(
        tmp_path,
        membership="/docker/demo/child",
        mount_root="/docker/demo",
        limits={"child": str(7 * gib), "": str(6 * gib)},
    )
    env["CODE_INTEL_MEM_CURRENT_BYTES"] = str(512 * 1024**2)

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    expected = 6 * gib - 512 * 1024**2 - 2 * gib
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert f"MemoryMax={expected}" in runs[0]


def test_cbm_accepts_v1_unlimited_sentinel(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    systemd_log = tmp_path / "systemd.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, systemd_log)
    repo = _make_repo(tmp_path)
    env = _fake_v1_cgroup(
        tmp_path,
        membership="/docker/demo",
        mount_root="/docker/demo",
        limits={"": "9223372036854771712"},
    )

    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}", env_extra=env,
    )

    assert res.returncode == 0, res.stdout + res.stderr
    runs = [
        line for line in systemd_log.read_text().splitlines()
        if "codebase-memory-mcp cli index_repository" in line
    ]
    assert len(runs) == 1
    assert "MemoryMax=4G" in runs[0]


def test_cbm_explicit_override_skips_unknown_cgroup_but_still_must_be_useful(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    poison = tmp_path / "does-not-exist"

    accepted = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={
            "CODE_INTEL_CBM_MEMORY_MAX": "5G",
            "CODE_INTEL_MEM_CEILING_BYTES": "",
            "CODE_INTEL_CGROUP_SELF": str(poison),
            "CODE_INTEL_CGROUP_MOUNTINFO": str(poison),
        },
    )
    refused = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={
            "CODE_INTEL_INDEX_MEMORY_MAX": "2G",
            "CODE_INTEL_MEM_CEILING_BYTES": "",
            "CODE_INTEL_CGROUP_SELF": str(poison),
            "CODE_INTEL_CGROUP_MOUNTINFO": str(poison),
        },
    )

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "ULIMIT_V:5242880" in log.read_text()
    assert refused.returncode == 3
    assert "below" in refused.stdout and "measured" in refused.stdout


def test_cbm_no_run_paths_do_not_read_cgroup_metadata(tmp_path):
    poison = tmp_path / "does-not-exist"
    env = {
        "CODE_INTEL_MEM_CEILING_BYTES": "",
        "CODE_INTEL_CGROUP_SELF": str(poison),
        "CODE_INTEL_CGROUP_MOUNTINFO": str(poison),
    }
    disabled = _run_entry(
        tmp_path,
        tmp_path / "missing",
        "cbm",
        path=_SYSTEM_PATH,
        env_extra={**env, "CODE_INTEL_INDEX_DISABLE": "1"},
    )
    invalid = _run_entry(
        tmp_path, tmp_path / "missing", "wat", path=_SYSTEM_PATH, env_extra=env,
    )
    worktree_root = tmp_path / "worktree-case"
    worktree_root.mkdir()
    worktree = _make_repo(worktree_root, worktree=True)
    skipped = _run_entry(
        tmp_path, worktree, "cbm", path=_SYSTEM_PATH, env_extra=env,
    )

    assert disabled.returncode == 0
    assert invalid.returncode == 1
    assert skipped.returncode == 0
    for result in (disabled, invalid, skipped):
        assert "cgroup" not in result.stdout.lower()


# ── the cap must be bounded by what the INSTALL has, not by a constant ───────

def _headroom_decision(tmp_path, ceiling_gib: int, want: str = "8G", env_overrides: dict | None = None) -> tuple[str, str]:
    """Run the SHIPPED decision block at a given install size.

    The block is EXTRACTED from the real script rather than re-typed: a copy
    would drift and leave these assertions describing a version nobody runs.
    """
    src = _ENTRYPOINT.read_text()
    start = src.index("_genesis_mem_bytes() {")
    end = src.index("\nfi\n", src.index("GITNEXUS_MEM_REFUSE=")) + len("\nfi\n")
    block = src[start:end]
    # pytest's tmp_path, NOT a hardcoded directory: an absolute path under a
    # home directory puts a username in a public repo and breaks the test on
    # every other machine.
    blockfile = tmp_path / "block.sh"
    blockfile.write_text(block)
    if True:
        out = subprocess.run(
            ["bash", "-c",
             f'source "{blockfile}" >/dev/null 2>&1; '
             'printf "%s|%s" "$GITNEXUS_MEM_MAX" "$GITNEXUS_MEM_REFUSE"'],
            capture_output=True, text=True, timeout=60,
            env={
                **os.environ,
                "CODE_INTEL_MEM_CEILING_BYTES": str(ceiling_gib * 1024**3),
                # Live usage feeds the same arithmetic; pin it or the verdict
                # depends on what the test host happens to be running.
                "CODE_INTEL_MEM_CURRENT_BYTES": str(512 * 1024**2),
                "GITNEXUS_MEM_MAX": want,
                **(env_overrides or {}),
            },
        ).stdout
    cap, _, why = out.partition("|")
    return cap, why


def _working_set_from_stat(tmp_path, current: int, stat: str, reserve: int) -> int:
    """Run the shipped cgroup working-set helper against synthetic statistics."""
    src = _ENTRYPOINT.read_text()
    start = src.index("_genesis_mem_working_set_from() {")
    end = src.index("\n}\n", start) + len("\n}\n")
    blockfile = tmp_path / "working-set.sh"
    blockfile.write_text(src[start:end])
    statfile = tmp_path / "memory.stat"
    statfile.write_text(stat)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{blockfile}"; '
            f'CODE_INTEL_FILE_CACHE_RESERVE_BYTES={reserve} '
            f'_genesis_mem_working_set_from {current} "{statfile}"',
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return int(result.stdout)


def test_working_set_discounts_only_clean_file_cache_above_reserve(tmp_path):
    gib = 1024**3
    mib = 1024**2
    current = 27 * gib
    working = _working_set_from_stat(
        tmp_path,
        current,
        "\n".join(
            (
                f"inactive_file {5 * gib}",
                f"active_file {8 * gib + 512 * mib}",
                f"file_dirty {512 * mib}",
                f"file_writeback {512 * mib}",
            )
        )
        + "\n",
        2 * gib,
    )
    # Of 13.5 GiB file memory, 1 GiB is not immediately reclaimable and 2 GiB
    # is deliberately retained. The remaining 10.5 GiB must not make a safe
    # cache-heavy box look permanently full.
    assert working == current - (10 * gib + 512 * mib)


def test_working_set_keeps_small_cache_and_fails_closed_on_bad_stats(tmp_path):
    gib = 1024**3
    current = 12 * gib
    small = f"inactive_file {gib}\nactive_file 0\nfile_dirty 0\nfile_writeback 0\n"
    assert _working_set_from_stat(tmp_path, current, small, 2 * gib) == current
    bad = "inactive_file not-a-number\nactive_file 0\nfile_dirty 0\nfile_writeback 0\n"
    assert _working_set_from_stat(tmp_path, current, bad, 2 * gib) == current
    assert _working_set_from_stat(
        tmp_path,
        current,
        f"inactive_file {gib}\nactive_file 0\nfile_dirty {2 * gib}\nfile_writeback 0\n",
        2 * gib,
    ) == current


def test_working_set_supports_the_complete_cgroup_v1_schema(tmp_path):
    gib = 1024**3
    current = 12 * gib
    v1 = (
        f"inactive_file {gib}\n"
        f"active_file {gib}\n"
        "file_dirty 0\n"
        "file_writeback 0\n"
        f"total_inactive_file {6 * gib}\n"
        "total_active_file 0\n"
        "total_dirty 0\n"
        "total_writeback 0\n"
    )
    # v1 exposes local and hierarchical counters in the same file. Its usage
    # charge is hierarchical, so its total_* values must win over local ones.
    assert _working_set_from_stat(tmp_path, current, v1, 2 * gib) == 8 * gib


def test_cache_heavy_install_is_admitted_by_working_set_not_total_charge(tmp_path):
    gib = 1024**3
    statfile = tmp_path / "memory.stat"
    statfile.write_text(
        f"inactive_file {4 * gib}\n"
        f"active_file {9 * gib}\n"
        "file_dirty 0\n"
        "file_writeback 0\n"
    )
    cap, why = _headroom_decision(
        tmp_path,
        32,
        env_overrides={
            "CODE_INTEL_MEM_CURRENT_BYTES": "",
            "CODE_INTEL_MEM_RAW_CURRENT_BYTES": str(27 * gib),
            "CODE_INTEL_MEM_STAT_PATH": str(statfile),
        },
    )
    assert not why, f"clean file cache falsely blocked a safe rebuild: {why}"
    assert cap == "8G", cap


def test_cache_heavy_v1_install_is_admitted_by_working_set_not_total_charge(tmp_path):
    gib = 1024**3
    statfile = tmp_path / "memory.stat"
    statfile.write_text(
        f"total_inactive_file {4 * gib}\n"
        f"total_active_file {9 * gib}\n"
        "total_dirty 0\n"
        "total_writeback 0\n"
    )
    cap, why = _headroom_decision(
        tmp_path,
        32,
        env_overrides={
            "CODE_INTEL_MEM_CURRENT_BYTES": "",
            "CODE_INTEL_MEM_RAW_CURRENT_BYTES": str(27 * gib),
            "CODE_INTEL_MEM_STAT_PATH": str(statfile),
        },
    )
    assert not why, f"clean v1 file cache falsely blocked a safe rebuild: {why}"
    assert cap == "8G", cap


def test_a_small_install_refuses_the_rebuild_rather_than_capping_it_uselessly(tmp_path):
    """A fixed 8G cap is a cap, not a reservation.

    On a 4-5 GiB container it is worse than no cap: the child scope never
    reaches its own MemoryMax, so the PARENT cgroup hits its limit first and the
    kernel picks a victim from everything in it — Genesis, Qdrant, the running
    session. The pressure watchdog does not cover this; it samples load and I/O
    wait, neither of which moves early enough on an OOM path.

    MEASURED working set for a full rebuild: 4,874,166,272 bytes (4.54 GiB), so
    below that a cap cannot bite and the job is refused instead.
    """
    for gib in (4, 5, 6):
        cap, why = _headroom_decision(tmp_path, gib)
        assert why, f"a {gib} GiB install was allowed to start a rebuild (cap {cap})"
        assert "total" in why and "rebuild needs" in why, why


def test_a_mid_size_install_gets_the_cap_trimmed_to_its_headroom(tmp_path):
    """The control that moves in the first direction: not every small-ish box is
    refused. At 8 GiB there IS room once siblings are reserved, so the cap is
    lowered to the headroom rather than left at a value the box cannot honour."""
    cap, why = _headroom_decision(tmp_path, 8)
    assert not why, f"an 8 GiB install was refused: {why}"
    assert cap.isdigit(), cap
    trimmed = int(cap)
    assert trimmed < 8 * 1024**3, "the cap was not trimmed to the install's headroom"
    assert trimmed >= 4874166272, (
        f"trimmed to {cap}, which is below the measured working set — that is the "
        "useless cap this change exists to avoid"
    )


def test_a_large_install_is_left_alone(tmp_path):
    """The control that moves in the other direction. Without it, a change that
    refused or trimmed everywhere would satisfy both tests above."""
    for gib in (16, 32):
        cap, why = _headroom_decision(tmp_path, gib)
        assert not why, f"a {gib} GiB install was refused: {why}"
        assert cap == "8G", f"a {gib} GiB install had its cap changed to {cap}"


def test_sub_mib_headroom_keeps_full_byte_precision(tmp_path):
    """Admission proves the byte cap safe, so trimming must not round it down.
    A spare that sits above the measured minimum but below the next whole MiB
    used to come out as a lower MiB cap and defeat the refusal it just passed.
    systemd accepts an integer byte count, so carry it exactly."""
    spare = 4874166272 + (1024 * 1024) - 1
    cap, why = _headroom_decision(
        tmp_path, 8,
        env_overrides={
            "CODE_INTEL_MEM_CEILING_BYTES": str((512 * 1024**2) + (512 * 1024**2) + spare),
            "CODE_INTEL_SIBLING_RESERVE_BYTES": str(512 * 1024**2),
        },
    )
    assert not why, f"a sub-MiB spare was refused: {why}"
    assert cap == str(spare), f"trimmed cap lost byte precision: {cap}"


def test_a_configured_cap_below_the_working_set_is_refused(tmp_path):
    """An operator override (or the legacy shared CODE_INTEL_INDEX_MEMORY_MAX=2G)
    below the measured 4.54 GiB working set cannot bite: on a large host the
    rebuild would still run and be killed by its own cgroup. Refuse instead."""
    for want in ("2G", "4G", "4096M"):
        cap, why = _headroom_decision(tmp_path, 32, want=want)
        assert why, f"cap {want} on a 32 GiB install was allowed to run"
        assert "below" in why and "rebuild needs" in why, why


def test_fractional_and_malformed_caps(tmp_path):
    """systemd accepts fractional MemoryMax (5.5G); Bash arithmetic is integer-
    only. Fractional values must PARSE (5.5G is fine headroom on a big box),
    and malformed values must refuse rather than run unbounded."""
    cap, why = _headroom_decision(tmp_path, 32, want="5.5G")
    assert not why, f"a valid fractional cap was refused: {why}"
    assert cap == "5.5G", cap
    for bad in ("banana", "5.5", "G", ""):
        cap, why = _headroom_decision(tmp_path, 32, want=bad)
        assert why, f"malformed cap {bad!r} was allowed to run"
        assert "not a parseable" in why, why
# ── 3b. OOM kill-order preference ─────────────────────────────────────────
# The disposable batch indexer gets the kernel's maximum OOM preference inside
# its applicable OOM domain. The supervisor, probe and watchdog retain their
# inherited score so they can still stop, thaw and reap the heavy child.


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
    assert "OOM_ADJ:1000" in log.read_text()
    # And NOT passed as a scope property, which systemd would reject outright.
    assert "OOMScoreAdjust" not in slog.read_text()


def test_oom_score_adj_1000_override_reaches_the_indexer(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "1000"},
    )
    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:1000" in log.read_text()


def test_oom_score_adj_non_numeric_refuses_index(tmp_path):
    """A malformed override cannot weaken the batch-victim contract."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "not-a-number"},
    )
    assert res.returncode != 0
    assert "requires 1000" in res.stderr
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


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
    assert res.returncode != 0
    assert "requires 1000" in res.stderr
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


def test_oom_score_adj_keeps_a_higher_inherited_value(tmp_path):
    """An inherited maximum remains maximum in the disposable child."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    # Change only subprocess state. Mutating the pytest parent would contaminate
    # later tests if restoration failed or the test process were interrupted.
    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        preexec_fn=lambda: Path("/proc/self/oom_score_adj").write_text("1000\n"),
    )
    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:1000" in log.read_text(), (
        "the inherited maximum did not reach the disposable child"
    )
    assert "requires 1000" not in res.stderr


def test_explicit_override_cannot_lower_child_preference(tmp_path):
    """An explicit lever cannot opt a heavy batch job out of the safety policy."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "321"},
        preexec_fn=lambda: Path("/proc/self/oom_score_adj").write_text("800\n"),
    )
    assert res.returncode != 0
    assert "requires 1000" in res.stderr
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


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
    assert res.returncode != 0
    assert "requires 1000" in res.stderr
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


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
    assert "OOM_ADJ:1000" in log.read_text()
    for claim in ("the server at 100", "at 500", "at 0)"):
        assert claim not in res.stdout, f"log still asserts a foreign score: {claim!r}"


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


def test_oom_adjustment_is_child_only_in_systemd_scope_path(tmp_path):
    """The disposable tool is maximally preferred; its supervisor is not."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    slog = tmp_path / "systemd-run.log"
    _fake_tools(fakebin, log)
    _fake_systemd_run(fakebin, slog, probe_ok=True)
    repo = _make_repo(tmp_path)

    res = _run_entry(tmp_path, repo, "cbm", path=f"{fakebin}:{_SYSTEM_PATH}")

    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:1000" in log.read_text()
    # The fake is called once for the capability probe and once for the real
    # workload.  Inspect the workload record specifically: checking the
    # combined log would let the probe's inherited score mask a regression
    # that changed only the execution-time systemd-run process.
    systemd_lines = slog.read_text().splitlines()
    assert len(systemd_lines) == 4, systemd_lines
    assert "--description" in systemd_lines[-2]
    assert systemd_lines[-1] == f"SYSTEMD_RUN_OOM_ADJ:{_inherited_oom_adj()}"


def test_oom_adjustment_reaches_child_in_fallback_path(tmp_path):
    minbin = _minimal_path(tmp_path)
    log = tmp_path / "tools.log"
    _fake_tools(minbin, log)
    repo = _make_repo(tmp_path)

    res = _run_entry(tmp_path, repo, "cbm", path=str(minbin))

    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:1000" in log.read_text()


def test_fallback_watchdog_kills_the_whole_indexer_process_group(tmp_path):
    """The no-systemd wall cap must not leave an indexer descendant orphaned."""
    import time

    minbin = _minimal_path(tmp_path, "date", "ps", "tr", "sleep")
    child_pid_log = tmp_path / "child.pid"
    _write_exec(
        minbin / "codebase-memory-mcp",
        "#!/usr/bin/env bash\n"
        "/bin/sleep 60 &\n"
        f'echo "$!" > "{child_pid_log}"\n'
        "wait\n",
    )
    repo = _make_repo(tmp_path)

    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        "fast",
        path=str(minbin),
        env_extra={
            "CODE_INTEL_WATCHDOG_WALL_FAST": "2",
            "CODE_INTEL_WATCHDOG_INTERVAL": "1",
            "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
            "CODE_INTEL_FAKE_LOADAVG": "0",
        },
    )

    assert res.returncode != 0, "a killed index must report failure"
    assert "wall cap" in res.stdout
    child_pid = int(child_pid_log.read_text())
    deadline = time.monotonic() + 3
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists(), (
        f"fallback watchdog left indexer descendant {child_pid} alive"
    )


def test_lower_oom_adjustment_override_refuses_workload(tmp_path):
    """An operator override may not weaken the batch-victim safety contract."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)

    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "321"},
    )

    assert res.returncode != 0
    assert "requires 1000" in (res.stdout + res.stderr)
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


def test_oom_adjustment_write_failure_refuses_workload(tmp_path):
    """If the child cannot prove its score, it must not start the heavy job."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_TEST_FORCE_OOM_ADJ_FAILURE": "1"},
    )

    assert res.returncode != 0
    assert "cannot establish oom_score_adj=1000" in (res.stdout + res.stderr)
    assert not log.exists() or "codebase-memory-mcp ARGS:" not in log.read_text()


def test_oom_child_launcher_preserves_argument_boundaries(tmp_path):
    log = tmp_path / "args.log"
    marker = tmp_path / "must-not-exist"
    tool = tmp_path / "tool with spaces"
    _write_exec(
        tool,
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$#" > "{log}"\n'
        f'printf "<%s>\\n" "$@" >> "{log}"\n',
    )
    args = ["space value", f"$(touch {marker})", "semi;colon", "*"]

    res = subprocess.run(
        ["bash", str(_ENTRYPOINT), "--exec-indexer-with-oom-adj", str(tool), *args],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert res.returncode == 0, res.stderr
    assert log.read_text().splitlines() == ["4", *(f"<{arg}>" for arg in args)]
    assert not marker.exists(), "a metacharacter argument was evaluated by a shell"


def test_oom_child_launcher_propagates_exit_status(tmp_path):
    tool = tmp_path / "fails"
    _write_exec(tool, "#!/usr/bin/env bash\nexit 23\n")

    res = subprocess.run(
        ["bash", str(_ENTRYPOINT), "--exec-indexer-with-oom-adj", str(tool)],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert res.returncode == 23


def test_relative_entrypoint_survives_indexer_working_directory_change(tmp_path):
    """The child wrapper path must remain valid after the GitNexus ``cd``."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path)
    env = {
        "PATH": f"{fakebin}:{_SYSTEM_PATH}",
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        "CODE_INTEL_WATCHDOG_INTERVAL": "0.05",
        "CODE_INTEL_WATCHDOG_WARMUP_INTERVAL": "0.05",
        "CODE_INTEL_FAKE_LOADAVG": "0",
        "CODE_INTEL_MEM_CEILING_BYTES": str(64 * 1024**3),
        "CODE_INTEL_MEM_CURRENT_BYTES": str(512 * 1024**2),
    }
    relative = _ENTRYPOINT.relative_to(_REPO_ROOT)

    res = subprocess.run(
        ["bash", str(relative), str(repo), "gitnexus"],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert res.returncode == 0, res.stderr
    assert "OOM_ADJ:1000" in log.read_text()


def test_noop_worktree_skip_does_not_validate_child_oom_override(tmp_path):
    """No child means no adjustment and no child-policy failure."""
    fakebin, log = tmp_path / "fakebin", tmp_path / "tools.log"
    _fake_tools(fakebin, log)
    repo = _make_repo(tmp_path, worktree=True)

    res = _run_entry(
        tmp_path,
        repo,
        "cbm",
        path=f"{fakebin}:{_SYSTEM_PATH}",
        env_extra={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": "321"},
    )

    assert res.returncode == 0, res.stderr
    assert "worktree" in res.stdout
    assert not log.exists()
