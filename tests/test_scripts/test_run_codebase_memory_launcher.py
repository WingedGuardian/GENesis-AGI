"""Memory-cap launcher for codebase-memory-mcp (.claude/mcp/run-codebase-memory).

Upstream v0.9.0 still leaks memory without bound on query operations (#581)
(DeusData/codebase-memory-mcp#581), so the launcher wraps the server in a
transient systemd scope with MemoryMax, falling back to an address-space
rlimit where no user manager is reachable.

These tests are binary- and environment-independent: the server binary and
``systemd-run`` are both faked via PATH/env injection, so they run in CI
runners with no systemd user manager and no codebase-memory-mcp install.
One real-cgroup smoke test runs only where ``systemd-run --user`` works.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _REPO_ROOT / ".claude" / "mcp" / "run-codebase-memory"
_REGISTER_LIB = _REPO_ROOT / "scripts" / "lib" / "mcp_register.sh"

_SYSTEM_PATH = "/usr/bin:/bin"  # real python3/bash/coreutils for harnesses


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_binary(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in server binary that records its args and its ulimit -v."""
    log = tmp_path / "binary.log"
    binary = _write_exec(
        tmp_path / "fake-cbm",
        "#!/usr/bin/env bash\n"
        f'echo "ARGS:$*" >> "{log}"\n'
        f'echo "ULIMIT_V:$(ulimit -v)" >> "{log}"\n',
    )
    return binary, log


def _fake_systemd_run(tmp_path: Path, *, probe_ok: bool = True) -> tuple[Path, Path]:
    """A fake systemd-run: logs argv, then execs the wrapped command.

    The launcher probes with ``-- /bin/true`` before committing; ``probe_ok``
    controls whether that probe (and everything else) succeeds.
    """
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    log = tmp_path / "systemd-run.log"
    if probe_ok:
        body = (
            "#!/usr/bin/env bash\n"
            f'echo "$*" >> "{log}"\n'
            "# exec everything after the -- separator\n"
            'while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
            "shift\n"
            'exec "$@"\n'
        )
    else:
        body = f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 1\n'
    _write_exec(fakebin / "systemd-run", body)
    return fakebin, log


