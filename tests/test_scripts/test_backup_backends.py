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

import pytest

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


def test_local_list_dirs_excludes_files(tmp_path):
    """backend_list_dirs returns DIRECTORY names only — a stray file is excluded
    (so the restore host/stamp auto-detect can't be fooled by a stray file)."""
    root = tmp_path / "offsite"
    (root / "Genesis" / "hostA").mkdir(parents=True)
    (root / "Genesis" / "hostB").mkdir(parents=True)
    (root / "Genesis" / "stray.txt").write_text("junk")
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "local", "GENESIS_BACKUP_LOCAL_PATH": str(root)}
    body = ('backend_init\n'
            'echo "DIRS_START"; backend_list_dirs "Genesis"; echo "DIRS_END"\n'
            'echo "ALL_START"; backend_list "Genesis"; echo "ALL_END"\n')
    proc = _run_bash(body, env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    dirs = set(proc.stdout.split("DIRS_START")[1].split("DIRS_END")[0].split())
    assert dirs == {"hostA", "hostB"}, dirs
    allnames = set(proc.stdout.split("ALL_START")[1].split("ALL_END")[0].split())
    assert "stray.txt" in allnames, "plain backend_list should still include files"


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
    printf '%s\\n' "$*" >> "$SMB_LOG"
    case "$cmd" in
      ls*)
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
        echo "DIRS_START"; backend_list_dirs "Genesis/host/STAMP"; echo "DIRS_END"
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
    # A checked starting directory and one operation preserve directory errors.
    assert '-D Genesis/host/STAMP/data -c put' in cmds
    assert '; put' not in cmds
    assert 'get "f.gpg"' in cmds
    # delete is a recursive deltree
    assert 'deltree "Genesis/host/STAMP"' in cmds
    # list parsing extracted real entries only (no ./.. and no "blocks" summary)
    listed = set(proc.stdout.split("LIST_START")[1].split("LIST_END")[0].split())
    assert listed == {"20260617T180000Z", "20260618T180000Z", "COMPLETE"}, listed
    # backend_list_dirs excludes COMPLETE (a file, attr "A") — dirs only.
    dirs = set(proc.stdout.split("DIRS_START")[1].split("DIRS_END")[0].split())
    assert dirs == {"20260617T180000Z", "20260618T180000Z"}, dirs
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


