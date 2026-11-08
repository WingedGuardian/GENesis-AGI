"""The shared startup consumes the genuine self-contained rollout reader."""

import json
import os
import py_compile
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def checkout(tmp_path):
    root, home = tmp_path / "checkout", tmp_path / "home"
    reader = root / "scripts/lib/mcp_rollout.py"
    reader.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[2] / "scripts/lib/mcp_rollout.py", reader)
    spec = {"unit": "genesis-mcp-health.service", "url": "http://127.0.0.1:9123/mcp",
            "db_units": 2, "db_background_units": 1, "db_background_jobs": 4,
            "db_checkout_timeout_s": 1, "tool_concurrency": 2, "max_sessions": 8,
            "session_idle_timeout_s": 30, "max_body_bytes": 4096, "shutdown_grace_s": 0}
    config = root / "config/mcp_shared.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"servers": {"health": spec}}))
    return root, home, reader, config, spec


def load(checkout):
    from genesis.mcp.shared_settings import load_shared_settings

    root, home, *_ = checkout
    return load_shared_settings(root, "health", {"GENESIS_HOME": str(home)})


def test_real_reader_overlay_and_frozen_generation(checkout):
    from dataclasses import FrozenInstanceError

    root, home, reader, config, spec = checkout
    overlay = home / "config/mcp_shared.json"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(json.dumps({"servers": {"health": {"max_sessions": 3}}}))
    settings = load(checkout)
    assert settings.http.max_sessions == 3 and settings.http.db_units == 2
    assert settings.db_background_units == 1 and settings.db_background_jobs == 4
    assert settings.db_checkout_timeout_s == 1 and settings.shutdown_grace_s == 0
    assert settings.unit == "genesis-mcp-health.service"
    overlay.write_text(json.dumps({"servers": {"health": {"max_sessions": 5}}}))
    assert settings.http.max_sessions == 3 and load(checkout).http.max_sessions == 5
    with pytest.raises(FrozenInstanceError):
        settings.unit = "other"


@pytest.mark.parametrize("name", ["db_units", "db_background_units", "db_background_jobs",
                                 "db_checkout_timeout_s", "tool_concurrency", "max_sessions",
                                 "session_idle_timeout_s", "max_body_bytes", "shutdown_grace_s"])
def test_every_service_limit_is_required(checkout, name):
    *_, config, spec = checkout
    del spec[name]
    config.write_text(json.dumps({"servers": {"health": spec}}))
    with pytest.raises(ValueError, match="shared MCP configuration"):
        load(checkout)


@pytest.mark.parametrize("name,value", [
    ("db_background_units", 0), ("db_background_units", True), ("db_background_units", 1.5),
    ("db_background_jobs", -1), ("db_background_jobs", False), ("db_background_jobs", "4"),
    ("db_checkout_timeout_s", 0), ("db_checkout_timeout_s", True), ("db_checkout_timeout_s", float("inf")),
    ("shutdown_grace_s", -1), ("shutdown_grace_s", False), ("shutdown_grace_s", float("nan")),
    ("unit", "other.service"), ("unit", "genesis-mcp-../../private.service"), ("unit", ""),
    ("url", "http://fixture-sensitive-string@127.0.0.1:9123/mcp"),
])
def test_bad_settings_refuse_without_echoing_values_or_paths(checkout, name, value):
    root, home, reader, config, spec = checkout
    spec[name] = value
    config.write_text(json.dumps({"servers": {"health": spec}}))
    with pytest.raises(ValueError, match="shared MCP configuration") as error:
        load(checkout)
    assert str(root) not in str(error.value) and "fixture-sensitive-string" not in str(error.value)


@pytest.mark.parametrize("source", [None, "", "not valid Python", "raise ValueError('fixture-sensitive-string')"])
def test_missing_or_broken_reader_refuses_without_fallback(checkout, source):
    root, home, reader, *_ = checkout
    if source is None:
        reader.unlink()
    else:
        reader.write_text(source)
    with pytest.raises(ValueError, match="shared MCP configuration") as error:
        load(checkout)
    assert str(root) not in str(error.value) and "fixture-sensitive-string" not in str(error.value)


def test_reader_executes_complete_current_bytes_not_timestamp_pyc(checkout):
    root, home, reader, config, spec = checkout
    # Include a future import and a class requiring its own module namespace.
    program = (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "import json\n"
        "assert __name__ != '__main__'\n"
        "assert __file__.endswith('/scripts/lib/mcp_rollout.py')\n"
        "@dataclass\n"
        "class Snapshot:\n    value: str\n"
        "CONFIG = json.loads(" + repr(json.dumps({"servers": {"health": spec}})) + ")\n"
        "def load_config(root, env):\n    return CONFIG\n"
        "def valid_unit(value):\n    return value == 'genesis-mcp-health.service'\n"
    )
    reader.write_text(program)
    py_compile.compile(str(reader), doraise=True)
    stamp = reader.stat()
    assert load(checkout).http.url == "http://127.0.0.1:9123/mcp"
    reader.write_text(program.replace(":9123/mcp", ":9124/mcp"))
    os.utime(reader, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert load(checkout).http.url == "http://127.0.0.1:9124/mcp"
