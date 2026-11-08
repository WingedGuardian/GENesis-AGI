"""Pure selection, handler cleanup and the dependency-free rotation protocol."""

from __future__ import annotations

import logging
import os
import pwd
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.util import mcp_logging as logs


@pytest.mark.parametrize("home_state", ["absolute", "unset", "empty", "relative"])
@pytest.mark.parametrize("value_kind", ["unset", "empty", "absolute", "relative", "bare", "tilde", "tilde-double", "tilde-triple", "named"])
def test_directory_home_and_override_states(tmp_path, monkeypatch, home_state, value_kind):
    home, absolute, named = tmp_path / "home", tmp_path / "absolute", tmp_path / "named"
    for p in [home / "tmp", home / "logs", absolute, named / "logs"]:
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setattr(pwd, "getpwnam", lambda user: SimpleNamespace(pw_dir=str(named)))
    monkeypatch.delenv("HOME", raising=False)
    if home_state != "unset":
        monkeypatch.setenv("HOME", {"absolute": str(home), "empty": "", "relative": "relative-home"}[home_state])
    value = {"unset": None, "empty": "", "absolute": str(absolute), "relative": "logs",
             "bare": "~", "tilde": "~/logs", "tilde-double": "~//logs",
             "tilde-triple": "~///logs", "named": "~fixture/logs"}[value_kind]
    monkeypatch.delenv("GENESIS_MCP_LOG_DIR", raising=False)
    if value is not None:
        monkeypatch.setenv("GENESIS_MCP_LOG_DIR", value)
    before = dict(os.environ)
    expected = {"unset": home / "tmp", "empty": home / "tmp", "absolute": absolute,
                "relative": home / "logs", "bare": home, "tilde": home / "logs",
                "tilde-double": home / "logs", "tilde-triple": home / "logs", "named": named / "logs"}[value_kind]
    if home_state == "relative" and value_kind not in {"absolute", "named"}:
        expected = None
    assert logs.mcp_log_directory() == expected
    assert dict(os.environ) == before
    assert all(not list(p.glob("*.log")) for p in [home, absolute, named])


@pytest.mark.parametrize("kind", ["passwd", "resolve", "stat"])
def test_selection_failure_is_optional(tmp_path, monkeypatch, kind):
    def fail(*args, **kwargs):
        raise OSError("synthetic path inspection failure")
    monkeypatch.setenv("GENESIS_MCP_LOG_DIR", str(tmp_path))
    if kind == "passwd":
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("GENESIS_MCP_LOG_DIR", raising=False)
        monkeypatch.setattr(pwd, "getpwuid", fail)
    elif kind == "resolve":
        monkeypatch.setattr(Path, "resolve", fail)
    else:
        monkeypatch.setattr(Path, "is_dir", fail)
    assert logs.mcp_log_directory() is None


@pytest.mark.parametrize("stage", ["format", "level", "filter", "attach"])
def test_partial_handler_setup_closes_and_detaches_the_owned_descriptor(tmp_path, monkeypatch, stage):
    monkeypatch.setenv("GENESIS_MCP_LOG_DIR", str(tmp_path))
    opened = []
    original = logging.FileHandler
    target = logging.getLogger("genesis")
    prior = list(target.handlers)
    attach = target.addHandler

    class FailingHandler(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        def setFormatter(self, formatter):
            if stage == "format":
                raise RuntimeError("synthetic formatter failure")
            return super().setFormatter(formatter)

        def setLevel(self, level):
            if stage == "level" and self in opened:
                raise RuntimeError("synthetic level failure")
            return super().setLevel(level)

        def addFilter(self, log_filter):
            if stage == "filter":
                raise RuntimeError("synthetic filter failure")
            return super().addFilter(log_filter)

    def attach_then_fail(handler):
        attach(handler)
        raise RuntimeError("synthetic partial attachment failure")

    monkeypatch.setattr(logging, "FileHandler", FailingHandler)
    if stage == "attach":
        monkeypatch.setattr(target, "addHandler", attach_then_fail)
    try:
        assert logs.configure_mcp_logging(logging.Filter()) is None
        assert len(opened) == 1, "the requested setup stage must actually be exercised"
        assert opened[0]._closed and opened[0].stream is None
        assert target.handlers == prior
    finally:
        for handler in opened:
            target.removeHandler(handler)
            handler.close()


@pytest.mark.parametrize("suffix", ["plain", "with space", "with-λ", "with\n\n", os.fsdecode(b"with-\xff")])
def test_direct_cli_is_pure_and_preserves_filesystem_bytes(tmp_path, suffix):
    destination = tmp_path / suffix
    destination.mkdir()
    poison = tmp_path / "poison"
    (poison / "genesis").mkdir(parents=True)
    (poison / "genesis/__init__.py").write_text('raise RuntimeError("package import forbidden")\n')
    result = subprocess.run(
        [sys.executable, "-I", logs.__file__], cwd=tmp_path,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(poison),
             "GENESIS_MCP_LOG_DIR": str(destination)}, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == os.fsencode(destination) + b"\0"
    assert result.stderr == b""
    assert list(destination.iterdir()) == []
