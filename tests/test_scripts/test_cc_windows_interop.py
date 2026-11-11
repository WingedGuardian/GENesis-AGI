"""A Windows Claude Code reached through WSL interop is not this install's CC.

WSL appends the Windows PATH to every Linux shell by default, so on a box where
Claude Code is installed on Windows but not inside the distro, ``command -v
claude`` resolves to the Windows npm shim under the drive mount
(``/mnt/c/Users/<First Last>/AppData/Roaming/npm/claude``). Genesis took that as
its own copy: ``cc_ensure_local`` reported "already at pin" and installed no
Linux CC, the unit renderers baked the Windows directory into every unit's PATH,
and on a version mismatch ``cc_ensure_local`` would have run ``npm install -g
--prefix`` against the Windows user's npm tree.

``cc_resolve_claude`` (scripts/lib/cc_version.sh) skips a copy on a Windows
drive mount, which ``findmnt`` reports as fstype ``drvfs`` (WSL1) or a 9p mount
with ``aname=drvfs`` (WSL2). A fake ``findmnt`` stands in for the real one so
the tests run on any Linux box.

Harness shape follows test_cc_shadow_scan.py: the REAL lib, sourced with a fake
$HOME, a minimal PATH and ``CC_PROBE_DIRS`` pointed at fake dirs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tests.test_scripts.test_cc_shadow_scan import _minimal_bin, _plant_npm_tree, _write_exec

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "lib" / "cc_version.sh"
_PIN = "2.1.201"


def _fake_findmnt(tmp_path: Path, win_root: Path) -> Path:
    """`findmnt -n -o FSTYPE,OPTIONS -T <path>`: drvfs under win_root, ext4 elsewhere."""
    return _write_exec(
        tmp_path / "sysbin" / "findmnt",
        "#!/usr/bin/env bash\n"
        't="${@: -1}"\n'
        f'case "$t" in "{win_root}"/*) echo "9p rw,noatime,aname=drvfs;path=C:\\;uid=1000" ;;\n'
        '  *) echo "ext4 rw,relatime" ;; esac\n',
    )


def _windows_claude(win_root: Path, version: str = _PIN) -> Path:
    # The real shim's directory has a space in it, which is half of the defect.
    return _write_exec(
        win_root / "Users" / "First Last" / "AppData" / "Roaming" / "npm" / "claude",
        f'#!/usr/bin/env bash\necho "{version} (Claude Code)"\n',
    )


def _fake_npm(tmp_path: Path, home: Path) -> Path:
    return _write_exec(
        tmp_path / "npmbin" / "npm",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{tmp_path}/npm.log"\n'
        f'if [ "$1" = "config" ]; then echo "{home}/.npm-global"; fi\n',
    )


def _run(tmp_path: Path, script: str, path_dirs: list[Path], *, probe: Path | None = None
         ) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    harness = tmp_path / "harness.sh"
    harness.write_text(f'#!/usr/bin/env bash\n. "{_LIB}"\n{script}\n', encoding="utf-8")
    env = {
        "PATH": ":".join(str(d) for d in [*path_dirs, _minimal_bin(tmp_path)]),
        "HOME": str(home),
        "CC_PROBE_DIRS": str(probe or tmp_path / "probe-nowhere"),
        "CC_VERSION": _PIN,
    }
    return subprocess.run(["bash", str(harness)], env=env, capture_output=True, text=True,
                          timeout=30)


def test_resolve_skips_a_windows_copy_ahead_of_the_linux_one(tmp_path):
    win_root = tmp_path / "mnt" / "c"
    win = _windows_claude(win_root)
    linux = _write_exec(tmp_path / "linuxbin" / "claude", '#!/usr/bin/env bash\necho x\n')
    findmnt = _fake_findmnt(tmp_path, win_root)
    res = _run(tmp_path, "cc_resolve_claude", [findmnt.parent, win.parent, linux.parent])
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == str(linux)


def test_resolve_with_only_a_windows_copy_finds_nothing_and_says_why(tmp_path):
    win_root = tmp_path / "mnt" / "c"
    win = _windows_claude(win_root)
    findmnt = _fake_findmnt(tmp_path, win_root)
    res = _run(tmp_path, "cc_resolve_claude", [findmnt.parent, win.parent])
    assert res.returncode == 1
    assert res.stdout == ""
    assert str(win) in res.stderr and "Windows" in res.stderr


def test_resolve_returns_an_ordinary_copy(tmp_path):
    # Control: on a box with no drive mount the answer is command -v's answer.
    win_root = tmp_path / "mnt" / "c"
    linux = _write_exec(tmp_path / "linuxbin" / "claude", '#!/usr/bin/env bash\necho x\n')
    findmnt = _fake_findmnt(tmp_path, win_root)
    res = _run(tmp_path, "cc_resolve_claude", [findmnt.parent, linux.parent])
    assert res.returncode == 0 and res.stdout.strip() == str(linux)


def test_ensure_local_installs_a_linux_copy_when_only_windows_has_one_at_the_pin(tmp_path):
    win_root = tmp_path / "mnt" / "c"
    win = _windows_claude(win_root, _PIN)
    home = tmp_path / "home"
    npm = _fake_npm(tmp_path, home)
    findmnt = _fake_findmnt(tmp_path, win_root)
    # WSL appends the Windows PATH last; the Windows copy is still the only one.
    res = _run(tmp_path, "cc_ensure_local", [npm.parent, findmnt.parent, win.parent])
    assert "already at pin" not in res.stdout, res.stdout
    assert "not installed — installing pinned" in res.stdout, res.stdout + res.stderr
    log = (tmp_path / "npm.log").read_text()
    assert f"--prefix {home}/.npm-global" in log
    assert str(win_root) not in log


def test_ensure_local_never_installs_into_the_windows_npm_prefix(tmp_path):
    # Off-pin Windows copy: before the fix this took the "drift" branch and
    # reinstalled into dirname(dirname(<windows shim>)), the Windows npm tree.
    win_root = tmp_path / "mnt" / "c"
    win = _windows_claude(win_root, "2.1.100")
    home = tmp_path / "home"
    npm = _fake_npm(tmp_path, home)
    findmnt = _fake_findmnt(tmp_path, win_root)
    _run(tmp_path, "cc_ensure_local", [npm.parent, findmnt.parent, win.parent])
    log = (tmp_path / "npm.log").read_text()
    assert "install -g" in log
    assert str(win_root) not in log, f"npm was pointed at the Windows tree: {log}"


def test_shadow_scan_never_crowns_a_windows_copy_canonical(tmp_path):
    # Windows copy at the pin, an older Linux npm copy on a scanned surface.
    # Crowning the Windows copy would remove the Linux one as a "shadow" and
    # leave the install with no Linux CC at all.
    win_root = tmp_path / "mnt" / "c"
    win = _windows_claude(win_root, _PIN)
    findmnt = _fake_findmnt(tmp_path, win_root)
    linux_link = _plant_npm_tree(tmp_path / "home" / ".npm-global", "2.1.150")
    res = _run(tmp_path, "cc_shadow_scan", [findmnt.parent, win.parent])
    assert linux_link.is_symlink(), res.stdout + res.stderr
    assert "REFUSING to remove anything" in res.stderr


def test_without_findmnt_nothing_is_detected_as_before(tmp_path):
    # Documented fail-open: no findmnt means no detection, which is exactly the
    # behaviour before the check existed (never worse than main).
    win_root = tmp_path / "mnt" / "c"
    win = _windows_claude(win_root)
    res = _run(tmp_path, "cc_resolve_claude", [win.parent])
    assert res.returncode == 0 and res.stdout.strip() == str(win)


def test_a_fresh_install_without_usable_sudo_goes_to_the_user_prefix(tmp_path):
    # npm's prefix is the system /usr, there is no sudo and no terminal (bootstrap
    # under update.sh): installing there can only fail and leave no Linux CC.
    home = tmp_path / "home"
    npm = _write_exec(
        tmp_path / "npmbin" / "npm",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{tmp_path}/npm.log"\n'
        'if [ "$1" = "config" ]; then echo /usr; fi\n',
    )
    res = _run(tmp_path, "cc_ensure_local < /dev/null", [npm.parent])
    log = (tmp_path / "npm.log").read_text()
    assert f"install -g --prefix {home}/.npm-global " in log, log + res.stderr
    assert "user prefix" in res.stderr


def _mcp_harness(tmp_path: Path, path_dirs: list[Path], call: str, *,
                 probe: str | None = None) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    tools = tmp_path / "pytools"
    tools.mkdir(exist_ok=True)
    for tool in ("python3", "basename", "id", "cp"):
        src = Path(shutil.which(tool) or f"/usr/bin/{tool}")
        if not (tools / tool).exists():
            (tools / tool).symlink_to(src)
    harness = tmp_path / "mcp.sh"
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_LIB}"\n. "{_REPO_ROOT}/scripts/lib/mcp_register.sh"\n{call}\n',
        encoding="utf-8")
    env = {"PATH": ":".join(str(d) for d in [*path_dirs, tools, _minimal_bin(tmp_path)]),
           "HOME": str(home), "CC_PROBE_DIRS": probe or str(tmp_path / "probe-nowhere"),
           "CC_VERSION": _PIN}
    return subprocess.run(["bash", str(harness)], env=env, capture_output=True, text=True,
                          timeout=30)


def _logging_claude(path: Path, log: Path) -> Path:
    return _write_exec(path, f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n')


def test_mcp_registration_uses_the_linux_copy_and_never_the_windows_one(tmp_path):
    win_root = tmp_path / "mnt" / "c"
    win_log, linux_log = tmp_path / "win.log", tmp_path / "linux.log"
    win = _logging_claude(win_root / "Users" / "First Last" / "npm" / "claude", win_log)
    linux = _logging_claude(tmp_path / "linuxbin" / "claude", linux_log)
    findmnt = _fake_findmnt(tmp_path, win_root)
    res = _mcp_harness(tmp_path, [findmnt.parent, win.parent, linux.parent],
                       '_register_mcp "demo" "user" "/opt/demo/run"\n'
                       '_register_mcp_http "demo-http" "user" "https://example.invalid/mcp"')
    assert not win_log.exists(), win_log.read_text()
    calls = linux_log.read_text()
    assert "mcp add demo -s user -- /opt/demo/run" in calls, calls + res.stdout + res.stderr
    assert "mcp add --transport http demo-http -s user https://example.invalid/mcp" in calls


def test_mcp_registration_with_only_a_windows_copy_is_skipped(tmp_path):
    win_root = tmp_path / "mnt" / "c"
    win_log = tmp_path / "win.log"
    win = _logging_claude(win_root / "Users" / "First Last" / "npm" / "claude", win_log)
    findmnt = _fake_findmnt(tmp_path, win_root)
    res = _mcp_harness(tmp_path, [findmnt.parent, win.parent],
                       '_register_mcp "demo" "user" "/opt/demo/run"')
    assert not win_log.exists(), win_log.read_text()
    assert "skipping demo registration" in res.stdout


def test_a_user_prefix_install_is_what_mcp_registration_then_uses(tmp_path):
    # The no-sudo fallback installs into ~/.npm-global, which is NOT on this
    # shell's PATH. The same run's MCP registration must still find that copy.
    home = tmp_path / "home"
    claude_log = tmp_path / "claude.log"
    planted = _write_exec(
        tmp_path / "npm-payload" / "claude",
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = --version ]; then echo "{_PIN} (Claude Code)"; exit 0; fi\n'
        f'echo "$*" >> "{claude_log}"\n',
    )
    npm = _write_exec(
        tmp_path / "npmbin" / "npm",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "config" ]; then echo /usr; exit 0; fi\n'
        # `npm install -g --prefix P pkg@V`: plant P/bin/claude like npm would.
        f'mkdir -p "$4/bin" && cp "{planted}" "$4/bin/claude"\n',
    )
    # The default CC_PROBE_DIRS carries $HOME/.npm-global/bin; the machine's own
    # /usr/local/bin and /usr/bin entries are left out so the test is hermetic.
    res = _mcp_harness(tmp_path, [npm.parent],
                       'cc_ensure_local < /dev/null\n_register_mcp "demo" "user" "/opt/demo/run"',
                       probe=f"{home}/.npm-global/bin")
    assert (home / ".npm-global" / "bin" / "claude").exists(), res.stdout + res.stderr
    assert "now at pin" in res.stdout, res.stdout + res.stderr
    assert "mcp add demo -s user -- /opt/demo/run" in claude_log.read_text(), res.stdout + res.stderr


def test_sudo_is_never_asked_for_a_password_without_a_terminal(tmp_path):
    sudo_log = tmp_path / "sudo.log"
    sudo = _write_exec(tmp_path / "sudobin" / "sudo",
                       f'#!/usr/bin/env bash\necho "$*" >> "{sudo_log}"\nexit 1\n')
    res = _run(tmp_path, "_cc_sudo_ready < /dev/null; echo rc=$?", [sudo.parent])
    assert "rc=1" in res.stdout
    assert sudo_log.read_text().split() == ["-n", "true"]


def test_installer_entry_points_stay_executable():
    # update.sh runs "$GENESIS_ROOT/scripts/bootstrap.sh" directly; an edit made
    # through a tool that drops the mode bit (e.g. a Windows editor on a WSL path)
    # turns that into "Permission denied" mid-update.
    for name in ("bootstrap.sh", "install.sh", "update.sh"):
        assert os.access(_REPO_ROOT / "scripts" / name, os.X_OK), name


def test_both_renderers_resolve_the_unit_cc_dir_through_the_helper():
    # The unit renderers decide CC_BIN_DIR, which every unit's PATH carries; a
    # bare `command -v claude` there is what baked the Windows dir into the units.
    # `command -v` stays only as the fallback for a checkout with no cc_version.sh.
    for name in ("install.sh", "bootstrap.sh"):
        src = (_REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert src.count('_cc_path="$(cc_resolve_claude || true)"') == 1, name
        assert src.count('_cc_path="$(command -v claude') == 1, name
        assert src.index("declare -F cc_resolve_claude") < src.index('_cc_path="$(command -v claude'), name
