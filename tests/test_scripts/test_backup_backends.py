"""Unit tests for ``scripts/lib/backup_backends.sh`` — the pluggable Tier-2 interface.

The ``local`` backend runs against a REAL filesystem (no stubs) and is the
regression anchor: it proves the interface contract (mkdir/put/get/list/exists/
delete + init/cleanup) end-to-end with pure shell. The ``smb`` backend is exercised
with a logging ``smbclient`` stub to assert the generated ``-c`` command shapes and
the ``ls``-output parsing. ``none`` is the public default (no off-site). Backward-
compat: a configured ``GENESIS_BACKUP_NAS`` with no explicit selector resolves to smb.

All snippets source the lib under ``set -euo pipefail`` so the lib's own safety
(case-dispatch, ``|| true`` on list pipes, no competing EXIT trap) is exercised.
"""

import os
import stat
import subprocess
import textwrap
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "backup_backends.sh"


def _run_bash(body: str, env: dict, extra_path: Path | None = None) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail\nsource "{_LIB}"\n{body}\n'
    full_env = dict(os.environ)
    full_env.update(env)
    if extra_path is not None:
        full_env["PATH"] = f'{extra_path}:{full_env["PATH"]}'
    return subprocess.run(["bash", "-c", script], env=full_env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ── local backend: real-filesystem regression anchor ─────────────────

def test_local_full_roundtrip(tmp_path):
    """init → mkdir → put → exists → list → get → delete on a REAL filesystem.

    This is the anchor: if a future backend regresses, the interface contract is
    still pinned here by pure shell + fs ops (no binary stub to drift from).
    """
    root = tmp_path / "offsite"
    root.mkdir()
    src = tmp_path / "payload.txt"
    src.write_text("HELLO-PAYLOAD")
    got = tmp_path / "fetched.txt"
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "local",
           "GENESIS_BACKUP_LOCAL_PATH": str(root)}
    body = textwrap.dedent(f"""
        backend_init
        backend_available || {{ echo "NOT-AVAILABLE"; exit 1; }}
        echo "backend=$(backend_name)"
        backend_mkdir "Genesis/host/STAMP/data"
        backend_put "{src}" "Genesis/host/STAMP/data/payload.txt"
        backend_exists "Genesis/host/STAMP/data/payload.txt" && echo "EXISTS=yes"
        backend_exists "Genesis/host/STAMP/data/missing" || echo "MISSING=correct"
        echo "LIST_START"; backend_list "Genesis/host/STAMP/data"; echo "LIST_END"
        backend_get "Genesis/host/STAMP/data/payload.txt" "{got}"
        backend_delete "Genesis/host/STAMP"
        backend_exists "Genesis/host/STAMP/data/payload.txt" || echo "DELETED=correct"
        backend_cleanup
    """)
    proc = _run_bash(body, env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "backend=local" in proc.stdout
    assert "EXISTS=yes" in proc.stdout
    assert "MISSING=correct" in proc.stdout
    assert "DELETED=correct" in proc.stdout
    # list emitted exactly the one child name
    listed = proc.stdout.split("LIST_START")[1].split("LIST_END")[0].split()
    assert listed == ["payload.txt"], listed
    # real bytes landed off-site and round-tripped identically; delete cleaned up
    assert got.read_text() == "HELLO-PAYLOAD"
    assert not (root / "Genesis/host/STAMP").exists()


def test_local_unavailable_when_root_missing(tmp_path):
    """A local target whose directory does not exist is NOT available (so the
    caller treats it like an unusable/unconfigured backend, not silent success)."""
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "local",
           "GENESIS_BACKUP_LOCAL_PATH": str(tmp_path / "does-not-exist")}
    body = 'backend_init\nbackend_available && echo "AVAIL=wrong" || echo "AVAIL=no"\n'
    proc = _run_bash(body, env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "AVAIL=no" in proc.stdout


# ── none backend (public default) ────────────────────────────────────

def test_none_backend_unavailable_and_safe(tmp_path):
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "none"}
    body = textwrap.dedent("""
        backend_init
        echo "backend=$(backend_name)"
        backend_available && echo "AVAIL=wrong" || echo "AVAIL=no"
        backend_exists "anything" && echo "EXISTS=wrong" || echo "EXISTS=no"
        backend_list "anything"   # must be a no-op, emit nothing
        backend_cleanup
    """)
    proc = _run_bash(body, env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "backend=none" in proc.stdout
    assert "AVAIL=no" in proc.stdout
    assert "EXISTS=no" in proc.stdout


# ── backward-compat: legacy NAS → smb ────────────────────────────────

def test_legacy_nas_resolves_to_smb(tmp_path):
    """A configured GENESIS_BACKUP_NAS with no explicit selector → backend=smb,
    so existing NAS installs keep working without setting the new selector."""
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "smbclient", "#!/usr/bin/env bash\nexit 0\n")
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "",  # explicitly unset selector
           "GENESIS_BACKUP_NAS": "//nas/share",
           "GENESIS_BACKUP_NAS_USER": "u", "GENESIS_BACKUP_NAS_PASS": "p"}
    body = textwrap.dedent("""
        backend_init
        echo "backend=$(backend_name)"
        backend_available && echo "AVAIL=yes"
        backend_cleanup
    """)
    proc = _run_bash(body, env, extra_path=bind)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "backend=smb" in proc.stdout
    assert "AVAIL=yes" in proc.stdout


