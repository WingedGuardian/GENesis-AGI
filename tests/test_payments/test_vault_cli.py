"""Tests for ``python -m genesis vault``.

Only Stripe's public test numbers appear here, built from parts.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib
import json
import os
import sqlite3
import warnings
from contextlib import closing
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pyrage import passphrase as age_passphrase

from genesis.payments import cli
from genesis.payments import vault_migrate as vm
from genesis.payments import vault_store as vs

PASSPHRASE = "quartz lantern ember noodle 41"  # only a legacy (sealed) vault has one
VISA = "4242" * 4


def _future() -> str:
    """An expiry two years out, computed per test (never frozen at import)."""
    return f"12/{(datetime.now(UTC).year + 2) % 100:02d}"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    for name in cli._AGENT_SESSION_MARKERS:
        monkeypatch.delenv(name, raising=False)
    # No genesis-server on a test box: never ask the real systemd about the live one.
    monkeypatch.setattr(cli, "_server_unit_active", lambda: False)
    return tmp_path


def _vault(home):
    return home / "payment_vault"


def _seal(path, passphrase=PASSPHRASE) -> bytes:
    """Turn a vault made by init_vault into one made before the passphrase was
    dropped: the key sealed as identity.age, cards.db recording the sealed bytes.
    Returns identity.key's text as it was."""
    key = (path / vs.IDENTITY).read_bytes()
    sealed = age_passphrase.encrypt(key.strip(), passphrase)
    fd = os.open(path / vs.SEALED_IDENTITY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(sealed)
    (path / vs.IDENTITY).unlink()
    with closing(sqlite3.connect(path / vs.CARDS)) as conn, conn:
        conn.execute(
            "UPDATE vault_meta SET value = ? WHERE key = 'identity_sha256'",
            (hashlib.sha256(sealed).hexdigest(),),
        )
    return key


def _legacy_vault(home, *, card: bool = True) -> bytes:
    """A sealed (pre-migration) vault under ``home``; returns its key text."""
    path = _vault(home)
    vs.init_vault(path)
    if card:
        vs.add_card(
            vs.parse_card(VISA, _future(), "123"),
            label="x",
            merchant=None,
            single_use=True,
            path=path,
        )
    key = _seal(path)
    assert vm.migration_state(path) == "needed"
    return key


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)


def _answers(monkeypatch, secrets, typed=("", "")):
    """Every answer goes through getpass; an echoing input() prompt fails the test."""
    getpass = MagicMock(side_effect=[*secrets, *typed])
    monkeypatch.setattr(cli.getpass, "getpass", getpass)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=AssertionError("echoed")))
    return getpass


def _run(argv):
    parser = argparse.ArgumentParser()
    cli.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["vault", *argv])
    return args.func(args)


