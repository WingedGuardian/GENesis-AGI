"""`scripts/browser.py fill` refuses a payment card number before launching a browser.

The CLI imports no `genesis` modules, so it loads `card_shapes.py` by file path.
Playwright is stubbed (see `_load_cli`): reaching the stub proves the fill went
ahead, and a refusal must exit before it.
"""

from __future__ import annotations

import argparse

import pytest

from tests.test_scripts.test_browser_cli_screenshot_path import _load_cli

_TEST_CARD = "4242" * 4  # Stripe's public test number, built from parts


def _args(value: str) -> argparse.Namespace:
    return argparse.Namespace(selector="#field", value=value)


@pytest.mark.parametrize(
    "value",
    [_TEST_CARD, f"{_TEST_CARD[:4]}-{_TEST_CARD[4:8]}-{_TEST_CARD[8:12]}-{_TEST_CARD[12:]}"],
    ids=["unbroken", "grouped"],
)
def test_fill_refuses_a_card_number(value, capsys):
    cli = _load_cli()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_fill(_args(value))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "payment card number" in err
    assert _TEST_CARD[:8] not in err


def test_fill_goes_ahead_for_an_ordinary_value():
    cli = _load_cli()
    with pytest.raises(RuntimeError, match="playwright is stubbed"):
        cli.cmd_fill(_args("user@example.com"))


def test_fill_refuses_when_the_check_cannot_load(tmp_path, capsys):
    cli = _load_cli()
    cli._CARD_SHAPES_PATH = tmp_path / "missing.py"
    with pytest.raises(SystemExit) as exc:
        cli.cmd_fill(_args("user@example.com"))
    assert exc.value.code == 2
    assert "could not load" in capsys.readouterr().err
