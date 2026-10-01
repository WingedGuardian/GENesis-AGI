"""Tests for ``python -m genesis session-address`` and the lookup it shares
with the ``session_address`` MCP tool. Synthetic ids; no real registry."""

from __future__ import annotations

import argparse
import json
import subprocess

import aiosqlite
import pytest

from genesis.session_awareness import peer_address as pa
from genesis.session_awareness import peer_address_cli as cli

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "11111111-9999-3333-4444-555555555555"


@pytest.fixture
def fake_resolver(monkeypatch):
    """Answer every full id as live under the name 'genesis-7f'."""
    seen: list[list[str]] = []

    def resolve_many(ids, **_k):
        seen.append(list(ids))
        return {sid: pa.Resolution(sid, pa.OK, name="genesis-7f", pane="cc-2:@1.%1") for sid in ids}

    monkeypatch.setattr(pa, "resolve_many", resolve_many)
    return seen


async def _db(tmp_path, ids):
    db = await aiosqlite.connect(tmp_path / "g.db")
    await db.execute("CREATE TABLE session_charters (session_id TEXT)")
    await db.execute("CREATE TABLE cc_sessions (cc_session_id TEXT)")
    await db.execute("CREATE TABLE session_heartbeats (cc_session_id TEXT)")
    for sid in ids:
        await db.execute("INSERT INTO cc_sessions VALUES (?)", (sid,))
    await db.commit()
    return db


async def test_a_unique_prefix_is_expanded_and_resolved(tmp_path, fake_resolver):
    db = await _db(tmp_path, [SID])
    try:
        (res,) = await cli.lookup(db, ["11111111-2"])
    finally:
        await db.close()
    assert (res["query"], res["session_id"], res["status"]) == ("11111111-2", SID, "ok")
    assert res["display"] == "-> genesis-7f (cc-2:@1.%1)"
    assert fake_resolver == [[SID]]


async def test_an_ambiguous_prefix_is_never_looked_up_as_an_id(tmp_path, fake_resolver):
    db = await _db(tmp_path, [SID, SID2])
    try:
        (res,) = await cli.lookup(db, ["11111111"])
    finally:
        await db.close()
    assert res["status"] == "unresolved-prefix" and res["session_id"] is None
    assert fake_resolver == [[]]


async def test_a_full_id_resolves_without_a_database(fake_resolver):
    (res,) = await cli.lookup(None, [SID])
    assert res["status"] == "ok"


def test_the_check_reports_each_disagreement():
    results = [
        {"session_id": SID, "status": "ok", "name": "genesis-7f"},
        {"session_id": SID2, "status": "not-reachable", "name": None},
        {"session_id": None, "status": "unresolved-prefix", "name": None},
    ]
    assert cli.disagreements(results, {SID: "genesis-7f"}) == []
    problems = cli.disagreements(results, {SID: "genesis-xx", SID2: "genesis-5a"})
    assert len(problems) == 2
    assert SID in problems[0] and "genesis-xx" in problems[0]
    assert SID2 in problems[1] and "genesis-5a" in problems[1]


def _completed(stdout: str, rc: int = 0):
    return subprocess.CompletedProcess(["claude"], rc, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    ("stdout", "rc", "expect"),
    [
        ("not json", 0, "not JSON"),
        ('{"a": 1}', 0, "did not print a list"),
        ("[]", 3, "exited 3"),
    ],
)
def test_an_unusable_oracle_is_an_error_not_an_empty_answer(monkeypatch, stdout, rc, expect):
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _completed(stdout, rc))
    out = cli._oracle()
    assert isinstance(out, str) and expect in out


def test_the_oracle_maps_session_ids_to_names(monkeypatch):
    rows = [{"sessionId": SID, "name": "genesis-7f"}, {"name": "no-id"}, "junk"]
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/bin/claude")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _completed(json.dumps(rows)))
    assert cli._oracle() == {SID: "genesis-7f"}


def test_check_exits_1_on_disagreement(monkeypatch, capsys, fake_resolver):
    async def no_db():
        return None

    monkeypatch.setattr(cli, "_open_db", no_db)
    monkeypatch.setattr(cli, "_oracle", lambda: {SID: "someone-else"})
    args = argparse.Namespace(ids=[SID], json=False, check=True)
    assert cli._cmd(args) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == "11111111 -> genesis-7f (cc-2:@1.%1)"
    assert "DISAGREE" in captured.err


