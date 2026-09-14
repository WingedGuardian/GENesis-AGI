"""generate-ssh-config.sh: the client SSH config it emits.

The one-click "lobby" door (2026-08-14) adds a stable landing session that sees
every live cc-* slot, so a single reconnect after a client/reboot brings the
whole fleet back (the slots persist in tmux on the box). The load-bearing
invariant is ORDERING: the specific ``Host <host>-lobby`` block must precede the
``Host <host>-*`` wildcard, because ssh takes the FIRST matching RemoteCommand —
if the wildcard came first, ``<host>-lobby`` would route into cc-slot.sh and be
rejected as a non-numeric slot. These tests run the real script against a fake
`tailscale` on PATH: ``TestLobbyDoor`` exercises what the script *emits*, and
``TestSshResolution`` feeds that output to a real ``ssh -G`` so the actual
first-match *resolution* (not merely text order) is verified.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN = _REPO_ROOT / "scripts" / "generate-ssh-config.sh"

# DNSName "testbox.tail1234.ts.net." -> TS_HOSTNAME "testbox"; TailscaleIPs ->
# TS_IP "192.0.2.5" (the script picks the v4, filtering the v6 by ':'). HostName
# in the emitted config is that IP, NOT the MagicDNS name (DNS-independent).
# (RFC5737 TEST-NET / RFC3849 doc addresses — placeholders, not real hosts.)
_FAKE_TAILSCALE = """#!/usr/bin/env bash
if [[ "$*" == *--json* ]]; then
  cat <<'JSON'
{"Self": {"DNSName": "testbox.tail1234.ts.net.", "TailscaleIPs": ["192.0.2.5", "2001:db8::1"]}}
JSON
  exit 0
