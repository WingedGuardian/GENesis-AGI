"""Snapshot puller command-builders (pure; the live SSH pull is verified in the spike)."""
import shlex
from pathlib import Path

from genesis.attention.snapshot import (
    _SNAPSHOT_SSH_TIMEOUT_S,
    _backup_py,
    _remote_backup_arg,
    _scp_cmd,
    _ssh_base,
)
from genesis.observability.ambient_health import AmbientRemoteConfig


def _cfg() -> AmbientRemoteConfig:
    return AmbientRemoteConfig(host_ip="192.0.2.1", host_user="edge", ssh_key="~/.ssh/id_ed25519")


def test_ssh_base_has_batchmode_and_target():
    b = _ssh_base(_cfg())
    assert b[0] == "ssh" and "BatchMode=yes" in b and "edge@192.0.2.1" in b


def test_backup_py_is_readonly_and_expands_home():
    s = _backup_py("~/ambient.db", "~/.ambient_snap.db")
    assert "mode=ro" in s and "expanduser('~/ambient.db')" in s and "backup(d)" in s


def test_scp_cmd_pulls_remote_to_local():
    c = _scp_cmd(_cfg(), "~/.ambient_snap.db", Path("/tmp/x.db"))
    assert c[0] == "scp" and "edge@192.0.2.1:~/.ambient_snap.db" in c and "/tmp/x.db" in c


def test_timeout_more_generous_than_health_cat():
    assert _SNAPSHOT_SSH_TIMEOUT_S >= 30  # a DB-streaming backup needs more than cat's 10s


def test_remote_backup_arg_is_single_shell_safe_token():
    # regression: ssh re-parses the command in the remote shell; the python one-liner's
    # parens/quotes must survive as ONE token (the '(' syntax-error bug).
    arg = _remote_backup_arg("~/ambient.db", "~/.ambient_snap.db")
    parts = shlex.split(arg)  # how the remote shell would split it
    assert parts[0] == "python3" and parts[1] == "-c" and len(parts) == 3
    assert parts[2] == _backup_py("~/ambient.db", "~/.ambient_snap.db")  # code intact, unsplit
