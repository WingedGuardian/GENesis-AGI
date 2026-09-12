"""Running-binary sweep (scripts/check_cc_running_versions.sh).

Reports which LIVE Claude Code processes execute the binary currently on disk
and which still run one npm has replaced. Origin: on a live install, most CC
processes were still executing the pre-align binary while `claude --version` —
which spawns a fresh child — truthfully reported the new one, so a local-first
soak had accumulated days of "real use" on the old release.

THE INVARIANT UNDER TEST, and the reason this file is shaped the way it is:

    Any process the sweep cannot POSITIVELY PROVE is not-Claude-Code must not
    contribute to a clean verdict.

The previous design had no such rule, so each review round fixed the case a
reviewer named and silently flipped the fail direction. Tests here are grouped
by which HALF of the invariant they pin — "must not report OK" and "must not
false-alarm" — because a fix that satisfies one by breaking the other is the
failure mode, and a test file organised by feature hides that.

HERMETIC BY CONSTRUCTION. Every run sets `CC_PROBE_DIRS` and `HOME` to
fixture-only paths. Without that the script reaches the REAL /usr/local/bin
during the installed-copy scan, and the suite passes only while a fixture
literal happens to equal the host's installed CC version — a time bomb whose
fuse is the next pin bump, invisible on CI where no CC is installed.
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


def _make_binary(path: Path, marker: str = "canonical") -> Path:
    """A real, executable stand-in. Content differs per marker so two fixtures
    are genuinely different files (different inodes), which is what the sweep
    compares — never a version string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{marker}"\n')
    path.chmod(0o755)
    return path


def _make_proc(
    proc_root: Path, pid: int, *, exe: Path | None = None, cmdline: str | None = "claude\x00"
) -> Path:
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    if exe is not None:
        (d / "exe").symlink_to(exe)
    if cmdline is not None:
        (d / "cmdline").write_bytes(cmdline.encode())
    return d