fi
exit 0
"""


@pytest.fixture()
def gen(tmp_path):
    """Run generate-ssh-config.sh with a fake `tailscale`; return the run fn."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "tailscale"
    fake.write_text(_FAKE_TAILSCALE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run() -> subprocess.CompletedProcess:
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
        return subprocess.run(
            ["bash", str(_GEN)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return run


class TestLobbyDoor:
    def test_emits_lobby_block(self, gen):
        result = gen()
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "Host testbox-lobby" in out
        # The door is a SCRIPT now (see TestLobbyDoorScript for why).
        assert "scripts/lobby-door.sh" in out
        # PATH-prefixed so tmux resolves even when it's user-local (no .bashrc)
        assert 'RemoteCommand PATH="' in out and "/.local/bin:" in out
        assert "RequestTTY yes" in out

    def test_hostname_is_tailscale_ip_not_magicdns(self, gen):
        # HostName must be the stable Tailscale IP (DNS-resolver-independent),
        # never the MagicDNS name — that dependency is the failure this avoids.
        out = gen().stdout
        assert "HostName 192.0.2.5" in out
        assert "HostName testbox.tail1234.ts.net" not in out

    def test_lobby_block_precedes_wildcard(self, gen):
        # The correctness invariant: specific block first, or ssh routes
        # <host>-lobby into cc-slot.sh (rejected as a non-numeric slot).
        out = gen().stdout
        assert out.index("Host testbox-lobby") < out.index("Host testbox-*"), (
            "lobby block must appear before the wildcard block"
        )

    def test_wildcard_still_routes_slots_to_cc_slot(self, gen):
        # The lobby door must not disturb numeric slot routing.
        out = gen().stdout
        assert "Host testbox-*" in out
        assert "cc-slot.sh %n" in out

    def test_lobby_alias_is_not_a_cc_slot_name(self, gen):
        # 'lobby' must not look like cc-N (else it would count against the cap).
        out = gen().stdout
        assert "testbox-lobby" in out
        assert "-s cc-" not in out  # the generator never hard-codes a cc-N session


class TestSshResolution:
    """Pin the ACTUAL ssh first-match semantics, not just text order.

    The text-order assertion above is only a proxy: if ssh were "last value
    wins" it would still pass while the feature broke. ``ssh -G`` resolves the
    config exactly as a real connection would (without connecting), so this is
    the load-bearing check. Skipped where ssh is unavailable.
    """

    @pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh not on PATH")
    def test_lobby_resolves_to_tmux_not_cc_slot(self, gen, tmp_path):
        cfg = tmp_path / "sshcfg"
        cfg.write_text(gen().stdout)
        r = subprocess.run(
            ["ssh", "-G", "-F", str(cfg), "testbox-lobby"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stderr
        rc = [ln for ln in r.stdout.splitlines() if ln.lower().startswith("remotecommand ")]
        assert len(rc) == 1, rc
        # PATH-prefixed, and routed to the lobby door SCRIPT (not cc-slot.sh).
        assert rc[0].startswith("remotecommand PATH="), rc
        assert "lobby-door.sh" in rc[0], rc
        assert "cc-slot.sh" not in rc[0], rc
        # HostName resolves to the stable Tailscale IP, not the MagicDNS name.
        hn = [ln for ln in r.stdout.splitlines() if ln.lower().startswith("hostname ")]
        assert hn == ["hostname 192.0.2.5"], hn

    @pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh not on PATH")
    def test_numeric_slot_still_resolves_to_cc_slot(self, gen, tmp_path):
        cfg = tmp_path / "sshcfg"
        cfg.write_text(gen().stdout)
        r = subprocess.run(
            ["ssh", "-G", "-F", str(cfg), "testbox-2"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stderr
        rc = [ln for ln in r.stdout.splitlines() if ln.lower().startswith("remotecommand ")]
        assert len(rc) == 1 and rc[0].endswith("cc-slot.sh testbox-2"), rc


class TestScriptHygiene:
    def test_syntax_clean(self):
        subprocess.run(["bash", "-n", str(_GEN)], check=True, timeout=10)


_LOBBY = _REPO_ROOT / "scripts" / "lobby-door.sh"


class TestHeredocHasNoCommandSubstitution:
    """The emitted config is built by an UNQUOTED heredoc, so a backtick in it
    is command substitution, not quoting.

    MEASURED while writing this change: a comment containing an inline tmux
    example in backticks was EXECUTED at generation time and vanished from the
    output, leaving "not an inline  chain". Two failures at once — silent
    content loss in a file the operator pastes into ssh_config, and arbitrary
    execution during generation. The heredoc must stay unquoted (it expands
    ${TS_HOSTNAME} etc.), so the rule is: no unescaped backticks inside it.
    """

    def test_no_unescaped_backticks_in_the_heredoc(self):
        text = _GEN.read_text()
        start = text.index("cat << SSHEOF")
        end = text.index("\nSSHEOF", start)
        body = text[start:end]
        offenders = [
            ln for ln in body.split("\n")
            if "`" in ln.replace("\\`", "")
        ]
        assert not offenders, (
            "unescaped backtick inside the unquoted heredoc — it will be run as "
            f"a command and its text silently dropped: {offenders}"
        )

    def test_the_door_comment_survives_generation(self, gen):
        """Guard the SYMPTOM too, but precisely.

        A generic "double space means eaten text" heuristic was tried and
        rejected: it flags deliberately ALIGNED prose (`# Or:    ssh ...`), and a
        check that cries wolf gets deleted by whoever hits it. This names the
        block that was actually eaten and asserts its words arrive.
        """
        out = gen().stdout
        for phrase in ("lobby-door.sh", "MEASURED", "stealing the first window"):
            assert phrase in out, (
                f"{phrase!r} missing from the emitted config — the comment block "
                "was probably swallowed by command substitution again"
            )


class TestLobbyDoorScript:
    """The door's load-bearing decisions, pinned statically.

    Full behavioural proof needs a live tmux server; that was done by hand
    against an isolated socket (three scenarios: fresh, stale-unattached,
    attached-and-busy). These pin the choices that were arrived at the hard way,
    so a plausible-looking edit cannot quietly undo them.
    """

    def test_script_exists_and_is_executable(self):
        assert _LOBBY.exists(), _LOBBY
        assert _LOBBY.stat().st_mode & 0o111, "lobby-door.sh must be executable"

    def test_syntax_clean(self):
        subprocess.run(["bash", "-n", str(_LOBBY)], check=True, timeout=10)

    def test_pane_targets_use_the_colon_form(self):
        """`=lobby` is a SESSION qualifier; for a PANE target it silently fails.

        MEASURED: `display-message -p -t =lobby` returns rc=0 with EMPTY output,
        and `respawn-pane -t =lobby` errors with "can't find pane". The empty
        read is the dangerous one — it made an earlier revision take the
        secondary path on every connect, so the reset never ran and nothing
        looked wrong. `=lobby:` resolves correctly and stays exact.
        """
        text = _LOBBY.read_text()
        for cmd in ("display-message", "respawn-pane"):
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("#") or cmd not in stripped:
                    continue
                assert '${PRIMARY}:"' in stripped or "=lobby:" in stripped, (
                    f"{cmd} must use the =NAME: pane-target form: {stripped}"
                )

    def test_reset_is_gated_on_nobody_being_attached(self):
        """The whole point of the second fix: never respawn a pane someone is
        using. A second Fleet window killed a live codex when this was
        unconditional."""
        # CODE only — both names also appear in the header comment that explains
        # them, and an earlier version of this test compared those instead and
        # failed against a correct script.
        code = [
            ln for ln in _LOBBY.read_text().split("\n")
            if ln.strip() and not ln.strip().startswith("#")
        ]
        attached = next(
            (i for i, ln in enumerate(code) if "session_attached" in ln), None
        )
        respawn = next(
            (i for i, ln in enumerate(code) if "respawn-pane" in ln), None
        )
        assert attached is not None, "the door must check attachment"
        assert respawn is not None, "the door must reset a stale pane"
        assert attached < respawn, (
            "the attachment check must come BEFORE the respawn, or the reset is "
            "unconditional again"
        )

    def test_destroy_unattached_is_never_set_globally(self):
        """A global `set-option -g destroy-unattached on` would reap every cc-*
        slot the moment its terminal window closed — the exact opposite of why
        the slots exist. It must be pinned to the ephemeral session with -t."""
        for line in _LOBBY.read_text().split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or "destroy-unattached" not in stripped:
                continue
            assert " -g " not in stripped, f"must not be global: {stripped}"
            assert "-t " in stripped, f"must be pinned to a session: {stripped}"

    def test_secondary_session_is_distinct_from_the_primary(self):
        text = _LOBBY.read_text()
        assert 'lobby-$$' in text, (
            "a concurrent window needs its OWN session name, or it shares the "
            "primary's pane and steals it"
        )
