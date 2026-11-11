"""Manual-only metadata survives discovery without changing deliberate loading."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.conftest import private_module

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
gen = private_module("manual_catalog", SCRIPTS / "generate_skill_catalog.py")
hook = private_module("manual_hook", SCRIPTS / "hooks/skill_injection_hook.py")


@pytest.mark.parametrize("mode", ["yaml", "no_yaml", "oversized", "malformed"])
@pytest.mark.parametrize("value", ["true", '"true"', "FALSE", ""])
def test_manual_flag_survives_all_parser_paths(monkeypatch, mode, value):
    if mode == "no_yaml":
        monkeypatch.setattr(gen, "yaml", None)
    elif mode == "oversized":
        monkeypatch.setattr(gen, "_MAX_FRONTMATTER_CHARS", 1)
    line = f"disable-model-invocation: {value}\n" if value else ""
    content = "---\nname: inbox-review\n" + line
    if mode == "malformed":
        content += "broken: [\n"
    content += "---\nbody\n"
    assert gen._parse_frontmatter(content).get("disable-model-invocation", False) is (
        value in ("true", '"true"')
    )


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    from genesis.autonomy.executor import resources

    entries = []
    for name in ("inbox-review", "evaluate", "research", "web-research"):
        directory = tmp_path / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"BODY-{name}")
        entries.append(
            {
                "name": name,
                "description": name,
                "path": f"skills/{name}",
                "tier": 1,
                "keywords": ["review"],
            }
        )
    path = tmp_path / "catalog.json"
    monkeypatch.setattr(resources, "_CATALOG_PATH", path)
    monkeypatch.setattr(resources, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(resources, "_skill_catalog_cache", None)
    monkeypatch.setattr(resources, "_skill_name_index", None)
    monkeypatch.setattr(
        resources,
        "_REQUIRED_STEP_SKILL_FILES",
        {name: tmp_path / "skills" / name / "SKILL.md" for name in ("research", "web-research")},
    )
    return path, entries, resources


def save(catalog, manual):
    path, entries, _ = catalog
    for entry in entries:
        entry["disable-model-invocation"] = entry["name"] == manual
    path.write_text(json.dumps({"tier1": entries, "tier2": []}))


def test_manual_name_and_keyword_never_nudge():
    skill = {"name": "inbox-review", "keywords": ["review"], "disable-model-invocation": True}
    assert hook._score_skill(skill, ["inbox", "review"], "inbox review") == 0
    skill["disable-model-invocation"] = False
    assert hook._score_skill(skill, ["inbox", "review"], "inbox review") >= 2


def test_inventory_hides_manual_but_retains_direct_path(catalog):
    save(catalog, "inbox-review")
    _, _, resources = catalog
    inventory = resources._load_skill_catalog()
    assert "inbox-review" not in inventory
    assert "evaluate" in inventory
    assert resources._find_skill_path("inbox-review").name == "inbox-review"


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", ["inbox-review", "research", "web-research"])
async def test_assigned_and_delegated_manual_bodies_are_not_injected(catalog, manual):
    save(catalog, manual)
    _, _, resources = catalog
    assigned = "research" if manual == "web-research" else manual
    assert await resources.load_step_resources(None, {"skills": [assigned]}) is None
    assert "BODY-evaluate" in await resources.load_step_resources(
        None,
        {"skills": ["evaluate"]},
    )


@pytest.mark.asyncio
async def test_unmarked_research_still_loads_both_required_bodies(catalog):
    save(catalog, "inbox-review")
    result = await catalog[2].load_step_resources(None, {"skills": ["research"]})
    assert "BODY-research" in result
    assert "BODY-web-research" in result


def test_usage_keeps_manual_population_without_nudge_eligibility(catalog, tmp_path):
    from genesis.learning.skills import usage

    save(catalog, "inbox-review")
    population = usage.load_population(
        catalog_path=catalog[0],
        user_skills_dir=tmp_path / "absent",
        plugins_manifest=tmp_path / "absent.json",
    )
    assert population.skills["inbox-review"]["nudge_eligible"] is False
    assert population.skills["evaluate"]["nudge_eligible"] is True


def test_scan_roundtrip_preserves_manual_flag(tmp_path):
    directory = tmp_path / "inbox-review"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: inbox-review\ndisable-model-invocation: true\n---\nbody",
    )
    assert gen._scan_tier(tmp_path, 1, None)[0]["disable-model-invocation"] is True


@pytest.mark.asyncio
async def test_unassigned_step_does_not_read_optional_catalog(catalog, monkeypatch):
    def unavailable():
        raise AssertionError("an unassigned step must not read the skill catalog")

    monkeypatch.setattr(catalog[2], "_get_skill_catalog", unavailable)
    assert await catalog[2].load_step_resources(None, {}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", [[], {"tier1": None}, {"tier1": [None]}, {"tier1": {}}, {"tier1": [{"name": None}]}]
)
async def test_optional_catalog_shape_cannot_break_required_delegation(catalog, data):
    catalog[0].write_text(json.dumps(data))
    result = await catalog[2].load_step_resources(None, {"skills": ["research"]})
    assert "BODY-research" in result
    assert "BODY-web-research" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [b"\xff", b"[" * 12000 + b"]" * 12000, b"9" * 5000],
    ids=["invalid-utf8", "deep-json", "oversized-integer"],
)
async def test_optional_catalog_decode_failure_keeps_required_delegation(catalog, payload):
    catalog[0].write_bytes(payload)
    result = await catalog[2].load_step_resources(None, {"skills": ["research"]})
    assert "BODY-research" in result
    assert "BODY-web-research" in result


@pytest.mark.asyncio
async def test_generated_catalog_drives_hook_and_executor(catalog, tmp_path, monkeypatch, capsys):
    path, _, resources = catalog
    for name in ("inbox-review", "evaluate"):
        manual = "disable-model-invocation: true\n" if name == "inbox-review" else ""
        (tmp_path / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\nkeywords: [review, evaluate]\n{manual}---\nBODY-{name}",
        )
    monkeypatch.setattr(gen, "TIER1_DIR", tmp_path / "skills")
    monkeypatch.setattr(gen, "TIER2_DIRS", [])
    monkeypatch.setattr(gen, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gen, "CATALOG_PATH", path)
    gen.main()
    capsys.readouterr()
    monkeypatch.setattr(hook, "CATALOG_PATH", path)
    # Empty local hook key does not persist session nudges or register a session.
    monkeypatch.setattr(
        hook.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "prompt": "evaluate inbox review",
                    "session_id": "",
                }
            )
        ),
    )
    hook.main()
    nudges = capsys.readouterr().out
    assert "[Skill]" in nudges and "evaluate" in nudges
    assert "inbox-review" not in nudges
    body = await resources.load_step_resources(None, {"skills": ["inbox-review", "evaluate"]})
    assert "BODY-evaluate" in body
    assert "BODY-inbox-review" not in body
    assert resources._find_skill_path("inbox-review") == tmp_path / "skills" / "inbox-review"


@pytest.mark.parametrize("mode", ["yaml", "no_yaml", "oversized", "malformed"])
@pytest.mark.parametrize("value,wanted", [("true", True), ("false", False)])
def test_manual_flag_survives_crlf_frontmatter(monkeypatch, mode, value, wanted):
    if mode == "no_yaml":
        monkeypatch.setattr(gen, "yaml", None)
    elif mode == "oversized":
        monkeypatch.setattr(gen, "_MAX_FRONTMATTER_CHARS", 1)
    content = f"---\r\nname: review\r\ndisable-model-invocation: {value}\r\n"
    if mode == "malformed":
        content += "broken: [\r\n"
    content += "---\r\nbody\r\n"
    assert gen._parse_frontmatter(content).get("disable-model-invocation", False) is wanted


_FLAG_SPELLINGS = [
    ("disable-model-invocation: true", True),
    ("disable-model-invocation: TRUE", True),
    ("disable-model-invocation: ' true '", True),
    ('"disable-model-invocation": true', True),
    ("disable-model-invocation : true", True),
    ("disable-model-invocation:   true   # why", True),
    ("disable-model-invocation: !!bool yes", True),
    ("disable-model-invocation: !!bool on", True),
    ("disable-model-invocation: !!str true", True),
    ("disable-model-invocation: |\n  true", True),
    ("disable-model-invocation:\n  true", True),
    ("disable-model-invocation: yes", False),
    ("disable-model-invocation: on", False),
    ("disable-model-invocation: 1", False),
    ("disable-model-invocation: !!int 1", False),
    ("disable-model-invocation: null", False),
    ("disable-model-invocation:", False),
    ("disable-model-invocation: !!bool false", False),
    ("  disable-model-invocation: true", False),
]


@pytest.mark.parametrize("mode", ["yaml", "no_yaml", "oversized"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("bom", ["", "﻿"], ids=["no-bom", "bom"])
@pytest.mark.parametrize("line,wanted", _FLAG_SPELLINGS)
def test_fallback_reads_every_flag_spelling_as_yaml_does(
    monkeypatch, mode, newline, bom, line, wanted
):
    """The fallback parser gives the YAML path's answer for every spelling.

    ``wanted`` is what the YAML path returns for the line on clean text (an
    indented key is a nested one, not the top-level flag). A byte-order mark
    or CRLF endings must not change any answer on any path.
    """
    if mode == "no_yaml":
        monkeypatch.setattr(gen, "yaml", None)
    elif mode == "oversized":
        monkeypatch.setattr(gen, "_MAX_FRONTMATTER_CHARS", 1)
    content = bom + f"---\nname: review\n{line}\n---\nbody\n".replace("\n", newline)
    assert gen._parse_frontmatter(content).get("disable-model-invocation", False) is wanted


@pytest.mark.parametrize("mode", ["yaml", "no_yaml", "oversized", "malformed"])
def test_embedded_separator_does_not_hide_manual_metadata(monkeypatch, mode):
    if mode == "no_yaml":
        monkeypatch.setattr(gen, "yaml", None)
    elif mode == "oversized":
        monkeypatch.setattr(gen, "_MAX_FRONTMATTER_CHARS", 1)
    content = '---\nname: review\ndescription: "A --- B"\ndisable-model-invocation: true\n'
    if mode == "malformed":
        content += "broken: [\n"
    assert gen._parse_frontmatter(content + "---\nbody").get("disable-model-invocation") is True


@pytest.mark.parametrize("mode", ["yaml", "no_yaml", "oversized", "malformed"])
@pytest.mark.parametrize(
    "value,wanted", [("!!bool true", True), ("!!bool false", False), ("!!int 1", False)]
)
def test_tagged_boolean_metadata_is_not_numeric_truthiness(monkeypatch, mode, value, wanted):
    if mode == "no_yaml":
        monkeypatch.setattr(gen, "yaml", None)
    elif mode == "oversized":
        monkeypatch.setattr(gen, "_MAX_FRONTMATTER_CHARS", 1)
    content = f"---\nname: review\ndisable-model-invocation: {value}\n"
    if mode == "malformed":
        content += "broken: [\n"
    assert (
        gen._parse_frontmatter(content + "---\nbody").get("disable-model-invocation", False)
        is wanted
    )


@pytest.mark.asyncio
async def test_catalog_access_failure_keeps_required_delegation(catalog, monkeypatch):
    path, _, resources = catalog
    original_exists, original_read = Path.exists, Path.read_text

    def exists(candidate):
        if candidate == path:
            raise PermissionError("inaccessible catalog parent")
        return original_exists(candidate)

    def read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("inaccessible catalog parent")
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read)
    result = await resources.load_step_resources(None, {"skills": ["research"]})
    assert "BODY-research" in result and "BODY-web-research" in result
