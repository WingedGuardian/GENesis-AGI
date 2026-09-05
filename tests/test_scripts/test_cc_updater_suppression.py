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

import functools
import json
import os
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
        # A missing tool must be LOUD. Silently omitting python3 here would send
        # every test in TestSuppressionReconcile down the pure-bash fallback
        # branch instead, and the assertions (both keys present) hold on that
        # branch too -- so the suite would go green while covering a different
        # code path than its docstrings claim.
        assert dest.exists() or dest.is_symlink(), (
            f"{tool} not found in /usr/bin, /bin or /usr/local/bin -- this harness "
            f"would silently test a different branch than it claims"
        )
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

# A stand-in `python3` for the OWNERSHIP arm: makes the original settings.json
# appear to carry a gid this process cannot set (stat lies by +1, for the
# SETTINGS path only), and makes os.chown raise EPERM — the unprivileged-user
# reality on a nobody:daemon file, forced deterministically without root and
# without a test-only branch in production code. The temp's stat stays
# truthful, so the reconciler's effect-comparison sees exactly the mismatch the
# real machine state produces. The lie is CONSISTENT across identity() reads
# (before and after both go through it), so the compare-and-swap is unaffected;
# st_mtime_ns is carried into the forged stat_result explicitly because
# identity() keys on it and a bare 10-tuple would not preserve it. Used by
# TestVerifiedByConstruction.
_CHOWN_SHIM_TEMPLATE = """#!%(python)s
import subprocess, sys

ANCHOR = "import json, os, random, shutil, signal, sys, tempfile, time"
INJECT = (
    "import errno as _te, os as _to\\n"
    "_t_real_stat = _to.stat\\n"
    "def _t_lying_stat(p, *a, **k):\\n"
    "    st = _t_real_stat(p, *a, **k)\\n"
    "    if isinstance(p, (str, bytes)) and "
    "_to.path.basename(_to.fsdecode(p)) == 'settings.json':\\n"
    "        vals = list(st)\\n"
    "        vals[5] = st.st_gid + 1\\n"
    "        return _to.stat_result(tuple(vals), "
    "{'st_mtime_ns': st.st_mtime_ns, 'st_atime_ns': st.st_atime_ns, "
    "'st_ctime_ns': st.st_ctime_ns})\\n"
    "    return st\\n"
    "_to.stat = _t_lying_stat\\n"
    "def _t_chown(*_a, **_k):\\n"
    "    raise OSError(_te.EPERM, 'Operation not permitted')\\n"
    "_to.chown = _t_chown\\n"
)

script = sys.stdin.read()
if ANCHOR not in script:
    sys.stderr.write("CHOWN SHIM: anchor line not found in the lib\\n")
    sys.exit(97)
script = script.replace(ANCHOR, ANCHOR + "\\n" + INJECT, 1)

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
        """A file we create from scratch is 0600 — it is secrets-adjacent.

        Honest about its own strength: this pins the OUTCOME, not the mechanism.
        MEASURED, `tempfile.mkstemp` already returns 0600 even under `umask 0`,
        so deleting the explicit `os.chmod(tmp, 0o600)` in cc_version.sh leaves
        this green. The chmod stays as belt-and-braces against a future swap to
        a different temp-file helper; no test here can distinguish the two, and
        pretending otherwise would be worse than saying so.
        """
        r = _run(tmp_path, "cc_ensure_updater_suppressed")
        assert r.returncode == 0, r.stderr
        assert stat.S_IMODE(_settings(tmp_path).stat().st_mode) == 0o600

    def test_a_same_length_repair_still_bumps_mtime(self, tmp_path: Path) -> None:
        """`os.utime(tmp, None)` after copystat, and why it is load-bearing.

        copystat carries the SOURCE file's timestamps onto the replacement, so a
        correction that changes no byte count ("0" -> "1") would land with the
        pre-write mtime AND the same size — byte-identical to a file that was
        never touched, to anything watching for a change. The mechanism carries
        a nine-line justification comment and, until now, no test: exactly the
        shape a later refactor deletes silently.
        """
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "0"}}))
        before = s.stat().st_mtime_ns
        os.utime(s, ns=(before - 10**10, before - 10**10))  # 10s into the past
        aged = s.stat().st_mtime_ns
        r = _run(tmp_path, "cc_ensure_updater_suppressed || true")
        assert json.loads(s.read_text())["env"]["DISABLE_UPDATES"] == "1", r.stderr
        assert s.stat().st_mtime_ns > aged, (
            "the repair restored the pre-write mtime — a same-length correction "
            "is then indistinguishable from an untouched file"
        )

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

    def _stub_python3(self, tmp_path: Path, code: int, reason: str) -> Path:
        """A bin dir whose python3 consumes stdin and exits with *code*.

        The reconciler's exit codes are its whole contract with three consumers
        (the wrapper's message, update.sh's HOST_CC_DEGRADED, the timer unit's
        state) and NONE of the contention codes had a test. Forcing a real
        retry-exhaustion is nondeterministic; forcing the CODE is not, and the
        code is what the wrapper dispatches on.
        """
        bindir = _minimal_bin(tmp_path)
        stub = bindir / "python3"
        if stub.is_symlink() or stub.exists():
            stub.unlink()
        stub.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"                      # swallow the piped script
            f"echo '{reason}' >&2\n"
            f"exit {code}\n"
        )
        stub.chmod(0o755)
        return bindir

    def _run_with_stub(self, tmp_path: Path, code: int, reason: str):
        bindir = self._stub_python3(tmp_path, code, reason)
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
        return _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; '
            'echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )

    def test_exit_3_is_contended_and_says_the_repair_did_not_stick(
        self, tmp_path: Path
    ) -> None:
        """Exit 3: a write LANDED and was overwritten before we could confirm."""
        r = self._run_with_stub(tmp_path, 3, "repair did not stick — reason")
        assert "STATE=contended" in r.stdout, r.stdout
        assert "did NOT" in r.stderr and "stick" in r.stderr, r.stderr

    def test_exit_4_is_contended_but_says_nothing_was_written(
        self, tmp_path: Path
    ) -> None:
        """Exit 4: retries exhausted, NOTHING written.

        Same state as exit 3 — both are contention and both leave the file
        unsuppressed — but not the same sentence. These shared one code and one
        message until now, so a run that wrote nothing told the operator a
        repair "did not stick" and sent them hunting a pathological writer over
        a file that was merely busy. The two assertions below are the pin: same
        state, different message.
        """
        r = self._run_with_stub(tmp_path, 4, "kept changing under us — left untouched")
        assert "STATE=contended" in r.stdout, r.stdout
        assert "NOT applied" in r.stderr, r.stderr
        assert "did NOT" not in r.stderr, (
            "exit 4 wrote nothing; claiming a repair that did not stick is false"
        )

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

    def test_no_defaults_passed_means_only_the_two_keys_are_written(
        self, tmp_path: Path
    ) -> None:
        """Called with no set-if-absent defaults, exactly two keys land.

        RENAMED: this used to be called `test_host_leg_...`, but it calls with
        no arguments — the CONTAINER default — while the host leg passes an
        explicit path (host-setup.sh:1330). The invariant it checks is real; the
        old name claimed coverage it did not have. The host's recovery
        `claude -p` being single-brain is why no default is passed there, and
        that remains the motivation.
        """
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
        # UNSET must not read as ok. `${VAR:-ok}` would let a partial deploy --
        # cc_ensure_local sourced from a revision predating the variable -- record
        # a clean deploy that verified nothing. scripts/cc_settings_align.sh
        # refuses that read; this is the sibling consumer of the same channel and
        # must refuse it too. A SOURCE pin: driving a real version skew from a test
        # is not worth the machinery, and the failure mode is a silent default.
        assert "${CC_SUPPRESSION_STATE:-ok}" not in src, (
            "unset state must not default to ok -- that is the fail-open this "
            "channel's other consumer explicitly rejects"
        )
        assert "CC_SUPPRESSION_STATE+set" in src, "unset must be distinguishable from ok"
        assert "cc_updater_suppression_unverified" in src

    def test_retry_exhaustion_has_its_own_exit_code(self) -> None:
        """Exit 3 and exit 4 say opposite things about what is on disk.

        3 = a write landed and was overwritten; 4 = nothing was written at all.
        They report the same STATE (both are contention) but must not share a
        sentence -- collapsing them is what made the wrapper tell an operator a
        repair "did not stick" on a run that repaired nothing.

        A SOURCE pin, deliberately. The behavioural tests above stub the exit
        code to reach the wrapper deterministically, so they cannot see which
        code the producer chooses; forcing genuine retry-exhaustion is a race.
        This catches the revert, and says plainly that it is the weaker check.
        """
        src = _LIB.read_text()
        assert "sys.exit(4)" in src, "_exhausted must not share exit 3's code"
        assert '[ "$rc" -eq 3 ] || [ "$rc" -eq 4 ]' in src, (
            "the wrapper must handle both contention codes"
        )

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
        not running, so unit state is where that signal lands.

        Scoped honestly: `failed` unit state is COLLECTED (the infra profile
        sweeps `systemctl --user list-units 'genesis-*'`) but nothing ALERTS on
        it — this unit is not in observability's `_SYSTEMD_UNITS` map, which
        holds three entries and omits the other genesis timers too. So on a
        headless box the signal reaches a file, not a person. That is the house
        pattern rather than a regression here, and widening the map is a
        separate change.
        """
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