def test_strict_local_distinguishes_empty_absent_and_failed(tmp_path):
    root = tmp_path / "backend"
    root.mkdir()
    (root / "empty").mkdir()
    (root / "file").write_text("not a directory")
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "local", "GENESIS_BACKUP_LOCAL_PATH": str(root)}
    proc = _run_bash(
        'backend_init\nbackend_list_strict empty\n'
        'rc=0; backend_list_strict absent || rc=$?; echo "ABSENT=$rc"\n'
        'rc=0; backend_list_strict file || rc=$?; echo "FAILED=$rc"\n'
        'rc=0; backend_list_dirs_strict file || rc=$?; echo "DIRFAILED=$rc"\n', env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ABSENT=3" in proc.stdout
    assert "FAILED=1" in proc.stdout and "DIRFAILED=1" in proc.stdout


def test_strict_smb_reports_single_command_failure(tmp_path):
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "smbclient", '#!/bin/sh\necho NT_STATUS_ACCESS_DENIED\nexit 1\n')
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "smb", "GENESIS_BACKUP_NAS": "//synthetic/share"}
    proc = _run_bash(
        'backend_init\nrc=0; backend_list_strict Genesis || rc=$?; echo "FILES=$rc"\n'
        'rc=0; backend_list_dirs_strict Genesis || rc=$?; echo "DIRS=$rc"\nbackend_cleanup\n',
        env, extra_path=bind,
    )
    assert proc.returncode == 0, proc.stderr
    assert "FILES=1" in proc.stdout and "DIRS=1" in proc.stdout


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["", "upload", "rename_status", "rename_exit"])
def test_smb_atomic_replacement_preserves_final_until_commit(tmp_path, existing, failure):
    """Exercise replacement and failure cleanup against a filesystem SMB model.

    The model implements smbclient's documented rename [-f] supersede contract;
    it does not establish network/server compatibility.
    """
    bind = tmp_path / "bin"
    bind.mkdir()
    remote = tmp_path / "remote"
    directory = remote / "Genesis" / "pool"
    directory.mkdir(parents=True)
    final = directory / "capture.gpg"
    if existing:
        final.write_bytes(b"corrupt-original")
    source = tmp_path / "source.gpg"
    source.write_bytes(b"validated-ciphertext")
    log = tmp_path / "commands.log"
    _make_stub(bind / "smbclient", textwrap.dedent("""\
        #!/usr/bin/env python3
        import os, pathlib, shlex, shutil, sys
        command = sys.argv[sys.argv.index('-c') + 1]
        with open(os.environ['SMB_LOG'], 'a') as stream:
            stream.write(command + '\\n')
        cwd = pathlib.Path(os.environ['SMB_ROOT'])
        if '-D' in sys.argv:
            cwd = cwd / sys.argv[sys.argv.index('-D') + 1]
            if not cwd.is_dir():
                print('cd ' + str(cwd) + ': NT_STATUS_OBJECT_PATH_NOT_FOUND')
                sys.exit(1)
        failure = os.environ['SMB_FAILURE']
        for part in command.split(';'):
            args = shlex.split(part)
            if args[0] == 'cd':
                cwd = cwd / args[1]
            elif args[0] == 'put':
                shutil.copyfile(args[1], cwd / args[2])
                if failure == 'upload':
                    print('NT_STATUS_DISK_FULL')
                    sys.exit(1)
            elif args[0] == 'rename':
                destination = cwd / args[2]
                if failure == 'rename_exit':
                    sys.exit(1)
                if failure == 'rename_status' or (destination.exists() and args[3:] != ['-f']):
                    print('NT_STATUS_ACCESS_DENIED')
                    sys.exit(1)
                os.replace(cwd / args[1], destination)
            elif args[0] == 'get':
                shutil.copyfile(cwd / args[1], args[2])
            elif args[0] == 'deltree':
                path = cwd / args[1]
                if path.exists():
                    path.unlink()
            else:
                raise AssertionError(args)
        """))
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "smb",
           "GENESIS_BACKUP_NAS": "//synthetic/share", "SMB_ROOT": str(remote),
           "SMB_LOG": str(log), "SMB_FAILURE": failure}
    proc = _run_bash(
        f'backend_init\nrc=0; backend_put_atomic "{source}" Genesis/pool/capture.gpg || rc=$?\n'
        'echo "PUT=$rc"\nbackend_cleanup\n', env, extra_path=bind,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"PUT={1 if failure else 0}" in proc.stdout
    if failure:
        assert final.exists() == existing
        if existing:
            assert final.read_bytes() == b"corrupt-original"
    else:
        assert final.read_bytes() == source.read_bytes()
    assert sorted(p.name for p in directory.iterdir()) == ([final.name] if final.exists() else [])
    commands = log.read_text().splitlines()
    assert not any('deltree "Genesis/pool/capture.gpg"' in command for command in commands)
    if failure != "upload":
        assert any(command.endswith('"capture.gpg" -f') for command in commands)


@pytest.mark.parametrize("failure", ["", "missing", "altered", "read_error"])
def test_verified_upload_requires_fresh_exact_readback(tmp_path, failure):
    root = tmp_path / "remote"
    root.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"good")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "local", "GENESIS_BACKUP_LOCAL_PATH": str(root),
           "FAILURE": failure}
    proc = _run_bash(
        'backend_init\n'
        '_local_put() { mkdir -p "$(dirname "$_BACKEND_LOCAL_ROOT/$2")"; '
        'case "$FAILURE" in missing) return 0;; altered) printf bad! >"$_BACKEND_LOCAL_ROOT/$2";; '
        '*) cp -- "$1" "$_BACKEND_LOCAL_ROOT/$2";; esac; }\n'
        '_local_get() { [ "$FAILURE" != read_error ] && cp -- "$_BACKEND_LOCAL_ROOT/$1" "$2"; }\n'
        f'rc=0; backend_put_verified "{source}" payload "{scratch}" || rc=$?\n'
        'echo "VERIFIED=$rc"\n', env,
    )
    assert proc.returncode == 0, proc.stderr
    assert ("VERIFIED=0" in proc.stdout) == (failure == "")
    assert not list(scratch.iterdir())


