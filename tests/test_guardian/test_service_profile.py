"""Future service readers must match before operator ingress migration."""

from __future__ import annotations

import json
import subprocess

import pytest

from genesis.guardian import _service_profile as profile
from genesis.guardian import dashboard_ingress as ingress


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    deployment = tmp_path / "guardian"
    deployment.mkdir()
    config = deployment / "guardian.yaml"
    config.write_text("container_name: fixture\nhealth_api_port: 5000\n")
    interpreter = str(deployment / ".venv/bin/python")
    monkeypatch.setattr(profile, "_DEPLOYMENT", deployment)
    monkeypatch.setattr(profile.sys, "executable", interpreter)
    unit = {
        "Id": {"type": "s", "data": "genesis-guardian.service"},
        "LoadState": {"type": "s", "data": "loaded"},
        "Transient": {"type": "b", "data": False},
        "NeedDaemonReload": {"type": "b", "data": False},
        "JoinsNamespaceOf": {"type": "as", "data": []},
    }
    service = {
        "Type": {"type": "s", "data": "oneshot"},
        "ProtectHome": {"type": "s", "data": "no"},
        "WorkingDirectory": {"type": "s", "data": str(deployment)},
        "ExecStartEx": {"type": "a(sasasttttuii)", "data": [[interpreter, [interpreter, "-m", "genesis.guardian"], [], 0, 0, 0, 0, 0, 0, 0]]},
        "Environment": {"type": "as", "data": [f"GUARDIAN_CONFIG={config}", f"PYTHONPATH={deployment / 'src'}"]},
        "UnsetEnvironment": {"type": "as", "data": []},
    }
    empty_by_signature = {
        "s": ("PAMName", "User", "Group", "RootDirectory", "RootImage", "NetworkNamespacePath"),
        "b": ("PrivateNetwork", "PrivateMounts", "PrivateUsers", "DynamicUser", "PrivateTmp"),
        "a(sb)": ("EnvironmentFiles",),
        "a(ssbt)": ("BindPaths", "BindReadOnlyPaths"),
        "a(ss)": ("TemporaryFileSystem",),
        "a(ssba(ss))": ("MountImages",),
        "a(sba(ss))": ("ExtensionImages",),
        "as": ("ExtensionDirectories", "InaccessiblePaths", "ReadOnlyPaths", "ReadWritePaths", "NoExecPaths", "ExecPaths"),
        "a(sasasttttuii)": ("ExecStartPreEx", "ExecConditionEx", "ExecStartPostEx", "ExecStopEx", "ExecStopPostEx"),
    }
    for signature, names in empty_by_signature.items():
        empty = "" if signature == "s" else False if signature == "b" else []
        for name in names:
            service[name] = {"type": signature, "data": empty}
    manager = []

    def read(signature, *args):
        if "GetUnit" in args:
            return ["/org/freedesktop/systemd1/unit/fixture"]
        if "GetAll" in args:
            return [unit if args[-1].endswith(".Unit") else service]
        return manager

    monkeypatch.setattr(profile, "_read", read)
    return config, unit, service, manager


def test_standard_loaded_profile(loaded):
    profile.prove_guardian_profile(loaded[0], "fixture")


@pytest.mark.parametrize("name,value", [
    ("GUARDIAN_HEALTH_HOST", ""), ("GUARDIAN_HEALTH_HOST", "localhost"),
    ("GUARDIAN_HEALTH_PORT", ""), ("GUARDIAN_HEALTH_PORT", "5001"),
    ("GUARDIAN_CONTAINER_NAME", ""), ("GUARDIAN_CONTAINER_NAME", "other"),
    ("GUARDIAN_CONFIG", ""), ("GUARDIAN_CONFIG", "/different.yaml"),
    ("PYTHONPATH", "/other/src"), ("PYTHONHOME", "/other"), ("PYTHONUSERBASE", "/other"),
])
@pytest.mark.parametrize("origin", ["manager", "service"])
def test_service_only_overrides_are_not_shell_proof(loaded, name, value, origin):
    config, _, service, manager = loaded
    (manager if origin == "manager" else service["Environment"]["data"]).append(f"{name}={value}")
    # Shipped service assignments supersede matching manager assignments.
    if origin == "manager" and name in {"GUARDIAN_CONFIG", "PYTHONPATH"}:
        profile.prove_guardian_profile(config, "fixture")
    else:
        with pytest.raises(ValueError):
            profile.prove_guardian_profile(config, "fixture")


@pytest.mark.parametrize("unset", ["GUARDIAN_HEALTH_HOST", "GUARDIAN_HEALTH_HOST=elsewhere"])
def test_final_unset_removes_matching_conflict(loaded, unset):
    config, _, service, manager = loaded
    manager.append("GUARDIAN_HEALTH_HOST=elsewhere")
    service["UnsetEnvironment"]["data"] = [unset]
    profile.prove_guardian_profile(config, "fixture")


def test_nonmatching_exact_unset_keeps_conflict(loaded):
    config, _, service, manager = loaded
    manager.append("GUARDIAN_HEALTH_HOST=elsewhere")
    service["UnsetEnvironment"]["data"] = ["GUARDIAN_HEALTH_HOST=127.0.0.1"]
    with pytest.raises(ValueError):
        profile.prove_guardian_profile(config, "fixture")


