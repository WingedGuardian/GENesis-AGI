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
import pathlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

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


# A stand-in `python3` that splices a racing writer into the reconciler's own
# embedded script, immediately before its compare-and-swap re-check. The
# function pipes that script in on stdin, so intercepting it here forces the
# race window deterministically WITHOUT adding a test-only branch to production
# code. `%(marker)s` makes the racer fire exactly once, so the bounded retry can
# still converge. Used by TestLostUpdate.
_CAS_SHIM_TEMPLATE = """#!%(python)s
import subprocess, sys

ANCHOR = "        after, _ = identity()"
INJECT = (
    "        import json as _rj, os as _ro\\n"
    "        if not _ro.path.exists('%(marker)s'):\\n"
    "            open('%(marker)s', 'w').close()\\n"
    "            _rt = path + '.racer'\\n"
    "            _rj.dump({'env': {'RACER': '1'}}, open(_rt, 'w'))\\n"
    "            _ro.replace(_rt, path)\\n"
)

script = sys.stdin.read()
if ANCHOR not in script:
    sys.stderr.write("CAS SHIM: anchor line not found in the lib\\n")
    sys.exit(97)
script = script.replace(ANCHOR, INJECT + ANCHOR, 1)

# The caller invokes `python3 - <settings_file> [defaults...]`, so argv[1] is
# ALREADY the "-" meaning read-from-stdin. Passing argv[1:] through together
# with our own "-" hands the real interpreter a second one, and it then reads
# argv[1] == "-" as the settings path instead of the real file.
rest = sys.argv[2:] if sys.argv[1:2] == ["-"] else sys.argv[1:]
sys.exit(subprocess.run(
    ["%(python)s", "-"] + rest, input=script, text=True
).returncode)
"""


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


