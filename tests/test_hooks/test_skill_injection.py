"""Tests for skill injection hook logic."""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

# Add scripts dirs to path for import
_scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_scripts_dir / "hooks"))
sys.path.insert(0, str(_scripts_dir))


def test_score_skill_name_match():
    from skill_injection_hook import _score_skill

    skill = {"name": "genesis-development", "description": "For building Genesis"}
    assert _score_skill(skill, ["genesis", "development"]) > 0.5


def test_score_skill_no_match():
    from skill_injection_hook import _score_skill

    skill = {"name": "youtube-fetch", "description": "Fetch YouTube videos"}
    assert _score_skill(skill, ["database", "migration"]) == 0.0


def test_score_skill_description_match():
    from skill_injection_hook import _score_skill

    skill = {
        "name": "evaluate",
        "description": "Evaluate technologies against Genesis architecture",
    }
    assert _score_skill(skill, ["evaluate", "technology"]) > 0.3


def test_score_skill_empty_keywords():
    from skill_injection_hook import _score_skill

    skill = {"name": "anything", "description": "whatever"}
    assert _score_skill(skill, []) == 0.0


def test_extract_keywords():
    from skill_injection_hook import _extract_keywords

    keywords = _extract_keywords("Can you help me debug the memory system?")
    assert "memory" in keywords
    assert "debug" in keywords
    assert "the" not in keywords
    assert "can" not in keywords


def test_extract_keywords_limit():
    from skill_injection_hook import _extract_keywords

    long_prompt = " ".join(f"word{i}" for i in range(50))
    keywords = _extract_keywords(long_prompt)
    assert len(keywords) <= 12


def test_catalog_parse_frontmatter():
    """Generate and load a catalog."""
    from generate_skill_catalog import _parse_frontmatter

    info = _parse_frontmatter(
        '---\nname: test-skill\ndescription: "A test"\n---\n# Content'
    )
    assert info["name"] == "test-skill"
    assert info["description"] == "A test"


def test_catalog_parse_frontmatter_no_yaml():
    from generate_skill_catalog import _parse_frontmatter

    info = _parse_frontmatter("# Just a heading\nSome content", fallback_name="fallback")
    assert info["name"] == "fallback"
    assert info["description"] == ""


def test_catalog_parse_folded_scalar():
    """YAML folded scalar (>) descriptions are joined into one line."""
    from generate_skill_catalog import _parse_frontmatter

    content = (
        "---\n"
        "name: my-skill\n"
        "description: >\n"
        "  This skill is used when developing,\n"
        "  debugging, or refactoring Genesis.\n"
        "---\n"
        "# Content\n"
    )
    info = _parse_frontmatter(content)
    assert info["name"] == "my-skill"
    assert "developing" in info["description"]
    assert "refactoring" in info["description"]
    # Should be joined into a single string, not contain ">"
    assert info["description"] != ">"


# --- Raw-point scoring (threshold repair) ---


def test_score_skill_raw_points_keyword_hit():
    """A single explicit-keyword hit scores 2 raw points."""
    from skill_injection_hook import _MIN_SCORE, _score_skill

    skill = {"name": "stealth-browser", "description": "", "keywords": ["selenium"]}
    assert _score_skill(skill, ["selenium"]) == 2.0
    assert _score_skill(skill, ["selenium"]) >= _MIN_SCORE


def test_score_skill_raw_points_desc_hit_below_threshold():
    """A lone description hit (1 point) stays below the firing threshold."""
    from skill_injection_hook import _MIN_SCORE, _score_skill

    skill = {"name": "forecasting", "description": "predict future trends", "keywords": []}
    score = _score_skill(skill, ["trends"])
    assert score == 1.0
    assert score < _MIN_SCORE


def test_score_skill_not_diluted_by_long_prompt():
    """Raw scoring: many unrelated prompt keywords must not dilute one hit.

    The old score/(2*n_keywords) normalization made a single keyword hit in
    a 12-keyword prompt score 2/24 < 0.1 — so keyword-rich prompts near-never
    fired a nudge.
    """
    from skill_injection_hook import _MIN_SCORE, _score_skill

    skill = {"name": "stealth-browser", "description": "", "keywords": ["selenium"]}
    keywords = ["selenium"] + [f"filler{i}" for i in range(11)]
    score = _score_skill(skill, keywords)
    assert score == 2.0
    assert score >= _MIN_SCORE


# --- main() end-to-end behavior (stdin -> nudge output) ---

# 12 significant keywords, exactly one ("selenium") matching the skill below.
_LONG_SELENIUM_PROMPT = (
    "selenium grid nodes keep dropping sessions during long "
    "overnight batch runs tonight"
)

_TIER2_SKILL = {
    "name": "stealth-browser",
    "description": "Browser automation with anti-detection",
    "keywords": ["selenium"],
    "tier": 2,
    "path": "src/genesis/skills/stealth-browser",
}


def _write_catalog(path: Path, tier1: list | None = None, tier2: list | None = None) -> None:
    path.write_text(json.dumps({"tier1": tier1 or [], "tier2": tier2 or []}))


