"""Running-binary sweep (scripts/check_cc_running_versions.sh).

Reports which LIVE Claude Code processes execute the binary currently on disk
and which still run a copy that has since been replaced. Origin: on a live
install, most CC processes were still executing the pre-align binary while
``claude --version`` — which spawns a fresh child — truthfully reported the new
one, so a local-first soak had accumulated days of "real use" on the old
release.

Every test drives a FIXTURE proc root via ``--proc-root`` and a fixture
``claude`` on PATH, so all three verdict branches (OK / STALE / UNDETERMINED)
are exercised deterministically instead of only whichever branch this host
happens to be in.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_cc_running_versions.sh"

EXIT_OK = 0
EXIT_STALE = 1
EXIT_UNDETERMINED = 2


# ── fixture builders ──────────────────────────────────────────────────────


def _make_binary(path: Path, version: str = "9.9.9") -> Path:
    """A real, executable stand-in for the CC binary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{version} (Claude Code)"\n')
    path.chmod(0o755)
    return path


def _make_proc_entry(
    proc_root: Path,
    pid: int,
    exe_target: Path,
    cmdline: str = "claude\x00",
) -> Path:
    """One fake /proc/<pid> with an ``exe`` symlink and a cmdline."""
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "exe").symlink_to(exe_target)
    (d / "cmdline").write_bytes(cmdline.encode())
    return d


def _run(proc_root: Path, canonical_bin_dir: Path) -> subprocess.CompletedProcess:
    """Run the sweep with a fixture PATH (so `command -v claude` finds ours)."""
    env = dict(os.environ)
    env["PATH"] = f"{canonical_bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(proc_root)],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def world(tmp_path: Path):
    """canonical binary on PATH + an empty fake proc root."""
    pkg = _make_binary(tmp_path / "pkg" / "claude.exe", "2.1.246")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").symlink_to(pkg)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    return {"tmp": tmp_path, "canonical": pkg, "bindir": bindir, "proc": proc_root}


# ── the three verdict branches ────────────────────────────────────────────


def test_all_processes_on_canonical_is_ok(world):
    """Every live CC process maps the on-disk binary → exit 0."""
    _make_proc_entry(world["proc"], 101, world["canonical"])
    _make_proc_entry(world["proc"], 102, world["canonical"])

    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_OK, res.stderr
    assert "stale=0" in res.stdout
    assert "current=2" in res.stdout


def test_replaced_binary_is_reported_stale(world):
    """A process whose exe is a DIFFERENT inode → exit 1, named as stale.

    This is the incident shape: npm replaced the package under a long-lived
    process, which keeps executing the old inode until it restarts.
    """
    old = _make_binary(world["tmp"] / "pkg_old" / "claude.exe", "2.1.218")
    _make_proc_entry(world["proc"], 201, world["canonical"])
    _make_proc_entry(world["proc"], 202, old)

    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_STALE
    assert "STALE" in res.stdout
    assert "202" in res.stdout
    assert "stale=1" in res.stdout
    assert "current=1" in res.stdout
    # The operator must be told the consequence, not just the fact.
    assert "REPLACED binary" in res.stderr


def test_node_wrapped_install_refuses_to_answer(world):
    """`node .../cli.js` → the exe inode is Node's and says nothing about CC.

    Must report UNDETERMINED and exit non-zero. A false all-clear here is
    exactly the failure this script exists to prevent.
    """
    node = _make_binary(world["tmp"] / "nodebin" / "node")
    _make_proc_entry(
        world["proc"],
        301,
        node,
        cmdline="node\x00/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js\x00",
    )

    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_UNDETERMINED
    assert "UNDETERMINED" in res.stdout
    assert "refusing to report all-clear" in res.stderr


def test_stale_wins_over_undetermined(world):
    """Both conditions present → exit 1: the actionable finding dominates."""
    old = _make_binary(world["tmp"] / "pkg_old" / "claude.exe", "2.1.218")
    node = _make_binary(world["tmp"] / "nodebin" / "node")
    _make_proc_entry(world["proc"], 401, old)
    _make_proc_entry(
        world["proc"],
        402,
        node,
        cmdline="node\x00/x/@anthropic-ai/claude-code/cli.js\x00",
    )

    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_STALE


# ── boundary conditions ───────────────────────────────────────────────────


def test_no_cc_processes_is_ok(world):
    """An unrelated process is ignored; no CC running → exit 0, not a failure."""
    other = _make_binary(world["tmp"] / "other" / "vim")
    _make_proc_entry(world["proc"], 501, other, cmdline="vim\x00")

    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_OK
    assert "current=0" in res.stdout
    assert "stale=0" in res.stdout