@functools.lru_cache(maxsize=1)
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
        """Substitute EVERY placeholder the installer does, not just the ones
        these two templates happen to use today.

        `test_only_supported_render_placeholders` sanctions four tokens; this
        used to substitute two. Adding a sanctioned `__HOME__` to a template
        would then leave it literal in the rendered unit, and
        `systemd-analyze verify` would be checking a unit that does not match
        what install.sh produces. Mirrors scripts/install.sh:900-903.
        """
        src = (self._UNIT_DIR / name).read_text()
        rendered = (
            src.replace("__HOME__", str(Path.home()))
            .replace("__VENV__", str(_REPO_ROOT / ".venv"))
            .replace("__REPO_DIR__", str(_REPO_ROOT))
            .replace("__CC_BIN_DIR__", str(Path.home() / ".local" / "bin"))
        )
        out = tmp_path / name.replace(".template", "")
        out.write_text(rendered)
        return out

    @pytest.mark.parametrize(
        "name",
        [
            "genesis-cc-settings-align.service.template",
            "genesis-cc-settings-align.timer.template",
        ],
    )
    def test_rendered_unit_is_valid(self, tmp_path: Path, name: str) -> None:
        # Probed HERE, not as a `skipif` argument. A skipif predicate is
        # evaluated at COLLECTION, so this 30s `systemd-analyze --user verify`
        # ran on every collection of this module -- including --collect-only
        # and -k runs that deselect it. Same class as the two most recent test
        # fixes on main; cached, so parametrisation still costs one probe.
        if not _user_manager_reachable():
            pytest.skip("no reachable systemd user manager")
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