class TestOwnerTerminal:
    @pytest.mark.parametrize(
        "argv",
        [["init"], ["add", "--label", "x", "--single-use"], ["retire", "card_x"], ["migrate"]],
    )
    @pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID"])
    def test_refused_inside_a_coding_agent_session(self, argv, marker, tty, monkeypatch, capsys):
        monkeypatch.setenv(marker, "1")
        getpass = _answers(monkeypatch, [])
        assert _run(argv) == 2
        getpass.assert_not_called()
        assert "coding agent's session" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [["init"], ["add", "--label", "x", "--single-use"], ["retire", "card_x"], ["migrate"]],
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
        for argv in (["init"], ["migrate"]):
            assert _run(argv) == 2
            assert "payments extra" in capsys.readouterr().err
        getpass.assert_not_called()

    def test_list_and_status_run_anywhere(self, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDECODE", "1")
        assert _run(["list"]) == 0 and "no cards" in capsys.readouterr().out
        assert _run(["status"]) == 0 and "not initialised" in capsys.readouterr().out


class TestInit:
    def test_creates_the_vault_asking_nothing(self, tty, monkeypatch, _home):
        getpass = _answers(monkeypatch, [])
        assert _run(["init"]) == 0
        getpass.assert_not_called()
        path = _vault(_home)
        assert vs.is_initialised(path)
        key = path / vs.IDENTITY
        assert oct(os.stat(key).st_mode & 0o777) == "0o600"
        assert vs.key_text_ok(key.read_bytes())
        assert not (path / vs.SEALED_IDENTITY).exists()
        assert vm.migration_state(path) is None

    def test_an_initialised_vault_is_refused(self, tty, monkeypatch, capsys):
        assert _run(["init"]) == 0
        getpass = _answers(monkeypatch, [])
        assert _run(["init"]) == 2
        getpass.assert_not_called()
        assert "already initialised" in capsys.readouterr().err


class TestAdd:
    @pytest.fixture
    def initialised(self, tty, monkeypatch):
        assert _run(["init"]) == 0

    def test_adds_without_printing_the_number(self, initialised, monkeypatch, capsys):
        getpass = _answers(monkeypatch, [VISA, _future(), "123"], typed=["A Person", ""])
        capsys.readouterr()
        assert _run(["add", "--label", "bills", "--merchant", "shop.example.com"]) == 0
        out = capsys.readouterr()
        assert "ending 4242" in out.out and VISA not in out.out + out.err
        assert "scripts/backup.sh" in out.out  # the next scheduled backup may be hours away
        assert getpass.call_count == 5  # number, expiry, code, name and postal code
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
        assert _run(["init"]) == 0
        _answers(monkeypatch, [VISA, _future(), "123"])
        assert _run(["add", "--label", "x", "--single-use"]) == 0
        return vs.list_cards()[0]["card_ref"]

    def test_retires_asking_nothing(self, card, monkeypatch, capsys, _home):
        getpass = _answers(monkeypatch, [])
        assert _run(["retire", card]) == 0
        getpass.assert_not_called()
        assert vs.list_cards(_vault(_home))[0]["status"] == "retired"
        out = capsys.readouterr().out
        assert "close the virtual card with its issuer" in out
        # Until the next backup, a restore would bring the retired card's details back.
        assert "scripts/backup.sh" in out
        # A new backup does not erase the card from the backups already taken.
        assert "earlier backups still hold the card" in out
        assert "kept by vault migrate" not in out  # no pre-migrate copy beside this vault

    def test_retire_names_a_pre_migrate_copy_that_still_holds_the_card(
        self, card, monkeypatch, capsys, _home
    ):
        copy = _home / f"{vm.PRE_MIGRATE_PREFIX}20261001T000000Z"
        copy.mkdir(mode=0o700)
        assert _run(["retire", card]) == 0
        out = capsys.readouterr().out
        assert f"{copy} (kept by vault migrate) still holds every card it had" in out

    def test_bad_ref_is_refused_before_anything_changes(self, card, monkeypatch, capsys, _home):
        getpass = _answers(monkeypatch, [])
        assert _run(["retire", VISA]) == 2
        getpass.assert_not_called()
        assert VISA not in capsys.readouterr().err
        assert vs.list_cards(_vault(_home))[0]["status"] == "active"


class TestJson:
    def test_list_and_status_json(self, tty, monkeypatch, capsys):
        assert _run(["init"]) == 0
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
        assert _run(["init"]) == 0
        (_home / "payment_vault" / vs.CARDS).write_bytes(b"junk" * 300)
        capsys.readouterr()
        assert _run(["status"]) == 1
        assert "unreadable" in capsys.readouterr().out
        assert _run(["list"]) == 2

    def test_json_keeps_the_unhealthy_exit_code(self, tty, monkeypatch, capsys, _home):
        assert _run(["init"]) == 0
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
        assert _run(["init"]) == 0
        (_home / "payment_vault" / vs.RECIPIENT).write_bytes(b"\xff\xfeage1")
        getpass = _answers(monkeypatch, [])
        assert _run(["add", "--label", "x", "--single-use"]) == 2
        getpass.assert_not_called()
        assert "recipient.txt" in capsys.readouterr().err

    def test_flags_a_loose_file(self, tty, monkeypatch, capsys, _home):
        assert _run(["init"]) == 0
        (_home / "payment_vault" / vs.CARDS).chmod(0o644)
        capsys.readouterr()
        assert _run(["status"]) == 1
        assert "NOT PRIVATE" in capsys.readouterr().out


class TestServiceCommands:
    """unlock / lock (retired) and status against the vault service, replies scripted."""

    def _script(self, monkeypatch, replies):
        sent = []

        def fake(request):
            sent.append(request)
            return replies.pop(0)

        monkeypatch.setattr(cli, "_service", fake)
        return sent

    BASE = {
        "ok": True,
        "unlocked": False,
        "loaded_at": None,
        "initialised": True,
        "ready_error": None,
        "payments_extra": True,
        "audit": True,
    }

    def test_unlock_is_retired_and_says_why(self, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDECODE", "1")  # nothing secret is asked: it runs anywhere
        sent = self._script(monkeypatch, [])
        getpass = _answers(monkeypatch, [])
        assert _run(["unlock"]) == 0
        assert sent == []  # nothing is sent to the service
        getpass.assert_not_called()
        out = capsys.readouterr().out
        assert f"vault unlock: {vm.RETIRED}" in out

    def test_lock_is_retired_and_never_says_locked(self, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDECODE", "1")
        sent = self._script(monkeypatch, [])
        assert _run(["lock"]) == 2  # nothing was locked: a script relying on it must notice
        assert sent == []
        captured = capsys.readouterr()
        assert captured.out == ""
        assert f"vault lock: {vm.RETIRED}; nothing was locked" in captured.err

    def test_status_shows_a_loaded_key(self, monkeypatch, capsys):
        self._script(
            monkeypatch,
            [
                {
                    **self.BASE,
                    "unlocked": True,
                    "loaded_at": "2026-10-11T06:00:00+00:00",
                    "last_event": {
                        "event": "key_loaded",
                        "created_at": "2026-10-11T06:00:00+00:00",
                    },
                },
                None,
            ],
        )
        assert _run(["status"]) == 0
        out = capsys.readouterr().out
        assert "service: key loaded since 2026-10-11T06:00:00+00:00" in out
        assert "last event: key_loaded at 2026-10-11T06:00:00+00:00" in out
        assert "UNLOCKED" not in out and "relocks" not in out
        assert _run(["status"]) == 0
        assert "service: not running" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "reply, shown",
        [
            (
                {"ready_error": "not private: identity.key; fix the modes"},
                "service: no key loaded (not private: identity.key; fix the modes)",
            ),
            ({"initialised": False}, "service: no key loaded (not initialised)"),
        ],
    )
    def test_status_says_why_no_key_is_loaded(self, monkeypatch, capsys, reply, shown):
        self._script(monkeypatch, [{**self.BASE, **reply}])
        assert _run(["status"]) == 0
        assert shown in capsys.readouterr().out

    def test_status_flags_an_unavailable_audit_log(self, monkeypatch, capsys):
        self._script(monkeypatch, [{**self.BASE, "audit": False}])
        assert _run(["status"]) == 0
        assert "(audit log unavailable: no key is held)" in capsys.readouterr().out


class TestMigrate:
    """``vault migrate``: a vault made with a passphrase, unsealed once."""

    def test_refused_outside_the_owners_terminal(self, tty, monkeypatch, capsys, _home):
        _legacy_vault(_home)
        monkeypatch.setenv("CLAUDECODE", "1")
        getpass = _answers(monkeypatch, [])
        assert _run(["migrate"]) == 2
        getpass.assert_not_called()
        assert "coding agent's session" in capsys.readouterr().err
        assert vm.migration_state(_vault(_home)) == "needed"  # nothing changed

    def test_migrates_a_sealed_vault_asking_the_passphrase_once(
        self, tty, monkeypatch, capsys, _home
    ):
        path = _vault(_home)
        key = _legacy_vault(_home)
        getpass = _answers(monkeypatch, [PASSPHRASE])
        assert _run(["migrate"]) == 0
        assert getpass.call_count == 1
        assert (path / vs.IDENTITY).read_bytes() == key
        assert oct(os.stat(path / vs.IDENTITY).st_mode & 0o777) == "0o600"
        assert not (path / vs.SEALED_IDENTITY).exists()
        assert vm.migration_state(path) is None
        status = vs.vault_status(path)
        assert status["error"] is None and status["cards"] == {"active": 1, "retired": 0}
        [aside] = vm.pre_migrate_copies(path)
        assert (aside / vs.SEALED_IDENTITY).exists()  # the vault as it was
        out = capsys.readouterr().out
        assert "vault migrated" in out
        assert f"the vault as it was (key still sealed) is kept in {aside}" in out
        assert "scripts/backup.sh" in out
        assert f"{aside} (kept by vault migrate) still holds every card it had" in out
        assert PASSPHRASE not in out

    def test_a_wrong_passphrase_changes_nothing(self, tty, monkeypatch, capsys, _home):
        path = _vault(_home)
        _legacy_vault(_home)
        _answers(monkeypatch, [PASSPHRASE + "x"])
        assert _run(["migrate"]) == 2
        assert "wrong passphrase" in capsys.readouterr().err
        assert vm.migration_state(path) == "needed"
        assert not (path / vs.IDENTITY).exists()
        assert vm.pre_migrate_copies(path) == []

    def test_a_stopped_migration_resumes_without_the_passphrase(
        self, tty, monkeypatch, capsys, _home
    ):
        path = _vault(_home)
        key = _legacy_vault(_home)
        # Killed after identity.key was placed: cards.db still records the sealed key.
        fd = os.open(path / vs.IDENTITY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        assert vm.migration_state(path) == "resume"
        getpass = _answers(monkeypatch, [])
        assert _run(["migrate"]) == 0
        getpass.assert_not_called()
        assert not (path / vs.SEALED_IDENTITY).exists()
        assert vm.migration_state(path) is None
        assert vs.vault_status(path)["error"] is None
        out = capsys.readouterr().out
        assert "vault migrated" in out and "kept in" not in out  # no copy made on resume

    def test_a_sealed_copy_left_beside_the_recorded_key_is_removed(
        self, tty, monkeypatch, capsys, _home
    ):
        # Killed after the row moved: the CLI must reach the cleanup, not say "nothing to do".
        assert _run(["init"]) == 0
        path = _vault(_home)
        (path / vs.SEALED_IDENTITY).write_bytes(b"a sealed copy")
        os.chmod(path / vs.SEALED_IDENTITY, 0o600)
        capsys.readouterr()
        getpass = _answers(monkeypatch, [])
        assert _run(["migrate"]) == 0
        getpass.assert_not_called()
        assert not (path / vs.SEALED_IDENTITY).exists()
        assert "removed the sealed identity.age" in capsys.readouterr().out

    def test_a_migrated_vault_has_nothing_to_do(self, tty, monkeypatch, capsys, _home):
        assert _run(["init"]) == 0
        capsys.readouterr()
        getpass = _answers(monkeypatch, [])
        assert _run(["migrate"]) == 0
        getpass.assert_not_called()
        assert "nothing to do" in capsys.readouterr().out
        assert vm.pre_migrate_copies(_vault(_home)) == []

    def test_a_vault_status_refuses_is_refused_before_the_prompt(
        self, tty, monkeypatch, capsys, _home
    ):
        path = _vault(_home)
        _legacy_vault(_home)
        (path / vs.CARDS).write_bytes(b"junk" * 300)
        getpass = _answers(monkeypatch, [])
        assert _run(["migrate"]) == 2
        getpass.assert_not_called()
        assert "vault migrate:" in capsys.readouterr().err
        assert (path / vs.SEALED_IDENTITY).exists()


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
        assert reply["loaded_at"] is None and "relock_in_s" not in reply
        for op in ("unlock", "lock"):
            assert cli._service({"op": op}) == {"ok": False, "error": vm.RETIRED}
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


class TestPromptsNeverEcho:
    """#3253 round 1: a prompt that cannot hide its answer refuses instead."""

    @pytest.fixture
    def initialised(self, tty, monkeypatch):
        assert _run(["init"]) == 0

    @staticmethod
    def _echo_fallback(monkeypatch):
        """getpass's own fallback: warn, then read with echo on."""
        read = MagicMock(return_value=VISA)

        def fallback(prompt=""):
            warnings.warn(
                "Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2
            )
            return read(prompt)

        monkeypatch.setattr(cli.getpass, "getpass", fallback)
        return read

    def test_add_refuses_when_the_terminal_cannot_hide_input(
        self, initialised, monkeypatch, capsys
    ):
        read = self._echo_fallback(monkeypatch)
        capsys.readouterr()
        assert _run(["add", "--label", "x", "--single-use"]) == 2
        read.assert_not_called()
        assert "cannot hide" in capsys.readouterr().err
        assert vs.list_cards() == []

    def test_migrates_passphrase_prompt_refuses_when_the_terminal_cannot_hide_input(
        self, tty, monkeypatch, capsys, _home
    ):
        _legacy_vault(_home)
        read = self._echo_fallback(monkeypatch)
        assert _run(["migrate"]) == 2
        read.assert_not_called()
        assert "cannot hide" in capsys.readouterr().err
        assert vm.migration_state(_vault(_home)) == "needed"
        assert vm.pre_migrate_copies(_vault(_home)) == []

    def test_the_real_getpass_fallback_is_refused_before_it_reads(self, monkeypatch):
        """CPython's own getpass, with no terminal to hide input on and a pipe on stdin."""
        import io

        real_open = getpass.os.open

        def no_tty(path, *a, **kw):
            if path == "/dev/tty":
                raise OSError("no controlling terminal")
            return real_open(path, *a, **kw)

        stdin = io.StringIO(VISA + "\n")
        monkeypatch.setattr(getpass.os, "open", no_tty)
        monkeypatch.setattr(cli.sys, "stdin", stdin)
        with pytest.raises(vs.VaultError, match="cannot hide"):
            cli._ask("Card number (hidden): ")
        assert stdin.read() == VISA + "\n"  # nothing was read

    def test_ctrl_c_at_a_prompt_is_a_controlled_refusal(self, initialised, monkeypatch, capsys):
        monkeypatch.setattr(cli.getpass, "getpass", MagicMock(side_effect=KeyboardInterrupt))
        capsys.readouterr()
        assert _run(["add", "--label", "x", "--single-use"]) == 2
        err = capsys.readouterr().err
        assert "cancelled" in err and "Traceback" not in err

    @pytest.mark.parametrize("answered", range(5))
    def test_ended_input_at_any_add_prompt_is_a_controlled_refusal(
        self, initialised, monkeypatch, capsys, answered
    ):
        given = [VISA, _future(), "123", "", ""][:answered]
        monkeypatch.setattr(cli.getpass, "getpass", MagicMock(side_effect=[*given, EOFError]))
        capsys.readouterr()
        assert _run(["add", "--label", "x", "--single-use"]) == 2
        err = capsys.readouterr().err
        assert "input ended" in err and "Traceback" not in err and VISA not in err
        assert vs.list_cards() == []



def test_migrate_refuses_while_the_server_runs_code_from_before_the_change(
    tty, monkeypatch, capsys, _home
):
    # Its status reply has no "loaded_at": its credential mirror would copy the
    # unsealed key's archive to the host.
    monkeypatch.setattr(cli, "_service", lambda request: {"ok": True, "unlocked": False})
    getpass = _answers(monkeypatch, [])
    assert _run(["migrate"]) == 2
    getpass.assert_not_called()
    assert "restart it" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("command", ["init", "migrate"])
@pytest.mark.parametrize(
    ("reply", "unit_active", "allowed"),
    [
        ({"ok": True, "unlocked": False, "loaded_at": None}, True, True),  # current code
        ({"ok": True, "unlocked": False}, True, False),  # pre-change code
        ({"ok": False, "error": "x"}, True, False),  # cannot tell
        (None, False, True),  # no server at all: it will start on this code
        (None, True, False),  # a unit up with no vault socket (a degraded old server)
    ],
)
def test_a_key_is_unsealed_only_while_the_server_runs_current_code(
    tty, monkeypatch, capsys, _home, command, reply, unit_active, allowed
):
    if command == "migrate":
        _vault(_home).mkdir(mode=0o700)
        vs.init_vault(_vault(_home))
        _seal(_vault(_home))
    monkeypatch.setattr(cli, "_service", lambda request: reply)
    monkeypatch.setattr(cli, "_server_unit_active", lambda: unit_active)
    _answers(monkeypatch, [PASSPHRASE] if allowed and command == "migrate" else [])
    code = _run([command])
    if allowed:
        assert code == 0
        assert (_vault(_home) / vs.IDENTITY).exists()
    else:
        assert code == 2 and "restart it" in capsys.readouterr().err.lower()
        assert not (_vault(_home) / vs.IDENTITY).exists()

