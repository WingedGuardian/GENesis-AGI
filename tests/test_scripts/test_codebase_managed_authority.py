"""Whole native source contract, independently of immutable settings validity."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import ROOT, managed

operator = managed


@pytest.fixture
def authority(operator, tmp_path, monkeypatch):
    module = operator.inspect_sources.__globals__["canonical_sources"].__globals__
    home = tmp_path / 'home=$%λ" \\ '
    monkeypatch.setenv("HOME", str(home))
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    roots = (operator.units_dir(), runtime / "systemd/user")
    paths = (Path(str(roots[0]) + ".control"), Path(str(roots[1]) + ".control"),
             *roots, *(tmp_path / f"native-{i}" for i in range(12)))
    for root in roots:
        root.mkdir(parents=True)
    parameters = (str(tmp_path / 'repo=$%λ" \\ '), str(home), str(tmp_path / 'venv=$%λ" \\ '))
    monkeypatch.setitem(module, "manager_unit_paths", lambda: paths)
    parameter_reader = module["retirement_parameters"]
    monkeypatch.setitem(module, "retirement_parameters", lambda unit: parameters)
    monkeypatch.setitem(operator.inspect_sources.__globals__, "manager_absent", lambda unit: True)
    for index, unit in enumerate((operator.BACKEND, operator.SLICE)):
        template = ROOT / "scripts/systemd" / (unit + ".template")
        (roots[0] / unit).write_bytes(module["canonical_bytes"](template, parameters if index == 0 else None))
    return SimpleNamespace(module=module, roots=roots, paths=paths, parameters=parameters,
                           parameter_reader=parameter_reader)


@pytest.mark.parametrize("directive", [
    "Environment=LD_PRELOAD=/other.so", "EnvironmentFile=/other.env",
    "ExecCondition=/bin/false", "ExecStartPre=/bin/false", "ExecReload=/bin/false",
    "ExecStop=/bin/false", "ExecStopPost=/bin/false", "RootDirectory=/other",
    "RootImage=/other", "BindPaths=/other", "ExecSearchPath=/other", "User=other",
    "Group=other", "PAMName=other", "UnsetEnvironment=HOME", "OnFailure=other.service",
    "OnSuccess=other.service", "Requires=other.service", "Wants=other.service",
    "Requisite=other.service", "BindsTo=other.service", "Upholds=other.service",
    "Conflicts=other.service", "PartOf=other.service", "StopPropagatedFrom=other.service",
    "Alias=other.service", "Also=other.service", "RequiredBy=other.target",
    "UpheldBy=other.target", "DefaultInstance=other", "WantedBy=other.target",
    "# equivalent comment", " ",
])
@pytest.mark.parametrize("slot", [0, 1])
def test_any_extra_native_authority_refuses_before_mutation(operator, authority, directive, slot):
    unit = (operator.BACKEND, operator.SLICE)[slot]
    path = authority.roots[0] / unit
    before = path.read_bytes()
    path.write_bytes(before + directive.encode() + b"\n")
    with pytest.raises(ValueError, match="noncanonical"):
        operator.inspect_sources()
    assert path.read_bytes() == before + directive.encode() + b"\n"


SCOPES = ["genesis-cbm-query.service.d", "genesis-cbm-.service.d", "genesis-.service.d",
          "service.d", "genesis-cbm-query-clients.slice.d", "genesis-cbm-query-.slice.d",
          "genesis-cbm-.slice.d", "genesis-.slice.d", "slice.d"]


@pytest.mark.parametrize("position", range(16))
@pytest.mark.parametrize("scope", SCOPES)
def test_every_lookup_position_and_override_scope_refuses(operator, authority, position, scope):
    path = authority.paths[position] / scope
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match="override namespace"):
        operator.inspect_sources()


@pytest.mark.parametrize("position", [0, 1, *range(3, 16)])
def test_even_lower_priority_competing_fragment_refuses(operator, authority, position):
    path = authority.paths[position] / operator.BACKEND
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((authority.roots[0] / operator.BACKEND).read_bytes())
    with pytest.raises(ValueError, match="source"):
        operator.inspect_sources()


@pytest.mark.parametrize("kind", ["mask", "link", "dangling", "directory", "fifo", "large"])
def test_nonregular_or_unbounded_source_is_never_canonical(operator, authority, kind):
    path = authority.roots[0] / operator.BACKEND
    path.unlink()
    if kind == "mask":
        path.symlink_to("/dev/null")
    elif kind in ("link", "dangling"):
        target = path.parent / "foreign"
        if kind == "link":
            target.write_text("preserved")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"x" * 65537)
    with pytest.raises(ValueError):
        operator.inspect_sources()
    assert path.lstat()


def test_native_layout_mismatch_refuses_before_source_mutation(operator, authority):
    before = (authority.roots[0] / operator.BACKEND).read_bytes()
    authority.module["manager_unit_paths"] = lambda: authority.paths[1:]
    with pytest.raises(ValueError, match="namespace"):
        operator.inspect_sources()
    assert (authority.roots[0] / operator.BACKEND).read_bytes() == before


def test_selected_directory_alias_and_duplicate_physical_root_are_supported(operator, authority):
    root = authority.roots[0]
    target = root.parent / "external-units"
    root.rename(target)
    root.symlink_to(target, target_is_directory=True)
    authority.module["manager_unit_paths"] = lambda: (*authority.paths, target)
    sources = operator.inspect_sources()
    assert sources[1][operator.BACKEND][0][0] == target / operator.BACKEND


@pytest.mark.parametrize("unit", ["backend", "slice"])
def test_missing_disk_cannot_claim_loaded_native_absence(operator, authority, monkeypatch, unit):
    selected = operator.BACKEND if unit == "backend" else operator.SLICE
    (authority.roots[0] / selected).unlink()
    monkeypatch.setitem(operator.inspect_sources.__globals__, "manager_absent", lambda name: False)
    monkeypatch.setitem(operator.inspect_sources.__globals__, "implicit_slice_absent", lambda name: False)
    with pytest.raises(ValueError, match="not native absence"):
        operator.inspect_sources()


def test_supported_retirement_does_not_require_settings_pin_sentinel_or_venv(operator, authority, monkeypatch):
    namespace = operator.retire_managed.__globals__
    for name in ("read_settings", "verified_binary", "verify_cache", "sentinel_armed"):
        monkeypatch.setitem(namespace, name, Mock(side_effect=AssertionError("settings-independent")))
    monkeypatch.setenv("VENV_PATH", "/does/not/exist")
    assert operator.inspect_sources()[1][operator.BACKEND][1] == authority.parameters


def test_source_change_blocks_refresh_before_native_mutation(operator, authority, monkeypatch):
    sources = operator.inspect_sources()
    path = authority.roots[0] / operator.BACKEND
    path.write_bytes(path.read_bytes() + b"# changed\n")
    call = Mock()
    monkeypatch.setattr(operator.refresh_sources.__globals__["subprocess"], "run", call)
    with pytest.raises(ValueError):
        operator.refresh_sources(sources)
    call.assert_not_called()


@pytest.mark.parametrize("fault", [None, "Id", "Names", "LoadState", "FragmentPath",
                                  "SourcePath", "DropInPaths", "UnitFileState", "Transient"])
def test_only_empty_nontransient_native_implicit_slice_counts_as_absent(operator, authority, monkeypatch, fault):
    module = authority.module
    values = {"Id": operator.SLICE, "Names": [operator.SLICE], "LoadState": "loaded",
              "FragmentPath": "", "SourcePath": "", "DropInPaths": [],
              "UnitFileState": "", "Transient": False}
    properties = {name: {"type": "as" if isinstance(value, list) else
                        "b" if isinstance(value, bool) else "s", "data": value}
                  for name, value in values.items()}
    if fault:
        properties[fault]["data"] = (True if fault == "Transient" else ["other"]
                                      if isinstance(values[fault], list) else "other")
    monkeypatch.setitem(module, "loaded_properties", lambda *a: properties)
    assert operator.implicit_slice_absent(operator.SLICE) is (fault is None)


@pytest.mark.parametrize("active", [False, True])
def test_absent_slice_still_requires_quiescence_even_with_no_source(operator, authority, monkeypatch, active):
    (authority.roots[0] / operator.SLICE).unlink()
    namespace = operator.inspect_sources.__globals__
    monkeypatch.setitem(namespace, "manager_absent", lambda unit: unit == operator.BACKEND)
    monkeypatch.setitem(namespace, "implicit_slice_absent", lambda unit: True)
    proof = Mock(side_effect=ValueError("nonquiescent") if active else None)
    monkeypatch.setitem(namespace, "require_quiescent", proof)
    if active:
        with pytest.raises(ValueError, match="nonquiescent"):
            operator.inspect_sources()
    else:
        assert operator.inspect_sources()[1][operator.SLICE][0] is None
    proof.assert_called_once_with(operator.SLICE)


@pytest.mark.parametrize("action", ["enable", "disable"])
@pytest.mark.parametrize("runtime", [False, True])
def test_installation_uses_only_typed_manager_not_client_side_environment(operator, monkeypatch, action, runtime):
    module = operator.native_install.__globals__
    monkeypatch.setenv("SYSTEMCTL_INSTALL_CLIENT_SIDE", "1")
    monkeypatch.setenv("SYSTEMD_OFFLINE", "1")
    result = {"type": "ba(sss)", "data": [True, []]} if action == "enable" else {
        "type": "a(sss)", "data": [[]]}
    call = Mock(return_value=result)
    monkeypatch.setitem(module, "bus_call", call)
    operator.native_install(action, operator.BACKEND, runtime=runtime)
    signature, *args = call.call_args.args[3:]
    assert signature == ("asbb" if action == "enable" else "asb")
    assert args[:3] == ["1", operator.BACKEND, str(runtime).lower()]


@pytest.mark.parametrize("role", ["serve", "ready"])
@pytest.mark.parametrize("fault", ["empty", "record", "argv", "flag", "path", "relative", "disagree"])
def test_inert_retirement_parameters_reject_unparseable_roles(operator, authority, monkeypatch, role, fault):
    module = authority.module
    main, home, venv = authority.parameters
    argv = ["/bin/sh", "-c", 'exec "$@"', "--", venv + "/bin/python", "-I",
            main + "/scripts/codebase_managed.py", "--config",
            home + "/.genesis/config/codebase-managed.json"]
    values = {field: {"type": "a(sasasttttuii)", "data":
              [["/bin/sh", argv + [value], ["no-env-expand"], *([0] * 7)]]}
              for field, value in (("ExecStartEx", "serve"), ("ExecStartPostEx", "ready"))}
    selected = values["ExecStartEx" if role == "serve" else "ExecStartPostEx"]["data"]
    if fault == "empty":
        selected.clear()
    elif fault == "record":
        selected[0].pop()
    elif fault == "argv":
        selected[0][1][5] = "-c"
    elif fault == "flag":
        selected[0][2].append("ignore-failure")
    elif fault == "path":
        selected[0][1][4] = venv + "/other"
    elif fault == "relative":
        selected[0][1][6] = "relative/scripts/codebase_managed.py"
    else:
        selected[0][1][4] = venv + "-different/bin/python"
    monkeypatch.setitem(module, "loaded_properties", lambda *args: copy.deepcopy(values))
    # Invoke the actual function, bypassing the fixture's inert-parameter stub.
    with pytest.raises(ValueError):
        authority.parameter_reader(operator.BACKEND)


@pytest.mark.parametrize("value", [
    {"type": "as", "data": [["/native"]]},
    {"type": "v", "data": []},
    {"type": "v", "data": [{"type": "s", "data": "/native"}]},
    {"type": "v", "data": [{"type": "as", "data": "/native"}]},
    {"type": "v", "data": [{"type": "as", "data": []}]},
    *({"type": "v", "data": [{"type": "as", "data": [path]}]}
      for path in (None, "relative", "/native/../other", "/native\nother")),
])
def test_manager_namespace_decoder_refuses_incomplete_or_ambiguous_paths(operator, monkeypatch, value):
    module = operator.native_install.__globals__
    monkeypatch.setitem(module, "bus_call", lambda *args: value)
    with pytest.raises(ValueError):
        module["manager_unit_paths"]()


@pytest.mark.parametrize("action", ["enable", "disable"])
@pytest.mark.parametrize("fault", ["signature", "envelope", "changes", "row", "item", "enabled"])
def test_native_install_reply_cannot_turn_partial_or_untyped_results_into_success(
    operator, monkeypatch, action, fault
):
    data = [True, []] if action == "enable" else [[]]
    value = {"type": "ba(sss)" if action == "enable" else "a(sss)", "data": data}
    if fault == "signature":
        value["type"] = "s"
    elif fault == "envelope":
        data.append([])
    elif fault == "changes":
        data[-1] = "untyped"
    elif fault == "row":
        data[-1] = [["unlink", "/native"]]
    elif fault == "item":
        data[-1] = [["unlink", "/native", None]]
    elif action == "enable":
        data[0] = 1  # bool, not truthiness, is the native installation contract
    else:
        data[0] = True
    monkeypatch.setitem(operator.native_install.__globals__, "bus_call", lambda *args: value)
    with pytest.raises(ValueError):
        operator.native_install(action, operator.BACKEND)


@pytest.mark.parametrize("fault", [None, "Id", "Names", "LoadState", "FragmentPath", "SourcePath", "DropInPaths"])
def test_refreshed_native_identity_must_match_the_selected_source(operator, authority, monkeypatch, fault):
    snapshot = operator.inspect_sources()[1][operator.BACKEND][0]
    values = {"Id": operator.BACKEND, "Names": [operator.BACKEND], "LoadState": "loaded",
              "FragmentPath": str(snapshot[0]), "SourcePath": "", "DropInPaths": []}
    properties = {name: {"type": "as" if isinstance(value, list) else "s", "data": value}
                  for name, value in values.items()}
    if fault:
        properties[fault]["data"] = ["other"] if isinstance(values[fault], list) else "/other"
    monkeypatch.setitem(authority.module, "loaded_properties", lambda *args: properties)
    if fault:
        with pytest.raises(ValueError):
            operator.validate_source_identity(operator.BACKEND, snapshot)
    else:
        operator.validate_source_identity(operator.BACKEND, snapshot)


def test_unsupported_pair_never_mutates_but_preserves_both_independent_proofs(operator, authority, monkeypatch):
    path = authority.roots[0] / operator.BACKEND
    path.write_bytes(path.read_bytes() + b"ExecStopPost=/bin/false\n")
    namespace = operator.retire_managed.__globals__
    native = Mock(side_effect=AssertionError("unsupported authority must not mutate"))
    monkeypatch.setitem(namespace, "native_install", native)
    monkeypatch.setattr(namespace["subprocess"], "run", native)
    proof = Mock(side_effect=lambda unit: (_ for _ in ()).throw(ValueError("active slice"))
                 if unit == operator.SLICE else None)
    monkeypatch.setitem(namespace, "require_quiescent", proof)
    monkeypatch.setitem(namespace, "show", lambda *args: {"LoadState": "loaded", "UnitFileState": "enabled"})
    with pytest.raises(ValueError, match="active slice"):
        operator.retire_managed()
    assert [call.args for call in proof.call_args_list] == [(operator.BACKEND,), (operator.SLICE,)]
    native.assert_not_called()