@pytest.mark.parametrize("name,value", [
    ("EnvironmentFiles", [["/optional.env", True]]),
    ("PAMName", "custom"), ("PrivateNetwork", True),
    ("PrivateTmp", True), ("ProtectHome", "yes"),
    ("NoExecPaths", ["/"]), ("ExecPaths", ["/limited"]),
    ("NetworkNamespacePath", "/custom"), ("PrivateMounts", True),
    ("BindPaths", [["/other", "/target", False, 0]]),
    ("ExecStartPreEx", [["/wrapper", [], [], 0, 0, 0, 0, 0, 0, 0]]),
])
def test_unproven_profile_refuses(loaded, name, value):
    config, _, service, _ = loaded
    service[name]["data"] = value
    with pytest.raises(ValueError):
        profile.prove_guardian_profile(config, "fixture")


@pytest.mark.parametrize("name,value", [("NeedDaemonReload", True), ("Transient", True), ("LoadState", "error")])
def test_unloaded_or_stale_profile_refuses(loaded, name, value):
    config, unit, _, _ = loaded
    unit[name]["data"] = value
    with pytest.raises(ValueError):
        profile.prove_guardian_profile(config, "fixture")


def test_alternate_selected_yaml_is_supported(loaded):
    config, _, service, _ = loaded
    alternate = config.with_name("alternate.yaml")
    alternate.write_bytes(config.read_bytes())
    service["Environment"]["data"][0] = f"GUARDIAN_CONFIG={alternate}"
    profile.prove_guardian_profile(alternate, "fixture")


@pytest.mark.parametrize("variant", ["missing", "wrong_signature", "wrong_type"])
def test_missing_or_malformed_properties_are_unknown(loaded, variant):
    config, unit, _, _ = loaded
    if variant == "missing":
        del unit["NeedDaemonReload"]
    elif variant == "wrong_signature":
        unit["NeedDaemonReload"]["type"] = "s"
    else:
        unit["NeedDaemonReload"]["data"] = "false"
    with pytest.raises(ValueError):
        profile.prove_guardian_profile(config, "fixture")


@pytest.mark.parametrize("variant", ["wrapper", "extra_command", "flags", "extra_argument"])
def test_nonstandard_command_is_not_certified(loaded, variant):
    config, _, service, _ = loaded
    commands = service["ExecStartEx"]["data"]
    if variant == "wrapper":
        commands[0][0] = "/wrapper"
    elif variant == "extra_command":
        commands.append(commands[0])
    elif variant == "flags":
        commands[0][2] = ["ignore-failure"]
    else:
        commands[0][1].append("--custom")
    with pytest.raises(ValueError):
        profile.prove_guardian_profile(config, "fixture")


@pytest.mark.parametrize("selector", ["GUARDIAN_CONFIG", "PYTHONPATH"])
def test_required_selector_removed_at_final_stage_refuses(loaded, selector):
    config, _, service, _ = loaded
    service["UnsetEnvironment"]["data"] = [selector]
    with pytest.raises(ValueError):
        profile.prove_guardian_profile(config, "fixture")


def test_service_assignment_supersedes_conflicting_manager(loaded):
    config, _, service, manager = loaded
    manager.extend(["GUARDIAN_HEALTH_HOST=elsewhere", "GUARDIAN_HEALTH_PORT=5001"])
    service["Environment"]["data"].extend(["GUARDIAN_HEALTH_HOST=127.0.0.1", "GUARDIAN_HEALTH_PORT=5000"])
    profile.prove_guardian_profile(config, "fixture")


def test_current_directory_cannot_shadow_deployed_module(loaded):
    config, _, _, _ = loaded
    (config.parent / "genesis.py").write_text("# fixture shadow\n")
    with pytest.raises(ValueError, match="shadows"):
        profile.prove_guardian_profile(config, "fixture")


def test_profile_failure_precedes_both_mutations(tmp_path, monkeypatch):
    config = tmp_path / "guardian.yaml"
    config.write_text("container_name: fixture\n")
    original = config.read_bytes()
    monkeypatch.setattr(ingress, "prove_guardian_profile", lambda *args: (_ for _ in ()).throw(ValueError("unproven profile")))
    monkeypatch.setattr(ingress, "_device", lambda *args: pytest.fail("Incus reached before service proof"))
    with pytest.raises(ValueError, match="unproven profile"):
        ingress.migrate(config, apply=True)
    assert config.read_bytes() == original


def test_property_failure_has_fixed_safe_error(monkeypatch):
    monkeypatch.setattr(profile.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(1, ["busctl"])))
    with pytest.raises(ValueError, match="^Guardian service profile could not be read$"):
        profile._read("as", "get-property")


@pytest.mark.parametrize("payload", [
    "not JSON", "null", "[]", '{}', '{"type":"s","data":"wrong"}',
])
def test_transport_rejects_missing_or_malformed_response(monkeypatch, payload):
    monkeypatch.setattr(profile.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(["busctl"], 0, stdout=payload, stderr=""))
    with pytest.raises(ValueError, match="^Guardian service profile could not be read$"):
        profile._read("as", "get-property")


def test_transport_decodes_native_array_schema(monkeypatch):
    response = json.dumps({"type": "as", "data": ["GUARDIAN_HEALTH_HOST=127.0.0.1"]})
    monkeypatch.setattr(profile.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(["busctl"], 0, stdout=response, stderr=""))
    assert profile._read("as", "get-property") == ["GUARDIAN_HEALTH_HOST=127.0.0.1"]


@pytest.mark.parametrize("payload", [None, {}, [], [{} , {}], [None]])
def test_get_all_requires_single_typed_property_mapping(monkeypatch, payload):
    monkeypatch.setattr(profile, "_read", lambda *args: payload)
    with pytest.raises(ValueError, match="properties are unsupported"):
        profile._properties("/fixture", "Service")
