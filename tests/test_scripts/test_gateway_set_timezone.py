"""Tests for the guardian-gateway.sh ``set-timezone`` verb.

Runs the REAL gateway with stub ``timedatectl``, ``sudo`` and ``incus`` on PATH,
each logging its calls and keeping its clock zone in a file.

The contract pinned here: the zone argument is refused, before any sudo, unless
it is a listed IANA name; the host clock is set and read back (a mismatch is a
failure on stderr, exit 1); the container leg reports its own outcome on stdout
with exit 0; nothing but ``true`` and ``timedatectl set-timezone`` runs under
sudo, so no host job's configuration is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"
_ZONES = ["UTC", "Etc/UTC", "Europe/Paris", "America/Argentina/Buenos_Aires", "Etc/GMT+5"]

_STUB_TIMEDATECTL = r"""
import os, sys
args = sys.argv[1:]
state = os.environ["TZT_HOST_ZONE"]
with open(os.environ["TZT_LOG"], "a") as f:
    f.write("timedatectl " + " ".join(args) + "\n")
if args[:1] == ["list-timezones"]:
    if os.environ.get("TZT_LIST_FAILS"):
        sys.exit(1)
    print("\n".join(os.environ["TZT_ZONES"].split(",")))
elif args[:1] == ["show"]:
    print(open(state).read().strip())
elif args[:1] == ["set-timezone"]:
    if os.environ.get("TZT_SET_FAILS"):
        sys.exit(1)
    if not os.environ.get("TZT_SET_IGNORED"):
        open(state, "w").write(args[-1])
"""

_STUB_SUDO = r"""
import os, sys
args = sys.argv[1:]
with open(os.environ["TZT_LOG"], "a") as f:
    f.write("sudo " + " ".join(args) + "\n")
if args[:1] == ["-n"]:
    args = args[1:]
if os.environ.get("TZT_NO_SUDO"):
    sys.exit(1)
if args == ["true"]:
    sys.exit(0)
os.execvp(args[0], args)
"""

# The container: timedatectl, or (TZT_CT_NO_TIMEDATED) only readlink and the
# guarded ln fallback, as on an image without systemd-timedated. The fallback's
# own shell script runs, against a scratch zoneinfo tree and localtime link.
_STUB_INCUS = r"""
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["TZT_LOG"], "a") as f:
    f.write("incus " + " ".join(args) + "\n")
state = os.environ["TZT_CT_ZONE"]
root = os.environ["TZT_CT_ROOT"]
link = os.path.join(root, "localtime")
cmd = args[args.index("--") + 1:]
if os.environ.get("TZT_CT_DOWN"):
    sys.exit(1)
if os.environ.get("TZT_CT_NO_TIMEDATED"):
    if cmd[:1] == ["readlink"]:
        print(os.readlink(link))
    elif cmd[:2] == ["sh", "-c"]:
        script = cmd[2].replace("/usr/share/zoneinfo/", root + "/zoneinfo/")
        script = script.replace("/etc/localtime", link)
        sys.exit(subprocess.run(["sh", "-c", script, *cmd[3:]]).returncode)
    else:
        sys.exit(1)
elif cmd[:2] == ["timedatectl", "show"]:
    print(open(state).read().strip())
elif cmd[:2] == ["timedatectl", "set-timezone"]:
    open(state, "w").write(cmd[-1])