class TestSuppressionState:
    """``CC_SUPPRESSION_STATE`` is the ONLY channel by which this reconcile
    reaches a human (update.sh folds it into HOST_CC_DEGRADED; the align timer
    turns it into unit state). The suite never asserted on it, so every one of
    those consumers rested on an unpinned contract."""

    def _state(self, tmp_path: Path, snippet: str) -> str:
        r = _run(tmp_path, f'{snippet}; echo "STATE=${{CC_SUPPRESSION_STATE:-unset}}"')
        for line in r.stdout.splitlines():
            if line.startswith("STATE="):
                return line.split("=", 1)[1]
        raise AssertionError(f"no STATE line; stdout={r.stdout!r} stderr={r.stderr!r}")

    def test_ok_when_already_correct(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}}))
        assert self._state(tmp_path, "cc_ensure_updater_suppressed || true") == "ok"

    def test_repaired_when_it_writes(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
        assert self._state(tmp_path, "cc_ensure_updater_suppressed || true") == "repaired"

    def test_failed_on_unparseable(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text("{ not json")
        assert self._state(tmp_path, "cc_ensure_updater_suppressed || true") == "failed"

    def test_failure_names_its_actual_cause(self, tmp_path: Path) -> None:
        """One catch-all message made a full disk read as file corruption."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text("{ not json")
        r = _run(tmp_path, "cc_ensure_updater_suppressed || true")
        assert "not valid JSON" in r.stderr, r.stderr


class TestMalformedShapes:
    """A dict is not enough of a check — these all reached the write path.

    Each case asserts the STATE as well as non-clobber. Asserting only "the file
    was not overwritten" is satisfiable by a regression that returns `ok`: the
    file would be intact AND the unit green, with suppression not in effect —
    the precise false-green this whole feature exists to prevent.
    """

    def _write(self, tmp_path: Path, payload: str) -> Path:
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(payload)
        return s

    def _state(self, tmp_path: Path) -> str:
        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; '
            'echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
        )
        for line in r.stdout.splitlines():
            if line.startswith("STATE="):
                return line.split("=", 1)[1]
        raise AssertionError(f"no STATE line; stderr={r.stderr!r}")

    def test_top_level_list_is_refused(self, tmp_path: Path) -> None:
        s = self._write(tmp_path, '["not", "an", "object"]')
        # No returncode assertion: the snippet ends in `|| true`, so it is 0
        # unconditionally and would pass however the function behaved.
        assert self._state(tmp_path) == "failed", (
            "a shape the write path cannot handle must report `failed`; `ok` "
            "would be a green unit over unsuppressed settings"
        )
        assert s.read_text() == '["not", "an", "object"]', "must not clobber"

    def test_env_null_is_refused(self, tmp_path: Path) -> None:
        s = self._write(tmp_path, '{"env": null}')
        assert self._state(tmp_path) == "failed"
        assert json.loads(s.read_text())["env"] is None, "must not clobber"

    def test_env_list_is_refused(self, tmp_path: Path) -> None:
        s = self._write(tmp_path, '{"env": []}')
        assert self._state(tmp_path) == "failed"
        assert json.loads(s.read_text())["env"] == [], "must not clobber"


class TestSetIfAbsentDefaults:
    """install.sh folds the subagent-nesting default into the SAME write as the
    suppression keys, so one write contract (mode/xattr carry-over, CAS, fsync)
    covers both rather than this file keeping a second, weaker copy of it."""

    def test_default_is_applied_with_the_suppression_keys(self, tmp_path: Path) -> None:
        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed "" "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=2" || true',
        )
        assert r.returncode == 0, r.stderr
        env = json.loads(_settings(tmp_path).read_text())["env"]
        assert env["DISABLE_UPDATES"] == "1"
        assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "2"

    def test_an_operator_override_of_the_default_survives(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(
            json.dumps(
                {
                    "env": {
                        "DISABLE_AUTOUPDATER": "1",
                        "DISABLE_UPDATES": "1",
                        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "0",
                    }
                }
            )
        )
        _run(
            tmp_path,
            'cc_ensure_updater_suppressed "" "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=2" || true',
        )
        env = json.loads(s.read_text())["env"]
        assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "0", "set-if-absent, never overwrite"

    def test_host_leg_passes_no_default_so_none_is_added(self, tmp_path: Path) -> None:
        """The host's recovery `claude -p` is single-brain and never nests."""
        _run(tmp_path, "cc_ensure_updater_suppressed || true")
        env = json.loads(_settings(tmp_path).read_text())["env"]
        assert set(env) == {"DISABLE_AUTOUPDATER", "DISABLE_UPDATES"}, env


class TestLostUpdate:
    """The write is a read-modify-write on a file OTHER processes rewrite (CC
    persists settings on a /config change or a permission grant). os.replace is
    atomic for readers but is not a compare-and-swap, so a stale in-memory copy
    would silently revert a concurrent writer."""

    _ROUNDS = 120

    def _race_once(self, tmp_path: Path) -> int:
        """Run one reconcile against an active writer; return the round left in
        the file. The writer counts monotonically and STOPS, so afterwards the
        file must show its LAST round: either the writer wrote last (its round),
        or we wrote last having re-read (its round, carried through). A LOWER
        round means our stale snapshot landed on top and rolled it back."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))

        writer = subprocess.Popen(
            [
                "python3",
                "-c",
                (
                    "import json,os,sys,time\n"
                    "p, n = sys.argv[1], int(sys.argv[2])\n"
                    "for i in range(n):\n"
                    "    t = p + '.w'\n"
                    "    with open(t, 'w') as f:\n"
                    "        json.dump({'env': {'DISABLE_AUTOUPDATER': '1'}, 'round': i}, f)\n"
                    "    os.replace(t, p)\n"
                    "    time.sleep(0.004)\n"
                ),
                str(s),
                str(self._ROUNDS),
            ],
        )
        try:
            _run(tmp_path, "cc_ensure_updater_suppressed || true")
        finally:
            writer.wait(timeout=60)
        # A JSONDecodeError here IS a failure: the file must never be torn.
        return json.loads(s.read_text()).get("round", -1)

    def test_a_concurrent_writer_never_corrupts_or_erases_the_file(self, tmp_path: Path) -> None:
        """E2E under real contention, asserting only what ALWAYS holds.

        Deliberately NOT asserting "the writer's last round survives". MEASURED:
        that assertion neither reliably catches a missing compare-and-swap (5
        passes stayed green with the CAS deleted) nor is it always true when the
        CAS is present — the few syscalls between the final identity check and
        os.replace are an irreducible window, so a strict version would flake in
        the other direction. ``test_the_write_path_compare_and_swaps`` is the
        deterministic guard; this exercises the real path and pins the invariants
        that cannot break either way: the file stays parseable, and a concurrent
        writer is never erased wholesale.
        """
        got = self._race_once(tmp_path / "race")
        assert got >= 0, "the concurrent writer's data was erased entirely"

    def test_the_write_path_compare_and_swaps(self) -> None:
        """Deterministic companion to the race above: the re-check must exist.
        os.replace is atomic for readers but is NOT a compare-and-swap."""
        src = _LIB.read_text()
        assert "after, _ = identity()" in src and "if after != before:" in src, (
            "the settings write must re-check file identity immediately before "
            "os.replace and retry, or a concurrent writer is silently reverted"
        )

    def test_a_writer_that_wins_the_race_is_not_reverted(self, tmp_path: Path) -> None:
        """BEHAVIOURAL proof of the CAS — the property, not its source text.

        The test above is a substring assertion: it passes if the re-check is
        present but neutered. The race test above it cannot detect a missing CAS
        either (its own docstring records 5 green passes with the CAS deleted),
        because the window is microseconds wide and cannot be hit on demand.

        So force the window from the TEST side rather than adding a seam to
        production code. The function pipes its Python to `python3` on stdin, and
        the harness already builds the PATH — so a shim named `python3` can read
        that script, splice in a racing write at the exact line after the
        identity re-check, and hand it to the real interpreter. Production code
        is untouched and has no idea this happened.

        A racing writer lands a UNIQUE key inside the window, once. With the CAS
        the write is abandoned, the file is re-read, and that key survives into
        the final content. Without it, the stale in-memory copy is renamed over
        the top and the key is gone.
        """
        real_python = shutil.which("python3")
        assert real_python, "python3 required"

        bindir = _minimal_bin(tmp_path)
        (bindir / "python3").unlink(missing_ok=True)
        marker = tmp_path / "raced.once"
        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))

        # Runs INSIDE the reconciler, immediately BEFORE its identity re-check.
        # The side matters: a writer landing AFTER that check but before the
        # rename is the irreducible residual the code documents and cannot
        # defend against. A writer landing BEFORE it is what the compare-and-swap
        # exists to catch, so that is where this injects.
        shim = bindir / "python3"
        shim.write_text(_CAS_SHIM_TEMPLATE % {"python": real_python, "marker": str(marker)})
        shim.chmod(0o755)

        _run(tmp_path, "cc_ensure_updater_suppressed || true", path_dir=bindir)

        assert marker.exists(), "the shim never fired — the race was not forced"
        final = json.loads(settings.read_text())
        env = final.get("env", {})
        assert env.get("RACER") == "1", (
            "the racing writer's key was LOST — the stale in-memory copy was "
            "renamed over the top, i.e. the compare-and-swap did not take effect"
        )


class TestCallerWiring:
    """Every consumer of the outcome, pinned. The reconcile reached a human only
    through these, and none of them were asserted on."""

    def test_update_sh_folds_the_state_into_degraded_subsystems(self) -> None:
        src = (_REPO_ROOT / "scripts" / "update.sh").read_text()
        assert "CC_SUPPRESSION_STATE" in src
        assert "cc_updater_suppression_" in src, "non-ok outcome must reach update_history"

    def test_the_recurring_reconcile_has_its_own_container_unit(self) -> None:
        script = _REPO_ROOT / "scripts" / "cc_settings_align.sh"
        assert script.exists(), "the recurring container-side reconcile script"
        assert "cc_ensure_updater_suppressed" in script.read_text()

    def test_cc_align_host_has_no_container_leg(self) -> None:
        """It is host-only by contract and its unit is hardened on that basis;
        a container-side filesystem write there invalidated both."""
        src = (_REPO_ROOT / "scripts" / "cc_align_host.sh").read_text()
        assert "cc_ensure_updater_suppressed" not in src

    def test_install_sh_passes_the_nesting_default_through_one_call(self) -> None:
        src = (_REPO_ROOT / "scripts" / "install.sh").read_text()
        assert (
            'cc_ensure_updater_suppressed "$_settings_file" "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=2"'
            in src
        )
        # The real invariant is not how often the key is NAMED (a comment and a
        # manual-fix hint legitimately mention it) but that install.sh no longer
        # opens its OWN read-modify-write of the settings file.
        assert 'python3 - "$_settings_file"' not in src, (
            "install.sh must not run a second read-modify-write on settings.json — "
            "it doubles the lost-update window and duplicates the write contract"
        )


class TestSettingsAlignUnit:
    """The unit is what makes the reconcile RECURRING. Rendered and enabled by
    the generic template loops, so the templates themselves are the contract."""

    _UNIT_DIR = _REPO_ROOT / "scripts" / "systemd"

    def test_both_templates_exist(self) -> None:
        for name in (
            "genesis-cc-settings-align.service.template",
            "genesis-cc-settings-align.timer.template",
        ):
            assert (self._UNIT_DIR / name).exists(), name

    def test_service_runs_the_align_script(self) -> None:
        svc = (self._UNIT_DIR / "genesis-cc-settings-align.service.template").read_text()
        assert "cc_settings_align.sh" in svc

    def test_sandbox_scope_documents_the_settings_write(self) -> None:
        """ReadWritePaths=%h is load-bearing: it is what permits the write to
        ~/.claude. Narrowing it to %h/.genesis — which a stale 'only writes its
        lock' comment would invite — silently breaks the reconcile."""
        svc = (self._UNIT_DIR / "genesis-cc-settings-align.service.template").read_text()
        rw = [ln for ln in svc.splitlines() if ln.startswith("ReadWritePaths=")]
        assert rw == ["ReadWritePaths=%h"], (
            f"expected exactly ReadWritePaths=%h, got {rw!r} — the unit writes BOTH "
            "~/.claude/settings.json and ~/.genesis/locks, so narrowing to either "
            "one silently breaks the reconcile"
        )
        assert ".claude" in svc, "the sandbox comment must name the settings write"

    def test_timer_is_persistent(self) -> None:
        """A box powered off across its window must still reconcile on boot."""
        tmr = (self._UNIT_DIR / "genesis-cc-settings-align.timer.template").read_text()
        assert "Persistent=true" in tmr


class TestSettingsAlignScriptRuns:
    """The align script was asserted on only as TEXT — never executed. Its exit
    codes ARE the mechanism (unit state is the sole signal on a box that goes
    weeks between deploys), so every one of them needs to be exercised."""

    _SCRIPT = _REPO_ROOT / "scripts" / "cc_settings_align.sh"

    def _run_script(self, home: Path, *, env_extra: dict | None = None):
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(self._SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def _seed(self, tmp_path: Path, payload: dict | str) -> Path:
        home = tmp_path / "h"
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        f = home / ".claude" / "settings.json"
        f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return home

    def test_a_missing_flock_never_reports_success(self, tmp_path: Path) -> None:
        """`if ! flock` cannot tell "lock held" (rc 1) from "no flock" (rc 127).

        Without an explicit probe, a box without util-linux printed "another run
        is in progress", exited 0, and left the file unrepaired — a GREEN unit
        over an unguarded auto-updater, which is the exact class this script's
        header promises it never produces ("a path that verified nothing must
        never report success"). The sibling test covers the renamed-function
        variant; this covers the missing-lock one the header names first.
        """
        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1"}})
        bindir = tmp_path / "noflock"
        bindir.mkdir()
        # Everything the script needs EXCEPT flock.
        for tool in ("bash", "sh", "python3", "mkdir", "cat", "dirname",
                     "id", "getent", "cut", "date", "printf", "rm"):
            src = shutil.which(tool)
            if src:
                (bindir / tool).symlink_to(src)

        r = self._run_script(home, env_extra={"PATH": str(bindir)})

        assert r.returncode != 0, (
            "a run that could not take the lock verified NOTHING and must not "
            f"exit 0; stdout={r.stdout!r}"
        )
        assert "flock not found" in r.stdout + r.stderr
        assert "another run is in progress" not in r.stdout, (
            "a missing flock must not be misreported as a concurrent run"
        )

    def test_parses_clean(self) -> None:
        r = subprocess.run(["bash", "-n", str(self._SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_exits_zero_and_quiet_when_already_correct(self, tmp_path: Path) -> None:
        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}})
        r = self._run_script(home)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.strip() == "", "a timer that logs every run trains you to ignore it"

    def test_repairs_and_exits_zero(self, tmp_path: Path) -> None:
        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1"}})
        r = self._run_script(home)
        assert r.returncode == 0, r.stdout + r.stderr
        env = json.loads((home / ".claude" / "settings.json").read_text())["env"]
        assert env["DISABLE_UPDATES"] == "1"

    def test_second_consecutive_repair_fails_the_unit(self, tmp_path: Path) -> None:
        """The doc calls a REPEAT repair the durable "something keeps rewriting
        this file" signal. During the gaps this timer exists for, update.sh is
        not running, so unit state is the only receiver that signal has."""
        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1"}})
        first = self._run_script(home)
        assert first.returncode == 0, first.stdout + first.stderr

        (home / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}})
        )  # something rewrote it again
        second = self._run_script(home)
        assert second.returncode != 0, "a repair that does not hold must fail the unit"
        assert "SECOND consecutive repair" in second.stdout

    def test_a_clean_run_resets_the_repeat_counter(self, tmp_path: Path) -> None:
        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1"}})
        self._run_script(home)  # repair
        assert self._run_script(home).returncode == 0  # ok -> resets
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}})
        )
        assert self._run_script(home).returncode == 0, "isolated repairs must not escalate"

    def test_unparseable_settings_fails_the_unit(self, tmp_path: Path) -> None:
        home = self._seed(tmp_path, "{ not json")
        r = self._run_script(home)
        assert r.returncode != 0
        assert (home / ".claude" / "settings.json").read_text() == "{ not json"

    def test_a_run_that_verified_NOTHING_never_reports_success(self, tmp_path: Path) -> None:
        """The failure this whole timer exists to prevent, one layer up: a green
        unit while the auto-updater is unguarded. If the reconciler is ever
        renamed or moved, this must fail loudly rather than tick over daily."""
        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}})
        fake_root = tmp_path / "fake"
        (fake_root / "scripts" / "lib").mkdir(parents=True)
        (fake_root / "scripts" / "lib" / "cc_version.sh").write_text(
            "# library present, but the function was renamed away\n"
        )
        script_src = self._SCRIPT.read_text()
        (fake_root / "scripts" / "cc_settings_align.sh").write_text(script_src)
        r = subprocess.run(
            ["bash", str(fake_root / "scripts" / "cc_settings_align.sh")],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            timeout=60,
        )
        assert r.returncode != 0, (
            "the reconciler was absent, so NOTHING was verified — exiting 0 here "
            "would leave the unit green while CC could self-update past the pin"
        )
        assert "NOT verified" in r.stdout