# ── smb backend: command shapes + ls parsing (logging stub) ──────────

_SMB_STUB = textwrap.dedent("""\
    #!/usr/bin/env bash
    cmd=""; prev=""
    for a in "$@"; do [ "$prev" = "-c" ] && cmd="$a"; prev="$a"; done
    printf '%s\\n' "$cmd" >> "$SMB_LOG"
    case "$cmd" in
      *"; ls"*)
         printf '  .                          D        0  Mon\\n'
         printf '  ..                         D        0  Mon\\n'
         printf '  20260617T180000Z           D        0  Mon\\n'
         printf '  20260618T180000Z           D        0  Mon\\n'
         printf '  COMPLETE                   A        0  Mon\\n'
         printf '\\t\\t65211 blocks of size 4096. 12345 blocks available\\n'
         ;;
    esac
    exit 0
""")


def test_smb_command_shapes_and_list_parsing(tmp_path):
    bind = tmp_path / "bin"
    bind.mkdir()
    log = tmp_path / "smb.log"
    _make_stub(bind / "smbclient", _SMB_STUB)
    src = tmp_path / "f.gpg"
    src.write_text("x")
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "smb",
           "GENESIS_BACKUP_NAS": "//nas/share",
           "GENESIS_BACKUP_NAS_USER": "u", "GENESIS_BACKUP_NAS_PASS": "p",
           "SMB_LOG": str(log)}
    body = textwrap.dedent(f"""
        backend_init
        backend_mkdir "Genesis/host/STAMP/data"
        backend_put "{src}" "Genesis/host/STAMP/data/f.gpg"
        backend_get "Genesis/host/STAMP/data/f.gpg" "{tmp_path}/out.gpg"
        echo "LIST_START"; backend_list "Genesis/host/STAMP"; echo "LIST_END"
        backend_exists "Genesis/host/STAMP/COMPLETE" && echo "COMPLETE=present"
        backend_delete "Genesis/host/STAMP"
        backend_cleanup
    """)
    proc = _run_bash(body, env, extra_path=bind)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    cmds = log.read_text()
    # mkdir creates EACH ancestor (smbclient mkdir is non-recursive)
    assert 'mkdir "Genesis"' in cmds
    assert 'mkdir "Genesis/host"' in cmds
    assert 'mkdir "Genesis/host/STAMP/data"' in cmds
    # put cd's into the dir and puts the basename
    assert 'cd "Genesis/host/STAMP/data"; put' in cmds
    # get cd's into the dir and gets the basename
    assert 'get "f.gpg"' in cmds
    # delete is a recursive deltree
    assert 'deltree "Genesis/host/STAMP"' in cmds
    # list parsing extracted real entries only (no ./.. and no "blocks" summary)
    listed = set(proc.stdout.split("LIST_START")[1].split("LIST_END")[0].split())
    assert listed == {"20260617T180000Z", "20260618T180000Z", "COMPLETE"}, listed
    assert "COMPLETE=present" in proc.stdout


def test_smb_creds_cleaned_up(tmp_path):
    """backend_cleanup removes the temp creds file (no plaintext creds left behind)."""
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "smbclient", "#!/usr/bin/env bash\nexit 0\n")
    marker = tmp_path / "creds_path.txt"
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "smb",
           "GENESIS_BACKUP_NAS": "//nas/share",
           "GENESIS_BACKUP_NAS_USER": "u", "GENESIS_BACKUP_NAS_PASS": "p"}
    body = textwrap.dedent(f"""
        backend_init
        printf '%s' "$_BACKEND_CREDS" > "{marker}"
        [ -f "$_BACKEND_CREDS" ] && echo "CREDS_EXIST=yes"
        backend_cleanup
        [ -z "$_BACKEND_CREDS" ] && echo "CREDS_VAR_CLEARED=yes"
    """)
    proc = _run_bash(body, env, extra_path=bind)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "CREDS_EXIST=yes" in proc.stdout
    assert "CREDS_VAR_CLEARED=yes" in proc.stdout
    creds_path = marker.read_text().strip()
    assert creds_path and not Path(creds_path).exists(), "creds temp file not removed by cleanup"
