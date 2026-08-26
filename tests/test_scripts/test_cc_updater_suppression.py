"""CC auto-updater suppression is re-asserted on every align, not just at setup.

``DISABLE_AUTOUPDATER`` and ``DISABLE_UPDATES`` must BOTH be present in the
USER-level ``~/.claude/settings.json``: repo/project settings apply only when CC
is launched from that directory, and the auto-updater runs in contexts where they
do not. Two real incidents came from exactly that gap (a container that
self-bumped mid-session, and a host VM found several minor versions past the
script pin while running the Guardian recovery brain).

install.sh / host-setup.sh set the keys at SETUP time. Nothing re-asserted them
afterwards, so an install whose settings later drifted stayed silently
unprotected — the npm pin only governs what a DELIBERATE install writes. These
tests pin the fix: ``cc_ensure_updater_suppressed`` owns both keys, and
``cc_ensure_local`` (the align path that install/bootstrap/update all run) calls
it BEFORE any early return, so the steady "already at pin" state — precisely when
drift would otherwise rot unnoticed — still reconciles.

Harness mirrors test_cc_shadow_scan.py: sources the REAL lib with a fake $HOME
and a minimal PATH, so nothing depends on the machine's actual CC install.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "lib" / "cc_version.sh"

_TOOLS = ("bash", "sh", "env", "python3", "mkdir", "dirname", "cat", "rm")


def _minimal_bin(tmp_path: Path) -> Path:
    """PATH dir with the tools the function needs — deliberately NO npm."""
    d = tmp_path / "minbin"
    d.mkdir(exist_ok=True)
    for tool in _TOOLS:
        dest = d / tool
        if dest.exists() or dest.is_symlink():
            continue  # reusable across repeated calls in one test
        for src_dir in ("/usr/bin", "/bin", "/usr/local/bin"):
            src = Path(src_dir) / tool
            if src.exists():
                dest.symlink_to(src)
                break
    return d


def _run(tmp_path: Path, snippet: str, *, path_dir: Path | None = None):
    """Source the real lib under a fake HOME and run *snippet*."""
    bindir = path_dir or _minimal_bin(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "-c", f'set -u; source "{_LIB}"; {snippet}'],
        capture_output=True,
        text=True,
        env={"HOME": str(fake_home), "PATH": str(bindir), "CC_VERSION": "9.9.9"},
        timeout=60,
    )


def _settings(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".claude" / "settings.json"


class TestSuppressionReconcile:
    """cc_ensure_updater_suppressed owns both keys, idempotently."""

    def test_creates_settings_when_absent(self, tmp_path: Path) -> None:
        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        env = json.loads(_settings(tmp_path).read_text())["env"]
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["DISABLE_UPDATES"] == "1"

    def test_repairs_the_half_configured_case(self, tmp_path: Path) -> None:
        """The real-world drift: AUTOUPDATER set, UPDATES missing."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))

        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        assert json.loads(s.read_text())["env"]["DISABLE_UPDATES"] == "1"
        # LOUD on repair — a silent fix would let the regression recur unseen.
        assert "DISABLE_UPDATES" in r.stderr
        assert "MISSING" in r.stderr

    def test_preserves_unrelated_settings(self, tmp_path: Path) -> None:
        """Never trample the operator's other config."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(
            json.dumps(
                {
                    "env": {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "2", "FOO": "bar"},
                    "permissions": {"allow": ["Bash(ls:*)"]},
                    "statusLine": {"type": "command"},
                }
            )
        )

        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        data = json.loads(s.read_text())
        assert data["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "2"
        assert data["env"]["FOO"] == "bar"
        assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
        assert data["statusLine"] == {"type": "command"}
        assert data["env"]["DISABLE_UPDATES"] == "1"

    def test_idempotent_and_quiet_when_already_correct(self, tmp_path: Path) -> None:
        """Runs on EVERY align — must not spam a correct machine."""
        _run(tmp_path, "cc_ensure_updater_suppressed")
        before = _settings(tmp_path).read_text()

        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        assert _settings(tmp_path).read_text() == before
        assert "MISSING" not in r.stderr

    def test_never_clobbers_an_unparseable_file(self, tmp_path: Path) -> None:
        """Destroying operator settings is worse than the drift — report, don't overwrite."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text("{ this is not json")

        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 1
        assert s.read_text() == "{ this is not json"
        assert "left untouched" in r.stderr

    def test_preserves_file_mode(self, tmp_path: Path) -> None:
        """A 0600 settings.json must NOT come back 0644.

        settings.json's ``env`` block is where operators put provider API keys, and
        an atomic replace installs a FRESH inode — which takes ``0666 & ~umask``
        (0644 typically) unless the mode is carried across explicitly. Widening a
        credential store while "fixing" a security setting is a bad trade.
        """
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
        s.chmod(0o600)

        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        assert json.loads(s.read_text())["env"]["DISABLE_UPDATES"] == "1"
        assert stat.S_IMODE(s.stat().st_mode) == 0o600

    def test_new_file_is_private(self, tmp_path: Path) -> None:
        """A file we create from scratch starts 0600 — it is secrets-adjacent."""
        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        assert stat.S_IMODE(_settings(tmp_path).stat().st_mode) == 0o600

    def test_follows_a_symlinked_settings_file(self, tmp_path: Path) -> None:
        """A dotfiles-managed settings.json must survive as a symlink.

        Write-by-rename onto the link would replace it with a regular file, forking
        the dotfiles copy (left holding stale content) from the live one — silently
        and permanently, so every later dotfiles edit is invisible to CC.
        """
        real = tmp_path / "dotfiles" / "settings.json"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
        link = _settings(tmp_path)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)

        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        assert link.is_symlink(), "symlink was replaced by a regular file"
        # The repair landed in the dotfiles SOURCE, not a forked copy.
        assert json.loads(real.read_text())["env"]["DISABLE_UPDATES"] == "1"

    def test_leaves_no_stray_temp_file(self, tmp_path: Path) -> None:
        """The temp must never be left behind holding the settings contents."""
        _run(tmp_path, "cc_ensure_updater_suppressed")
        leftovers = [
            p.name for p in _settings(tmp_path).parent.iterdir() if p.name != "settings.json"
        ]
        assert leftovers == [], f"stray temp files: {leftovers}"

    def test_accepts_an_explicit_path_for_the_host_leg(self, tmp_path: Path) -> None:
        target = tmp_path / "hosthome" / ".claude" / "settings.json"
        r = _run(tmp_path, f'cc_ensure_updater_suppressed "{target}"')
        assert r.returncode == 0, r.stderr
        assert json.loads(target.read_text())["env"]["DISABLE_UPDATES"] == "1"