def _user_manager_reachable() -> bool:
    """Whether `systemd-analyze --user verify` can actually run here.

    Having the BINARY is not the same as being able to use it — the same
    distinction the CC invoker's systemd-run probe exists for, and it bites
    here for the same reason: without XDG_RUNTIME_DIR (an env-scrubbed spawner,
    CI) verify dies with "Failed to initialize manager" before it ever looks at
    the unit, which would fail this test for an environmental reason rather than
    a defect. So probe the exact capability, on a unit known to be valid —
    `unit-paths` succeeding proves nothing about `verify`.
    """
    if shutil.which("systemd-analyze") is None:
        return False
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        probe = pathlib.Path(d) / "genesis-probe.service"
        probe.write_text(
            "[Unit]\nDescription=probe\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
        )
        try:
            r = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(probe)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return r.returncode == 0 and "Failed to initialize manager" not in r.stderr


class TestUnitsAreLoadable:
    """Substring greps on template text pass on a unit systemd refuses to load.

    `systemd-analyze --user verify` on the RENDERED unit is the assertion that
    actually holds the shape — it parses the file the way the manager will, and
    catches a misspelled directive, a bad Type=, or a malformed timer expression
    that no `assert "X" in text` ever would.
    """

    _UNIT_DIR = _REPO_ROOT / "scripts" / "systemd"

    def _render(self, tmp_path: Path, name: str) -> Path:
        src = (self._UNIT_DIR / name).read_text()
        rendered = (
            src.replace("__REPO_DIR__", str(_REPO_ROOT))
            .replace("__VENV__", str(_REPO_ROOT / ".venv"))
        )
        out = tmp_path / name.replace(".template", "")
        out.write_text(rendered)
        return out

    @pytest.mark.skipif(
        not _user_manager_reachable(), reason="no reachable systemd user manager"
    )
    @pytest.mark.parametrize(
        "name",
        [
            "genesis-cc-settings-align.service.template",
            "genesis-cc-settings-align.timer.template",
        ],
    )
    def test_rendered_unit_is_valid(self, tmp_path: Path, name: str) -> None:
        unit = self._render(tmp_path, name)
        r = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(unit)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # verify exits non-zero on a genuinely malformed unit. Unresolvable
        # dependency warnings on a synthetic path are normal and go to stderr
        # without failing, so the return code is the signal.
        assert r.returncode == 0, (
            f"{name} does not parse as a systemd unit:\n{r.stderr}"
        )

    def test_timer_has_no_boot_relative_trigger(self) -> None:
        """OnBootSec is MACHINE-boot relative (OnStartupSec is the user-manager
        one), and a trigger already in the past elapses IMMEDIATELY on
        activation. install.sh and bootstrap.sh run `enable --now` on every
        bootstrap and update, so an OnBootSec here fired the reconcile seconds
        after the same deploy's align had already run it — manufacturing the
        very write contention the CAS exists to survive. Persistent=true already
        covers the powered-off case it was there for.
        """
        timer = (self._UNIT_DIR / "genesis-cc-settings-align.timer.template").read_text()
        directives = [
            ln.split("=", 1)[0].strip()
            for ln in timer.splitlines()
            if "=" in ln and not ln.lstrip().startswith("#")
        ]
        assert "OnBootSec" not in directives
        assert "Persistent" in directives, "the downtime catch-up must remain"