"""


class Host:
    def __init__(self, tmp_path: Path, *, zone: str = "UTC", ct_zone: str = "UTC"):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.host_zone = tmp_path / "host_zone"
        self.host_zone.write_text(zone)
        self.ct_zone = tmp_path / "ct_zone"
        self.ct_zone.write_text(ct_zone)
        self.ct_root = tmp_path / "ct"
        for name in _ZONES:
            (self.ct_root / "zoneinfo" / name).parent.mkdir(parents=True, exist_ok=True)
            (self.ct_root / "zoneinfo" / name).write_text("TZif")
        (self.ct_root / "localtime").symlink_to(self.ct_root / "zoneinfo" / ct_zone)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (
            ("timedatectl", _STUB_TIMEDATECTL),
            ("sudo", _STUB_SUDO),
            ("incus", _STUB_INCUS),
        ):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{body}")
            path.chmod(0o755)

    def run(self, arg: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "SSH_ORIGINAL_COMMAND": f"set-timezone {arg}",
            "TZT_LOG": str(self.log),
            "TZT_HOST_ZONE": str(self.host_zone),
            "TZT_CT_ZONE": str(self.ct_zone),
            "TZT_CT_ROOT": str(self.ct_root),
            "TZT_ZONES": ",".join(_ZONES),
            "LC_ALL": "C.UTF-8",
            **extra_env,
        }
        return subprocess.run(
            ["bash", str(_GATEWAY)],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def guardian_yaml(self, body: str) -> None:
        cfg = self.home / ".local" / "share" / "genesis-guardian" / "config" / "guardian.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(body)


def _ok(proc) -> dict:
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, proc.stdout
    return json.loads(lines[0])


def _err(proc) -> dict:
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert proc.stdout == ""
    return json.loads(proc.stderr.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "arg",
    [
        "",
        "-f",
        "../etc/passwd",
        "Europe/Paris; id",
        "Europe/Paris id",
        "Europe//Paris",
        "/Europe/Paris",
        "Europe/Paris/",
        "A/B/C/D",
        "Europe/Païs",
        "E" * 65,
        "$(id)",
    ],
)
def test_malformed_zone_is_refused_before_sudo(tmp_path, arg):
    host = Host(tmp_path)
    payload = _err(host.run(arg))
    assert payload["action"] == "set-timezone" and "invalid zone" in payload["error"]
    assert not any(c.startswith(("sudo", "incus")) for c in host.calls())
    assert host.host_zone.read_text() == "UTC"


def test_unlisted_zone_is_refused(tmp_path):
    host = Host(tmp_path)
    payload = _err(host.run("Mars/Base"))
    assert "not known" in payload["error"]
    assert not any(c.startswith(("sudo", "incus")) for c in host.calls())


def test_a_failed_zone_listing_says_so(tmp_path):
    host = Host(tmp_path)
    payload = _err(host.run("Europe/Paris", TZT_LIST_FAILS="1"))
    assert "could not list" in payload["error"]
    assert not any(c.startswith(("sudo", "incus")) for c in host.calls())


def test_no_sudo_is_refused_before_any_change(tmp_path):
    host = Host(tmp_path)
    payload = _err(host.run("Europe/Paris", TZT_NO_SUDO="1"))
    assert "sudo" in payload["error"]
    assert host.host_zone.read_text() == "UTC"
    assert not any(c.startswith("incus") for c in host.calls())


def test_sets_both_clocks_and_runs_nothing_else_under_sudo(tmp_path):
    host = Host(tmp_path)
    payload = _ok(host.run("Europe/Paris"))
    assert payload == {
        "ok": True,
        "action": "set-timezone",
        "zone": "Europe/Paris",
        "from": "UTC",
        "changed": True,
        "container": "set",
    }
    assert host.host_zone.read_text() == "Europe/Paris"
    assert host.ct_zone.read_text() == "Europe/Paris"
    # No drop-in, cron or unit change: host jobs follow the new local time.
    sudo = [c for c in host.calls() if c.startswith("sudo")]
    assert sudo == ["sudo -n true", "sudo -n timedatectl set-timezone -- Europe/Paris"]


def test_same_zone_is_a_no_op(tmp_path):
    host = Host(tmp_path, zone="Europe/Paris", ct_zone="Europe/Paris")
    payload = _ok(host.run("Europe/Paris"))
    assert payload["changed"] is False and payload["container"] == "unchanged"
    assert not any("set-timezone" in c for c in host.calls())


def test_same_host_zone_still_aligns_the_container(tmp_path):
    host = Host(tmp_path, zone="Europe/Paris", ct_zone="UTC")
    payload = _ok(host.run("Europe/Paris"))
    assert payload["changed"] is False and payload["container"] == "set"
    assert host.ct_zone.read_text() == "Europe/Paris"


def test_failed_host_set_is_reported_and_container_untouched(tmp_path):
    host = Host(tmp_path)
    payload = _err(host.run("Europe/Paris", TZT_SET_FAILS="1"))
    assert "set-timezone failed" in payload["error"] and payload["from"] == "UTC"
    assert host.host_zone.read_text() == "UTC"
    assert not any(c.startswith("incus") for c in host.calls())


def test_host_read_back_mismatch_is_a_failure(tmp_path):
    host = Host(tmp_path)
    payload = _err(host.run("Europe/Paris", TZT_SET_IGNORED="1"))
    assert "set-timezone failed" in payload["error"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "set"),
        ({"TZT_CT_NO_TIMEDATED": "1"}, "set"),  # readlink + the guarded ln fallback
        ({"TZT_CT_DOWN": "1"}, "failed"),
    ],
)
def test_container_leg_outcomes(tmp_path, env, expected):
    host = Host(tmp_path)
    payload = _ok(host.run("Europe/Paris", **env))
    assert payload["container"] == expected and payload["ok"] is True
    assert host.host_zone.read_text() == "Europe/Paris"


def test_container_fallback_never_links_a_missing_zone_file(tmp_path):
    host = Host(tmp_path)
    (host.ct_root / "zoneinfo" / "Europe" / "Paris").unlink()
    assert _ok(host.run("Europe/Paris", TZT_CT_NO_TIMEDATED="1"))["container"] == "failed"
    assert os.readlink(host.ct_root / "localtime").endswith("zoneinfo/UTC")


def test_container_without_timedated_already_in_zone_is_unchanged(tmp_path):
    host = Host(tmp_path, ct_zone="Europe/Paris")
    assert _ok(host.run("Europe/Paris", TZT_CT_NO_TIMEDATED="1"))["container"] == "unchanged"


def _system_path_without(tmp_path: Path, name: str) -> str:
    """A PATH of the system tools minus ``name``, so a test of its absence can
    never reach a client installed on the host running the suite."""
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    for src in (Path("/usr/bin"), Path("/bin")):
        if not src.is_dir():
            continue
        for tool in src.iterdir():
            link = sysbin / tool.name
            if tool.name != name and not link.exists() and not link.is_symlink():
                link.symlink_to(tool)
    return str(sysbin)


def test_no_incus_is_reported(tmp_path):
    host = Host(tmp_path)
    (host.bin / "incus").unlink()
    path = f"{host.bin}:{_system_path_without(tmp_path, 'incus')}"
    assert _ok(host.run("Europe/Paris", PATH=path))["container"] == "no-incus"


def test_container_name_ignores_a_trailing_yaml_comment(tmp_path):
    host = Host(tmp_path)
    host.guardian_yaml('container_name: "other-box"  # production container\n')
    _ok(host.run("Europe/Paris"))
    incus = [c for c in host.calls() if c.startswith("incus")]
    assert incus and all(c.startswith("incus exec other-box -- ") for c in incus)


def test_container_name_comes_from_guardian_yaml(tmp_path):
    host = Host(tmp_path)
    host.guardian_yaml("container_name: other-box\n")
    _ok(host.run("Europe/Paris"))
    incus = [c for c in host.calls() if c.startswith("incus")]
    assert incus and all(c.startswith("incus exec other-box -- ") for c in incus)


@pytest.mark.parametrize("name", ["Bad_Name", "-flag", "a" * 64])
def test_unsafe_container_name_is_never_used(tmp_path, name):
    host = Host(tmp_path)
    host.guardian_yaml(f"container_name: {name}\n")
    assert _ok(host.run("Europe/Paris"))["container"] == "failed"
    assert not any(c.startswith("incus") for c in host.calls())


def test_gateway_lists_the_verb():
    assert "#   set-timezone <IANA>" in _GATEWAY.read_text()
    assert os.access(_GATEWAY, os.R_OK)
