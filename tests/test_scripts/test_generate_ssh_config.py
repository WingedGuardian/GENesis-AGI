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
        # opens the session picker (choose-tree), not a bare shell
        assert "tmux -u new-session -A -s lobby \\; choose-tree -Zs" in out
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
        assert "-s lobby" in out
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
        # PATH-prefixed, opens the picker via tmux (choose-tree)
        assert rc[0].startswith("remotecommand PATH=") and (
            "tmux -u new-session -A -s lobby" in rc[0] and "choose-tree" in rc[0]
        ), rc
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
