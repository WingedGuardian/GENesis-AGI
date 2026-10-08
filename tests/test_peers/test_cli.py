"""Operator CLI demands explicit authority and preserves fail-closed errors."""

import argparse

import pytest

from genesis.peers.cli import add_parser, execute


def parser():
    p = argparse.ArgumentParser()
    add_parser(p.add_subparsers(dest="command"))
    return p


@pytest.mark.parametrize(
    "arguments",
    [
        "register muse",
        "register muse --same-owner",
        "register muse --daily-allowance 2",
        "register muse --same-owner --cross-owner --daily-allowance 2",
    ],
)
def test_registration_requires_ownership_and_allowance(arguments):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(["peers", *arguments.split()])
    assert error.value.code == 2


async def test_cli_configure_register_grant_list_revoke(registry, capsys):
    commands = [
        "configure fallback --service-url https://genesis.example/v1/agent/a2a",
        "register muse --same-owner --daily-allowance 2 --token-name GENESIS_PEER_MUSE_TOKEN",
        "grant muse conversation ask",
        "list",
        "revoke muse",
    ]
    for command in commands:
        await execute(parser().parse_args(["peers", *command.split()]), registry)
    assert (await registry.get("muse"))["active"] == 0
    assert await registry.grants("muse") == {"conversation": "ask"}
    assert "GENESIS_PEER_MUSE_TOKEN" in capsys.readouterr().out
