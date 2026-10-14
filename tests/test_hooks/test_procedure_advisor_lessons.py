"""procedure_advisor.py delivers lessons (advisory-lessons T-5).

Each test writes its own trigger cache and points the hook's session and
database paths at a temp dir, so nothing reads or writes live state.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "procedure_advisor.py"


def _entry(pid: str, *, tool="Bash", kind="lesson", repeat="session", **extra) -> dict:
    return {
        "tool": [tool],
        "procedure_id": pid,
        "task_type": f"task_{pid}",
        "principle": f"do {pid}",
        "steps": ["a step"],
        "confidence": 0.9,
        "kind": kind,
        "repeat": repeat,
        "patterns": [],
        **extra,
    }


@pytest.fixture
def env(tmp_path):
    """Write a cache of the given entries; return a runner for one tool call."""
    cache = tmp_path / "procedure_triggers.yaml"
    sessions = tmp_path / "sessions"

    def write(*entries: dict) -> None:
        text = json.dumps({"triggers": list(entries)})
        cache.write_text(text)  # YAML is a JSON superset
        cache.with_suffix(".json").write_text(text)  # written last: the fresher sidecar

    def run(tool_name: str, tool_input: dict, *, sid="sess-1", agent=None) -> str:
        payload = {"tool_name": tool_name, "tool_input": tool_input}
        if sid is not None:
            payload["session_id"] = sid
        if agent is not None:
            payload["agent_id"] = agent
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "GENESIS_PROCEDURE_TRIGGERS": str(cache),
                "GENESIS_SESSIONS_DIR": str(sessions),
                "GENESIS_DB_PATH": str(tmp_path / "absent.db"),
            },
        )
        assert result.returncode == 0, result.stderr
        if not result.stdout.strip():
            return ""
        hook = json.loads(result.stdout)["hookSpecificOutput"]
        assert "permissionDecision" not in hook  # advice never approves or blocks
        return hook["additionalContext"]

    run.write = write
    run.sessions = sessions
    return run


def test_a_session_lesson_fires_once_per_session_and_agent(env):
    env.write(_entry("l1", patterns=[r"deploy_code_only\.sh"]))
    call = {"command": "scripts/deploy_code_only.sh"}
    assert "task_l1" in env("Bash", call)
    assert env("Bash", call) == ""  # already fired in this session
    assert "task_l1" in env("Bash", call, agent="sub-1")  # a subagent has its own context
    assert "task_l1" in env("Bash", call, sid="sess-2")  # a new session
    log = env.sessions / "sess-1" / "advisor_fires.jsonl"
    assert [json.loads(line)["agent"] for line in log.read_text().splitlines()] == ["main", "sub-1"]


def test_a_session_lesson_fires_again_after_a_compaction(env):
    env.write(_entry("l1", patterns=["deploy"]))
    assert "task_l1" in env("Bash", {"command": "deploy"})
    assert env("Bash", {"command": "deploy"}) == ""
    # SessionStart rewrites the context mirrors when the window is compacted.
    mirror = env.sessions / "sess-1" / "context-identity-core.md"
    mirror.write_text("new window")  # written after the first fire
    assert "task_l1" in env("Bash", {"command": "deploy"})  # a new window
    assert env("Bash", {"command": "deploy"}) == ""  # and once in it


def test_every_fires_each_time_and_without_a_session_nothing_is_recorded(env):
    env.write(_entry("e", repeat="every", patterns=["deploy"]), _entry("s", patterns=["deploy"]))
    assert "task_e" in env("Bash", {"command": "deploy"}) and "task_e" in env(
        "Bash", {"command": "deploy"}
    )
    for _ in range(2):  # no session id: never deduplicated, never written
        assert "task_s" in env("Bash", {"command": "deploy"}, sid=None)
    assert "task_s" in env("Bash", {"command": "deploy"}, sid="../escape")
    assert not (env.sessions / "..").joinpath("escape").exists()


def test_a_malformed_or_partial_log_line_is_skipped(env):
    env.write(_entry("l1", patterns=["deploy"]))
    log = env.sessions / "sess-1" / "advisor_fires.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('not json\n{"procedure_id": "l1", "agent": "main"}\n{"procedure_id": "l')
    assert env("Bash", {"command": "deploy"}) == ""  # the valid line still counts


def test_an_unwritable_log_still_delivers_the_advice(env):
    env.write(_entry("l1", patterns=["deploy"]))
    env.sessions.write_text("a file where the directory should be")
    assert "task_l1" in env("Bash", {"command": "deploy"})


def test_no_match_writes_nothing(env):
    env.write(_entry("l1", patterns=["deploy"]))
    assert env("Bash", {"command": "ls"}) == ""
    assert not env.sessions.exists()


@pytest.mark.parametrize(
    ("phrase", "command", "fires"),
    [
        ("git push", "git -C x push origin HEAD", True),
        ("git push", "echo git push", False),
        ("git push", "bash -c 'git push'", True),
        ("git push", "sudo git push", True),
        ("git push", "git pushx", False),
        ("pytest", "python -m pytest -q tests", True),
        ("pytest", "/venv/bin/pytest tests", True),
        ("gh pr create", "gh --repo o/r pr create --fill", True),
        ("gh pr create", "gh pr list", False),
        ("git worktree add", "git -C x worktree add ../w", True),
        ("git worktree add", "git worktree list", False),
        # Known over-match, inherited from shell_parse: a heredoc body line
        # parses as a command, so a body that says `git push` fires.
        ("git push", "cat > notes <<EOF\ngit push\nEOF", True),
    ],
)
def test_commands_match_the_parsed_command(env, phrase, command, fires):
    env.write(_entry("c", repeat="every", commands=[phrase]))
    assert ("task_c" in env("Bash", {"command": command})) is fires


def test_commands_apply_to_bash_only(env):
    env.write(_entry("c", tool="Agent", repeat="every", commands=["git push"]))
    assert env("Agent", {"prompt": "git push the branch"}) == ""


def test_an_agent_prompt_is_matched(env):
    env.write(_entry("a", tool="Agent", repeat="every", patterns=["genesis-architect"]))
    assert "task_a" in env("Agent", {"prompt": "dispatch genesis-architect over the diff"})
    assert env("Agent", {"description": "genesis-architect", "prompt": "unrelated"}) == ""


def test_a_slow_or_broken_pattern_is_skipped_and_the_next_still_matches(env):
    # `(a|aa)+$` backtracks for seconds on this input without the timeout.
    env.write(_entry("p", repeat="every", patterns=["(a|aa)+$", "(", "deploy"]))
    assert "task_p" in env("Bash", {"command": "a" * 40 + "b deploy"})


def test_only_the_first_8_kb_is_matched(env):
    env.write(_entry("p", repeat="every", patterns=["needle"]))
    assert env("Bash", {"command": "x" * 8192 + "needle"}) == ""
    assert "task_p" in env("Bash", {"command": "x" * 8000 + "needle"})


@pytest.mark.parametrize(
    "bad",
    [
        {"commands": "git push"}, {"commands": [""]}, {"commands": [5]}, {"patterns": None},
        {"patterns": [5]}, {"tool": 5}, {"task_type": None}, {"confidence": "high"},
        {"procedure_id": ["x"]},
        {"procedure_id": ["x"], "repeat": "session"},  # reaches the fire-once filter
    ],
)
def test_a_malformed_entry_is_skipped_and_the_others_still_fire(env, bad):
    env.write({**_entry("bad", repeat="every", patterns=["git"]), **bad},
              _entry("good", repeat="every", patterns=["git"]))
    assert "task_good" in env("Bash", {"command": "git push"})


def test_an_earned_tag_with_regex_syntax_is_bounded_too(env):
    env.write(_entry("e", kind="earned", repeat="every", patterns=["(a|aa)+$", "dep.oy"]))
    assert "task_e" in env("Bash", {"command": "a" * 40 + "b deploy"})


def test_the_fire_log_holds_no_input_text(env):
    env.write(_entry("l1", patterns=["deploy"]))
    env("Bash", {"command": "deploy --token sk-secret-value"})
    log = (env.sessions / "sess-1" / "advisor_fires.jsonl").read_text()
    assert "sk-secret" not in log and "deploy" not in log


def test_lesson_patterns_are_case_sensitive_and_earned_ones_are_not(env):
    env.write(
        _entry("lesson", repeat="every", patterns=["Deploy"]),
        _entry("earned", kind="earned", repeat="every", patterns=["Deploy"]),
    )
    out = env("Bash", {"command": "deploy now"})
    assert "task_earned" in out and "task_lesson" not in out


def test_an_old_format_cache_still_matches(env):
    old = _entry("old")
    for key in ("kind", "repeat", "patterns"):
        del old[key]
    env.write({**old, "context_patterns": ["deploy"]})
    assert "task_old" in env("Bash", {"command": "Deploy now"})


def test_an_unreviewed_lesson_is_labelled(env):
    env.write(_entry("u", repeat="every", patterns=["deploy"], label="unreviewed"))
    assert "PROCEDURE: task_u (unreviewed lesson" in env("Bash", {"command": "deploy"})


def _load():
    spec = importlib.util.spec_from_file_location("_advisor_lessons_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_without_the_regex_module_lessons_match_by_substring(monkeypatch):
    advisor = _load()
    monkeypatch.setitem(sys.modules, "regex", None)  # import raises ImportError
    matcher = advisor._Matcher("run deploy.sh", None)
    assert matcher.lesson_regex() is None
    assert matcher.hits(_entry("x", patterns=["deploy.sh"]))
    assert not matcher.hits(_entry("x", patterns=[r"deploy\.sh"]))  # not run as a regex


def test_the_deadline_stops_matching(monkeypatch):
    advisor = _load()
    monkeypatch.setattr(advisor, "_DEADLINE_S", -1.0)
    assert not advisor._Matcher("deploy", None).hits(_entry("x", patterns=["deploy"]))


def test_a_call_with_no_command_word_never_imports_the_parser(env, tmp_path):
    env.write(_entry("c", repeat="every", commands=["git push"]))
    probe = (
        "import io, json, runpy, sys\n"
        "sys.stdin = io.StringIO(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'ls -la'}}))\n"
        f"runpy.run_path({str(_SCRIPT)!r})['main']()\n"
        "print('shell_parse' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "GENESIS_PROCEDURE_TRIGGERS": str(tmp_path / "procedure_triggers.yaml")},
    )
    assert result.stdout.strip().splitlines()[-1] == "False", result.stderr
