"""Operator ingress changes preserve Guardian configuration and fail honestly."""

from __future__ import annotations

import stat
import subprocess

import pytest
import yaml

from genesis.guardian import dashboard_ingress as ingress


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "guardian.yaml"
    path.write_text(
        '# operator note\ncontainer_name: test-container\ncontainer_ip: "192.0.2.1"\n'
        "health_api_port: 5000\nprobes:\n  ping_count: 3 # preserve this\n"
    )
    path.chmod(0o640)
    state = {
        "listen": "tcp:0.0.0.0:5000",
        "connect": "tcp:127.0.0.1:5000",
        "bind": "",
        "nat": "",
        "proxy_protocol": "",
        "changes": [],
    }

    def device(container, action, key, *value):
        assert container == "test-container"
        if action == "get":
            return state[key]
        assert action == "set"
        # The HTTP override must be durable before changing the network.
        assert yaml.safe_load(path.read_text())["health_api_host"] == "127.0.0.1"
        state[key] = value[0]
        state["changes"].append(value[0])
        return ""

    monkeypatch.setattr(ingress, "_device", device)
    monkeypatch.setattr(ingress, "_ready_loopback", lambda: None)
    monkeypatch.setattr(ingress, "_container_loopback", lambda container: None)
    monkeypatch.setattr(ingress, "prove_guardian_profile", lambda path, container: None)
    monkeypatch.delenv("GUARDIAN_HEALTH_HOST", raising=False)
    monkeypatch.delenv("GUARDIAN_HEALTH_PORT", raising=False)
    monkeypatch.delenv("GUARDIAN_CONTAINER_NAME", raising=False)
    return path, state


def test_dry_run_has_no_mutations(world):
    path, state = world
    original = path.read_bytes()
    assert not ingress.migrate(path)["applied"]
    assert path.read_bytes() == original
    assert state["changes"] == []


@pytest.mark.parametrize(
    "key,value", [("bind", "instance"), ("nat", "true"), ("proxy_protocol", "true")]
)
@pytest.mark.parametrize("apply", [False, True])
def test_custom_proxy_modes_refuse_before_mutation(world, key, value, apply):
    path, state = world
    original = path.read_bytes()
    state[key] = value
    with pytest.raises(ValueError, match="direction or transport"):
        ingress.migrate(path, apply=apply)
    assert path.read_bytes() == original and state["changes"] == []


def test_explicit_default_proxy_modes_allow_migration(world):
    path, state = world
    state.update(bind="host", nat="false", proxy_protocol="false")
    assert ingress.migrate(path, apply=True)["applied"]


def test_unreadable_proxy_mode_refuses_before_mutation(world, monkeypatch):
    path, state = world
    original = path.read_bytes()
    device = ingress._device

    def fail_mode(container, action, key, *values):
        if key == "nat":
            raise subprocess.CalledProcessError(1, ["incus"])
        return device(container, action, key, *values)

    monkeypatch.setattr(ingress, "_device", fail_mode)
    with pytest.raises(subprocess.CalledProcessError):
        ingress.migrate(path, apply=True)
    assert path.read_bytes() == original and state["changes"] == []


def test_changed_proxy_mode_after_apply_reports_failure_without_public_rollback(world, monkeypatch):
    path, state = world
    device = ingress._device

    def change_mode(container, action, key, *values):
        result = device(container, action, key, *values)
        if action == "set":
            state["nat"] = "true"
        return result

    monkeypatch.setattr(ingress, "_device", change_mode)
    with pytest.raises(ValueError, match="was not confirmed"):
        ingress.migrate(path, apply=True)
    assert yaml.safe_load(path.read_text())["health_api_host"] == "127.0.0.1"
    assert state["changes"] == ["tcp:127.0.0.1:5000"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("GUARDIAN_HEALTH_PORT", "5999"),
        ("GUARDIAN_HEALTH_PORT", ""),
        ("GUARDIAN_HEALTH_PORT", "invalid"),
        ("GUARDIAN_HEALTH_HOST", "localhost"),
        ("GUARDIAN_CONTAINER_NAME", "other-container"),
    ],
)
def test_conflicting_overrides_refuse_before_mutation(world, monkeypatch, name, value):
    path, state = world
    original = path.read_bytes()
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        ingress.migrate(path, apply=True)
    assert path.read_bytes() == original
    assert state["changes"] == []


