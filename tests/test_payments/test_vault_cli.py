"""Tests for ``python -m genesis vault``.

Only Stripe's public test numbers appear here, built from parts.
"""

from __future__ import annotations

import argparse
import importlib
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pyrage import passphrase as age_passphrase

from genesis.payments import cli
from genesis.payments import vault_store as vs

PASSPHRASE = "quartz lantern ember noodle 41"
VISA = "4242" * 4


def _future() -> str:
    """An expiry two years out, computed per test (never frozen at import)."""
    return f"12/{(datetime.now(UTC).year + 2) % 100:02d}"


# A stand-in for scrypt that has the shape of a sealed key (header, stanza,
# 43-character MAC, 106-byte payload), so the vault reads as intact.
FAKE_SEALED = (
    b"age-encryption.org/v1\n-> scrypt "
    + b"c2FsdA 18\nd3JhcA\n--- "
    + b"m" * 43
    + b"\n"
    + b"p" * 106
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    for name in cli._AGENT_SESSION_MARKERS:
        monkeypatch.delenv(name, raising=False)
    # Fast stand-in for scrypt; the store tests exercise the real one.
    monkeypatch.setattr(age_passphrase, "encrypt", lambda data, pw: FAKE_SEALED)
    return tmp_path


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)


def _answers(monkeypatch, secrets, typed=("", "")):
    getpass = MagicMock(side_effect=list(secrets))
    monkeypatch.setattr(cli.getpass, "getpass", getpass)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=list(typed)))
    return getpass


def _run(argv):
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["vault", *argv])
    return args.func(args)


class TestOwnerTerminal:
    @pytest.mark.parametrize(
        "argv", [["init"], ["add", "--label", "x", "--single-use"], ["retire", "card_x"]]
    )
    @pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID"])
    def test_refused_inside_a_coding_agent_session(self, argv, marker, tty, monkeypatch, capsys):
        monkeypatch.setenv(marker, "1")
        getpass = _answers(monkeypatch, [])
        assert _run(argv) == 2
        getpass.assert_not_called()
        assert "coding agent's session" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv", [["init"], ["add", "--label", "x", "--single-use"], ["retire", "card_x"]]
    )
    def test_refused_without_a_terminal(self, argv, monkeypatch):
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        getpass = _answers(monkeypatch, [])
        assert _run(argv) == 2
        getpass.assert_not_called()

    def test_refused_without_the_payments_extra(self, tty, monkeypatch, capsys):
        real = cli.importlib.util.find_spec
        monkeypatch.setattr(
            cli.importlib.util, "find_spec", lambda m: None if m == "pyrage" else real(m)
        )
        getpass = _answers(monkeypatch, [])
        assert _run(["init"]) == 2
        getpass.assert_not_called()
        assert "payments extra" in capsys.readouterr().err

    def test_list_and_status_run_anywhere(self, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDECODE", "1")
        assert _run(["list"]) == 0 and "no cards" in capsys.readouterr().out
        assert _run(["status"]) == 0 and "not initialised" in capsys.readouterr().out


class TestInit:
    def test_creates_the_vault(self, tty, monkeypatch, _home):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        assert _run(["init"]) == 0
        assert vs.is_initialised(_home / "payment_vault")

    def test_mismatched_passphrases_create_nothing(self, tty, monkeypatch, _home):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE + "x"])
        assert _run(["init"]) == 2
        assert not (_home / "payment_vault").exists()

    def test_an_initialised_vault_is_refused_before_any_prompt(self, tty, monkeypatch, capsys):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        assert _run(["init"]) == 0
        getpass = _answers(monkeypatch, [])
        assert _run(["init"]) == 2
        getpass.assert_not_called()
        assert "already initialised" in capsys.readouterr().err

    def test_weak_passphrase(self, tty, monkeypatch, capsys):
        _answers(monkeypatch, ["password1", "password1"])
        assert _run(["init"]) == 2
        assert "guessable" in capsys.readouterr().err


