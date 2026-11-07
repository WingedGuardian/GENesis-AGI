"""Compatibility boundaries for the reviewed Serena 2.1 provider."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "serena_upgrade_shared", ROOT / "scripts/serena_shared.py"
)
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)


def test_reviewed_provider_version_is_accepted(monkeypatch):
    monkeypatch.setattr(shared.subprocess, "check_output", lambda *a, **k: "Serena 2.1.0\n")
    shared.validate_version("/provider/serena")


@pytest.mark.parametrize("version", ["1.7.0", "2.0.0", "2.1.1", "3.0.0", "unknown"])
def test_other_provider_versions_still_require_revalidation(monkeypatch, version):
    monkeypatch.setattr(shared.subprocess, "check_output", lambda *a, **k: f"Serena {version}\n")
    with pytest.raises(ValueError, match="revalidate"):
        shared.validate_version("/provider/serena")


def test_shared_server_explicitly_selects_classic_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(shared, "validate_version", lambda *a: None)
    calls = []
    monkeypatch.setattr(shared.os, "execv", lambda *a: calls.append(a))
    shared.serve(tmp_path, "codex", "/provider/serena")
    argv = calls[0][1]
    assert argv[argv.index("--agent-interface") + 1] == "tools"


@pytest.mark.parametrize("forbidden", ["", "activate_project", "switch_modes", "serena_repl"])
def test_capability_check_executes_sdk2_transport_and_rejects_unsafe_tools(
    tmp_path, monkeypatch, forbidden
):
    package = tmp_path / "mcp"
    (package / "client").mkdir(parents=True)
    (package / "client/__init__.py").write_text("")
    (package / "__init__.py").write_text(
        "import os\nfrom types import SimpleNamespace\n"
        "class ClientSession:\n"
        " def __init__(self,r,w): assert (r,w)==('read','write')\n"
        " async def __aenter__(self): return self\n"
        " async def __aexit__(self,*args): pass\n"
        " async def initialize(self): pass\n"
        " async def list_tools(self):\n"
        "  names=['find_symbol']+[os.environ['FORBIDDEN']]\n"
        "  return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in names])\n"
    )
    (package / "client/streamable_http.py").write_text(
        "from contextlib import asynccontextmanager\n"
        "@asynccontextmanager\n"
        "async def streamable_http_client(url):\n"
        " assert url=='http://127.0.0.1:12345/mcp'\n"
        " yield ('read','write')\n"
    )
    run = subprocess.run
    monkeypatch.setattr(shared, "provider_python", lambda *a: sys.executable)
    monkeypatch.setattr(
        shared.subprocess,
        "run",
        lambda argv, **kwargs: run(
            argv,
            **kwargs,
            env=dict(os.environ, PYTHONPATH=str(tmp_path), FORBIDDEN=forbidden),
            capture_output=True,
            text=True,
        ),
    )
    if forbidden:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            shared.check_capabilities("/provider/serena", 12345)
        assert "Shared project switching or REPL exposed" in caught.value.stderr
    else:
        shared.check_capabilities("/provider/serena", 12345)


@pytest.mark.parametrize(
    "location", ["path", "uv-only", "foreign-older", "foreign-pinned", "foreign-unknown"]
)
@pytest.mark.parametrize(
    "version,exit_code,installs",
    [
        ("1.7.0", 0, True),
        ("2.1.0", 0, False),
        ("2.2.0", 0, False),
        ("2.1.0rc1", 0, False),
        ("unknown", 0, False),
        ("1.7.0", 1, False),
    ],
)
def test_installer_converges_only_known_older_provider(
    tmp_path, version, exit_code, installs, location
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("python3", "flock", "mkdir"):
        (bindir / name).symlink_to(shutil.which(name))
    tools = tmp_path / "tools "
    provider_bin = tools / "serena-agent/bin"
    provider_bin.mkdir(parents=True)
    provider = provider_bin / "serena"
    provider.write_text('#!/bin/sh\nprintf "Serena %s\\n" "$VERSION"\nexit "$VERSION_EXIT"\n')
    provider.chmod(0o755)
    if location == "path":
        (bindir / "serena").symlink_to(provider)
    elif location.startswith("foreign-"):
        selected_version = {
            "foreign-older": "1.7.0",
            "foreign-pinned": "2.1.0",
            "foreign-unknown": "unknown",
        }[location]
        selected = bindir / "serena"
        selected.write_text(f'#!/bin/sh\necho "Serena {selected_version}"\n')
        selected.chmod(0o755)
    (bindir / "uv").write_text(
        '#!/bin/sh\nif [ "$*" = "tool dir" ]; then printf "%s\\n" "$TOOLS"; '
        'else printf "%s\\n" "$*" >> "$CALLS"; fi\n'
    )
    (bindir / "systemctl").write_text(
        "#!/bin/sh\nprintf 'LoadState=not-found\\nActiveState=inactive\\nUnitFileState=\\n'\n"
    )
    (bindir / "uv").chmod(0o755)
    (bindir / "systemctl").chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; _install_serena',
            "bash",
            str(ROOT / "scripts/lib/serena_install.sh"),
        ],
        env=dict(
            os.environ,
            HOME=str(tmp_path),
            GENESIS_HOME=str(tmp_path / "state"),
            PATH=str(bindir),
            TOOLS=str(tools),
            CALLS=str(calls),
            VERSION=version,
            VERSION_EXIT=str(exit_code),
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    if location.startswith("foreign-"):
        assert not calls.exists(), (result.stdout, calls.read_text())
        assert "outside uv installation" in result.stdout
    elif installs:
        assert calls.read_text() == "tool install serena-agent==2.1.0\n"
    else:
        assert not calls.exists(), (result.stdout, calls.read_text())
