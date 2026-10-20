"""Tests for genesis.learning.skills.usage (skill-usage telemetry, issue #3197).

Fixtures are synthetic transcripts shaped like the records Claude Code writes
(shapes read from live transcripts, CC 2.1.218-2.1.301). No real transcript
content is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.learning.skills import usage

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=14)


def ts(hours_ago: float = 1.0) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def rec(sid: str, kind: str = "cli", **fields) -> dict:
    base = {"sessionId": sid, "entrypoint": kind, "timestamp": ts()}
    base.update(fields)
    return base


def skill_load(sid: str, name: str, *, kind: str = "cli", hours_ago: float = 1.0) -> dict:
    return rec(
        sid,
        kind,
        type="assistant",
        timestamp=ts(hours_ago),
        message={
            "content": [
                {
                    "type": "tool_use",
                    "id": f"toolu_{name}",
                    "name": "Skill",
                    "input": {"skill": name},
                },
            ]
        },
    )


def tool_use(sid: str, tool: str, file_path: str, *, hours_ago: float = 1.0) -> dict:
    return rec(
        sid,
        type="assistant",
        timestamp=ts(hours_ago),
        message={
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_x",
                    "name": tool,
                    "input": {"file_path": file_path},
                },
            ]
        },
    )


def base_dir_record(sid: str, path: str, *, tool_driven: bool, hours_ago: float = 1.0) -> dict:
    r = rec(
        sid,
        type="user",
        isMeta=True,
        timestamp=ts(hours_ago),
        message={
            "content": [
                {"type": "text", "text": f"Base directory for this skill: {path}\n\n## Overview"},
            ]
        },
    )
    if tool_driven:
        r["sourceToolUseID"] = "toolu_y"
    return r


def nudge(sid: str, stdout: str, *, hours_ago: float = 2.0) -> dict:
    return rec(
        sid,
        type="attachment",
        timestamp=ts(hours_ago),
        attachment={
            "type": "hook_success",
            "hookName": "UserPromptSubmit",
            "hookEvent": "UserPromptSubmit",
            "content": stdout,
            "stdout": stdout + "\n",
            "exitCode": 0,
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook hooks/skill_injection_hook.py",
        },
    )


def write_transcript(
    projects: Path,
    project: str,
    sid: str,
    records: list[dict],
    *,
    mtime: float | None = None,
    subagent_of: str | None = None,
) -> Path:
    if subagent_of:
        path = projects / project / subagent_of / "subagents" / f"agent-{sid}.jsonl"
    else:
        path = projects / project / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def env(tmp_path):
    """A projects tree plus a skill population: two catalog skills, one user
    skill, one plugin skill."""
    projects = tmp_path / "projects"
    projects.mkdir()
    catalog = tmp_path / "skill_catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "tier1": [
                    {"name": "alpha", "tier": 1, "path": ".claude/skills/alpha"},
                    {"name": "beta", "tier": 1, "path": ".claude/skills/beta"},
                ],
                "tier2": [
                    {
                        "name": "frontend-slides",
                        "tier": 2,
                        "path": str(tmp_path / "library" / "slides"),
                    },
                ],
            }
        )
    )
    user_skills = tmp_path / "user-skills"
    (user_skills / "gamma").mkdir(parents=True)
    (user_skills / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\n")
    plugin_root = tmp_path / "plugins" / "cache" / "mkt" / "superpowers" / "5.1.0"
    (plugin_root / "skills" / "brainstorming").mkdir(parents=True)
    (plugin_root / "skills" / "brainstorming" / "SKILL.md").write_text("x")
    (plugin_root / "skills" / "writing-plans").mkdir(parents=True)
    (plugin_root / "skills" / "writing-plans" / "SKILL.md").write_text("x")
    manifest = tmp_path / "installed_plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "superpowers@mkt": [{"installPath": str(plugin_root)}],
                },
            }
        )
    )
    population = usage.load_population(
        catalog_path=catalog,
        user_skills_dir=user_skills,
        plugins_manifest=manifest,
    )
    return {"projects": projects, "population": population, "tmp": tmp_path}


def report(env, injected=None) -> dict:
    return usage.run_scan(
        projects_root=env["projects"],
        since=SINCE,
        until=NOW,
        population=env["population"],
        injected=injected,
    )


def row(rep: dict, name: str) -> dict:
    matches = [r for r in rep["skills"] if r["name"] == name]
    assert len(matches) == 1, f"{name}: {[r['name'] for r in rep['skills']]}"
    return matches[0]


# --- states -----------------------------------------------------------------


def test_loaded_skill_is_active_and_nudged_never_loaded_is_zombie(env):
    write_transcript(
        env["projects"],
        "p",
        "s1",
        [
            nudge("s1", "[Skill] The 'beta' skill is relevant here. desc"),
            skill_load("s1", "alpha"),
        ],
    )
    rep = report(env)
    assert row(rep, "alpha")["state"] == "active"
    assert row(rep, "alpha")["loads"]["interactive"] == 1
    assert row(rep, "beta")["state"] == "zombie"
    assert row(rep, "beta")["nudges"]["total"] == 1


def test_silent_catalog_skill_is_dead_and_silent_plugin_skill_is_not_nudgeable(env):
    write_transcript(env["projects"], "p", "s1", [skill_load("s1", "alpha")])
    rep = report(env)
    assert row(rep, "beta")["state"] == "dead"
    assert row(rep, "beta")["nudge_eligible"] is True
    # a [Process] nudge target is nudge-eligible though it is a plugin skill
    assert row(rep, "superpowers:brainstorming")["state"] == "dead"
    assert row(rep, "superpowers:writing-plans")["state"] == "not_nudgeable"
    assert row(rep, "gamma")["state"] == "not_nudgeable"
    assert row(rep, "gamma")["source"] == "user"


def test_commands_are_a_source_and_shared_names_count_once(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "deep-review.md").write_text("x")
    (commands / "alpha.md").write_text("x")  # also a catalog skill: counted once
    catalog = tmp_path / "c.json"
    catalog.write_text(json.dumps({"tier1": [{"name": "alpha", "path": ".claude/skills/alpha"}]}))
    pop = usage.load_population(
        catalog_path=catalog,
        user_skills_dir=tmp_path / "none",
        plugins_manifest=tmp_path / "none.json",
        commands_dirs=[commands],
    )
    assert pop.skills["deep-review"] == {"source": "command", "nudge_eligible": False}
    assert pop.skills["alpha"]["source"] == "catalog-tier1"
    assert {s["source"]: s["skills"] for s in pop.sources}["commands"] == 1


def test_skill_seen_but_in_no_root_is_uncatalogued(env):
    write_transcript(env["projects"], "p", "s1", [skill_load("s1", "mystery")])
    r = row(report(env), "mystery")
    assert r["uncatalogued"] is True
    assert r["state"] == "active"


# --- load signals -------------------------------------------------------------


def test_slash_load_counts_and_tool_echo_does_not_double_count(env):
    lib = str(env["tmp"] / "user-skills" / "gamma")
    write_transcript(
        env["projects"],
        "p",
        "s1",
        [
            # user-typed /gamma: no sourceToolUseID
            rec("s1", type="user", message={"content": "<command-name>/gamma</command-name>"}),
            base_dir_record("s1", lib, tool_driven=False),
            # tool-driven load of alpha: the Skill tool_use plus its isMeta echo
            skill_load("s1", "alpha"),
            base_dir_record("s1", "/w/.claude/skills/alpha", tool_driven=True),
        ],
    )
    rep = report(env)
    assert row(rep, "gamma")["slash_loads"]["total"] == 1
    assert row(rep, "gamma")["loads"]["total"] == 0
    assert row(rep, "alpha")["loads"]["total"] == 1
    assert row(rep, "alpha")["slash_loads"]["total"] == 0


def test_invoked_skills_reinjection_is_ignored(env):
    write_transcript(
        env["projects"],
        "p",
        "s1",
        [
            rec(
                "s1",
                type="attachment",
                attachment={
                    "type": "invoked_skills",
                    "skills": [
                        {
                            "name": "alpha",
                            "path": "projectSettings:alpha",
                            "content": "Base directory for this skill: /r/.claude/skills/alpha",
                        }
                    ],
                },
            )
        ],
    )
    assert row(report(env), "alpha")["state"] == "dead"


def test_skill_md_read_under_worktree_counts_but_editing_session_does_not(env):
    wt = "/srv/checkout/.claude/worktrees/x/.claude/skills/alpha/SKILL.md"
    write_transcript(env["projects"], "p", "s1", [tool_use("s1", "Read", wt)])
    write_transcript(
        env["projects"],
        "p",
        "s2",
        [
            tool_use("s2", "Read", "/r/.claude/skills/beta/SKILL.md"),
            tool_use("s2", "Edit", "/r/.claude/skills/beta/SKILL.md"),
        ],
    )
    rep = report(env)
    assert row(rep, "alpha")["reads"]["total"] == 1
    assert row(rep, "alpha")["state"] == "active"
    assert row(rep, "beta")["reads"]["total"] == 0
    assert row(rep, "beta")["state"] == "dead"


def test_path_resolution_uses_catalog_map_and_plugin_roots(env):
    tmp = env["tmp"]
    plugin_md = tmp / "plugins/cache/mkt/superpowers/5.1.0/skills/brainstorming/SKILL.md"
    write_transcript(
        env["projects"],
        "p",
        "s1",
        [
            tool_use("s1", "Read", str(tmp / "library" / "slides" / "SKILL.md")),
            tool_use("s1", "Read", str(plugin_md)),
        ],
    )
    rep = report(env)
    assert row(rep, "frontend-slides")["reads"]["total"] == 1
    assert row(rep, "superpowers:brainstorming")["reads"]["total"] == 1


# --- nudges -------------------------------------------------------------------


def test_every_nudge_form_credits_the_right_skill(env):
    lines = "\n".join(
        [
            "[Process] Non-trivial work detected. Consider ... use superpowers:brainstorming to plan.",
            "[Skill] The 'alpha' skill matches this task. Read .claude/skills/alpha/SKILL.md. d",
            f"[Skill] A skill matches this task. Read {env['tmp'] / 'library' / 'slides'}/SKILL.md. d",
            "[Skill] A matching skill cannot be named in one line (400-char path); see the skill catalog.",
        ]
    )
    write_transcript(env["projects"], "p", "s1", [nudge("s1", lines)])
    rep = report(env)
    assert row(rep, "superpowers:brainstorming")["nudges"]["total"] == 1
    assert row(rep, "alpha")["nudges"]["total"] == 1
    assert row(rep, "frontend-slides")["nudges"]["total"] == 1
    assert rep["nudges_unparsed"] == 1


def test_real_hook_output_parses(env, tmp_path):
    """Drive the REAL skill_injection_hook and feed its stdout to the parser,
    so a wording change in the hook fails here instead of silently dropping
    nudges."""
    home = tmp_path / "home"
    cat = home / ".genesis" / "skill_catalog.json"
    cat.parent.mkdir(parents=True)
    cat.write_text(
        json.dumps(
            {
                "tier1": [
                    {
                        "name": "zebrafoo",
                        "tier": 1,
                        "path": ".claude/skills/zebrafoo",
                        "keywords": ["zebrafoo"],
                        "description": "z",
                    }
                ],
                "tier2": [
                    {
                        "name": "quokkabar",
                        "tier": 2,
                        "path": "src/genesis/skills/quokkabar",
                        "keywords": ["quokkabar"],
                        "description": "q",
                    }
                ],
            }
        )
    )
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "hooks" / "skill_injection_hook.py")],
        input=json.dumps({"prompt": "implement feature zebrafoo quokkabar", "session_id": "hk"}),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "zebrafoo" in proc.stdout and "superpowers:brainstorming" in proc.stdout

    found = usage.parse_nudges(proc.stdout)[0]
    assert {name for name, _form in found} == {
        "zebrafoo",
        "quokkabar",
    } | usage.PROCESS_NUDGE_TARGETS
    # the hook's [Process] lines name exactly the skills marked nudge-eligible
    assert {name for name, form in found if form == "process"} == usage.PROCESS_NUDGE_TARGETS


# --- conversion -----------------------------------------------------------------


def test_conversion_counts_a_use_after_the_nudge_in_the_same_session(env):
    write_transcript(
        env["projects"],
        "p",
        "s1",
        [
            nudge("s1", "[Skill] The 'alpha' skill is relevant here.", hours_ago=3),
            skill_load("s1", "alpha", hours_ago=2),
        ],
    )
    write_transcript(
        env["projects"],
        "p",
        "s2",
        [
            nudge("s2", "[Skill] The 'alpha' skill is relevant here.", hours_ago=3),
        ],
    )
    write_transcript(env["projects"], "p", "s3", [skill_load("s3", "alpha")])
    write_transcript(
        env["projects"],
        "bg",
        "h1",
        [
            {
                **nudge("h1", "[Skill] The 'alpha' skill is relevant here.", hours_ago=3),
                "entrypoint": "sdk-cli",
            },
            skill_load("h1", "alpha", kind="sdk-cli", hours_ago=2),
        ],
    )
    r = row(report(env), "alpha")
    assert r["sessions_nudged"] == {"interactive": 2, "headless": 1, "total": 3}
    assert r["conversion"] == {"interactive": 0.5, "headless": 1.0, "total": 0.667}
    assert r["sessions_used"]["total"] == 3


def test_use_before_the_nudge_is_not_a_conversion(env):
    write_transcript(
        env["projects"],
        "p",
        "s1",
        [
            skill_load("s1", "alpha", hours_ago=5),
            nudge("s1", "[Skill] The 'alpha' skill is relevant here.", hours_ago=3),
        ],
    )
    r = row(report(env), "alpha")
    assert r["sessions_nudged"]["total"] == 1
    assert r["conversion"]["total"] == 0.0


def test_never_nudged_skill_has_null_conversion(env):
    write_transcript(env["projects"], "p", "s1", [skill_load("s1", "alpha")])
    assert row(report(env), "alpha")["conversion"] == {
        "interactive": None,
        "headless": None,
        "total": None,
    }


# --- window and denominators -----------------------------------------------------


def test_old_record_in_fresh_file_excluded_and_old_file_not_opened(env):
    write_transcript(env["projects"], "p", "s1", [skill_load("s1", "alpha", hours_ago=24 * 30)])
    old = write_transcript(
        env["projects"],
        "p",
        "s2",
        [skill_load("s2", "beta")],
        mtime=(SINCE - timedelta(days=1)).timestamp(),
    )
    rep = report(env)
    assert row(rep, "alpha")["loads"]["total"] == 0
    assert row(rep, "beta")["loads"]["total"] == 0
    assert rep["files_scanned"] == 1
    assert str(old) not in json.dumps(rep)


def test_subagent_records_credit_parent_and_kinds_split(env):
    write_transcript(env["projects"], "p", "parent", [skill_load("parent", "alpha")])
    write_transcript(
        env["projects"], "p", "agent1", [skill_load("parent", "beta")], subagent_of="parent"
    )
    write_transcript(env["projects"], "bg", "h1", [skill_load("h1", "alpha", kind="sdk-cli")])
    rep = report(env)
    assert rep["subagent_files"] == 1
    assert rep["sessions_scanned"] == {"interactive": 1, "headless": 1, "total": 2}
    assert row(rep, "beta")["sessions_used"]["interactive"] == 1
    assert row(rep, "alpha")["loads"] == {"interactive": 1, "headless": 1, "total": 2}


def test_malformed_and_half_written_lines_are_counted_not_raised(env):
    path = env["projects"] / "p" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    good = json.dumps(skill_load("s1", "alpha"))
    half = json.dumps(skill_load("s1", "beta"))[:-5]
    path.write_text('{"name":"Skill" broken\n' + good + "\n" + half)
    rep = report(env)
    assert rep["lines_unparsed"] == 1
    assert row(rep, "alpha")["loads"]["total"] == 1
    assert row(rep, "beta")["loads"]["total"] == 0


def test_window_and_note_are_explicit(env):
    rep = report(env)
    assert rep["window"]["days"] == 14
    assert rep["sessions_scanned"]["total"] == 0
    assert "none found" in rep["note"]
    assert rep["db_join"] == "unavailable"


# --- injected (cc_sessions) --------------------------------------------------------


def test_injected_counts_tags_and_failed_rows(env):
    rows = [
        (json.dumps({"skill_tags": ["alpha", "deep-reflection"]}), "completed"),
        (json.dumps({"skill_tags": ["alpha"]}), "failed"),
        (None, "completed"),
        ("not json", "completed"),
    ]
    rep = report(env, injected=usage.count_injected(rows))
    a = row(rep, "alpha")
    assert a["injected"] == 2 and a["injected_failed"] == 1
    # configured injection is shown but never decides the state (owner ruling)
    assert a["state"] == "dead"
    assert rep["injected_sessions"] == 2
    assert rep["db_join"] == "ok"
    assert "deep-reflection" in rep["uncatalogued_tags"]


def test_read_injected_sync_windows_by_started_at(tmp_path):
    import sqlite3

    db = tmp_path / "g.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cc_sessions (metadata TEXT, status TEXT, started_at TEXT)")
    con.executemany(
        "INSERT INTO cc_sessions VALUES (?,?,?)",
        [
            (
                json.dumps({"skill_tags": ["alpha"]}),
                "completed",
                (NOW - timedelta(days=1)).isoformat(),
            ),
            (
                json.dumps({"skill_tags": ["alpha"]}),
                "completed",
                (NOW - timedelta(days=30)).isoformat(),
            ),
        ],
    )
    con.commit()
    con.close()
    inj = usage.read_injected_sync(db, SINCE)
    assert inj["sessions"] == 1
    assert inj["per_skill"]["alpha"]["injected"] == 1
    assert usage.read_injected_sync(tmp_path / "missing.db", SINCE) is None


# --- resource bound -----------------------------------------------------------------


def test_memory_does_not_grow_with_transcript_length(env):
    """Streaming means peak memory tracks the longest LINE, not the file: four
    times as many 1 MB lines must not raise the peak materially."""
    filler = json.dumps(rec("big", type="user", message={"content": "x" * 1_000_000}))

    def peak_for(lines: int) -> tuple[int, int]:
        path = env["projects"] / "p" / "big.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for _ in range(lines):
                fh.write(filler + "\n")
            fh.write(json.dumps(skill_load("big", "alpha")) + "\n")
        tracemalloc.start()
        rep = report(env)
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert row(rep, "alpha")["loads"]["total"] == 1
        return peak, path.stat().st_size

    small_peak, _ = peak_for(10)
    big_peak, big_size = peak_for(40)
    assert big_peak < small_peak * 1.5, (small_peak, big_peak)
    assert big_peak < big_size / 4, (big_peak, big_size)


def test_cli_writes_json(env, tmp_path, monkeypatch):
    out = tmp_path / "out.json"
    write_transcript(env["projects"], "p", "s1", [skill_load("s1", "alpha", hours_ago=0)])
    monkeypatch.setattr(usage, "_default_population", lambda: env["population"])
    rc = usage.main(
        [
            "--days",
            "14",
            "--projects-root",
            str(env["projects"]),
            "--db",
            str(tmp_path / "none.db"),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["window"]["days"] == 14
    assert time.time() - datetime.fromisoformat(data["window"]["end"]).timestamp() < 600


# --- review fixes: preloads, kind precedence, window validation ----------------------


def test_agent_frontmatter_preload_is_counted_but_never_a_use(env):
    text = (
        "<command-message>alpha</command-message>\n<command-name>alpha</command-name>\n"
        "<skill-format>true</skill-format>Base directory for this skill: /r/.claude/skills/alpha\n\nbody"
    )
    write_transcript(
        env["projects"],
        "p",
        "agent1",
        [
            rec(
                "s1",
                type="user",
                isMeta=True,
                message={"content": [{"type": "text", "text": text}]},
            )
        ],
        subagent_of="s1",
    )
    r = row(report(env), "alpha")
    assert r["preloads"]["total"] == 1
    assert r["loads"]["total"] == 0 and r["slash_loads"]["total"] == 0
    assert r["state"] == "dead"


def test_parent_transcript_decides_session_kind(env):
    # the subagent dir sorts before the parent file, and claims a different kind
    write_transcript(
        env["projects"], "p", "agent1", [skill_load("s1", "beta", kind="sdk-cli")], subagent_of="s1"
    )
    write_transcript(env["projects"], "p", "s1", [skill_load("s1", "alpha")])
    rep = report(env)
    assert rep["sessions_scanned"] == {"interactive": 1, "headless": 0, "total": 1}
    assert row(rep, "beta")["loads"]["interactive"] == 1


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), 1e7, True])
def test_invalid_window_is_rejected(bad):
    with pytest.raises(ValueError):
        usage.validate_days(bad)


def test_cli_rejects_invalid_window(capsys):
    with pytest.raises(SystemExit) as exc:
        usage.main(["--days", "-5"])
    assert exc.value.code == 2
    assert "days must be" in capsys.readouterr().err


# --- security review fixes ------------------------------------------------------------


def test_fifo_transcripts_never_block_and_broken_entries_skip_only_themselves(env):
    proj = env["projects"] / "p"
    write_transcript(env["projects"], "p", "agent2", [skill_load("s1", "alpha")], subagent_of="s1")
    sub = proj / "s1" / "subagents"
    os.mkfifo(sub / "agent1.jsonl")  # sorts before agent2
    os.symlink(sub / "missing", sub / "agent0.jsonl")  # broken link
    os.mkfifo(proj / "top.jsonl")
    files = [p.name for p, _ in usage.iter_transcript_files(env["projects"], SINCE)]
    assert files == ["agent-agent2.jsonl"]  # write_transcript names it agent-<sid>
    scanner = usage.Scanner(SINCE, NOW)
    scanner.scan_file(proj / "top.jsonl")  # returns at once instead of blocking
    assert scanner.files_scanned == 0
    assert row(report(env), "alpha")["loads"]["total"] == 1


def test_read_only_uri_survives_special_characters_in_the_db_path(tmp_path):
    import sqlite3

    db = tmp_path / "a?b#c.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cc_sessions (metadata TEXT, status TEXT, started_at TEXT)")
    con.execute(
        "INSERT INTO cc_sessions VALUES (?,?,?)",
        (json.dumps({"skill_tags": ["alpha"]}), "completed", NOW.isoformat()),
    )
    con.commit()
    con.close()
    before = sorted(p.name for p in tmp_path.iterdir())
    assert usage.read_injected_sync(db, SINCE)["sessions"] == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # no stray file created


def test_names_that_are_not_identifiers_never_become_rows(env):
    bad = "not an identifier; it has spaces & punctuation!"
    write_transcript(env["projects"], "p", "s1", [skill_load("s1", bad), skill_load("s1", "alpha")])
    tags = usage.count_injected([(json.dumps({"skill_tags": ["x y"]}), "completed")])
    rep = report(env, injected=tags)
    assert bad not in {r["name"] for r in rep["skills"]}
    assert "x y" not in rep["uncatalogued_tags"]
    assert rep["names_invalid"] == 2
    assert row(rep, "alpha")["loads"]["total"] == 1


def test_oversized_line_is_skipped_and_counted(env, monkeypatch):
    monkeypatch.setattr(usage, "MAX_LINE_BYTES", 1000)
    path = env["projects"] / "p" / "s1.jsonl"
    path.parent.mkdir(parents=True)
    big = json.dumps(rec("s1", type="user", message={"content": '"Skill"' + "x" * 5000}))
    path.write_text(big + "\n" + json.dumps(skill_load("s1", "alpha")) + "\n")
    rep = report(env)
    assert rep["lines_oversized"] == 1
    assert row(rep, "alpha")["loads"]["total"] == 1