class TestAdd:
    @pytest.fixture
    def initialised(self, tty, monkeypatch):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        assert _run(["init"]) == 0

    def test_adds_without_printing_the_number(self, initialised, monkeypatch, capsys):
        getpass = _answers(monkeypatch, [VISA, _future(), "123"], typed=["A Person", ""])
        capsys.readouterr()
        assert _run(["add", "--label", "bills", "--merchant", "shop.example.com"]) == 0
        out = capsys.readouterr()
        assert "ending 4242" in out.out and VISA not in out.out + out.err
        assert getpass.call_count == 3  # number, expiry and code are all hidden
        [card] = vs.list_cards()
        assert card["label"] == "bills" and card["merchant"] == "shop.example.com"

    def test_scope_is_checked_before_any_prompt(self, initialised, monkeypatch, capsys):
        getpass = _answers(monkeypatch, [])
        assert _run(["add", "--label", "x"]) == 2
        getpass.assert_not_called()
        assert "--single-use" in capsys.readouterr().err

    def test_bad_merchant_is_checked_before_any_prompt(self, initialised, monkeypatch):
        getpass = _answers(monkeypatch, [])
        assert _run(["add", "--label", "x", "--merchant", "https://a.example"]) == 2
        getpass.assert_not_called()

    def test_card_number_label_is_refused_before_any_prompt(self, initialised, monkeypatch):
        getpass = _answers(monkeypatch, [])
        assert _run(["add", "--label", "4242 " * 4, "--single-use"]) == 2
        getpass.assert_not_called()

    def test_uninitialised_vault_is_refused_before_any_prompt(self, tty, monkeypatch, capsys):
        getpass = _answers(monkeypatch, [])
        assert _run(["add", "--label", "x", "--single-use"]) == 2
        getpass.assert_not_called()
        assert "vault init" in capsys.readouterr().err

    def test_bad_number_is_not_echoed(self, initialised, monkeypatch, capsys):
        bad = VISA[:-1] + "1"
        _answers(monkeypatch, [bad, _future(), "123"])
        assert _run(["add", "--label", "x", "--single-use"]) == 2
        out = capsys.readouterr()
        assert bad not in out.out + out.err
        assert vs.list_cards() == []

    def test_list_shows_last_four_and_scope(self, initialised, monkeypatch, capsys):
        _answers(monkeypatch, [VISA, _future(), "123"])
        _run(["add", "--label", "bills", "--single-use"])
        capsys.readouterr()
        assert _run(["list"]) == 0
        line = capsys.readouterr().out
        assert "visa ...4242" in line and "single-use" in line and _future() not in line


class TestRetire:
    @pytest.fixture
    def card(self, tty, monkeypatch):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        assert _run(["init"]) == 0
        _answers(monkeypatch, [VISA, _future(), "123"])
        assert _run(["add", "--label", "x", "--single-use"]) == 0
        return vs.list_cards()[0]["card_ref"]

    def test_retires(self, card, monkeypatch, capsys):
        monkeypatch.setattr(vs, "verify_passphrase", lambda pw, path=None: None)
        _answers(monkeypatch, [PASSPHRASE])
        assert _run(["retire", card]) == 0
        assert vs.list_cards()[0]["status"] == "retired"
        assert "close the virtual card with its issuer" in capsys.readouterr().out

    def test_bad_ref_is_refused_before_the_passphrase_prompt(self, card, monkeypatch, capsys):
        getpass = _answers(monkeypatch, [])
        assert _run(["retire", VISA]) == 2
        getpass.assert_not_called()
        assert VISA not in capsys.readouterr().err