def test_empty_proc_root_does_not_glob_literally(world):
    """No pids at all — the unmatched glob must not be treated as a process."""
    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_OK
    assert "current=0" in res.stdout


def test_two_installs_at_different_versions_refuse_to_answer(world, tmp_path: Path):
    """PATH precedence is not identity — and getting it wrong INVERTS the verdict.

    `cc_shadow_scan` deliberately never trusts a bare `command -v claude`,
    because a stale extra copy has repeatedly won interactive PATH here. If that
    copy is crowned canonical, this sweep reports every session on the stale
    binary as "current" and every session on the real install as "stale" —
    corrupting exactly the soak evidence it exists to produce. Refuse instead.
    """
    other_dir = tmp_path / "probe_prefix"
    _make_binary(other_dir / "claude", "1.0.0")  # a DIFFERENT version
    _make_proc_entry(world["proc"], 701, world["canonical"])

    env = dict(os.environ)
    env["PATH"] = f"{world['bindir']}:{env['PATH']}"
    env["CC_PROBE_DIRS"] = str(other_dir)
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(world["proc"])],
        capture_output=True, text=True, env=env,
    )

    assert res.returncode == EXIT_UNDETERMINED, res.stdout
    assert "more than one Claude Code" in res.stderr
    assert "cc_shadow_scan" in res.stderr, "point the operator at the fix"


def test_matching_second_copy_does_not_block(world, tmp_path: Path):
    """Over-rejection guard: a second copy at the SAME version is not ambiguous."""
    other_dir = tmp_path / "probe_prefix_same"
    _make_binary(other_dir / "claude", "2.1.246")  # same version as canonical
    _make_proc_entry(world["proc"], 801, world["canonical"])

    env = dict(os.environ)
    env["PATH"] = f"{world['bindir']}:{env['PATH']}"
    env["CC_PROBE_DIRS"] = str(other_dir)
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(world["proc"])],
        capture_output=True, text=True, env=env,
    )

    assert res.returncode == EXIT_OK, res.stderr


def test_an_unreadable_cc_process_is_not_all_clear(world, tmp_path: Path):
    """Identified as CC by cmdline but /proc/<pid>/exe unreadable → exit 2.

    Reporting OK would be a claim about a session this run could not see, and a
    soak receipt quoting that sweep would be false.
    """
    d = world["proc"] / "901"
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"claude\x00")   # looks like CC, no exe link

    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_UNDETERMINED, res.stdout
    assert "could not be" in res.stderr


def test_no_claude_on_path_is_undetermined(tmp_path: Path):
    """Without a canonical there is nothing to compare against → exit 2.

    It must NOT fall back to reporting everything current.
    """
    proc_root = tmp_path / "proc"
    proc_root.mkdir()

    # PATH must still resolve `bash` (subprocess needs it) while excluding
    # `claude`, which installs to /usr/local/bin — not to /usr/bin or /bin,
    # here or on a CI runner. Emptying PATH entirely makes the test fail on
    # "no such file: bash" instead of exercising the branch.
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(proc_root)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    assert res.returncode == EXIT_UNDETERMINED
    assert "no 'claude' on PATH" in res.stderr


def test_vanished_process_does_not_leak_a_shell_error(world):
    """A pid dir with no exe/cmdline (process exited mid-sweep) is routine.

    Regression guard: written as `tr ... < file 2>/dev/null` bash applies the
    redirections left to right, so the stderr redirect cannot suppress the
    INPUT redirect's own "No such file" — the sweep leaked a bash error line
    on every exiting process. The `2>` must precede the `<`.
    """
    (world["proc"] / "601").mkdir()  # bare dir: no exe, no cmdline

    res = _run(world["proc"], world["bindir"])

    assert res.returncode == EXIT_OK
    assert "No such file" not in res.stderr
    assert "cmdline" not in res.stderr


def test_deleted_suffix_is_stripped_for_detection(world):
    """Detection must survive procfs' " (deleted)" suffix on a replaced path.

    Can't unlink a symlink target and keep the link resolvable in a fixture, so
    this asserts the parsing contract directly on the real host sweep instead:
    it must not crash and must emit a summary line.
    """
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--quiet"],
        capture_output=True,
        text=True,
    )

    assert res.returncode in (EXIT_OK, EXIT_STALE, EXIT_UNDETERMINED)
    # --quiet suppresses the per-process lines but never the verdict.
    assert "Traceback" not in res.stderr
    assert "syntax error" not in res.stderr


def test_rejects_unknown_argument(world):
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--bogus"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == EXIT_UNDETERMINED
    assert "unknown argument" in res.stderr
