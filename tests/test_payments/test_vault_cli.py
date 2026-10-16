"""Tests for ``python -m genesis vault``.

Only Stripe's public test numbers appear here, built from parts.
"""

from __future__ import annotations

import argparse
import getpass
import json
import warnings
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
        _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
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

    @pytest.mark.parametrize("argv", [["init"], ["retire", "card_x"]])
    def test_passphrase_prompts_refuse_when_the_terminal_cannot_hide_input(
        self, tty, monkeypatch, capsys, argv
    ):
        if argv[0] == "retire":
            _answers(monkeypatch, [PASSPHRASE, PASSPHRASE])
            assert _run(["init"]) == 0
            monkeypatch.setattr(vs, "check_active_card", lambda ref, path=None: None)
        read = self._echo_fallback(monkeypatch)
        assert _run(argv) == 2
        read.assert_not_called()
        assert "cannot hide" in capsys.readouterr().err

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
