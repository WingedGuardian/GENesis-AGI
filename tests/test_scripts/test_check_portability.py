"""Tests for scripts/check_portability.sh (network-address class scan).

The script guards the public repo against install-specific network
literals. Patterns are address CLASSES (RFC1918, Tailscale CGNAT,
IPv6 ULA), so the next leak is caught without anyone enumerating it
first — and RFC 5737 documentation addresses stay allowed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_portability.sh"

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def _make_repo(tmp_path: Path, src_line: str) -> Path:
    """Build a minimal scan-target layout with one src file."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "scripts").mkdir()
    (repo / "src" / "sample.py").write_text(f"VALUE = '{src_line}'\n")
    (repo / "env.example").write_text("# no addresses here\n")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), str(repo)], capture_output=True, text=True)


@pytest.mark.parametrize(
    "leak",
    [
        "10.20.30.40",  # RFC1918 class A — not just the historical subnet
        "192.168.99.7",  # RFC1918 class C — any subnet, not only .50
        "172.16.0.9",  # RFC1918 class B
        "172.31.255.1",  # RFC1918 class B upper bound
        "100.64.0.1",  # Tailscale CGNAT lower bound
        "100.127.9.9",  # Tailscale CGNAT upper bound
        "fd42:abcd::1",  # IPv6 ULA (container prefix shape)
        "fd7a:115c::1",  # IPv6 ULA (Tailscale prefix shape)
    ],
)
def test_flags_private_address_classes(tmp_path, leak):
    repo = _make_repo(tmp_path, leak)
    proc = _run(repo)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Portability check failed" in proc.stdout
    assert "sample.py" in proc.stdout


@pytest.mark.parametrize(
    "allowed",
    [
        "192.0.2.10",  # RFC 5737 TEST-NET-1 (documentation examples)
        "198.51.100.4",  # RFC 5737 TEST-NET-2
        "203.0.113.99",  # RFC 5737 TEST-NET-3
        "8.8.8.8",  # public address
        "127.0.0.1",  # loopback
        "100.63.1.1",  # just below CGNAT range
        "100.128.0.1",  # just above CGNAT range
        "172.32.0.1",  # just outside RFC1918 172.16/12
        "version 10.2 of the 3.4.5.6-style spec",  # prose, no address class
    ],
)
def test_allows_public_and_documentation_addresses(tmp_path, allowed):
    repo = _make_repo(tmp_path, allowed)
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_clean_tree_exits_zero(tmp_path):
    repo = _make_repo(tmp_path, "nothing to see")
    proc = _run(repo)
    assert proc.returncode == 0


def test_missing_target_dirs_do_not_crash(tmp_path):
    # env.example present, but no config/scripts dirs — rg must still run.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "ok.py").write_text("x = 1\n")
    proc = subprocess.run(["bash", str(SCRIPT), str(repo)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
