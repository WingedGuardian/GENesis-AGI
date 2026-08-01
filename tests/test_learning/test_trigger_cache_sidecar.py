"""trigger_cache.regenerate writes the JSON sidecar alongside the YAML (LOW-c).

The PreToolUse advisor prefers the JSON sidecar (stdlib load, no yaml import),
so the generator must emit it with identical content and a mtime >= the YAML's.
"""

from __future__ import annotations

import asyncio
import json

from genesis.learning.procedural import trigger_cache


def _fake_row(task_type: str) -> dict:
    return {
        "tool_trigger": json.dumps(["Bash"]),
        "context_tags": json.dumps(["pip install"]),
        "id": f"proc-{task_type}",
        "task_type": task_type,
        "principle": "a principle",
        "steps": json.dumps(["step one"]),
        "confidence": 0.9,
    }


def test_regenerate_writes_yaml_and_json(tmp_path, monkeypatch):
    yaml_p = tmp_path / "procedure_triggers.yaml"
    json_p = tmp_path / "procedure_triggers.json"
    monkeypatch.setattr(trigger_cache, "_CACHE_PATH", yaml_p)
    monkeypatch.setattr(trigger_cache, "_JSON_CACHE_PATH", json_p)

    import genesis.db.crud.procedural as procedural

    async def fake_list_by_tier(db, tier):
        return [_fake_row("core_tt")] if tier == "CORE" else [_fake_row("adv_tt")]

    monkeypatch.setattr(procedural, "list_by_tier", fake_list_by_tier)

    n = asyncio.run(trigger_cache.regenerate(object()))
    assert n == 2  # one CORE + one ADVISORY

    # Both files exist with identical trigger content.
    assert yaml_p.exists() and json_p.exists()
    jdata = json.loads(json_p.read_text())
    task_types = {t["task_type"] for t in jdata["triggers"]}
    assert task_types == {"core_tt", "adv_tt"}

    # Freshness invariant the hook relies on: JSON is at least as new as the YAML.
    assert json_p.stat().st_mtime_ns >= yaml_p.stat().st_mtime_ns


def test_regenerate_json_matches_yaml(tmp_path, monkeypatch):
    yaml_p = tmp_path / "procedure_triggers.yaml"
    json_p = tmp_path / "procedure_triggers.json"
    monkeypatch.setattr(trigger_cache, "_CACHE_PATH", yaml_p)
    monkeypatch.setattr(trigger_cache, "_JSON_CACHE_PATH", json_p)

    import yaml as _yaml

    import genesis.db.crud.procedural as procedural

    async def fake_list_by_tier(db, tier):
        return [_fake_row("core_tt")] if tier == "CORE" else []

    monkeypatch.setattr(procedural, "list_by_tier", fake_list_by_tier)
    asyncio.run(trigger_cache.regenerate(object()))

    ydata = _yaml.safe_load(yaml_p.read_text())
    jdata = json.loads(json_p.read_text())
    assert ydata["triggers"] == jdata["triggers"]
