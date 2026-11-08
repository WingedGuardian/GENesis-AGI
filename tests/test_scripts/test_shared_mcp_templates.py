"""Both real renderers preserve ownership and leave the four services inert."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts import test_codebase_managed_templates as helpers
from tests.test_scripts.test_codebase_managed_templates import render as render

ROOT = Path(__file__).resolve().parents[2]
UNITS = tuple(f"genesis-mcp-{name}.service" for name in ("health", "memory", "outreach", "recon"))


@pytest.fixture
def shared_render(monkeypatch, request):
    # Reuse the existing exact renderer-loop/fake-manager fixture with this
    # inventory. Resolve it AFTER changing the helper inventory.
    monkeypatch.setattr(helpers, "UNITS", UNITS)
    return request.getfixturevalue("render")


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("suffix", ["plain", 'quote"back\\slash', "=$value%&|unicode-λ", "trailing "])
def test_shared_templates_render_literal_paths_without_activation(shared_render, tmp_path, renderer, suffix):
    units, run = shared_render
    home = tmp_path / suffix
    result = run(renderer, home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {p.name for p in units.iterdir()} == set(UNITS)
    escaped = str(home).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    venv = home / ".venv" if renderer == "bootstrap.sh" else tmp_path / 'venv=$%&|λ" '
    escaped_venv = str(venv).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    for name in UNITS:
        body = (units / name).read_text()
        service = name.removeprefix("genesis-mcp-").removesuffix(".service")
        assert "genesis-managed" in body
        assert "Type=notify" in body and "NotifyAccess=main" in body
        assert "WorkingDirectory=/" in body and "LimitCORE=0" in body
        assert "WantedBy=default.target" in body
        assert "Wants=" not in body and "Requires=" not in body and "EnvironmentFile=" not in body
        assert f'Environment="GENESIS_HOME={escaped}/.genesis"' in body
        assert f'Environment="GENESIS_REPO_ROOT={escaped}"' in body
        assert f'ExecStart=:/bin/sh -c \'exec "$@"\' -- "{escaped_venv}/bin/python" "{escaped}/scripts/genesis_mcp_server.py" --shared-service {service} --transport streamable-http' in body
        assert "__" not in body
    # Execute the entire actual service-activation section, not a spelling
    # count. All commands are nondelegating shell functions; only shared files
    # are installed. The fresh-install browser provisioner is explicitly off.
    source = (ROOT / "scripts" / renderer).read_text()
    if renderer == "install.sh":
        start = source.index("# Reload and enable/start services.")
        end = source.index("# --- Memory resilience", start)
    else:
        start = source.index("    # A user manager stops at logout")
        end = source.index('\nelse\n    echo "  Template directory', start)
    log = tmp_path / "activation.log"
    script = (
        'systemctl() { printf "%s\\n" "$*" >> "$TEST_MANAGER_LOG"; }\n'
        'loginctl() { printf "Linger=yes\\n"; }\n'
        'setup_warn() { :; }\n_genesis_installed=0\n'
        + helpers.prelude(tmp_path) + source[start:end]
    )
    env = dict(os.environ, HOME=str(home), GENESIS_ROOT=str(home), REPO_DIR=str(home),
               SCRIPT_DIR=str(ROOT / "scripts"), SYSTEMD_USER_DIR=str(units),
               SYSTEMD_TEMPLATE_DIR=str(tmp_path / "templates"), TEST_MANAGER_LOG=str(log),
               GENESIS_TAKE_TEMPLATES="", GENESIS_UPDATE_TIER="0")
    activated = subprocess.run(["bash", "-euc", script], env=env, capture_output=True, text=True, timeout=10)
    assert activated.returncode == 0, activated.stdout + activated.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert not [c for c in calls if any(name in shlex.split(c) for name in UNITS)], calls


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("kind", ["foreign", "edited", "partial", "mask", "dangling"])
def test_existing_shared_unit_is_kept_without_implicit_take(shared_render, renderer, kind):
    units, run = shared_render
    if kind == "edited":
        assert run(renderer).returncode == 0
    before = {}
    for name in UNITS:
        target = units / name
        if kind == "mask":
            target.symlink_to("/dev/null")
        elif kind == "dangling":
            target.symlink_to(units.parent / "missing")
        elif kind == "edited":
            target.write_text(target.read_text() + "# hand edit\n")
        else:
            target.write_text("[Service]\n" if kind == "partial" else "foreign unit\n")
        before[name] = (target.lstat().st_ino, os.readlink(target) if target.is_symlink() else target.read_text())
    result = run(renderer)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in UNITS:
        target = units / name
        assert target.lstat().st_ino == before[name][0]
        assert (os.readlink(target) if target.is_symlink() else target.read_text()) == before[name][1]


@pytest.mark.parametrize("name", UNITS)
@pytest.mark.parametrize("take", ["*", "named"])
def test_only_explicit_named_bootstrap_take_replaces_a_mask(shared_render, name, take):
    units, run = shared_render
    for unit in UNITS:
        (units / unit).symlink_to("/dev/null")
    result = run("bootstrap.sh", take=name if take == "named" else take)
    assert result.returncode == 0, result.stdout + result.stderr
    for unit in UNITS:
        target = units / unit
        if unit == name and take == "named":
            assert target.is_file() and not target.is_symlink()
            backups = list((units.parent / ".genesis/deploy-backups").glob("*/" + name))
            assert len(backups) == 1 and backups[0].is_symlink()
            assert os.readlink(backups[0]) == "/dev/null"
        else:
            assert target.is_symlink() and os.readlink(target) == "/dev/null"