class TestVerifiedByConstruction:
    """The state channel after the fail-open audit: ok/repaired are EARNED.

    An enumeration of every exit path found NINE that reported success without
    a post-operation read — including `CC_SUPPRESSION_STATE=ok` set
    optimistically at function ENTRY (so `ok` was a default, not a conclusion)
    and the python3-less create, which wrote the file and never read it back
    yet returned the state that everywhere else means "verified after
    writing". None of those paths had a test, which is WHY they were
    fail-open. These are those tests.
    """

    def _bin_sans_python(self, tmp_path: Path) -> Path:
        """A bin dir with everything the no-python branch needs EXCEPT python3.

        mv, grep and mktemp are the create path's own dependencies and are
        deliberately included — they are not in _TOOLS because no test ever
        exercised this branch before. Every tool asserted present: a missing one
        would silently route down a different branch than the test names.

        mktemp is REQUIRED, not optional: the create path refuses to write
        through a predictable `.tmp.$$` name, so without mktemp it fails closed
        by design. Omitting it here would test the refusal rather than the
        create, which is a different branch wearing the same test names.
        """
        d = tmp_path / "nopybin"
        d.mkdir(exist_ok=True)
        for tool in ("bash", "sh", "env", "mkdir", "dirname", "cat", "rm", "mv", "grep", "mktemp"):
            dest = d / tool
            if dest.exists() or dest.is_symlink():
                continue
            for src_dir in ("/usr/bin", "/bin", "/usr/local/bin"):
                src = Path(src_dir) / tool
                if src.exists():
                    dest.symlink_to(src)
                    break
            assert dest.exists() or dest.is_symlink(), (
                f"{tool} not found in the probe dirs -- this harness would "
                f"silently test a different branch than it claims"
            )
        return d

    def test_a_defaults_only_write_reports_repaired_not_ok(self, tmp_path: Path) -> None:
        """A run that MODIFIED the file must not report `ok` (= untouched).

        Before this contract, a write that filled only set-if-absent defaults
        produced rc 0 with EMPTY stdout -- byte-identical to "already correct,
        nothing written" -- so the caller reported `ok` for a run that wrote.
        host-setup gates its created-as-root chown handback on `repaired`, so
        the collapse had a consumer-visible cost, not just a naming one.
        """
        s = _settings(tmp_path)
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}}))
        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed "" "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=9" || true; '
            'echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
        )
        assert "STATE=repaired" in r.stdout, (r.stdout, r.stderr)
        assert "set-if-absent default(s) applied" in r.stderr
        assert "MISSING" not in r.stderr, (
            "a defaults-only write must not cry 'suppression was MISSING' -- "
            "the suppression keys were present the whole time"
        )
        env = json.loads(s.read_text())["env"]
        assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "9"

    def test_the_no_python_create_verifies_before_claiming_repaired(self, tmp_path: Path) -> None:
        bindir = self._bin_sans_python(tmp_path)
        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )
        assert "STATE=repaired" in r.stdout, (r.stdout, r.stderr)
        assert "verified by re-read" in r.stderr
        env = json.loads(_settings(tmp_path).read_text())["env"]
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["DISABLE_UPDATES"] == "1"

    def test_a_dangling_symlink_is_written_through_not_replaced(self, tmp_path: Path) -> None:
        """A settings.json symlinked into a dotfiles checkout must survive.

        MEASURED, and the reason this branch is reachable at all: ``[ -e ]``
        FOLLOWS a symlink, so a link whose target does not exist yet tests as
        ABSENT — correctly, there is nothing there. But ``mv -f`` then renames
        over the LINK ITSELF:

            ln -s ./missing target-link; mv -f tmp target-link
              -> still a symlink? NO    target created? NO

        so the operator's dotfiles wiring was destroyed by a repair that then
        reported ``repaired``. The link must be resolved and its TARGET written.
        """
        bindir = self._bin_sans_python(tmp_path)
        for tool in ("readlink", "ln"):
            src = shutil.which(tool)
            assert src, f"{tool} missing — this test would not exercise its branch"
            if not (bindir / tool).exists():
                (bindir / tool).symlink_to(src)

        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "home" / "dotfiles" / "claude-settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        settings.symlink_to(target)          # DANGLING: target does not exist
        assert settings.is_symlink() and not target.exists()

        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )

        assert "STATE=repaired" in r.stdout, (r.stdout, r.stderr)
        assert settings.is_symlink(), (
            "the symlink was replaced by a regular file — the operator's dotfiles "
            "wiring is gone, and the function reported success"
        )
        assert target.exists(), "the symlink's target was never created"
        env = json.loads(target.read_text())["env"]
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["DISABLE_UPDATES"] == "1"
        # Reading back THROUGH the link is what proves the link still resolves.
        assert json.loads(settings.read_text())["env"]["DISABLE_UPDATES"] == "1"

    def test_a_pre_created_temp_symlink_cannot_be_written_through(self, tmp_path: Path) -> None:
        """The temp path must be created EXCLUSIVELY, not derived from the PID.

        `$$` is predictable, and this branch runs under sudo from host-setup.sh.
        A process able to write the operator's `.claude` directory can
        pre-create that exact path as a symlink; the redirect then follows it
        and overwrites the victim AS ROOT before `mv` ever runs, after which the
        function reports `repaired`.

        The victim here stands in for any file the attacker points at. It must
        come out untouched, and the run must not claim a repair it achieved by
        clobbering it.
        """
        bindir = self._bin_sans_python(tmp_path)
        for tool in ("mktemp", "ln", "readlink"):
            src = shutil.which(tool)
            assert src, f"{tool} missing — this test would not exercise its branch"
            if not (bindir / tool).exists():
                (bindir / tool).symlink_to(src)

        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "home" / "victim.txt"
        victim.write_text("PRECIOUS")

        # Plant the symlink at the EXACT path the old scheme would use, from
        # inside the same shell, so `$$` is the real PID rather than a guess.
        #
        # A first version of this test planted PIDs 2..4000 from Python and
        # passed against the vulnerable code — the shell's actual `$$` was
        # outside that range, so the attack was never planted and the test
        # proved nothing. Deriving the path from `$$` in the shell that will use
        # it removes the guess entirely.
        r = _run(
            tmp_path,
            'ln -s "$HOME/victim.txt" "$HOME/.claude/settings.json.tmp.$$"; '
            'cc_ensure_updater_suppressed || true; '
            'echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )

        assert victim.read_text() == "PRECIOUS", (
            "the redirect followed a pre-created symlink and overwrote an "
            f"unrelated file as the invoking user; stdout={r.stdout!r}"
        )
        # The real settings file must still have been produced properly.
        assert "STATE=repaired" in r.stdout, (r.stdout, r.stderr)
        assert json.loads(settings.read_text())["env"]["DISABLE_UPDATES"] == "1"

    def test_created_is_distinct_from_merely_repaired(self, tmp_path: Path) -> None:
        """`repaired` means modified; it cannot answer "did we make this file".

        host-setup.sh chowns the settings file to the operator, and its comments
        promise to hand back only what THIS run created. Keyed on `repaired` it
        also chowned a pre-existing dotfiles-managed file, because every
        successful modification reports `repaired`.
        """
        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)

        # Absent -> created.
        r = _run(tmp_path, 'cc_ensure_updater_suppressed || true; '
                           'echo "S=$CC_SUPPRESSION_STATE C=$CC_SUPPRESSION_CREATED"')
        assert "S=repaired" in r.stdout and "C=1" in r.stdout, (r.stdout, r.stderr)

        # Present but missing a key -> repaired, NOT created.
        settings.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
        r = _run(tmp_path, 'cc_ensure_updater_suppressed || true; '
                           'echo "S=$CC_SUPPRESSION_STATE C=$CC_SUPPRESSION_CREATED"')
        assert "S=repaired" in r.stdout, (r.stdout, r.stderr)
        assert "C=0" in r.stdout, (
            "a modification of a pre-existing file reported itself as a creation — "
            "host-setup.sh would chown a file the operator already owned"
        )

        # Already correct -> neither.
        r = _run(tmp_path, 'cc_ensure_updater_suppressed || true; '
                           'echo "S=$CC_SUPPRESSION_STATE C=$CC_SUPPRESSION_CREATED"')
        assert "S=ok" in r.stdout and "C=0" in r.stdout, (r.stdout, r.stderr)

    def test_a_lost_breadcrumb_is_reported_and_does_not_abort_the_caller(
        self, tmp_path: Path
    ) -> None:
        """Two properties that pull in opposite directions, both required.

        The breadcrumb is the ONLY channel a repair made inside the bootstrap
        subprocess has for reaching update_history, so a write failure must not
        be discarded — a real repair would be reported as a clean deploy.

        But it must also not ABORT the caller. install.sh, bootstrap.sh and
        host-setup.sh all run `set -euo pipefail`, and the suppression itself
        succeeded; failing the whole installer over a REPORTING failure would
        turn a cosmetic problem into an outage.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".genesis").mkdir()
        (home / ".genesis").chmod(0o500)          # readable, NOT writable
        try:
            r = subprocess.run(
                ["bash", "-c",
                 f'set -euo pipefail; source "{_LIB}"; '
                 'cc_ensure_updater_suppressed || true; '
                 'echo "S=$CC_SUPPRESSION_STATE L=$CC_SUPPRESSION_BREADCRUMB_LOST"'],
                capture_output=True, text=True, timeout=60,
                env={"HOME": str(home), "PATH": str(_minimal_bin(tmp_path)),
                     "CC_VERSION": "9.9.9"},
            )
        finally:
            (home / ".genesis").chmod(0o700)

        assert r.returncode == 0, (
            "an unwritable breadcrumb aborted a `set -e` caller; the suppression "
            f"had already succeeded. stdout={r.stdout!r} stderr={r.stderr!r}"
        )
        assert "S=repaired" in r.stdout, (r.stdout, r.stderr)
        assert "L=1" in r.stdout, "the lost breadcrumb was silently discarded"
        assert "could not persist the outcome" in r.stderr

    def test_an_uncreatable_breadcrumb_dir_is_reported_not_swallowed(
        self, tmp_path: Path
    ) -> None:
        """mkdir failure loses the channel exactly as a failed WRITE does.

        The reviewer's reproduction, replayed: ~/.genesis present as a regular
        FILE. `mkdir -p` cannot create the directory — and the old
        `|| return 0` swallowed that, so bootstrap repaired the keys, reported
        `repaired lost=0`, and the later update check recorded a clean deploy.
        The breadcrumb can never land in a directory that does not exist, so
        this is the same channel loss as the write-failure case above, one
        syscall earlier, and carries the same two opposite-pulling properties:
        surfaced loudly, never aborting a `set -e` caller.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".genesis").write_text("not a directory")
        r = subprocess.run(
            ["bash", "-c",
             f'set -euo pipefail; source "{_LIB}"; '
             'cc_ensure_updater_suppressed || true; '
             'echo "S=$CC_SUPPRESSION_STATE L=$CC_SUPPRESSION_BREADCRUMB_LOST"'],
            capture_output=True, text=True, timeout=60,
            env={"HOME": str(home), "PATH": str(_minimal_bin(tmp_path)),
                 "CC_VERSION": "9.9.9"},
        )
        assert r.returncode == 0, (
            "an uncreatable breadcrumb dir aborted a `set -e` caller; the "
            f"suppression had already succeeded. stdout={r.stdout!r} stderr={r.stderr!r}"
        )
        assert "S=repaired" in r.stdout, (r.stdout, r.stderr)
        assert "L=1" in r.stdout, (
            "an uncreatable breadcrumb directory was silently swallowed — a "
            "subprocess repair reads as a clean deploy"
        )
        assert "could not create the directory" in r.stderr
        assert (home / ".genesis").read_text() == "not a directory", (
            "the reporting path must not replace whatever is sitting at ~/.genesis"
        )

    def test_an_ownership_it_cannot_reproduce_declines_the_write(
        self, tmp_path: Path
    ) -> None:
        """A replacement that cannot carry the original's owner must not ship.

        The reviewer's reproduction, forced without root: a settings.json owned
        nobody:daemon whose group this process cannot set was PUBLISHED
        carrying the temp's default group — nobody:nogroup — silently changing
        group-based access to a credential-adjacent file, under a `repaired`.
        The shim makes stat report a gid one off for the settings path and
        chown raise EPERM, which is that machine state exactly; the write must
        be DECLINED on the effect comparison, the file left byte-identical, and
        no temp left behind.
        """
        real_python = shutil.which("python3")
        assert real_python, "python3 required"
        bindir = _minimal_bin(tmp_path)
        (bindir / "python3").unlink(missing_ok=True)
        shim = bindir / "python3"
        shim.write_text(_CHOWN_SHIM_TEMPLATE % {"python": real_python})
        shim.chmod(0o755)

        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        # One key missing, so a write IS attempted — an already-correct file
        # would exit before the ownership arm and pass vacuously.
        original = json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}})
        settings.write_text(original)

        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; '
            'echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )

        assert "STATE=failed" in r.stdout, (r.stdout, r.stderr)
        assert "ownership" in r.stderr, (r.stdout, r.stderr)
        assert settings.read_text() == original, (
            "the replacement shipped with ownership the process could not "
            "reproduce — group-based access to a credential-adjacent file changed"
        )
        strays = [p for p in settings.parent.iterdir() if ".settings." in p.name]
        assert not strays, f"stray temp left behind: {strays}"

    def test_a_nested_symlink_chain_is_resolved_to_its_end(self, tmp_path: Path) -> None:
        """settings.json -> dots/second -> target: BOTH links must survive.

        The reviewer's reproduction: one-level resolution renames onto
        `dots/second`, so the operator's INNER link becomes a regular file
        while the true target stays missing — nested dotfiles wiring broken by
        a repair that reports `repaired`. The chain must be walked to its end
        and the FINAL target written.
        """
        bindir = self._bin_sans_python(tmp_path)
        for tool in ("readlink", "ln"):
            src = shutil.which(tool)
            assert src, f"{tool} missing — this test would not exercise its branch"
            if not (bindir / tool).exists():
                (bindir / tool).symlink_to(src)

        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        dots = tmp_path / "home" / "dots"
        dots.mkdir(parents=True)
        second = dots / "second"
        target = dots / "claude-settings.json"
        second.symlink_to(target)        # inner link — DANGLING
        settings.symlink_to(second)      # outer link
        assert settings.is_symlink() and second.is_symlink() and not target.exists()

        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; '
            'echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )

        assert "STATE=repaired" in r.stdout, (r.stdout, r.stderr)
        assert second.is_symlink(), (
            "the INTERMEDIATE link was replaced by a regular file — the "
            "operator's nested dotfiles wiring is broken and the true target "
            "was never created"
        )
        assert settings.is_symlink(), "the outer link was replaced"
        assert target.exists() and not target.is_symlink(), (
            "the chain's final target was never written"
        )
        # Reading back THROUGH the whole chain is what proves it still resolves.
        env = json.loads(settings.read_text())["env"]
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["DISABLE_UPDATES"] == "1"

    def test_a_symlink_cycle_is_declined_not_broken(self, tmp_path: Path) -> None:
        """A cycle of links is operator wiring to DECLINE, not to rename over.

        `[ -e ]` on a cycle is false (ELOOP), so the create branch is reached;
        an unbounded walk would spin, and a one-level resolver would rename
        onto the first link in the loop. The walk must give up at its bound,
        report failure, and leave every link exactly as it found it.
        """
        bindir = self._bin_sans_python(tmp_path)
        for tool in ("readlink", "ln"):
            src = shutil.which(tool)
            assert src, f"{tool} missing — this test would not exercise its branch"
            if not (bindir / tool).exists():
                (bindir / tool).symlink_to(src)

        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        loop_a = tmp_path / "home" / "loop-a"
        loop_b = tmp_path / "home" / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        settings.symlink_to(loop_a)

        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; '
            'echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )

        assert "STATE=failed" in r.stdout, (r.stdout, r.stderr)
        assert "symlink" in r.stderr, (r.stdout, r.stderr)
        assert settings.is_symlink() and loop_a.is_symlink() and loop_b.is_symlink(), (
            "a link in the cycle was replaced — declining means touching nothing"
        )

    def test_sigterm_removes_the_temporary_settings_file(self, tmp_path: Path) -> None:
        """SIGTERM must RAISE, or the cleanup arm around the write is decorative.

        The arm's own comment claimed it covered "SIGTERM mid-write". It did
        not: SIGTERM's default disposition terminates the process outright, so
        no exception is raised and no `except` runs. The abandoned temp holds
        the FULL settings content, which is credential-adjacent.
        """
        # Structural rather than behavioural: delivering a real SIGTERM at the
        # exact moment of a stalled fsync is not reproducible in a unit test
        # without stalling a filesystem. What IS checkable, and what the defect
        # actually was, is whether the handler exists and covers the window.
        #
        # The handler must be installed BEFORE the
        # mkstemp it protects. A handler registered afterwards leaves the same
        # window open.
        src = _LIB.read_text(encoding="utf-8")
        assert "signal.signal(signal.SIGTERM" in src, (
            "no SIGTERM handler — the `except BaseException` cleanup arm cannot "
            "run, because a default-disposition SIGTERM raises nothing"
        )
        assert src.index("signal.signal(signal.SIGTERM") < src.index("tempfile.mkstemp"), (
            "the SIGTERM handler is installed after the temp file is created — "
            "the unprotected window is exactly the one being closed"
        )
        assert "import json, os, random, shutil, signal, sys, tempfile, time" in src

    def test_a_create_whose_write_did_not_land_reports_failed(self, tmp_path: Path) -> None:
        """Sabotaged mv: the rename 'succeeds' but the content never arrives.

        The stub creates an EMPTY destination, which is exactly what a torn or
        clobbered create looks like. Before the verify greps, this path
        reported `repaired` rc 0 over an empty file.
        """
        bindir = self._bin_sans_python(tmp_path)
        mv = bindir / "mv"
        mv.unlink()
        mv.write_text('#!/bin/sh\n: > "$3"\nrm -f "$2"\nexit 0\n')
        mv.chmod(0o755)
        r = _run(
            tmp_path,
            'cc_ensure_updater_suppressed || true; echo "STATE=${CC_SUPPRESSION_STATE:-unset}"',
            path_dir=bindir,
        )
        assert "STATE=failed" in r.stdout, (r.stdout, r.stderr)
        assert "could not create-and-verify" in r.stderr
        assert "left untouched" not in r.stderr, (
            "the create branch owns its failure message -- 'left untouched' is "
            "false when a write landed"
        )

    def test_entry_state_is_pessimistic_and_defaults_are_verified(self) -> None:
        """SOURCE pins for the two mechanisms a behavioural test cannot reach.

        (a) the entry assignment: every in-tree branch now sets a final state,
        so no behavioural test can observe the entry value -- but a future
        branch that forgets is exactly the audience. Reverting the entry to
        `ok` re-opens all nine holes at once.
        (b) the reconciler verifying WRITTEN DEFAULTS in its post-write
        re-read: forcing a clobber that eats only the default between
        os.replace and the re-read is a race this harness cannot stage
        deterministically. A pin is weaker than behaviour and is labelled so.
        """
        src = _LIB.read_text()
        assert "CC_SUPPRESSION_STATE=unverified" in src, (
            "the entry state must be pessimistic -- `ok` as a default is how "
            "nine paths reported success without verifying"
        )
        assert "still += [k for k in missing_defaults if k not in final_env]" in src