def test_smb_single_command_error_refuses_even_empty_payload(tmp_path):
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "smbclient", '#!/bin/sh\necho NT_STATUS_DISK_FULL\nexit 1\n')
    source = tmp_path / "empty"
    source.touch()
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "smb", "GENESIS_BACKUP_NAS": "//synthetic/share"}
    proc = _run_bash(
        f'backend_init\nrc=0; backend_put_verified "{source}" payload "{tmp_path}" || rc=$?\n'
        f'getrc=0; backend_get payload "{tmp_path}/readback" || getrc=$?\n'
        'echo "PUT=$rc GET=$getrc"\nbackend_cleanup\n', env, extra_path=bind,
    )
    assert proc.returncode == 0, proc.stderr
    assert "PUT=1 GET=1" in proc.stdout
    assert not list(tmp_path.glob(".backend-readback.*"))


def test_authority_marker_verified_before_final_publication(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    marker = tmp_path / "marker"
    marker.write_bytes(b"genesis-snapshot 1\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "local", "GENESIS_BACKUP_LOCAL_PATH": str(root)}
    proc = _run_bash(
        'backend_init\n_local_put() { printf corrupt >"$_BACKEND_LOCAL_ROOT/$2"; }\n'
        f'rc=0; backend_put_atomic "{marker}" COMPLETE "{scratch}" || rc=$?\n'
        'echo "MARKER=$rc"\n', env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "MARKER=1" in proc.stdout
    assert not list(root.iterdir())
    assert not list(scratch.iterdir())


def test_smb_checked_directory_and_status_like_names(tmp_path):
    """Single-operation statuses cannot be hidden by a later batch command."""
    bind = tmp_path / "bin"
    bind.mkdir()
    root = tmp_path / "remote"
    directory = root / "NT_STATUS_directory"
    directory.mkdir(parents=True)
    source = tmp_path / "NT_STATUS_source"
    source.write_bytes(b"synthetic ciphertext")
    (directory / "NT_STATUS_child").mkdir()
    _make_stub(bind / "smbclient", (Path(__file__).parents[1] / "smbclient_filesystem_model.py").read_text())
    env = {"GENESIS_BACKUP_TIER2_BACKEND": "smb", "GENESIS_BACKUP_NAS": "//synthetic/share",
           "SMB_ROOT": str(root), "SMB_LOG": str(tmp_path / "commands")}
    proc = _run_bash(
        f'backend_init\nbackend_put_verified "{source}" NT_STATUS_directory/NT_STATUS_payload "{tmp_path}"\n'
        f'backend_get NT_STATUS_directory/NT_STATUS_payload "{tmp_path}/NT_STATUS_fetched"\n'
        f'backend_put_atomic "{source}" NT_STATUS_directory/NT_STATUS_final "{tmp_path}"\n'
        'backend_list_strict NT_STATUS_directory\nbackend_list_dirs_strict NT_STATUS_directory\n'
        'backend_exists NT_STATUS_directory/NT_STATUS_final\n'
        'rc=0; backend_list_strict NT_STATUS_missing || rc=$?; echo "ABSENT=$rc"\n'
        f'rc=0; backend_put "{source}" missing/wrong-root || rc=$?; echo "PUT=$rc"\n'
        'backend_cleanup\n', env, extra_path=bind,
    )
    assert proc.returncode == 0, proc.stderr
    assert "NT_STATUS_payload" in proc.stdout and "NT_STATUS_child" in proc.stdout
    assert "ABSENT=3" in proc.stdout and "PUT=1" in proc.stdout
    assert (tmp_path / "NT_STATUS_fetched").read_bytes() == source.read_bytes()
    assert (directory / "NT_STATUS_final").read_bytes() == source.read_bytes()
    assert not (root / "wrong-root").exists()
