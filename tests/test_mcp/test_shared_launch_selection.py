"""Explicit shared selection must never fall through to legacy bootstrap."""

import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture
def launcher():
    path = Path(os.environ.get("GENESIS_SHARED_SELECTION_SOURCE",
        str(Path(__file__).resolve().parents[2] / "scripts/genesis_mcp_server.py")))
    spec = importlib.util.spec_from_file_location("shared_selection_test_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["health", "memory", "outreach", "recon"])
def test_explicit_shared_selection(launcher, name):
    args = launcher.parse_args(["--shared-service", name, "--transport", "streamable-http"])
    assert args.shared_service == args.server == name
    assert args.host is None and args.port is None and args.auth_token is None


@pytest.mark.parametrize("extra", [[], ["--host", "127.0.0.1"], ["--host", ""],
    ["--port", "8100"], ["--auth-token", ""], ["--external-client"],
    ["--server", "health"], ["--transport", "stdio"]])
def test_shared_incompatible_legacy_flags_refuse(launcher, extra):
    argv = ["--shared-service", "health"]
    if extra:
        argv += ["--transport", "streamable-http", *extra]
    with pytest.raises(SystemExit) as error:
        launcher.parse_args(argv)
    assert error.value.code == 2


def test_legacy_selection_retains_defaults(launcher):
    args = launcher.parse_args(["--server", "health"])
    assert args.host == "127.0.0.1" and args.transport == "stdio"
    assert args.external_client is None and args.shared_service is None


def test_discord_is_not_a_shared_service(launcher):
    with pytest.raises(SystemExit):
        launcher.parse_args(["--shared-service", "discord-bot", "--transport", "streamable-http"])
