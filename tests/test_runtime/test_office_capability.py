"""OfficeCLI capability wiring — external-binary presence → capabilities.json.

OfficeCLI is an optional external renderer for the deliverable-builder skill. Its
presence must surface as `office_deliverables: active` in capabilities.json and its
absence as `degraded` (NOT `failed`, NOT missing) — the "provision-or-surface"
contract. The probe must NEVER raise (a raise would record `failed`, per
_run_init_step, misrepresenting a normal optional-absent state as an error).
"""

from __future__ import annotations

from genesis.runtime import GenesisRuntime


def _bare_runtime():
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._bootstrap_manifest = {}
    rt._officecli_path = None
    return rt


def _fake_binary(home, arch="x64"):
    d = home / ".genesis" / "deps" / "officecli"
    d.mkdir(parents=True, exist_ok=True)
    b = d / f"officecli-linux-{arch}"
    b.write_text("#!/bin/sh\necho 1.0.143\n")
    b.chmod(0o755)
    return b


def test_office_in_init_checks_maps_to_path_attr():
    # Guards the silent-always-"ok" trap: absent from _INIT_CHECKS → attr is None →
    # _run_init_step can never record "degraded".
    assert GenesisRuntime._INIT_CHECKS.get("office_deliverables") == "_officecli_path"


def test_office_active_when_binary_present(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _fake_binary(tmp_path, "x64")
    rt = _bare_runtime()
    rt._run_init_step("office_deliverables", rt._init_office_deliverables)
    assert rt._officecli_path is not None
    assert rt._officecli_path.endswith("officecli-linux-x64")
    assert rt._bootstrap_manifest["office_deliverables"] == "ok"


def test_office_degraded_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)  # empty — no pinned binary
    rt = _bare_runtime()
    rt._run_init_step("office_deliverables", rt._init_office_deliverables)
    assert rt._officecli_path is None
    assert rt._bootstrap_manifest["office_deliverables"] == "degraded"


def test_office_ignores_unpinned_path_binary(tmp_path, monkeypatch):
    # Pinned-path ONLY: an `officecli` on PATH must NOT make the capability active
    # (it's unpinned/unverified AND wouldn't match the skill's $OCLI path).
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)  # no pinned binary
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/officecli")
    rt = _bare_runtime()
    rt._init_office_deliverables()
    assert rt._officecli_path is None  # PATH hit ignored


def test_office_probe_never_raises_on_unresolvable_home(monkeypatch):
    # Path.home() raises RuntimeError when HOME can't be resolved — the probe must
    # degrade (None), never propagate (else _run_init_step records "failed").
    def _boom():
        raise RuntimeError("home not resolvable")

    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("pathlib.Path.home", _boom)
    rt = _bare_runtime()
    rt._run_init_step("office_deliverables", rt._init_office_deliverables)  # must not raise
    assert rt._officecli_path is None
    assert rt._bootstrap_manifest["office_deliverables"] == "degraded"


def test_office_arm64_binary_name(tmp_path, monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _fake_binary(tmp_path, "arm64")
    rt = _bare_runtime()
    rt._init_office_deliverables()
    assert rt._officecli_path is not None
    assert rt._officecli_path.endswith("officecli-linux-arm64")


def test_office_description_registered():
    from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

    assert "office_deliverables" in _CAPABILITY_DESCRIPTIONS
    assert _CAPABILITY_DESCRIPTIONS["office_deliverables"].strip()