def test_matching_overrides_allow_migration(world, monkeypatch):
    path, state = world
    monkeypatch.setenv("GUARDIAN_HEALTH_HOST", "127.0.0.1")
    monkeypatch.setenv("GUARDIAN_HEALTH_PORT", "5000")
    monkeypatch.setenv("GUARDIAN_CONTAINER_NAME", "test-container")
    assert ingress.migrate(path, apply=True)["applied"]


def test_apply_preserves_comments_values_mode_and_is_idempotent(world):
    path, state = world
    assert ingress.migrate(path, apply=True)["applied"]
    first = path.read_bytes()
    assert b"# operator note" in first
    assert b"ping_count: 3 # preserve this" in first
    assert yaml.safe_load(first)["container_ip"] == "192.0.2.1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert ingress.migrate(path, apply=True)["applied"]
    assert path.read_bytes() == first
    assert state["changes"] == ["tcp:127.0.0.1:5000"]


@pytest.mark.parametrize(
    "key,value", [("listen", "tcp:192.0.2.9:5000"), ("connect", "tcp:192.0.2.1:5000")]
)
def test_unknown_topology_is_refused_before_mutation(world, key, value):
    path, state = world
    original = path.read_bytes()
    state[key] = value
    with pytest.raises(ValueError):
        ingress.migrate(path, apply=True)
    assert path.read_bytes() == original
    assert state["changes"] == []


def test_failed_preflight_does_not_mutate(world, monkeypatch):
    path, state = world
    original = path.read_bytes()
    monkeypatch.setattr(
        ingress, "_ready_loopback", lambda: (_ for _ in ()).throw(ValueError("not ready"))
    )
    with pytest.raises(ValueError, match="not ready"):
        ingress.migrate(path, apply=True)
    assert path.read_bytes() == original
    assert state["changes"] == []


def test_failed_incus_change_retains_loopback_health_without_public_rollback(world, monkeypatch):
    path, state = world
    original_device = ingress._device

    def fail_set(container, action, *arguments):
        if action == "set":
            raise subprocess.CalledProcessError(5, ["incus"], stderr="fixture rejection")
        return original_device(container, action, *arguments)

    monkeypatch.setattr(ingress, "_device", fail_set)
    with pytest.raises(subprocess.CalledProcessError) as error:
        ingress.migrate(path, apply=True)
    assert error.value.returncode == 5
    assert yaml.safe_load(path.read_text())["health_api_host"] == "127.0.0.1"
    assert state["changes"] == []


@pytest.mark.parametrize(
    "content",
    [
        'health_api_host: "" # HTTP note\ncontainer_name: fixture\n',
        "health_api_host:\ncontainer_name: fixture\n",
        "# first\ncontainer_name: fixture\n...\n",
    ],
)
def test_config_patch_preserves_other_fields_and_comments(tmp_path, content):
    path = tmp_path / "guardian.yaml"
    path.write_text(content)
    original = yaml.safe_load(content)
    ingress.configure_loopback_health(path)
    assert yaml.safe_load(path.read_text()) == {**original, "health_api_host": "127.0.0.1"}
    for line in content.splitlines():
        if line.startswith("#") or "# HTTP note" in line:
            assert line.split("#", 1)[1] in path.read_text()


def test_installer_preserves_explicit_operator_target(tmp_path):
    path = tmp_path / "guardian.yaml"
    content = 'health_api_host: "localhost" # operator-selected\n'
    path.write_text(content)
    ingress.configure_loopback_health(path, only_if_unset=True)
    assert path.read_text() == content


@pytest.mark.parametrize(
    "content",
    [
        'health_api_host: ""\nhealth_api_host: "localhost"\n',
        'other: &host "localhost"\nhealth_api_host: *host\n',
        '{health_api_host: "localhost"}\n',
    ],
)
def test_ambiguous_config_is_refused_without_mutation(tmp_path, content):
    path = tmp_path / "guardian.yaml"
    path.write_text(content)
    with pytest.raises(ValueError):
        ingress.configure_loopback_health(path)
    assert path.read_text() == content