def test_check_exits_0_when_it_agrees(monkeypatch, capsys, fake_resolver):
    async def no_db():
        return None

    monkeypatch.setattr(cli, "_open_db", no_db)
    monkeypatch.setattr(cli, "_oracle", lambda: {SID: "genesis-7f"})
    assert cli._cmd(argparse.Namespace(ids=[SID], json=True, check=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "genesis-7f"


def test_a_not_reachable_line_says_why(monkeypatch, capsys):
    async def no_db():
        return None

    monkeypatch.setattr(cli, "_open_db", no_db)
    monkeypatch.setattr(
        pa,
        "resolve_many",
        lambda ids, **k: {
            s: pa.Resolution(s, pa.NOT_REACHABLE, detail=("pid 7: dead",)) for s in ids
        },
    )
    assert cli._cmd(argparse.Namespace(ids=[SID], json=False, check=False)) == 0
    assert capsys.readouterr().out.strip() == "11111111 -> (not reachable)  [pid 7: dead]"


def test_the_subcommand_is_registered():
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["session-address", SID, "--check"])
    assert args.func is cli._cmd and args.ids == [SID] and args.check


def test_python_m_genesis_dispatches_the_subcommand(monkeypatch):
    """Drives the real entry point, so dropping the command from __main__'s
    dispatch fails here rather than silently printing help."""
    import genesis.__main__ as entry

    calls = []
    monkeypatch.setattr(cli, "_cmd", lambda args: calls.append(args.ids) or 0)
    monkeypatch.setattr(entry.sys, "argv", ["genesis", "session-address", SID])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
    assert calls == [[SID]]


def test_check_that_compared_nothing_is_not_clean(monkeypatch, capsys):
    """Every query an unresolved prefix: the check judged nothing, so it fails."""

    async def no_db():
        return None

    monkeypatch.setattr(cli, "_open_db", no_db)
    monkeypatch.setattr(cli, "_oracle", lambda: {})
    assert cli._cmd(argparse.Namespace(ids=["abc", "def"], json=False, check=True)) == 2
    captured = capsys.readouterr()
    assert "nothing compared (2 not compared" in captured.err
    assert "database was not found" in captured.out


def test_check_counts_only_what_it_compared(monkeypatch, capsys, fake_resolver):
    async def no_db():
        return None

    monkeypatch.setattr(cli, "_open_db", no_db)
    monkeypatch.setattr(cli, "_oracle", lambda: {SID: "genesis-7f"})
    assert cli._cmd(argparse.Namespace(ids=[SID, "abc"], json=False, check=True)) == 0
    assert "agrees with claude agents on 1 of 2 (1 not compared" in capsys.readouterr().err


async def test_a_32_to_35_character_prefix_still_expands(tmp_path, fake_resolver):
    """resolve_session_id passes 32+ characters through untouched, so a long
    prefix must not be mistaken for a full id."""
    db = await _db(tmp_path, [SID])
    try:
        results = await cli.lookup(db, [SID[:32], SID[:35], SID.upper()[:33]])
    finally:
        await db.close()
    assert [r["session_id"] for r in results] == [SID, SID, SID]


async def test_a_prefix_that_diverges_after_31_characters_is_not_expanded(tmp_path, fake_resolver):
    db = await _db(tmp_path, [SID])
    try:
        (res,) = await cli.lookup(db, [SID[:31] + "f"])
    finally:
        await db.close()
    assert res["status"] == "unresolved-prefix"


async def test_a_repeated_query_gets_one_answer_per_ask(fake_resolver):
    results = await cli.lookup(None, [SID, SID])
    assert [r["query"] for r in results] == [SID, SID]


@pytest.mark.parametrize("status", [pa.FORMAT_CHANGED, pa.AMBIGUOUS, pa.NO_REGISTRY])
def test_an_undecided_answer_fails_the_check_instead_of_matching_absent(status):
    """Claude Code omitting the session must not turn 'could not decide' into agreement."""
    results = [{"session_id": SID, "status": status, "name": None}]
    (problem,) = cli.disagreements(results, {})
    assert status in problem


async def test_the_database_path_is_percent_encoded(monkeypatch, tmp_path):
    import genesis.env

    weird = tmp_path / "work#1?x"
    weird.mkdir()
    dbfile = weird / "genesis.db"
    import sqlite3

    sqlite3.connect(dbfile).execute("CREATE TABLE marker (x)").connection.commit()
    monkeypatch.setattr(genesis.env, "genesis_db_path", lambda: dbfile)
    db = await cli._open_db()
    try:
        cur = await db.execute("SELECT name FROM sqlite_master WHERE name='marker'")
        assert await cur.fetchone() == ("marker",)
    finally:
        await db.close()


async def test_an_uppercase_full_id_needs_no_database(fake_resolver):
    hexy = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    assert hexy.upper() != hexy  # guard: the id really has letters to fold
    (res,) = await cli.lookup(None, [hexy.upper()])
    assert (res["session_id"], res["status"]) == (hexy, "ok")