class TestUninstallRemovesTheUnit:
    """`enable` is not the inverse of deleting a file: a timer left enabled leaves
    a dangling timers.target.wants symlink. The sibling cc-tmp-align suite pins
    this for exactly that reason."""

    def test_uninstall_disables_the_new_units(self) -> None:
        txt = (_REPO_ROOT / "scripts" / "uninstall.sh").read_text()
        assert txt.count("genesis-cc-settings-align.timer") >= 2, (
            "both the container-local and the host-side uninstall paths must "
            "stop/disable the timer, not just delete its unit file"
        )
        assert "genesis-cc-settings-align.service" in txt


class TestUnitTemplateShape:
    """Placeholders and hardening, mirroring the sibling cc-tmp-align template
    test: a typo'd placeholder renders literally and the unit fails at start."""

    _UNIT_DIR = _REPO_ROOT / "scripts" / "systemd"
    _SUPPORTED = {"__HOME__", "__VENV__", "__REPO_DIR__", "__CC_BIN_DIR__"}

    def _templates(self):
        for name in (
            "genesis-cc-settings-align.service.template",
            "genesis-cc-settings-align.timer.template",
        ):
            yield name, (self._UNIT_DIR / name).read_text()

    def test_only_supported_render_placeholders(self) -> None:
        import re

        for name, txt in self._templates():
            found = set(re.findall(r"__[A-Z_]+__", txt))
            assert found <= self._SUPPORTED, f"{name}: unrenderable {found - self._SUPPORTED}"

    def test_service_is_a_hardened_oneshot(self) -> None:
        svc = dict(self._templates())["genesis-cc-settings-align.service.template"]
        for directive in (
            "Type=oneshot",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "TimeoutStartSec=",
        ):
            assert directive in svc, directive

    def test_service_has_no_install_section(self) -> None:
        """Nothing enables this service by name — only its timer is enabled by the
        generic loop. A WantedBy here would never take effect while telling a
        reader the reconcile also runs at boot."""
        svc = dict(self._templates())["genesis-cc-settings-align.service.template"]
        # A section HEADER, not the substring: the template deliberately explains
        # in a comment why there is no [Install], and a substring check would trip
        # on that explanation rather than on a real section.
        sections = [ln.strip() for ln in svc.splitlines() if ln.strip().startswith("[")]
        assert "[Install]" not in sections, sections

    def test_timer_installs_into_timers_target(self) -> None:
        tmr = dict(self._templates())["genesis-cc-settings-align.timer.template"]
        assert "WantedBy=timers.target" in tmr