class TestAlignVerifiesNotAssumes:
    """cc_settings_align.sh: green means VERIFIED, on every path."""

    _SCRIPT = _REPO_ROOT / "scripts" / "cc_settings_align.sh"

    def _run_script(self, home: Path):
        return subprocess.run(
            ["bash", str(self._SCRIPT)],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            timeout=60,
        )

    def _seed(self, tmp_path: Path, payload) -> Path:
        home = tmp_path / "h"
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        f = home / ".claude" / "settings.json"
        f.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return home

    def _hold_lock(self, home: Path):
        lockdir = home / ".genesis" / "locks"
        lockdir.mkdir(parents=True, exist_ok=True)
        return open(lockdir / "cc_settings_align.lock", "w")

    def test_a_held_lock_with_the_keys_present_verifies_read_only(self, tmp_path: Path) -> None:
        """Busy lock used to exit 0 having checked NOTHING -- a green unit for a
        run that never looked at the file. Reads race nothing destructively,
        so the busy path now verifies read-only and earns its green."""
        import fcntl as _fcntl

        home = self._seed(
            tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}}
        )
        with self._hold_lock(home) as fh:
            _fcntl.flock(fh, _fcntl.LOCK_EX)
            r = self._run_script(home)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "verified read-only" in r.stdout
        assert "both suppression keys present" in r.stdout

    def test_a_clean_contended_run_resets_the_repair_history(self, tmp_path: Path) -> None:
        """repair -> clean-under-contention -> repair must NOT read as a repeat.

        The contended read-only path exits 0 directly. When it did so without
        recording an outcome, the stored `repaired` from run 1 survived, so the
        unrelated repair in run 3 was misclassified as the SECOND consecutive
        one and failed the unit — a false "something keeps rewriting
        settings.json" alarm with a verified-clean run sitting between the two.

        The observed sequence before the fix was exit 0, 0, 3; it must be
        0, 0, 0.
        """
        import fcntl as _fcntl

        both = {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}
        settings = tmp_path / "h" / ".claude" / "settings.json"

        # 1. a genuine repair — one key missing
        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1"}})
        r1 = self._run_script(home)
        assert r1.returncode == 0, (r1.returncode, r1.stdout, r1.stderr)

        # 2. a VERIFIED-CLEAN run that happens to hit a held lock
        with self._hold_lock(home) as fh:
            _fcntl.flock(fh, _fcntl.LOCK_EX)
            r2 = self._run_script(home)
        assert r2.returncode == 0, (r2.returncode, r2.stdout, r2.stderr)
        assert "verified read-only" in r2.stdout

        state = (home / ".genesis" / "cc_settings_align.last").read_text().split()[0]
        assert state == "ok", (
            f"a verified-clean contended run left the state as {state!r} — the "
            "next genuine repair will be misread as the second consecutive one"
        )

        # 3. an UNRELATED later repair — must be a FIRST repair, not a repeat
        settings.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
        r3 = self._run_script(home)
        assert r3.returncode == 0, (
            "the clean run in between was not recorded, so this repair was "
            f"escalated as a repeat; rc={r3.returncode} stdout={r3.stdout!r}"
        )
        assert "SECOND consecutive repair" not in r3.stdout
        assert json.loads(settings.read_text())["env"]["DISABLE_UPDATES"] == "1"
        assert both  # the pair this whole mechanism exists to keep on disk

    def test_a_held_lock_with_keys_absent_fails_the_unit(self, tmp_path: Path) -> None:
        import fcntl as _fcntl

        home = self._seed(tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1"}})
        with self._hold_lock(home) as fh:
            _fcntl.flock(fh, _fcntl.LOCK_EX)
            r = self._run_script(home)
        assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
        assert "could NOT" in r.stdout and "read-only" in r.stdout

    def test_an_unpersistable_state_file_fails_the_unit(self, tmp_path: Path) -> None:
        """The repeat-repair escalation reads this file; a swallowed write made
        every repair look like a FIRST repair forever, silently disarming the
        one durable "something keeps rewriting settings.json" signal. The
        state-file path is made a DIRECTORY so the redirect fails."""
        home = self._seed(
            tmp_path, {"env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}}
        )
        (home / ".genesis" / "cc_settings_align.last").mkdir(parents=True, exist_ok=True)
        r = self._run_script(home)
        assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
        assert "cannot persist repeat-repair state" in r.stdout


class TestConsumersReadTheChannel:
    """SOURCE pins: every consumer of CC_SUPPRESSION_STATE reads it honestly.

    Pins, not behaviour, deliberately: driving install.sh / bootstrap.sh /
    host-setup.sh end-to-end needs a box these tests do not get, and the
    failure mode being pinned is a silent DEFAULT -- precisely what a source
    assertion catches and a green e2e cannot.
    """

    def test_update_sh_marks_a_missing_lib_as_degraded(self) -> None:
        src = (_REPO_ROOT / "scripts" / "update.sh").read_text()
        assert "cc_env_missing" in src, (
            "a deploy that never ran the suppression check must not record a "
            "clean update_history row"
        )
        assert "unset CC_SUPPRESSION_STATE" in src

    def test_a_repair_leaves_a_breadcrumb_that_survives_the_subprocess(
        self, tmp_path: Path
    ) -> None:
        """The in-process state variable cannot cross a subprocess boundary.

        During a real update, bootstrap.sh runs FIRST as a subprocess and can
        repair the keys; that shell state dies with it, and update.sh's own
        later call then finds an already-correct file and reports `ok`. The
        repair reached neither update_history nor the deploy output. This
        breadcrumb is the surviving channel — and it is written by a wrapper, so
        every early `return` in the inner function records it too.

        `ok` must NOT be written: absence means "nothing to report", so a stale
        file can never manufacture a degradation on a later clean deploy.
        """
        # `_run` sources the library under tmp_path/"home" — use its own helper
        # rather than a hand-built path, or the library repairs a DIFFERENT file
        # and still reports `repaired`, which reads like a missing breadcrumb.
        settings = _settings(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        crumb = tmp_path / "home" / ".genesis" / "cc_suppression_outcome"

        # A repair: one key present, one missing.
        settings.write_text(json.dumps({"env": {"DISABLE_AUTOUPDATER": "1"}}))
        r = _run(tmp_path, 'cc_ensure_updater_suppressed || true; echo "state=$CC_SUPPRESSION_STATE"')
        assert "state=repaired" in r.stdout, (r.stdout, r.stderr)
        assert crumb.exists(), (
            "a repair left no breadcrumb — it cannot survive the bootstrap "
            "subprocess, so update.sh will report a clean deploy"
        )
        recorded, _, at = crumb.read_text().strip().partition(" ")
        assert recorded == "repaired", recorded
        assert at.isdigit() and int(at) > 0, f"breadcrumb has no usable epoch: {at!r}"

        # A subsequent CLEAN run must not overwrite it with `ok`: the reader
        # compares epochs against a watermark, and `ok` carries no information.
        before = crumb.read_text()
        r2 = _run(tmp_path, 'cc_ensure_updater_suppressed || true; echo "state=$CC_SUPPRESSION_STATE"')
        assert "state=ok" in r2.stdout, (r2.stdout, r2.stderr)
        assert crumb.read_text() == before, "an `ok` run overwrote the breadcrumb"

    def test_update_sh_consults_the_breadcrumb_when_its_own_check_is_ok(self) -> None:
        """`ok` in update.sh's own call does not mean nothing happened.

        Structural, because the branch only fires inside a real deploy: the
        watermark must be taken BEFORE bootstrap.sh runs, and the consumer must
        compare against it. A watermark read after the subprocess would always
        equal the breadcrumb and the branch would be dead.
        """
        src = (_REPO_ROOT / "scripts" / "update.sh").read_text()

        # TEXTUAL position cannot prove EXECUTION order in a shell script, and
        # two earlier versions of this test got that wrong in different ways:
        #   1. `"_CC_SUPP_MARK=" in src` was satisfied by the fallback line
        #      `_CC_SUPP_MARK=0` alone, so deleting the real read stayed green.
        #   2. comparing against the position of `scripts/bootstrap.sh` was
        #      satisfied by the CONSUMER's own `cat` of the breadcrumb — which
        #      lives inside `_sync_deploy_targets`, a function DEFINED near the
        #      top of the file and CALLED long after bootstrap.
        # What actually matters is that the watermark is read at top level,
        # ahead of the function that later consumes it.
        assert '_CC_SUPP_MARK="$(cut' in src, (
            "the watermark is never read from the breadcrumb file"
        )
        mark_read = src.index('_CC_SUPP_MARK="$(cut')
        consumer_def = src.index("_sync_deploy_targets() {")
        assert mark_read < consumer_def, (
            "the watermark must be taken at top level BEFORE the function that "
            "consumes it is even defined — inside it, the value would be read "
            "after bootstrap has already repaired, and a real repair would be "
            "indistinguishable from one recorded weeks ago"
        )
        assert '-gt "${_CC_SUPP_MARK:-0}"' in src, (
            "the breadcrumb is never compared against the watermark"
        )

    def test_uninstall_dry_run_does_not_clear_timer_state(self) -> None:
        """`systemctl --user clean --what=state` is a MUTATION, not a query.

        Every neighbouring uninstall step is DRY_RUN-guarded. Unguarded, a
        --dry-run silently deletes the persistent timer stamps, changing whether
        a later reinstall replays a missed run — precisely the class of outcome
        someone runs --dry-run to avoid.
        """
        src = (_REPO_ROOT / "scripts" / "uninstall.sh").read_text()
        idx = src.index("systemctl --user clean --what=state")
        window = src[max(0, idx - 500):idx]
        assert 'DRY_RUN" = true' in window, (
            "the timer-state clean is not inside a DRY_RUN guard"
        )
        assert "Would clear persistent timer state" in src, (
            "--dry-run must still SAY what it would have done, like its neighbours"
        )

    def test_bootstrap_reads_the_state_it_used_to_drop(self) -> None:
        src = (_REPO_ROOT / "scripts" / "bootstrap.sh").read_text()
        assert "CC_SUPPRESSION_STATE:-unverified" in src, (
            "bootstrap was the one caller with NO signal at all: rc discarded "
            "by `|| true` and the state never read"
        )

    def test_install_reads_the_state_and_claims_only_what_holds(self) -> None:
        src = (_REPO_ROOT / "scripts" / "install.sh").read_text()
        assert "CC_SUPPRESSION_STATE:-unverified" in src
        assert "suppression + subagent-nesting default verified" not in src, (
            "the summary line must not claim the nesting default on the "
            "python3-less path where it is provably not applied"
        )

    def test_host_setup_never_resets_the_group(self) -> None:
        """MEASURED: `chown user:` (trailing colon) sets the group to the
        user's LOGIN group -- a file at ubuntu:sudo became ubuntu:ubuntu --
        undoing the group preservation the write path just performed."""
        src = (_REPO_ROOT / "scripts" / "host-setup.sh").read_text()
        assert 'chown "$_host_user:"' not in src
        assert "sudo chown $_host_user: " not in src
        assert "CC_SUPPRESSION_STATE:-ok" not in src, (
            "the :-ok default is the fail-open idiom the other consumers of "
            "this channel explicitly refuse"
        )