def _run(world, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{world['bindir']}:{env['PATH']}"
    # The two seams that keep this hermetic. Without them the installed-copy
    # scan reads the real host install and the outcome depends on the host.
    env["CC_PROBE_DIRS"] = str(world["bindir"])
    env["HOME"] = str(world["tmp"])
    return subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(world["proc"]), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.fixture
def world(tmp_path: Path):
    pkg = _make_binary(tmp_path / "pkg" / "claude.exe", "canonical")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").symlink_to(pkg)
    proc = tmp_path / "proc"
    proc.mkdir()
    # The sweep refuses a --proc-root that does not look like a procfs (an
    # existing-but-wrong path used to produce a confident all-clear), so a
    # fixture root must carry a pid 1. It is a plain userspace process that is
    # positively not CC, so it never affects a verdict.
    one = proc / "1"
    one.mkdir()
    (one / "exe").symlink_to(_make_binary(tmp_path / "sbin" / "init", "init"))
    (one / "cmdline").write_bytes(b"/sbin/init\x00")
    return {"tmp": tmp_path, "canonical": pkg, "bindir": bindir, "proc": proc}


def test_the_suite_is_not_coupled_to_this_hosts_cc_version(world):
    """Guards the hermeticity above, which is a property of the HARNESS.

    The previous suite passed only because a fixture literal coincidentally
    equalled the host's installed version; the next pin bump broke eight tests,
    and CI could never catch it because no CC is installed there.
    """
    res = _run(world)

    assert "/usr/local" not in res.stdout, "the sweep reached a real install path"
    assert res.returncode == EXIT_OK


# ── half one: must never report OK when it cannot see everything ──────────


def test_a_replaced_binary_is_stale(world):
    """The incident shape: npm replaced the package under a long-lived process,
    which keeps executing the old inode until it restarts."""
    old = _make_binary(world["tmp"] / "old" / "claude.exe", "replaced")
    _make_proc(world["proc"], 101, exe=world["canonical"])
    _make_proc(world["proc"], 102, exe=old)

    res = _run(world)

    assert res.returncode == EXIT_STALE, res.stdout
    assert "stale=1" in res.stdout
    assert "current=1" in res.stdout


def test_a_wrapper_named_process_is_not_silently_dropped(world):
    """MEASURED false all-clear in the previous design.

    A process whose exe basename matched no branch — reproduced with a
    bun-wrapped CLI — incremented NO counter at all, so the sweep printed
    "OK — all 0 live Claude Code process(es)" and exited 0 with a live CC
    session running. Being ignored is a vote for a clean verdict.
    """
    bun = _make_binary(world["tmp"] / "rt" / "bun", "bun")
    _make_proc(world["proc"], 201, exe=bun, cmdline="bun\x00/opt/claude-alt/cli.js\x00")

    res = _run(world)

    assert res.returncode != EXIT_OK, res.stdout
    assert "UNDETERMINED" in res.stdout or "UNDETERMINED" in res.stderr


def test_a_cc_process_whose_exe_cannot_be_read_blocks_a_clean_verdict(world):
    """Identified as CC by argv[0], executable not inspectable (another user, or
    a procfs restriction). Reporting OK would be a claim about a session this
    run never saw, and a receipt quoting it would be false."""
    _make_proc(world["proc"], 301, exe=None, cmdline="claude\x00")

    res = _run(world)

    assert res.returncode == EXIT_UNDETERMINED, res.stdout


def test_neither_exe_nor_cmdline_readable_blocks_a_clean_verdict(world):
    """The one genuinely unclassifiable case. It cannot be proven not-CC, so by
    the invariant it must not contribute to OK."""
    d = world["proc"] / "401"
    d.mkdir(parents=True)
    (d / "stat").write_text("401 (x) S\n")  # enumerable, but nothing identifying

    res = _run(world)

    assert res.returncode == EXIT_UNDETERMINED, res.stdout


def test_a_node_wrapped_install_refuses_to_answer(world):
    """/proc/<pid>/exe resolves to the interpreter, whose inode says nothing
    about which CC revision is loaded. Refuse rather than answer wrongly."""
    node = _make_binary(world["tmp"] / "rt" / "node", "node")
    _make_proc(world["proc"], 501, exe=node, cmdline="node\x00/opt/cc/cli.js\x00")

    res = _run(world)

    assert res.returncode == EXIT_UNDETERMINED, res.stdout


def test_a_typod_proc_root_refuses_rather_than_reporting_all_clear(world):
    """MEASURED: the unmatched-glob guard cannot tell an empty procfs from a
    wrong path, so a typo produced a confident "OK — all 0"."""
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", "/nonexistent-typo"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert res.returncode == EXIT_UNDETERMINED
    assert "not a directory" in res.stderr


def test_no_claude_on_path_is_undetermined(tmp_path: Path):
    """Without a canonical there is nothing to compare against. It must NOT
    fall back to reporting everything current."""
    proc = tmp_path / "proc"
    (proc / "1").mkdir(parents=True)  # procfs-shaped, so the PATH check is what fires

    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(proc)],
        capture_output=True,
        text=True,
        # PATH must still resolve `bash` while excluding `claude`, which installs
        # to /usr/local/bin — not /usr/bin or /bin, here or on a CI runner.
        # Emptying PATH entirely fails on "no such file: bash" instead of
        # exercising the branch.
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout=60,
    )

    assert res.returncode == EXIT_UNDETERMINED
    assert "no 'claude' on PATH" in res.stderr


def test_home_unset_exits_undetermined_not_stale(tmp_path: Path):
    """MEASURED fail-direction inversion: `$HOME` under `set -u` aborted the
    script with exit 1 — the code documented as "stale binaries found". An
    environment error must never read as a finding. Regression against the
    repo-wide HOME guard, which this file must satisfy."""
    proc = tmp_path / "proc"
    proc.mkdir()
    env = {"PATH": "/usr/bin:/bin"}  # HOME deliberately absent

    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(proc)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert res.returncode != EXIT_STALE, "an environment error reported as a stale finding"
    assert res.returncode == EXIT_UNDETERMINED


# ── half two: must never false-alarm ──────────────────────────────────────


def test_all_processes_on_canonical_is_ok(world):
    _make_proc(world["proc"], 601, exe=world["canonical"])
    _make_proc(world["proc"], 602, exe=world["canonical"])

    res = _run(world)

    assert res.returncode == EXIT_OK, res.stderr
    assert "current=2" in res.stdout
    assert "stale=0" in res.stdout


def test_an_unrelated_process_is_positively_not_cc(world):
    """A readable exe whose basename is not a CC name is PROVEN not-CC. It must
    be ignored silently — counting it would fail an otherwise clean soak."""
    vim = _make_binary(world["tmp"] / "other" / "vim", "vim")
    _make_proc(world["proc"], 701, exe=vim, cmdline="vim\x00")

    res = _run(world)

    assert res.returncode == EXIT_OK, res.stdout