class TestNonFatalUnderErrexit:
    """The function's docstring calls it NON-FATAL. Nothing proved that.

    Under `set -e`, an assignment whose value is a command substitution inherits
    that substitution's exit status and fires errexit — so a bare
    `out="$(python3 …)"` aborts the CALLER when the helper exits non-zero, which
    is exactly the unparseable-settings case the function is documented to
    survive. install.sh, bootstrap.sh and host-setup.sh all run
    `set -euo pipefail`, so this is their behaviour, not a hypothetical.

    Every current call site happens to neutralise errexit (`if !`, `|| true`),
    which is precisely why the defect stayed invisible. These tests call it as a
    BARE statement — the one shape nothing else exercises. shellcheck does not
    flag this at any severity (verified against `-o all`), so a test is the only
    mechanical guard available.
    """

    def _run_errexit(self, tmp_path: Path, snippet: str):
        """Like _run, but under the callers' real `set -euo pipefail`."""
        bindir = _minimal_bin(tmp_path)
        fake_home = tmp_path / "home"
        fake_home.mkdir(exist_ok=True)
        return subprocess.run(
            ["bash", "-c", f'set -euo pipefail; source "{_LIB}"; {snippet}'],
            capture_output=True,
            text=True,
            env={"HOME": str(fake_home), "PATH": str(bindir), "CC_VERSION": "9.9.9"},
            timeout=60,
        )

    def test_a_bare_call_reaches_the_functions_own_error_reporting(self, tmp_path: Path) -> None:
        """The distinguishing signal is the SHELL-level WARNING, not the text.

        Both the buggy and fixed forms emit the Python helper's own
        "left untouched" line — asserting on that passes either way (measured).
        Only the fixed form survives the assignment long enough to reach the
        shell WARNING that names the function and the remediation. A bare call
        is required: `f || true` / `if ! f` disable errexit INSIDE f, which is
        exactly why this stayed latent.
        """
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text("{ this is not json")

        r = self._run_errexit(tmp_path, "cc_ensure_updater_suppressed")
        assert "WARNING: cc_ensure_updater_suppressed" in r.stderr, (
            "errexit killed the function at the assignment, so its own error "
            f"reporting never ran. stderr={r.stderr!r}"
        )
        assert "verify DISABLE_AUTOUPDATER=1" in r.stderr, "remediation must reach the operator"

    def test_a_bare_call_returns_the_functions_status_not_the_helpers(self, tmp_path: Path) -> None:
        """Second independent signal. The helper exits 2; the function returns 1.
        Under the buggy form errexit propagates the helper's 2 straight out,
        so the caller sees the wrong status as well as no message."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text("{ this is not json")

        r = self._run_errexit(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 1, (
            f"expected the function's own return 1, got {r.returncode} "
            "(2 means the helper's exit leaked out via errexit)"
        )

    def test_the_happy_path_is_errexit_clean(self, tmp_path: Path) -> None:
        """A correct settings file must not trip errexit either."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}}))

        r = self._run_errexit(tmp_path, 'cc_ensure_updater_suppressed; echo "REACHED"')
        assert r.returncode == 0, r.stderr
        assert "REACHED" in r.stdout

    def test_a_repair_is_errexit_clean(self, tmp_path: Path) -> None:
        """The repair path writes; it must not trip errexit on the way out."""
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))

        r = self._run_errexit(tmp_path, 'cc_ensure_updater_suppressed; echo "REACHED"')
        assert r.returncode == 0, r.stderr
        assert "REACHED" in r.stdout
        assert json.loads(s.read_text())["env"]["DISABLE_UPDATES"] == "1"