def _run_launcher(tmp_path, *args, fakebin=None, env_extra=None):
    binary, blog = _fake_binary(tmp_path)
    path = f"{fakebin}:{_SYSTEM_PATH}" if fakebin else _SYSTEM_PATH
    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "CODEBASE_MEMORY_MCP_BIN": str(binary),
        **(env_extra or {}),
    }
    res = subprocess.run(
        ["bash", str(_LAUNCHER), *args],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return res, blog


# ── launcher behavior (fully faked, CI-safe) ──────────────────────────────


def test_missing_binary_errors(tmp_path):
    res = subprocess.run(
        ["bash", str(_LAUNCHER)],
        env={"PATH": _SYSTEM_PATH, "HOME": str(tmp_path),
             "CODEBASE_MEMORY_MCP_BIN": str(tmp_path / "nope")},
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 1
    assert "not installed" in res.stderr


def test_scope_path_passes_memorymax(tmp_path):
    fakebin, slog = _fake_systemd_run(tmp_path, probe_ok=True)
    res, blog = _run_launcher(tmp_path, fakebin=fakebin)
    assert res.returncode == 0, res.stderr
    calls = slog.read_text()
    assert "MemoryMax=2G" in calls
    assert "MemorySwapMax=0" in calls
    assert "--scope" in calls
    assert "ARGS:" in blog.read_text()  # the server actually ran


def test_mem_max_env_override(tmp_path):
    fakebin, slog = _fake_systemd_run(tmp_path, probe_ok=True)
    res, _ = _run_launcher(
        tmp_path, fakebin=fakebin,
        env_extra={"CODEBASE_MEMORY_MCP_MEMORY_MAX": "512M"},
    )
    assert res.returncode == 0, res.stderr
    assert "MemoryMax=512M" in slog.read_text()


def test_args_passthrough_scope_path(tmp_path):
    fakebin, _ = _fake_systemd_run(tmp_path, probe_ok=True)
    res, blog = _run_launcher(tmp_path, "cli", "impact", fakebin=fakebin)
    assert res.returncode == 0, res.stderr
    assert "ARGS:cli impact" in blog.read_text()


def _minimal_path(tmp_path: Path) -> Path:
    """A PATH dir with ONLY bash + env (genuinely no systemd-run).

    ``/usr/bin:/bin`` contains the real systemd-run on most Linux hosts, so
    using it would exercise the probe-fail path, not the absent-binary path.
    """
    d = tmp_path / "minbin"
    d.mkdir(exist_ok=True)
    for tool in ("bash", "env", "sh"):
        src = Path("/usr/bin") / tool
        if not src.exists():
            src = Path("/bin") / tool
        (d / tool).symlink_to(src)
    return d


def test_fallback_when_systemd_run_absent(tmp_path):
    # command -v systemd-run itself fails → ulimit fallback (2G = 2097152 KB).
    minbin = _minimal_path(tmp_path)
    binary, blog = _fake_binary(tmp_path)
    res = subprocess.run(
        ["bash", str(_LAUNCHER)],
        env={"PATH": str(minbin), "HOME": str(tmp_path),
             "CODEBASE_MEMORY_MCP_BIN": str(binary)},
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert "ULIMIT_V:2097152" in blog.read_text()


def test_fallback_fractional_gig_truncates_cleanly(tmp_path):
    # 2.5G is valid for systemd but the rlimit fallback truncates to 2G —
    # and must NOT spray a bash arithmetic error on stderr.
    minbin = _minimal_path(tmp_path)
    binary, blog = _fake_binary(tmp_path)
    res = subprocess.run(
        ["bash", str(_LAUNCHER)],
        env={"PATH": str(minbin), "HOME": str(tmp_path),
             "CODEBASE_MEMORY_MCP_BIN": str(binary),
             "CODEBASE_MEMORY_MCP_MEMORY_MAX": "2.5G"},
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert "arithmetic" not in res.stderr
    assert "ULIMIT_V:2097152" in blog.read_text()


def test_fallback_when_probe_fails(tmp_path):
    fakebin, slog = _fake_systemd_run(tmp_path, probe_ok=False)
    res, blog = _run_launcher(tmp_path, "cli", fakebin=fakebin)
    assert res.returncode == 0, res.stderr
    # probe was attempted, then the binary ran under ulimit instead
    assert slog.read_text().count("\n") == 1
    out = blog.read_text()
    assert "ULIMIT_V:2097152" in out
    assert "ARGS:cli" in out


def test_unparseable_memmax_warns_and_runs_uncapped(tmp_path):
    res, blog = _run_launcher(
        tmp_path, fakebin=None,
        env_extra={"CODEBASE_MEMORY_MCP_MEMORY_MAX": "lots"},
    )
    assert res.returncode == 0, res.stderr
    assert "running uncapped" in res.stderr
    assert "ULIMIT_V:unlimited" in blog.read_text()


# ── real-cgroup smoke (local only; skipped where no user manager) ─────────


def _user_scope_works() -> bool:
    try:
        return subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--", "/bin/true"],
            capture_output=True, timeout=15,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _user_scope_works(), reason="no usable systemd user manager")
def test_real_scope_applies_memory_max(tmp_path):
    probe = _write_exec(
        tmp_path / "cgprobe",
        "#!/usr/bin/env bash\n"
        'cg="$(sed -n \'s/^0:://p\' /proc/self/cgroup)"\n'
        'cat "/sys/fs/cgroup${cg}/memory.max"\n',
    )
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
           "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
           "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
           "CODEBASE_MEMORY_MCP_BIN": str(probe)}
    res = subprocess.run(
        ["bash", str(_LAUNCHER)], env=env, capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(2 * 1024**3)  # MemoryMax=2G


# ── _register_mcp drift-healing (sources the REAL shared lib) ─────────────


def _run_register(tmp_path: Path, args: list[str], claude_json: dict | None,
                  mcp_list: str = "") -> tuple[subprocess.CompletedProcess, Path]:
    import json
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    clog = tmp_path / "claude.log"
    _write_exec(
        fakebin / "claude",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{clog}"\n'
        f'if [ "$1 $2" = "mcp list" ]; then cat "{tmp_path}/mcp_list.txt" 2>/dev/null; fi\n'
        "exit 0\n",
    )
    (tmp_path / "mcp_list.txt").write_text(mcp_list, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if claude_json is not None:
        (home / ".claude.json").write_text(json.dumps(claude_json), encoding="utf-8")
    harness = tmp_path / "harness.sh"
    quoted = " ".join(f"'{a}'" for a in args)
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_REGISTER_LIB}"\n_register_mcp {quoted}\n',
        encoding="utf-8",
    )
    res = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(home)},
        capture_output=True, text=True, timeout=30,
    )
    return res, clog


