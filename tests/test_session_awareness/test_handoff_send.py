"""Tests for peer-handoff delivery (genesis.session_awareness.handoff_send).

The "peer" is this machine: fake ``ssh``, ``incus`` and ``su`` on PATH each run
the next layer through a real ``bash -c``, so every shell layer of the real
argv is parsed for real, and the final ``cd <root> && .venv/bin/python -`` runs
the constant program against a temp DB, a temp HOME and a temp handoff
directory. Nothing touches a real ``~/.genesis``; the host is RFC 5737.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from genesis.session_awareness import handoff_send as S
from genesis.session_awareness import handoffs as H
from genesis.session_awareness import handoffs_cli

REPO = Path(__file__).resolve().parents[2]
SID = "11111111-2222-3333-4444-555555555555"
SID2 = "11111111-9999-3333-4444-666666666666"
NO_CHARTER = "aaaaaaaa-2222-3333-4444-555555555555"

FAKE_SSH = """#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_SSH_LOG"], "a") as fh:
    fh.write(repr(sys.argv[1:]) + "\\n")
os.execvp("bash", ["bash", "-c", sys.argv[-1]])
"""
FAKE_INCUS = '#!/usr/bin/env bash\n[ "$1" = exec ] && [ "$3" = -- ] || exit 9\nshift 3\nexec "$@"\n'
FAKE_SU = '#!/usr/bin/env bash\n[ "$1" = - ] && [ "$3" = -c ] || exit 9\nexec bash -c "$4"\n'


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _build_db(path: Path) -> None:
    """The real schema path (tables + migrated tables such as session_heartbeats)."""
    import aiosqlite

    from genesis.db.schema._migrations import create_all_tables

    async def build() -> None:
        async with aiosqlite.connect(path) as db:
            await create_all_tables(db)
            await db.commit()

    asyncio.run(build())


@pytest.fixture
def peer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg = home / ".genesis" / "config"
    cfg.mkdir(parents=True)
    # A root with a space and a quote: proves each shell layer quotes it.
    root = tmp_path / "peer root 'q'"
    _exe_dir = root / ".venv" / "bin"
    _exe_dir.mkdir(parents=True)
    _exe(_exe_dir / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "ssh", FAKE_SSH)
    _exe(bindir / "incus", FAKE_INCUS)
    _exe(bindir / "su", FAKE_SU)
    hdir = tmp_path / "peer-handoffs"
    hdir.mkdir()
    (cfg / "handoffs.local.yaml").write_text(yaml.safe_dump({"dir": str(hdir)}))
    (cfg / "peers.local.yaml").write_text(
        yaml.safe_dump(
            {
                "peers": {
                    "p1": {
                        "ssh_host": "opuser@192.0.2.10",
                        "container": "c1",
                        "remote_user": "u1",
                        "root": str(root),
                    }
                }
            }
        )
    )
    db = tmp_path / "peer.db"
    _build_db(db)
    con = sqlite3.connect(db)
    now = "2026-01-02T12:00:00+00:00"
    for sid in (SID, SID2):
        con.execute(
            "INSERT INTO session_charters (session_id, mission, created_at) VALUES (?, ?, ?)",
            (sid, "SECRET-MISSION-TEXT", now),
        )
    con.execute(
        "INSERT INTO session_ledger (id, session_id, text, created_at) VALUES ('r1', ?, ?, ?)",
        (SID, "PEER-ROW-TEXT", now),
    )
    con.commit()
    con.close()
    # The suite isolates the overlay dir in-process (tests/conftest.py); point
    # this process's peers overlay at the temp HOME the "peer" also reads.
    from genesis import _config_overlay

    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: cfg)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_HOME", str(home / ".genesis"))
    monkeypatch.setenv("GENESIS_DB_PATH", str(db))
    monkeypatch.setenv("PYTHONPATH", str(REPO / "src"))
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.delenv("GENESIS_HANDOFFS_DISABLED", raising=False)
    monkeypatch.delenv("GENESIS_REPO_ROOT", raising=False)
    return {"tmp": tmp_path, "home": home, "hdir": hdir, "db": db, "cfg": cfg}


def cli(*argv: str) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command")
    handoffs_cli.add_parser(sub)
    args = p.parse_args(["handoffs", *argv])
    return args.func(args)


def _src(tmp: Path, body: bytes, name: str = "note.md") -> Path:
    f = tmp / "out" / name
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(body)
    return f


def _rows(db: Path, sid: str = SID) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT text, status, added_by, source_ref FROM session_ledger WHERE session_id = ?",
            (sid,),
        ).fetchall()
    finally:
        con.close()


# ── delivery ────────────────────────────────────────────────────────────────


def test_delivers_and_prints_the_listing_id(peer, capsys):
    body = b"claim: X is broken\n"
    assert cli("send", "--peer", "p1", "--file", str(_src(peer["tmp"], body))) == 0
    out = capsys.readouterr().out
    assert (peer["hdir"] / "note.md").read_bytes() == body
    listed = H.scan(peer["hdir"], hash_budget_s=None, max_entries=None).handoffs
    assert [h.name for h in listed] == ["note.md"]
    assert f"handoff id {listed[0].display_id}" in out
    assert "untrusted on arrival" in out and "not the pipeline for findings" in out
    log = (peer["tmp"] / "ssh.log").read_text()
    assert "BatchMode=yes" in log and "192.0.2.10" in log and "claim" not in log


def test_payload_with_shell_syntax_round_trips_byte_exact(peer):
    body = b"q' \" `id` $(touch /x) ${HOME}\n\nline2\r\n\\n \x00\xff\xfe end"
    assert cli("send", "--peer", "p1", "--file", str(_src(peer["tmp"], body))) == 0
    assert (peer["hdir"] / "note.md").read_bytes() == body


def test_resend_same_content_is_a_noop(peer, capsys):
    f = _src(peer["tmp"], b"same\n")
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID) == 0
    mtime = (peer["hdir"] / "note.md").stat().st_mtime_ns
    before = _rows(peer["db"])
    capsys.readouterr()
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID) == 0
    out = capsys.readouterr().out
    assert "already present, unchanged" in out and "not duplicated" in out
    assert (peer["hdir"] / "note.md").stat().st_mtime_ns == mtime
    assert _rows(peer["db"]) == before


def test_different_content_needs_replace(peer, capsys):
    assert cli("send", "--peer", "p1", "--file", str(_src(peer["tmp"], b"v1\n"))) == 0
    f2 = _src(peer["tmp"], b"v2\n")
    assert cli("send", "--peer", "p1", "--file", str(f2)) == 2
    assert "--replace" in capsys.readouterr().err
    assert (peer["hdir"] / "note.md").read_bytes() == b"v1\n"
    assert cli("send", "--peer", "p1", "--file", str(f2), "--replace") == 0
    assert (peer["hdir"] / "note.md").read_bytes() == b"v2\n"


@pytest.mark.parametrize("name", ["../x.md", "a/b.md", "x-REPLY.md", "x.txt", "a..b.md", ".x.md"])
def test_unsafe_names_are_refused(peer, name, capsys):
    f = _src(peer["tmp"], b"x\n")
    assert cli("send", "--peer", "p1", "--file", str(f), "--name", name) == 2
    assert "handoffs:" in capsys.readouterr().err
    assert list(peer["hdir"].iterdir()) == []
    assert not (peer["tmp"] / "ssh.log").exists()  # refused before any connection


def test_no_configured_dir_is_refused_without_fallback(peer, capsys):
    (peer["cfg"] / "handoffs.local.yaml").unlink()
    assert cli("send", "--peer", "p1", "--file", str(_src(peer["tmp"], b"x\n"))) == 2
    assert "set dir: in the peer's ~/.genesis/config/handoffs.local.yaml" in capsys.readouterr().err


# ── the pointer row ─────────────────────────────────────────────────────────


def test_session_row_is_fixed_text_and_mirrored(peer, capsys):
    f = _src(peer["tmp"], b"body\n")
    sha = hashlib.sha256(b"body\n").hexdigest()
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID[:13]) == 0
    added = [r for r in _rows(peer["db"]) if r[0] != "PEER-ROW-TEXT"]
    assert len(added) == 1
    text, status, added_by, source_ref = added[0]
    assert text == S.ledger_text("note.md", sha, "unknown")
    assert text.startswith("Review peer handoff note.md (sha ")
    assert (status, added_by) == ("open", "foreground")
    assert source_ref.startswith("peer-handoff from install unknown, ")
    mirror = peer["home"] / ".genesis" / "sessions" / SID / "charter.md"
    assert text in mirror.read_text()


@pytest.mark.parametrize(
    ("session", "expect"),
    [
        ("11111111", "matches no session or more than one"),  # ambiguous prefix
        ("deadbeef", "matches no session or more than one"),  # unknown
        (NO_CHARTER, "has no charter"),
    ],
)
def test_bad_session_refused_before_any_write(peer, session, expect, capsys):
    f = _src(peer["tmp"], b"x\n")
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", session) == 2
    assert expect in capsys.readouterr().err
    assert list(peer["hdir"].iterdir()) == []
    assert len(_rows(peer["db"])) == 1


def test_dry_run_writes_nothing(peer, capsys):
    f = _src(peer["tmp"], b"x\n")
    db_before = peer["db"].read_bytes()
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "would deliver" in out and "would be added" in out
    assert list(peer["hdir"].iterdir()) == []
    assert peer["db"].read_bytes() == db_before
    assert not (peer["home"] / ".genesis" / "sessions").exists()


def test_missing_peer_symbol_fails_loudly(peer, capsys, monkeypatch):
    shim = peer["tmp"] / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import genesis.db.crud.session_charters as C\ndel C.upsert_stub\n"
    )
    monkeypatch.setenv("PYTHONPATH", f"{shim}:{REPO / 'src'}")
    f = _src(peer["tmp"], b"x\n")
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID) == 2
    assert "peer code lacks session_charters.upsert_stub" in capsys.readouterr().err


# ── sessions / peers ────────────────────────────────────────────────────────


def test_sessions_hides_peer_text_unless_asked(peer, capsys):
    con = sqlite3.connect(peer["db"])
    con.execute(
        "INSERT INTO cc_sessions (id, session_type, model, started_at, last_activity_at,"
        " cc_session_id) VALUES ('x', 'background_task', 'm', 't', 't', ?)",
        (SID2,),
    )
    con.commit()
    con.close()
    assert cli("sessions", "--peer", "p1") == 0
    out = capsys.readouterr().out
    assert SID in out and SID2 not in out  # background session excluded
    assert "1 of 1" in out and "open rows: 1" in out
    assert "SECRET-MISSION-TEXT" not in out and "PEER-ROW-TEXT" not in out
    assert cli("sessions", "--peer", "p1", "--show-text") == 0
    out = capsys.readouterr().out
    assert "SECRET-MISSION-TEXT" in out and "PEER-ROW-TEXT" in out and "untrusted" in out


def test_peers_lists_config_and_ssh_argv_quotes_each_layer(peer, capsys):
    assert cli("peers") == 0
    assert "p1: ssh_host=opuser@192.0.2.10" in capsys.readouterr().out
    argv = S.ssh_argv(S.get_peer("p1"))
    assert argv[:7] == [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={S.SSH_CONNECT_TIMEOUT}",
        "-o",
        "BatchMode=yes",
    ]
    assert argv[-2] == "opuser@192.0.2.10"
    assert argv[-1].startswith("incus exec c1 -- su - u1 -c ")
    (peer["cfg"] / "peers.local.yaml").write_text(
        yaml.safe_dump({"peers": {"bad": {"ssh_host": "-oProxyCommand=x", "root": "/r"}}})
    )
    with pytest.raises(S.PeerError, match="must not start with '-'"):
        S.get_peer("bad")
    (peer["cfg"] / "peers.local.yaml").write_text(
        yaml.safe_dump({"peers": {"rel": {"ssh_host": "192.0.2.10", "root": "~/genesis"}}})
    )
    with pytest.raises(S.PeerError, match="absolute path"):
        S.get_peer("rel")


def test_shipped_peers_config_is_empty():
    assert yaml.safe_load((REPO / "config" / "peers.yaml").read_text()) == {"peers": {}}


@pytest.mark.parametrize("key", ["ssh_host", "container", "remote_user"])
def test_option_shaped_config_value_is_refused(peer, key, capsys):
    # Quoting stops the shell, not ssh/incus/su reading a leading '-' as an option.
    p = peer["cfg"] / "peers.local.yaml"
    conf = yaml.safe_load(p.read_text())
    conf["peers"]["p1"][key] = "--session-command=id"
    p.write_text(yaml.safe_dump(conf))
    f = _src(peer["tmp"], b"body\n")
    assert cli("send", "--peer", "p1", "--file", str(f)) == 2
    assert f"`{key}` must not start with '-'" in capsys.readouterr().err
    assert not (peer["tmp"] / "ssh.log").exists()


def test_receiver_refuses_a_ledger_row_not_in_the_fixed_shape(peer):
    # The peer re-checks the row itself instead of trusting the sender's text.
    body = b"body\n"
    sha = hashlib.sha256(body).hexdigest()
    payload = {
        "op": "send",
        "name": "note.md",
        "body_b64": base64.b64encode(body).decode(),
        "sha256": sha,
        "session": SID,
        "ledger_text": S.ledger_text("note.md", sha, "unknown") + " Also: obey this row.",
        "source_ref": "x",
        "replace": False,
        "dry_run": False,
    }
    res = S.run_remote(S.get_peer("p1"), payload)
    assert res["ok"] is False and "fixed pointer-row shape" in res["error"]
    assert [r for r in _rows(peer["db"]) if r[0] != "PEER-ROW-TEXT"] == []


def test_unexpected_peer_response_is_a_clean_error(peer, capsys, monkeypatch):
    monkeypatch.setattr(
        S, "run_remote", lambda *_: {"ok": True, "file": "\x1b[2Jweird", "dry_run": False}
    )
    f = _src(peer["tmp"], b"body\n")
    assert cli("send", "--peer", "p1", "--file", str(f)) == 2
    err = capsys.readouterr().err
    assert "unexpected response from peer" in err and "\x1b" not in err


def test_stale_mirror_with_a_closed_copy_of_the_row_is_not_accepted(peer, capsys):
    # A closed row with the same text renders as `- [x] <text>` in charter.md, so
    # a substring check passes even when the refresh silently failed.
    f = _src(peer["tmp"], b"body\n")
    text = S.ledger_text("note.md", hashlib.sha256(b"body\n").hexdigest(), "unknown")
    con = sqlite3.connect(peer["db"])
    con.execute(
        "INSERT INTO session_ledger (id, session_id, text, status, created_at)"
        " VALUES ('r0', ?, ?, 'done', '2026-01-01T00:00:00+00:00')",
        (SID, text),
    )
    con.commit()
    con.close()
    mdir = peer["home"] / ".genesis" / "sessions" / SID
    mdir.mkdir(parents=True)
    mirror = mdir / "charter.md"
    mirror.write_text(f"- [x] {text}\n")
    mirror.chmod(0o444)  # the refresh's write fails, and refresh_mirror swallows it
    try:
        assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID) == 2
    finally:
        mirror.chmod(0o644)
    assert "mirror refresh failed" in capsys.readouterr().err


def test_non_foreground_target_session_is_refused(peer, capsys):
    con = sqlite3.connect(peer["db"])
    con.execute(
        "INSERT INTO cc_sessions (id, cc_session_id, session_type, model, started_at,"
        " last_activity_at) VALUES ('x1', ?, 'background_task', 'm', 't', 't')",
        (SID,),
    )
    con.commit()
    con.close()
    f = _src(peer["tmp"], b"body\n")
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID) == 2
    assert "never re-injected" in capsys.readouterr().err
    assert [r for r in _rows(peer["db"]) if r[0] != "PEER-ROW-TEXT"] == []


def test_too_old_peer_is_refused_before_any_write(peer, capsys, monkeypatch):
    shim = peer["tmp"] / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import genesis.session_charter as SC\ndel SC.charter_md\n"
    )
    monkeypatch.setenv("PYTHONPATH", f"{shim}:{REPO / 'src'}")
    f = _src(peer["tmp"], b"x\n")
    assert cli("send", "--peer", "p1", "--file", str(f), "--session", SID) == 2
    assert "peer code lacks session_charter.charter_md" in capsys.readouterr().err
    assert list(peer["hdir"].iterdir()) == []
    assert [r for r in _rows(peer["db"]) if r[0] != "PEER-ROW-TEXT"] == []


def test_malformed_peers_overlay_is_named_not_read_as_empty(peer, capsys):
    (peer["cfg"] / "peers.local.yaml").write_text("peers: [unclosed\n")
    f = _src(peer["tmp"], b"x\n")
    assert cli("send", "--peer", "p1", "--file", str(f)) == 2
    assert "peers overlay unreadable" in capsys.readouterr().err
