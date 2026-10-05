"""Behavioral regressions for the native-unit rebuild replacing PR2841/issue2887."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cbm_rebuild", ROOT / "scripts/codebase_managed.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import codebase_managed_state as state  # noqa: E402


def settings(base: Path, enabled=True):
    return dict(version=1, enabled=enabled, main=str(base), binary=str(base / "binary"),
                cache=str(base / "cache"), runtime=str(base / "runtime"),
                sentinel=str(base / "disabled"), build=m.BUILD)


@pytest.mark.parametrize("kind", ["file", "symlink", "directory", "missing"])
@pytest.mark.parametrize("spelling", ["plain", "trailing ", '${HOME}%"\\'])
def test_update_preserves_replacement_after_verified_open(tmp_path, monkeypatch, kind, spelling):
    path = tmp_path / spelling
    value = settings(tmp_path)
    path.write_text(json.dumps(value))
    target = tmp_path / "foreign-target"
    target.write_text("foreign")
    real_load = state.json.load

    def inject(stream):
        previous = real_load(stream)
        path.unlink()
        if kind == "file":
            path.write_text("foreign")
        elif kind == "symlink":
            path.symlink_to(target)
        elif kind == "directory":
            path.mkdir()
            (path / "keep").write_text("foreign")
        return previous

    monkeypatch.setattr(state.json, "load", inject)
    value["enabled"] = False
    with pytest.raises((ValueError, OSError)):
        m.write_settings(path, value, replace=True)
    assert target.read_text() == "foreign"
    if kind == "file":
        assert path.read_text() == "foreign"
    elif kind == "symlink":
        assert path.is_symlink()
    elif kind == "directory":
        assert (path / "keep").read_text() == "foreign"
    else:
        assert not path.exists()


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "fifo", "directory", "immutable-change"])
def test_update_refuses_unowned_or_changed_settings_without_mutation(tmp_path, fault):
    path = tmp_path / "settings.json"
    original = tmp_path / "original.json"
    value = settings(tmp_path)
    original.write_text(json.dumps(value))
    if fault == "symlink":
        path.symlink_to(original)
    elif fault == "hardlink":
        os.link(original, path)
    elif fault == "fifo":
        os.mkfifo(path)
    elif fault == "directory":
        path.mkdir()
    else:
        path.write_text(original.read_text())
        value["cache"] = str(tmp_path / "different")
    value["enabled"] = False
    with pytest.raises((ValueError, OSError)):
        m.write_settings(path, value, replace=True)
    assert json.loads(original.read_text())["enabled"] is True
    if fault == "immutable-change":
        assert json.loads(path.read_text())["enabled"] is True


@pytest.mark.parametrize("bad", ["missing", "partial", "old-build", "symlink", "write-error"])
def test_real_disable_cli_stops_backend_independent_of_settings_and_slice(
    tmp_path, monkeypatch, bad
):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    value = settings(tmp_path)
    if bad == "partial":
        path.write_text('{"enabled":')
    elif bad == "symlink":
        path.symlink_to(tmp_path / "lost-mount")
    elif bad != "missing":
        if bad == "old-build":
            value["build"] = "old"
        path.write_text(json.dumps(value))
    calls = []

    def manager(*args):
        calls.append(args)
        if args[0] == "show":
            assert args[1] == m.BACKEND  # the damaged slice is never a dependency
            return "ActiveState=inactive\nControlGroup="
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    if bad == "write-error":
        monkeypatch.setattr(m, "write_settings", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(m.sys, "argv", ["managed", "--config", str(path), "disable"])
    assert m.main() == (1 if bad == "write-error" else 0)
    assert calls[0] == ("disable", "--now", m.BACKEND)
    if bad == "old-build":
        assert json.loads(path.read_text())["enabled"] is False
    if bad == "partial":
        assert path.read_text() == '{"enabled":'  # no destructive repair/delete


def test_interrupted_actual_settings_update_still_has_a_stop_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    value = settings(tmp_path)
    path.write_text(json.dumps(value))
    value["enabled"] = False
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(state.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(OSError("interrupted after truncate")))
        with pytest.raises(OSError):
            m.write_settings(path, value, replace=True)
    with pytest.raises(ValueError):
        m.read_settings(path)
    calls = []
    monkeypatch.setattr(m, "systemctl", lambda *a: calls.append(a) or ("ActiveState=inactive\nControlGroup=" if a[0] == "show" else ""))
    m.disable(path)
    assert calls[0] == ("disable", "--now", m.BACKEND)


@pytest.mark.parametrize("special", ["fifo", "directory", "symlink"])
def test_binary_nonregular_inputs_refuse_without_reading(tmp_path, special):
    binary = tmp_path / "binary"
    if special == "fifo":
        os.mkfifo(binary, 0o700)
    elif special == "directory":
        binary.mkdir()
    else:
        binary.symlink_to(tmp_path / "missing")
    with pytest.raises((OSError, ValueError)):
        m.verified_binary(binary)


@pytest.mark.parametrize("native,rc,expected", [
    ("enabled", 0, 0), ("disabled", 1, 0), ("masked", 1, 0),
    ("static", 0, 0), ("not-found", 1, 1), ("", 1, 2), ("disabled", 9, 2),
])
def test_native_selection_keeps_disabled_install_selected_without_settings(
    tmp_path, native, rc, expected
):
    binary = tmp_path / "bin/systemctl"
    binary.parent.mkdir()
    binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{native}'\nexit {rc}\n")
    binary.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "CODEBASE_MEMORY_MCP_MANAGED_CONFIG"}
    env.update(PATH=f"{binary.parent}:{os.defpath}")
    p = subprocess.run(["bash", "-c", '. "$0"; codebase_managed_selected "$1" "$2"',
                        ROOT / "scripts/lib/codebase_managed_selection.sh",
                        tmp_path / ".genesis/config/codebase-managed.json", tmp_path], env=env, capture_output=True)
    assert p.returncode == expected
    assert not p.stdout


@pytest.mark.parametrize("renderer", ["bootstrap.sh", "install.sh"])
@pytest.mark.parametrize("configured", [False, True])
def test_both_real_template_loops_install_only_opted_in_disabled_units(
    tmp_path, renderer, configured
):
    home = tmp_path / "home"
    home.mkdir()
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("genesis-cbm-query.service.template", "genesis-cbm-query-clients.slice.template"):
        (templates / name).write_text((ROOT / "scripts/systemd" / name).read_text())
    if configured:
        cfg = home / ".genesis/config/codebase-managed.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("disabled configuration")
    source = (ROOT / "scripts" / renderer).read_text()
    start = source.index('    for template in "$SYSTEMD_TEMPLATE_DIR"/*.service.template')
    end = source.index('    done\n', start) + len('    done\n')
    root_path = tmp_path / 'literal${HOME}%quote"slash\\ trailing '
    env = dict(os.environ, HOME=str(home), GENESIS_ROOT=str(root_path), REPO_DIR=str(root_path),
               SCRIPT_DIR=str(ROOT / "scripts"), SYSTEMD_TEMPLATE_DIR=str(templates),
               SYSTEMD_USER_DIR=str(unit_dir), VENV_PATH=str(root_path / ".venv"), CC_BIN_DIR="/bin")
    p = subprocess.run(["bash", "-c", 'set -e; . "$SCRIPT_DIR/lib/codebase_managed_templates.sh";\n' + source[start:end]],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    files = list(unit_dir.iterdir())
    assert len(files) == (2 if configured else 0)
    if configured:
        service = (unit_dir / "genesis-cbm-query.service").read_text()
        assert '${HOME}' in service and '%%' in service and '\\"' in service
        assert '__REPO_' not in service and '__HOME_' not in service
        assert not (unit_dir / "default.target.wants").exists()


def test_linked_worktree_skips_before_any_managed_or_capacity_access(tmp_path):
    repo = tmp_path / "linked"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /absent")
    fake = tmp_path / "bin/systemctl"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 97\n")
    fake.chmod(0o755)
    p = subprocess.run(["bash", ROOT / "scripts/lib/code_intel_index.sh", repo, "both", "full"],
                       env=dict(os.environ, HOME=str(tmp_path), PATH=f"{fake.parent}:{os.defpath}",
                                CODEBASE_MEMORY_MCP_MANAGED_CONFIG="/unavailable/settings"),
                       text=True, capture_output=True, timeout=10)
    assert p.returncode == 0 and "linked git worktree" in p.stdout
    assert not p.stderr


@pytest.mark.parametrize("role", ["serve", "client"])
@pytest.mark.parametrize("fault", ["root-absent", "intermediate-absent", "root-small"])
def test_real_cgroup_root_is_unbounded_but_other_missing_or_small_limits_refuse(
    tmp_path, monkeypatch, role, fault
):
    unit = m.BACKEND if role == "serve" else m.NAME + "-client-" + "a" * 32 + ".service"
    parent = tmp_path / "user.slice"
    if role == "client":
        parent = parent / m.SLICE
    leaf = parent / unit
    leaf.mkdir(parents=True)
    for directory in leaf.parents:
        if directory == tmp_path or tmp_path not in directory.parents:
            break
        (directory / "memory.max").write_text(str(2 * m.GIB))
        (directory / "memory.swap.max").write_text("0")
    (leaf / "memory.max").write_text(str(2 * m.GIB if role == "serve" else m.GIB // 4))
    (leaf / "memory.swap.max").write_text("0")
    if fault == "intermediate-absent":
        (tmp_path / "user.slice/memory.max").unlink()
    elif fault == "root-small":
        (tmp_path / "memory.max").write_text(str(m.GIB))
    monkeypatch.setattr(m, "resolve_cgroup", lambda *a: (leaf, tmp_path, 2))
    if fault == "root-absent":
        m.verify_boundary({}, role, unit)
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            m.verify_boundary({}, role, unit)


def test_disable_unit_file_failure_still_attempts_independent_runtime_stop(monkeypatch):
    calls = []

    def manager(*args):
        calls.append(args)
        if args[0] == "disable":
            raise subprocess.CalledProcessError(1, "unit file readonly")
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    with pytest.raises(subprocess.CalledProcessError):
        m.stop_backend()
    assert calls[:2] == [("disable", "--now", m.BACKEND), ("stop", m.BACKEND)]
    assert all(c[0] == "show" for c in calls[2:])  # read-only absence check only


def test_enable_false_manager_success_requires_native_readiness(tmp_path, monkeypatch):
    import contextlib
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/systemd/user").mkdir(parents=True)
    path = tmp_path / "settings.json"
    value = settings(tmp_path, enabled=False)
    path.write_text(json.dumps(value))
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(m, "ready", lambda *a: (_ for _ in ()).throw(ValueError("not ready")))
    monkeypatch.setattr(m, "verify_client_slice", lambda: None)
    calls = []
    monkeypatch.setattr(m, "systemctl", lambda *a: calls.append(a) or "")
    with pytest.raises(ValueError, match="not ready"):
        m.set_enabled(value, path, True)
    assert json.loads(path.read_text())["enabled"] is False
    assert calls == [("enable", "--now", m.BACKEND), ("disable", "--now", m.BACKEND), ("stop", m.BACKEND)]


@pytest.mark.parametrize("command", ["disable", "remove"])
@pytest.mark.parametrize("fault", ["loop", "relative-env"])
def test_unresolvable_settings_directory_cannot_block_emergency_stop(
    tmp_path, monkeypatch, command, fault
):
    monkeypatch.setenv("HOME", str(tmp_path))
    if fault == "loop":
        parent = tmp_path / "loop"
        parent.symlink_to(parent)
        raw = str(parent / "settings.json")
    else:
        raw = "relative-settings.json"
    monkeypatch.setenv("CODEBASE_MEMORY_MCP_MANAGED_CONFIG", raw)
    monkeypatch.setattr(m.sys, "argv", ["managed", command])
    calls = []
    monkeypatch.setattr(m, "systemctl", lambda *a: calls.append(a) or ("ActiveState=inactive\nControlGroup=" if a[0] == "show" else ""))
    assert m.main() == 0
    assert calls[:2] == [("disable", "--now", m.BACKEND), ("stop", m.BACKEND)]
    if fault == "loop":
        assert parent.is_symlink()


# --- PR 2892 review round 1 -------------------------------------------------


@pytest.mark.parametrize("renderer", ["bootstrap.sh", "install.sh"])
@pytest.mark.parametrize("kind", ["valid-link", "dangling-link", "directory"])
def test_render_never_writes_through_symlinked_or_nonregular_unit_path(tmp_path, renderer, kind):
    home = tmp_path / "home"
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    cfg = home / ".genesis/config/codebase-managed.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("disabled configuration")
    templates = tmp_path / "templates"
    templates.mkdir()
    for name in ("genesis-cbm-query.service.template", "genesis-cbm-query-clients.slice.template"):
        (templates / name).write_text((ROOT / "scripts/systemd" / name).read_text())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    foreign = elsewhere / "foreign.service"
    service = unit_dir / "genesis-cbm-query.service"
    if kind == "valid-link":
        foreign.write_text("foreign unit")
        service.symlink_to(foreign)
    elif kind == "dangling-link":
        service.symlink_to(foreign)
    else:
        service.mkdir()
    source = (ROOT / "scripts" / renderer).read_text()
    start = source.index('    for template in "$SYSTEMD_TEMPLATE_DIR"/*.service.template')
    end = source.index('    done\n', start) + len('    done\n')
    env = dict(os.environ, HOME=str(home), GENESIS_ROOT=str(tmp_path / "repo"), REPO_DIR=str(tmp_path / "repo"),
               SCRIPT_DIR=str(ROOT / "scripts"), SYSTEMD_TEMPLATE_DIR=str(templates),
               SYSTEMD_USER_DIR=str(unit_dir), VENV_PATH=str(tmp_path / "repo/.venv"), CC_BIN_DIR="/bin")
    p = subprocess.run(["bash", "-c", 'set -e; . "$SCRIPT_DIR/lib/codebase_managed_templates.sh";\n' + source[start:end]],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert "preserved" in p.stderr
    if kind == "valid-link":
        assert service.is_symlink() and foreign.read_text() == "foreign unit"
    elif kind == "dangling-link":
        assert service.is_symlink() and not foreign.exists()
    else:
        assert service.is_dir() and not any(service.iterdir())
    # The sibling regular path is still rendered for the configured install.
    assert "MemoryMax=2G" in (unit_dir / "genesis-cbm-query-clients.slice").read_text()


ABSENT = "LoadState=not-found\nActiveState=inactive\nControlGroup="


@pytest.mark.parametrize("state,raises", [
    (ABSENT, False),
    ("LoadState=loaded\nActiveState=inactive\nControlGroup=", True),
    ("LoadState=not-found\nActiveState=active\nControlGroup=/user.slice/x", True),
    (None, True),  # manager unreadable: the original stop failure stands
])
def test_stop_backend_accepts_only_a_manager_confirmed_absent_unit(monkeypatch, state, raises):
    def manager(*args):
        if args[0] == "disable":
            raise subprocess.CalledProcessError(1, "disable")  # systemd 255: unknown unit file
        if args[0] == "stop":
            raise subprocess.CalledProcessError(5, "stop")  # systemd 255: unit not loaded
        if state is None:
            raise subprocess.CalledProcessError(1, "show")
        assert args[:2] == ("show", m.BACKEND)
        return state

    monkeypatch.setattr(m, "systemctl", manager)
    if raises:
        with pytest.raises(subprocess.CalledProcessError) as error:
            m.stop_backend()
        assert error.value.cmd == "disable"
    else:
        m.stop_backend()


def test_disable_cli_succeeds_on_configured_install_before_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings(tmp_path, enabled=False)))

    def manager(*args):
        if args[0] in ("disable", "stop"):
            raise subprocess.CalledProcessError(5, args[0])
        return ABSENT

    monkeypatch.setattr(m, "systemctl", manager)
    monkeypatch.setattr(m.sys, "argv", ["managed", "--config", str(path), "disable"])
    assert m.main() == 0


@pytest.mark.parametrize("slice_state,ok", [
    ("LoadState=loaded\nMemoryMax=2147483648\nMemorySwapMax=0", True),
    ("LoadState=not-found\nMemoryMax=infinity\nMemorySwapMax=infinity", False),
    ("LoadState=loaded\nMemoryMax=infinity\nMemorySwapMax=0", False),
    ("LoadState=loaded\nMemoryMax=2147483648\nMemorySwapMax=infinity", False),
])
def test_enable_verifies_client_slice_before_any_state_change(tmp_path, monkeypatch, slice_state, ok):
    import contextlib
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/systemd/user").mkdir(parents=True)
    path = tmp_path / "settings.json"
    value = settings(tmp_path, enabled=False)
    path.write_text(json.dumps(value))
    monkeypatch.setattr(m, "verify_cache", lambda *a: None)
    monkeypatch.setattr(m, "verified_binary", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(m, "ready", lambda *a: None)
    calls = []

    def manager(*args):
        calls.append(args)
        if args[0] == "show":
            assert args[1] == m.SLICE
            return slice_state
        return ""

    monkeypatch.setattr(m, "systemctl", manager)
    if ok:
        m.set_enabled(value, path, True)
        assert json.loads(path.read_text())["enabled"] is True
        assert ("enable", "--now", m.BACKEND) in calls
    else:
        with pytest.raises(ValueError, match="slice"):
            m.set_enabled(value, path, True)
        assert json.loads(path.read_text())["enabled"] is False
        assert [c for c in calls if c[0] != "show"] == []


def _retention(tmp_path, *, staged=False, reach=0, native="not-found", rc=4):
    home = tmp_path / "home"
    (home / "genesis/scripts/lib").mkdir(parents=True)
    (home / "genesis/scripts/lib/codebase_managed_selection.sh").write_text(SELECTION.read_text())
    if staged:
        (home / ".genesis/codebase-managed/bin").mkdir(parents=True)
    fake = tmp_path / "bin/systemctl"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\n"
        f'[ "$2" = show-environment ] && exit {reach}\n'
        f'[ "$2" = is-enabled ] && {{ printf "%s\\n" "{native}"; exit {rc}; }}\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    source = (ROOT / "scripts/uninstall.sh").read_text()
    body = source[source.index("managed_codebase_retention() {"):source.index("# Stop the bootstrap backend")]
    env = {k: v for k, v in os.environ.items() if k != "CODEBASE_MEMORY_MCP_MANAGED_CONFIG"}
    env.update(HOME=str(home), PATH=f"{fake.parent}:{os.defpath}")
    script = ("set -euo pipefail; MANAGED_STATE_RETAIN=false; info() { :; };\n" + body
              + 'managed_codebase_retention; printf "%s" "$MANAGED_STATE_RETAIN"')
    p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


SELECTION = ROOT / "scripts/lib/codebase_managed_selection.sh"


def test_uninstall_retains_failed_staging_at_documented_state_location(tmp_path):
    # Devin: configure failed before publishing settings; no units exist.
    assert _retention(tmp_path, staged=True) == "true"


@pytest.mark.parametrize("native", ["enabled-runtime", "transient", "generated"])
def test_uninstall_quiesces_runtime_only_managed_units(tmp_path, native):
    # Codex: settings and persistent fragments lost, manager still has the unit.
    assert _retention(tmp_path, native=native, rc=0) == "true"


@pytest.mark.parametrize("reach,native,rc", [
    (0, "not-found", 4),  # reachable manager, nothing managed: unchanged removal
    (1, "", 1),  # unreachable manager: reported, adds no evidence (pre-existing behaviour)
])
def test_uninstall_without_managed_evidence_still_removes(tmp_path, reach, native, rc):
    assert _retention(tmp_path, reach=reach, native=native, rc=rc) == "false"


def test_fresh_install_denominator_mirrors_installer_selection(tmp_path):
    workflow = (ROOT / ".github/workflows/install-test.yml").read_text()
    start = workflow.index("          # THE DENOMINATOR COMES FROM THE REPO")
    end = workflow.index('          echo "  $_tpl_count shipped unit template(s), all rendered"')
    block = "\n".join(line[10:] for line in workflow[start:end].splitlines())
    unit_dir = tmp_path / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    # An unconfigured fresh install renders neither managed unit.
    managed = {"genesis-cbm-query.service", "genesis-cbm-query-clients.slice"}
    for tpl in (ROOT / "scripts/systemd").glob("*.template"):
        name = tpl.name[: -len(".template")]
        if name not in managed:
            (unit_dir / name).write_text("rendered")

    def run():
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + block], cwd=ROOT,
                              env=dict(os.environ, HOME=str(tmp_path)), capture_output=True, text=True)

    p = run()
    assert p.returncode == 0, p.stdout + p.stderr
    # Configured install: the managed service becomes required like any other.
    cfg = tmp_path / ".genesis/config/codebase-managed.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}")
    p = run()
    assert p.returncode == 1 and "never rendered: genesis-cbm-query.service" in p.stdout
    # Any other unrendered template still fails.
    cfg.unlink()
    (unit_dir / "genesis-server.service").unlink()
    p = run()
    assert p.returncode == 1 and "never rendered: genesis-server.service" in p.stdout