class TestJson:
    def test_list_and_status_json(self, tty, monkeypatch, capsys):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        _run(["init"])
        _answers(monkeypatch, [VISA, _future(), "123"])
        _run(["add", "--label", "x", "--single-use"])
        capsys.readouterr()
        assert _run(["list", "--json"]) == 0
        [card] = json.loads(capsys.readouterr().out)
        assert card["last4"] == "4242" and "blob" not in card
        assert _run(["status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["cards"] == {"active": 1, "retired": 0}


class TestStatus:
    def test_corrupt_db_exits_1(self, tty, monkeypatch, capsys, _home):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        _run(["init"])
        (_home / "payment_vault" / vs.CARDS).write_bytes(b"junk" * 300)
        capsys.readouterr()
        assert _run(["status"]) == 1
        assert "unreadable" in capsys.readouterr().out
        assert _run(["list"]) == 2

    def test_json_keeps_the_unhealthy_exit_code(self, tty, monkeypatch, capsys, _home):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        _run(["init"])
        (_home / "payment_vault" / vs.CARDS).write_bytes(b"junk" * 300)
        capsys.readouterr()
        assert _run(["status", "--json"]) == 1
        assert "unreadable" in json.loads(capsys.readouterr().out)["error"]

    def test_a_file_at_the_vault_path_is_diagnosed_not_raised(
        self, tty, monkeypatch, capsys, _home
    ):
        (_home / "payment_vault").write_text("not a vault")
        getpass = _answers(monkeypatch, [])
        assert _run(["init"]) == 2
        getpass.assert_not_called()  # refused before the passphrase is asked for
        assert _run(["status"]) == 1
        assert "not a directory you own" in capsys.readouterr().out

    def test_a_damaged_recipient_is_refused_before_the_card_is_asked_for(
        self, tty, monkeypatch, capsys, _home
    ):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        _run(["init"])
        (_home / "payment_vault" / vs.RECIPIENT).write_bytes(b"\xff\xfeage1")
        getpass = _answers(monkeypatch, [])
        assert _run(["add", "--label", "x", "--single-use"]) == 2
        getpass.assert_not_called()
        assert "recipient.txt" in capsys.readouterr().err

    def test_flags_a_loose_file(self, tty, monkeypatch, capsys, _home):
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
        _run(["init"])
        (_home / "payment_vault" / vs.CARDS).chmod(0o644)
        capsys.readouterr()
        assert _run(["status"]) == 1
        assert "NOT PRIVATE" in capsys.readouterr().out


class TestServiceCommands:
    """unlock / lock / status against the vault service, with its replies scripted."""

    def _script(self, monkeypatch, replies):
        sent = []

        def fake(request):
            sent.append(request)
            return replies.pop(0)

        monkeypatch.setattr(cli, "_service", fake)
        return sent

    LOCKED = {
        "ok": True,
        "unlocked": False,
        "initialised": True,
        "payments_extra": True,
        "audit": True,
    }

    def test_unlock_refuses_before_the_prompt_when_the_audit_log_is_down(
        self, tty, monkeypatch, capsys
    ):
        self._script(monkeypatch, [{**self.LOCKED, "audit": False}])
        getpass = _answers(monkeypatch, [])
        assert _run(["unlock"]) == 2
        getpass.assert_not_called()
        assert "audit log is unavailable" in capsys.readouterr().err

    def test_unlock(self, tty, monkeypatch, capsys):
        sent = self._script(
            monkeypatch, [dict(self.LOCKED), {"ok": True, "state": "unlocked", "relock_in_s": 1800}]
        )
        getpass = _answers(monkeypatch, [PASSPHRASE])
        assert _run(["unlock"]) == 0
        assert sent == [{"op": "status"}, {"op": "unlock", "passphrase": PASSPHRASE}]
        assert getpass.call_count == 1
        out = capsys.readouterr().out
        assert "relocks after 30 minutes" in out and PASSPHRASE not in out

    def test_unlock_works_without_local_crypto(self, tty, monkeypatch):
        monkeypatch.setattr(cli.importlib.util, "find_spec", lambda m: None)
        self._script(monkeypatch, [dict(self.LOCKED), {"ok": True, "relock_in_s": 1800}])
        _answers(monkeypatch, [PASSPHRASE])
        assert _run(["unlock"]) == 0

    @pytest.mark.parametrize(
        "state, message",
        [
            (None, "not running"),
            ({**LOCKED, "initialised": False}, "not initialised"),
            ({**LOCKED, "payments_extra": False}, "payments extra"),
        ],
    )
    def test_unlock_is_refused_before_the_prompt(self, tty, monkeypatch, capsys, state, message):
        self._script(monkeypatch, [state])
        getpass = _answers(monkeypatch, [])
        assert _run(["unlock"]) == 2
        getpass.assert_not_called()
        assert message in capsys.readouterr().err

    def test_losing_a_race_to_another_unlock_reads_already_unlocked(self, tty, monkeypatch, capsys):
        self._script(monkeypatch, [dict(self.LOCKED), {"ok": True, "state": "already unlocked"}])
        _answers(monkeypatch, [PASSPHRASE])
        assert _run(["unlock"]) == 0
        out = capsys.readouterr().out
        assert "already unlocked" in out and "0 minutes" not in out

    def test_a_failed_lock_never_says_locked(self, monkeypatch, capsys):
        self._script(monkeypatch, [{"ok": False, "error": "bad request"}])
        assert _run(["lock"]) == 2
        captured = capsys.readouterr()
        assert "vault locked" not in captured.out and "bad request" in captured.err

    def test_already_unlocked_skips_the_prompt(self, tty, monkeypatch, capsys):
        self._script(monkeypatch, [{**self.LOCKED, "unlocked": True}])
        getpass = _answers(monkeypatch, [])
        assert _run(["unlock"]) == 0
        getpass.assert_not_called()

    def test_wrong_passphrase(self, tty, monkeypatch, capsys):
        self._script(monkeypatch, [dict(self.LOCKED), {"ok": False, "error": "wrong passphrase"}])
        _answers(monkeypatch, ["nope"])
        assert _run(["unlock"]) == 2
        assert "wrong passphrase" in capsys.readouterr().err

    def test_unlock_refused_inside_claude_code(self, tty, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        sent = self._script(monkeypatch, [])
        assert _run(["unlock"]) == 2
        assert sent == []

    def test_lock_runs_anywhere(self, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDECODE", "1")
        self._script(monkeypatch, [{"ok": True, "state": "locked"}, None])
        assert _run(["lock"]) == 0
        assert "vault locked" in capsys.readouterr().out
        assert _run(["lock"]) == 0
        assert "nothing is unlocked" in capsys.readouterr().out

    def test_status_shows_the_service(self, monkeypatch, capsys):
        self._script(
            monkeypatch,
            [
                {
                    **self.LOCKED,
                    "unlocked": True,
                    "unlocked_at": "2026-10-11T06:00:00+00:00",
                    "relock_in_s": 600,
                    "last_event": {"event": "unlock", "created_at": "2026-10-11T06:00:00+00:00"},
                },
                None,
            ],
        )
        assert _run(["status"]) == 0
        out = capsys.readouterr().out
        assert (
            "service: UNLOCKED" in out
            and "relocks in 10 min" in out
            and "last event: unlock" in out
        )
        assert _run(["status"]) == 0
        assert "service: not running" in capsys.readouterr().out


def test_an_oversized_reply_is_refused(monkeypatch):
    class Flood:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            pass

        def recv(self, n):
            return b"x" * n  # never a newline

    monkeypatch.setattr(cli.socket, "socket", Flood)
    with pytest.raises(vs.VaultError, match="oversized"):
        cli._service({"op": "status"})


def test_service_round_trip_against_the_real_service(_home):
    """cli._service talks to a real VaultService on its own event loop."""
    import asyncio
    import threading

    import aiosqlite

    from genesis.payments.vault_service import VaultService

    mig = importlib.import_module("genesis.db.migrations.20261011060631_payment_vault_events")
    loop = asyncio.new_event_loop()
    ready, holder = threading.Event(), {}

    async def start():
        conn = await aiosqlite.connect(str(_home / "g.db"))
        await mig.up(conn)
        holder["conn"] = conn
        holder["svc"] = VaultService(conn)
        await holder["svc"].start()
        ready.set()

    thread = threading.Thread(target=lambda: (loop.run_until_complete(start()), loop.run_forever()))
    thread.start()
    try:
        assert ready.wait(10)
        reply = cli._service({"op": "status"})
        assert reply["ok"] and reply["unlocked"] is False and reply["initialised"] is False
        assert cli._service({"op": "lock"}) == {"ok": True, "state": "already locked"}
    finally:

        async def stop():
            await holder["svc"].stop()
            await holder["conn"].close()

        asyncio.run_coroutine_threadsafe(stop(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
    assert cli._service({"op": "status"}) is None  # socket gone: "not running"


def test_interactive_needs_both_ends_a_terminal(monkeypatch):
    for stdin, stdout, expected in ((True, True, True), (True, False, False), (False, True, False)):
        monkeypatch.setattr(cli.sys, "stdin", type("S", (), {"isatty": lambda self, v=stdin: v})())
        monkeypatch.setattr(
            cli.sys, "stdout", type("S", (), {"isatty": lambda self, v=stdout: v})()
        )
        assert cli._interactive() is expected


def test_python_m_genesis_dispatches_vault(monkeypatch):
    """Drives the real entry point, so dropping ``vault`` from __main__'s
    dispatch fails here rather than silently printing help."""
    import genesis.__main__ as entry

    calls = []
    monkeypatch.setattr(cli, "_cmd_status", lambda args: calls.append(args.json) or 0)
    monkeypatch.setattr(entry.sys, "argv", ["genesis", "vault", "status", "--json"])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 0
    assert calls == [True]


def test_no_vault_module_imports_crypto_at_load():
    """``python -m genesis serve`` imports this CLI; the crypto stays optional."""
    import ast
    import inspect

    for module in (cli, vs):
        tree = ast.parse(inspect.getsource(module))
        top = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
        names = {getattr(n, "module", None) or n.names[0].name for n in top}
        assert not names & {"pyrage", "zxcvbn"}, module.__name__