def test_a_process_merely_MENTIONING_claude_is_not_counted(world):
    """MEASURED false hard-fail in the previous design: another user's
    `grep -r claude /var/log` had an unreadable exe, hit a `*claude*` cmdline
    substring test, and wedged the whole sweep at exit 2. argv[0] is `grep`."""
    _make_proc(world["proc"], 801, exe=None, cmdline="grep\x00-r\x00claude\x00/var/log\x00")

    res = _run(world)

    assert res.returncode == EXIT_OK, res.stdout + res.stderr


def test_a_vanished_process_does_not_leak_a_shell_error(world):
    """A pid dir with nothing in it — the process exited mid-sweep. Routine.

    Regression guard: written as `tr … < file 2>/dev/null` bash applies
    redirections left to right, so the stderr redirect cannot suppress the INPUT
    redirect's own "No such file". The `2>` must precede the `<`.
    """
    (world["proc"] / "901").mkdir()

    res = _run(world)

    assert "No such file" not in res.stderr
    assert "cmdline" not in res.stderr


def test_empty_proc_root_does_not_glob_literally(world):
    res = _run(world)

    assert res.returncode == EXIT_OK
    assert "current=0" in res.stdout


def test_rejects_an_unknown_argument(world):
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--bogus"], capture_output=True, text=True, timeout=60
    )

    assert res.returncode == EXIT_UNDETERMINED
    assert "unknown argument" in res.stderr


def test_a_deleted_predecessor_is_still_recognised_as_cc(world):
    """The single line the whole incident depends on.

    npm renames-then-deletes, so a stale process's `readlink` yields
    `.../claude.exe (deleted)`. Without stripping that suffix the name test
    fails and the process is SILENTLY DROPPED — MEASURED: removing the strip
    turned the live box from `stale=2, exit 1` into
    `OK — all 8 live Claude Code process(es)`, exit 0, with the whole suite
    still green. A one-character regression converting the exact incident into a
    clean receipt, invisible to CI.
    """
    old = _make_binary(world["tmp"] / "old" / "claude.exe (deleted)", "replaced")
    _make_proc(world["proc"], 1101, exe=old)

    res = _run(world)

    assert res.returncode == EXIT_STALE, res.stdout
    assert "stale=1" in res.stdout


def test_a_wholly_unreadable_pid_dir_blocks_a_clean_verdict(world):
    """MEASURED invariant violation: the count sat inside a `[ -r stat ]` guard
    while `continue` sat outside it, so a pid dir with nothing readable at all
    incremented nothing and voted CLEAN — in the one branch written to enforce
    "cannot prove not-CC must not contribute to a clean verdict". That is the
    hidepid=1 shape: directory visible, contents EACCES."""
    (world["proc"] / "1201").mkdir(parents=True)

    res = _run(world)

    assert res.returncode == EXIT_UNDETERMINED, res.stdout
    assert "unclassifiable=1" in res.stdout


@pytest.mark.parametrize(
    "flags", [["--enable-source-maps"], ["--no-warnings"], ["--max-old-space-size=4096"]]
)
def test_interpreter_flags_do_not_hide_the_cc_script(world, flags):
    """MEASURED: testing argv[1] treated `node --enable-source-maps .../cli.js`
    as PROOF of not-CC, because the flag occupied the slot. Node CLIs routinely
    carry these. Narrowing a detector that way trades a loud false positive for
    a silent false negative — the wrong direction for a check whose worst
    outcome is a false all-clear."""
    node = _make_binary(world["tmp"] / "rt" / "node", "node")
    argv = "\x00".join(["node", *flags, "/opt/cc/cli.js"]) + "\x00"
    _make_proc(world["proc"], 1301, exe=node, cmdline=argv)

    res = _run(world)

    assert res.returncode == EXIT_UNDETERMINED, res.stdout


def test_a_kernel_thread_is_positively_not_cc(world):
    """Kernel threads have no exe and an empty cmdline — the unclassifiable
    shape. Parking them there makes the sweep return 2 on ANY host that has
    them, which is every non-container host and every CI runner. This container
    shows none, which is exactly why the earlier "0 of 109" measurement did not
    reveal it: that number was namespace-scoped and stated as if general.
    """
    d = world["proc"] / "1401"
    d.mkdir(parents=True)
    (d / "stat").write_text("1401 (kworker/0:1) S 2 0 0\n")
    (d / "cmdline").write_bytes(b"")
    (d / "status").write_text("Name:\tkworker/0:1\nKthread:\t1\n")  # no VmSize: no mm

    res = _run(world)

    assert res.returncode == EXIT_OK, res.stdout
    assert "unclassifiable=0" in res.stdout