def test_symlink_config_is_refused(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text("container_name: fixture\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        ingress.configure_loopback_health(link)
    assert real.read_text() == "container_name: fixture\n"


@pytest.mark.parametrize("value", ["5000.0", "true", "'5000'", "null", "5555"])
def test_nonstandard_port_refuses_without_mutation(world, value):
    path, state = world
    path.write_text(path.read_text().replace("health_api_port: 5000", f"health_api_port: {value}"))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="custom health ports"):
        ingress.migrate(path, apply=True)
    assert path.read_bytes() == original and state["changes"] == []


@pytest.mark.parametrize(
    "content",
    [
        'container_name: test-container\nhealth_api_host: ""\nhealth_api_host: localhost\n',
        "container_name: test-container\nother: &host localhost\nhealth_api_host: *host\n",
        "{container_name: test-container, health_api_host: localhost}\n",
    ],
)
def test_dry_run_rejects_unpatchable_yaml(world, content):
    path, state = world
    path.write_text(content)
    with pytest.raises(ValueError):
        ingress.migrate(path)
    assert path.read_text() == content and state["changes"] == []


def test_dry_run_checks_deployed_override_support(world, monkeypatch):
    from types import SimpleNamespace

    path, state = world
    original = path.read_bytes()
    monkeypatch.setattr(
        ingress, "GuardianConfig", lambda **kwargs: SimpleNamespace(health_url="unsupported")
    )
    with pytest.raises(ValueError, match="does not support"):
        ingress.migrate(path)
    assert path.read_bytes() == original and state["changes"] == []


@pytest.mark.parametrize(
    "content,environment",
    [
        ("health_api_port: 5555\n", {}),
        ("health_api_port: 5000.0\n", {}),
        ("health_api_port: true\n", {}),
        ("health_api_port: 5000\n", {"GUARDIAN_HEALTH_PORT": "5555"}),
        ("health_api_port: 5000\n", {"GUARDIAN_HEALTH_PORT": "invalid"}),
        ("health_api_port: 5000\n", {"GUARDIAN_HEALTH_HOST": "localhost"}),
        ("health_api_port: 5000\n", {"GUARDIAN_HEALTH_HOST": ""}),
    ],
)
def test_installer_retains_custom_target(world, monkeypatch, content, environment):
    path, _ = world
    path.write_text(content)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert ingress.configure_loopback_health(path, only_if_unset=True) is False
    assert path.read_text() == content


def test_installer_aligns_standard_unset_target(world):
    path, _ = world
    assert ingress.configure_loopback_health(path, only_if_unset=True) is True
    assert yaml.safe_load(path.read_text())["health_api_host"] == "127.0.0.1"
    assert ingress.configure_loopback_health(path, only_if_unset=True) is False


@pytest.mark.parametrize("address", ["127.0.0.1:5000", "[::1]:5000", "::1:5000"])
def test_live_listener_preflight_allows_only_loopback(monkeypatch, address):
    def run(command, **kwargs):
        assert command == ["incus", "exec", "fixture", "--", "ss", "-H", "-lnt", "sport = :5000"]
        assert kwargs == {"check": True, "capture_output": True, "text": True, "timeout": 30}
        return subprocess.CompletedProcess(command, 0, stdout=f"LISTEN 0 128 {address} *:*\n")

    monkeypatch.setattr(ingress.subprocess, "run", run)
    ingress._container_loopback("fixture")


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "malformed\n",
        "LISTEN 0 128 0.0.0.0:5000 *:*\n",
        "LISTEN 0 128 [::]:5000 *:*\n",
        "LISTEN 0 128 *:5000 *:*\n",
        "LISTEN 0 128 192.0.2.1:5000 *:*\n",
        "LISTEN 0 128 127.0.0.1:5555 *:*\n",
        "LISTEN 0 128 [[127.0.0.1]]:5000 *:*\n",
        "LISTEN 0 128 127.0.0.1:5000 *:*\nLISTEN 0 128 0.0.0.0:5000 *:*\n",
    ],
)
def test_live_listener_preflight_refuses_unknown_or_broad(monkeypatch, rows):
    monkeypatch.setattr(
        ingress.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=rows),
    )
    with pytest.raises(ValueError):
        ingress._container_loopback("fixture")


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(127, ["incus"], stderr="ss unavailable"),
        subprocess.TimeoutExpired(["incus"], 30),
    ],
)
def test_listener_failure_prevents_config_or_device_mutation(world, monkeypatch, failure):
    path, state = world
    original = path.read_bytes()

    def fail(container):
        raise failure

    monkeypatch.setattr(ingress, "_container_loopback", fail)
    with pytest.raises(type(failure)):
        ingress.migrate(path, apply=True)
    assert path.read_bytes() == original and state["changes"] == []
