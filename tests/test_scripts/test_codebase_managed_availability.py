"""Queue probes use immutable setup and persistent native authority read-only."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.test_scripts.test_codebase_managed_config import config, managed

availability = managed


@pytest.mark.parametrize(
    "fault",
    ["none", "missing", "malformed", "schema", "build", "sentinel", "disabled", "other-repo"],
)
def test_readonly_availability_refuses_incomplete_authority(availability, tmp_path, monkeypatch, fault):
    managed = availability
    value = config(tmp_path, managed)
    main = Path(value["main"])
    (main / ".git").mkdir(parents=True)
    namespace = managed.main.__globals__
    monkeypatch.setitem(namespace, "SCRIPT", main / "scripts/codebase_managed.py")
    monkeypatch.setitem(
        namespace,
        "show",
        lambda *args, **kwargs: {"UnitFileState": "disabled" if fault == "disabled" else "enabled"},
    )
    cache = Mock()
    monkeypatch.setitem(namespace, "verify_cache", cache)
    # Native RPC/cgroup semantics have dedicated runtime/native tests. Preserve
    # real require_enabled here while controlling only endpoint readiness.
    monkeypatch.setitem(namespace, "ready", managed.require_enabled)
    path = tmp_path / "settings.json"
    if fault == "schema":
        value["version"] = 1
    elif fault == "build":
        value["build"] = "unsupported"
    elif fault == "sentinel":
        Path(value["sentinel"]).touch()
    if fault != "missing":
        path.write_text("{" if fault == "malformed" else json.dumps(value))
    before = path.read_bytes() if path.exists() else None
    repo = tmp_path if fault == "other-repo" else main
    assert managed.main(["--config", str(path), "available", "--repo", str(repo)]) == (
        0 if fault == "none" else 1
    )
    assert (path.read_bytes() if path.exists() else None) == before
    if fault in ("missing", "malformed", "schema", "build", "other-repo"):
        cache.assert_not_called()