def test_a_kernel_thread_is_detected_without_the_Kthread_field(world):
    """Portable fallback for kernels predating `Kthread:` — no VmSize means no
    mm, which means a kernel thread."""
    d = world["proc"] / "1501"
    d.mkdir(parents=True)
    (d / "stat").write_text("1501 (ksoftirqd/0) S 2 0 0\n")
    (d / "cmdline").write_bytes(b"")
    (d / "status").write_text("Name:\tksoftirqd/0\nState:\tS (sleeping)\n")

    res = _run(world)

    assert res.returncode == EXIT_OK, res.stdout


def test_an_all_nul_cmdline_does_not_abort_as_stale(world):
    """MEASURED fail-direction inversion: an all-NUL cmdline word-splits to zero
    tokens, so reading `$1` aborted under `set -u` with exit 1 — the code that
    means "stale binaries found". Same class as the HOME guard, which this test
    exists to generalise rather than leave pinned at one instance."""
    d = world["proc"] / "1601"
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\x00\x00")
    (d / "stat").write_text("1601 (x) S 1 0 0\n")
    (d / "status").write_text("Name:\tx\nVmSize:\t100 kB\n")  # userspace: has mm

    res = _run(world)

    assert res.returncode != EXIT_STALE, "a shell abort surfaced as a stale finding"
    assert res.returncode == EXIT_UNDETERMINED


def test_a_glob_in_an_unrelated_cmdline_does_not_manufacture_a_verdict(world, tmp_path):
    """MEASURED false alarm: `set -- $cmdline` word-splits AND glob-expands, so
    an unrelated `node * --flag` swept from a directory containing cli.js
    expanded into a CC-shaped argv and produced a refusal purely from the
    operator's working directory. The inverse hides a real one."""
    node = _make_binary(world["tmp"] / "rt" / "node", "node")
    _make_proc(world["proc"], 1701, exe=node, cmdline="node\x00*\x00--flag\x00")
    cwd = tmp_path / "trap"
    cwd.mkdir()
    (cwd / "cli.js").write_text("//\n")

    env = dict(os.environ)
    env["PATH"] = f"{world['bindir']}:{env['PATH']}"
    env["CC_PROBE_DIRS"] = str(world["bindir"])
    env["HOME"] = str(world["tmp"])
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(world["proc"])],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
    )

    assert res.returncode == EXIT_OK, res.stdout


def test_an_existing_but_wrong_proc_root_refuses(world):
    """MEASURED: the existence check alone left the likelier typo open —
    `--proc-root /tmp` produced a confident "OK — all 0", exit 0."""
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", "/tmp"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert res.returncode == EXIT_UNDETERMINED
    assert "does not look like a procfs" in res.stderr


# ── the third verdict: an installed-but-not-canonical copy ────────────────


def test_a_process_on_another_INSTALLED_copy_is_named_not_refused(world):
    """The previous design refused the WHOLE sweep when two installed copies
    disagreed on `--version` — a refusal that (a) no-opped entirely whenever
    either probe failed, and (b) discarded the per-process answer that actually
    resolves the question. Reporting which copy each process runs is strictly
    more evidence than refusing."""
    other = _make_binary(world["tmp"] / "other-prefix" / "claude", "second-install")
    probe2 = world["tmp"] / "probe2"
    probe2.mkdir()
    (probe2 / "claude").symlink_to(other)
    _make_proc(world["proc"], 1001, exe=other)

    env = dict(os.environ)
    env["PATH"] = f"{world['bindir']}:{env['PATH']}"
    env["CC_PROBE_DIRS"] = f"{world['bindir']}:{probe2}"
    env["HOME"] = str(world["tmp"])
    res = subprocess.run(
        ["bash", str(_SCRIPT), "--proc-root", str(world["proc"])],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert res.returncode == EXIT_STALE, res.stdout
    assert "OTHER-COPY" in res.stdout
    assert "cc_shadow_scan" in res.stderr, "point the operator at the fix"