class TestAlignPathReconciles:
    """The regression guard: the align path must reconcile BEFORE its early returns."""

    def test_cc_ensure_local_reconciles_even_when_it_cannot_align(self, tmp_path: Path) -> None:
        """npm absent -> cc_ensure_local returns early, but suppression is STILL restored.

        This is the whole point of calling the reconciler first: the common
        steady-state paths (already at pin, npm missing, CC_VERSION unset) all
        return early, and those are exactly when settings drift goes unnoticed.
        """
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))

        # _minimal_bin deliberately has no npm -> cc_ensure_local takes its
        # "npm not found" early return.
        r = _run(tmp_path, "cc_ensure_local")
        assert r.returncode == 0, r.stderr
        assert "npm not found" in r.stderr  # confirms the early return fired
        assert json.loads(s.read_text())["env"]["DISABLE_UPDATES"] == "1"

    def test_cc_ensure_local_reconciles_when_version_unset(self, tmp_path: Path) -> None:
        """CC_VERSION unset is the earliest return of all — still reconciles."""
        bindir = _minimal_bin(tmp_path)
        fake_home = tmp_path / "home"
        fake_home.mkdir(exist_ok=True)
        r = subprocess.run(
            ["bash", "-c", f'set -u; source "{_LIB}"; unset CC_VERSION; cc_ensure_local'],
            capture_output=True,
            text=True,
            env={"HOME": str(fake_home), "PATH": str(bindir)},
            timeout=60,
        )
        assert r.returncode == 0, r.stderr
        env = json.loads(_settings(tmp_path).read_text())["env"]
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["DISABLE_UPDATES"] == "1"