def test_register_user_scope_fresh_adds(tmp_path):
    res, clog = _run_register(tmp_path, ["srv", "user", "/opt/launcher"], {"mcpServers": {}})
    assert res.returncode == 0
    assert "mcp add srv -s user -- /opt/launcher" in clog.read_text()


def test_register_user_scope_same_basename_is_noop(tmp_path):
    # Registered by bare name, stored resolved — same basename is NOT drift.
    cfg = {"mcpServers": {"gitnexus": {"command": "/home/x/.local/bin/gitnexus"}}}
    res, clog = _run_register(tmp_path, ["gitnexus", "user", "gitnexus", "mcp"], cfg)
    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert not clog.exists() or "mcp add" not in clog.read_text()


def test_register_user_scope_drift_reregisters(tmp_path):
    cfg = {"mcpServers": {"codebase-memory-mcp":
                          {"command": "/home/x/.local/bin/codebase-memory-mcp"}}}
    res, clog = _run_register(
        tmp_path, ["codebase-memory-mcp", "user", "/repo/.claude/mcp/run-codebase-memory"], cfg,
    )
    assert res.returncode == 0
    calls = clog.read_text()
    assert "mcp remove codebase-memory-mcp -s user" in calls
    assert "mcp add codebase-memory-mcp -s user -- /repo/.claude/mcp/run-codebase-memory" in calls
    assert calls.index("mcp remove") < calls.index("mcp add")


def test_register_user_scope_absolute_path_drift_reregisters(tmp_path):
    # Same basename, different absolute path (e.g. a launcher registered from
    # a since-reaped worktree) IS drift for an absolute intended command.
    stale = "/repo/.claude/worktrees/old/.claude/mcp/run-codebase-memory"
    cfg = {"mcpServers": {"codebase-memory-mcp": {"command": stale}}}
    res, clog = _run_register(
        tmp_path,
        ["codebase-memory-mcp", "user", "/repo/.claude/mcp/run-codebase-memory"], cfg,
    )
    assert res.returncode == 0
    calls = clog.read_text()
    assert "mcp remove codebase-memory-mcp -s user" in calls
    assert "mcp add codebase-memory-mcp -s user -- /repo/.claude/mcp/run-codebase-memory" in calls


def test_register_warns_on_local_scope_shadow(tmp_path):
    # A local-scope (per-project) entry takes precedence over user scope and
    # must be surfaced, never silently shadowed. It is warned, not removed.
    cfg = {
        "mcpServers": {"codebase-memory-mcp":
                       {"command": "/repo/.claude/mcp/run-codebase-memory"}},
        "projects": {"/some/project": {"mcpServers": {
            "codebase-memory-mcp": {"command": "/old/bare-binary"}}}},
    }
    res, clog = _run_register(
        tmp_path,
        ["codebase-memory-mcp", "user", "/repo/.claude/mcp/run-codebase-memory"], cfg,
    )
    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert "LOCAL-scope" in res.stdout
    assert "/old/bare-binary" in res.stdout
    assert "mcp remove" not in (clog.read_text() if clog.exists() else "")


def test_register_project_scope_existing_is_noop(tmp_path):
    res, clog = _run_register(
        tmp_path, ["serena", "project", "serena", "start-mcp-server"],
        None, mcp_list="serena: serena start-mcp-server\n",
    )
    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert "mcp add" not in clog.read_text()