def _run_main(monkeypatch, capsys, catalog_file: Path, prompt: str) -> str:
    """Drive hook main() with a synthetic catalog and prompt; return stdout."""
    import skill_injection_hook as hook

    monkeypatch.setattr(hook, "CATALOG_PATH", catalog_file)
    # Empty session_id -> session-nudge persistence is a no-op (no ~ writes).
    payload = {"prompt": prompt, "session_id": ""}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    hook.main()
    return capsys.readouterr().out


def test_main_single_keyword_hit_fires_on_long_prompt(tmp_path, monkeypatch, capsys):
    """One keyword hit in a 12-keyword prompt emits a [Skill] nudge."""
    catalog_file = tmp_path / "skill_catalog.json"
    _write_catalog(catalog_file, tier2=[_TIER2_SKILL])

    out = _run_main(monkeypatch, capsys, catalog_file, _LONG_SELENIUM_PROMPT)
    assert "[Skill]" in out
    assert "stealth-browser" in out


def test_main_desc_only_hit_does_not_fire(tmp_path, monkeypatch, capsys):
    """A lone description match (1 point) emits no [Skill] nudge."""
    catalog_file = tmp_path / "skill_catalog.json"
    skill = {
        "name": "forecasting",
        "description": "predict future trends",
        "keywords": [],
        "tier": 2,
        "path": "src/genesis/skills/forecasting",
    }
    _write_catalog(catalog_file, tier2=[skill])

    out = _run_main(
        monkeypatch, capsys, catalog_file, "market trends overnight tonight"
    )
    assert "[Skill]" not in out


def test_main_catalog_nudges_not_crowded_out_by_process_nudges(
    tmp_path, monkeypatch, capsys
):
    """Process nudges keep their own budget; catalog nudges still fire.

    Old behavior: 2 process nudges consumed the whole shared budget, so the
    catalog nudge was silently dropped.
    """
    catalog_file = tmp_path / "skill_catalog.json"
    _write_catalog(catalog_file, tier2=[_TIER2_SKILL])

    # "implement"/"build"/"feature"/"endpoint" trigger BOTH process nudges;
    # "selenium" matches the catalog skill.
    prompt = "implement and build the new feature endpoint with selenium"
    out = _run_main(monkeypatch, capsys, catalog_file, prompt)

    assert out.count("[Process]") == 2
    assert "[Skill]" in out
    assert "stealth-browser" in out


def test_main_tier2_nudge_says_read_skill_md(tmp_path, monkeypatch, capsys):
    """Tier-2 nudges give a real invocation instruction: Read <path>/SKILL.md."""
    catalog_file = tmp_path / "skill_catalog.json"
    _write_catalog(catalog_file, tier2=[_TIER2_SKILL])

    out = _run_main(monkeypatch, capsys, catalog_file, _LONG_SELENIUM_PROMPT)
    assert "Read src/genesis/skills/stealth-browser/SKILL.md" in out
    assert "/skill stealth-browser" not in out


def test_main_tier1_nudge_text_unchanged(tmp_path, monkeypatch, capsys):
    """Tier-1 skills keep the 'is relevant here' phrasing (no Read line)."""
    catalog_file = tmp_path / "skill_catalog.json"
    skill = {
        "name": "stealth-browser",
        "description": "Browser automation",
        "keywords": ["selenium"],
        "tier": 1,
        "path": ".claude/skills/stealth-browser",
    }
    _write_catalog(catalog_file, tier1=[skill])

    out = _run_main(monkeypatch, capsys, catalog_file, _LONG_SELENIUM_PROMPT)
    assert "is relevant here" in out
    assert "SKILL.md" not in out


def test_stale_catalog_regen_detached_and_stale_catalog_still_used(
    tmp_path, monkeypatch, capsys
):
    """Stale catalog: regen is spawned detached; the stale copy still serves.

    The 500ms hook timeout must never kill nudge output while a regeneration
    runs, so the spawn is fire-and-forget (Popen + start_new_session) and the
    current prompt reads the stale file.
    """
    import subprocess

    import skill_injection_hook as hook

    catalog_file = tmp_path / "skill_catalog.json"
    _write_catalog(catalog_file, tier2=[_TIER2_SKILL])
    stale = time.time() - (hook._CATALOG_MAX_AGE_S + 600)
    os.utime(catalog_file, (stale, stale))

    popen_calls: list[dict] = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    out = _run_main(monkeypatch, capsys, catalog_file, _LONG_SELENIUM_PROMPT)

    # Nudge came from the stale catalog — no blocking on regeneration.
    assert "[Skill]" in out
    assert popen_calls, "stale catalog should spawn a detached regeneration"
    kwargs = popen_calls[0]
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdout") == subprocess.DEVNULL
    assert kwargs.get("stderr") == subprocess.DEVNULL


def test_fresh_catalog_spawns_no_regen(tmp_path, monkeypatch, capsys):
    """A fresh catalog must not spawn the generator at all."""
    import subprocess

    popen_calls: list[dict] = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    catalog_file = tmp_path / "skill_catalog.json"
    _write_catalog(catalog_file, tier2=[_TIER2_SKILL])  # mtime = now

    out = _run_main(monkeypatch, capsys, catalog_file, _LONG_SELENIUM_PROMPT)
    assert "[Skill]" in out
    assert not popen_calls
