"""rm_trash_advisory: a note on a shell delete of user data, never a block.

Every case runs the real hook as a subprocess with HOME pointed at a synthetic
tree, so path resolution, the existence checks and the JSON output are the ones
production runs.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts" / "hooks" / "rm_trash_advisory.py"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    files = [
        ".claude/projects/p/memory/a.md",
        ".claude/projects/p/transcript.jsonl",
        ".claude/plans/x.md",
        ".claude/CLAUDE.md",
        ".claude/settings.json",
        ".claude/hooks/zz-probe.sh",
        ".genesis/output/o.txt",
        ".genesis/config/c.yaml",
        ".genesis/cc-tmp/f.txt",
        ".genesis/locks/l.lock",
        "genesis/src/genesis/__init__.py",
        "genesis/src/genesis/identity/USER.md",
        "genesis/src/genesis/identity/SOUL.md",
        "genesis/src/genesis/x.py",
        "genesis/config/model_routing.local.yaml",
        "genesis/config/model_routing.yaml",
        "genesis/secrets.env",
        "wt/a/src/genesis/__init__.py",
        "wt/a/src/genesis/identity/SOUL.md",
        "proj/config/app.local.yaml",
        "tmp/s.txt",
    ]
    for rel in files:
        p = h / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    (h / ".claude/skills").mkdir()
    (h / ".claude/skills/linked").symlink_to(h / "tmp")
    (h / ".claude/skills/linked.md").symlink_to(h / "tmp/s.txt")
    (h / "tmp/alias.md").symlink_to(h / ".claude/plans/x.md")  # a link INTO user data
    (h / "tmp/alias-dir").symlink_to(h / ".claude/plans")
    return h


def _run(home: Path, command: str, cwd: Path | None = None, *, stdin: str | None = None):
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOME"}
    env["HOME"] = str(home)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or home)}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin if stdin is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _note(res) -> str | None:
    assert res.returncode == 0, res.stderr
    if not res.stdout.strip():
        return None
    out = json.loads(res.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"]
    return out["hookSpecificOutput"]["additionalContext"]


FIRES = [
    ("rm ~/.claude/projects/p/memory/a.md", None),
    ("rm -f $HOME/.claude/plans/x.md", None),
    ('rm "${HOME}/.claude/CLAUDE.md"', None),
    ("rm ~/.claude/settings.json", None),
    ("rm ~/.genesis/output/o.txt", None),
    ("rm -rf ~/.genesis/config", None),
    ("unlink ~/.claude/projects/p/memory/a.md", None),
    ("shred -u ~/.claude/projects/p/memory/a.md", None),
    ("sudo rm ~/.claude/plans/x.md", None),
    ("bash -c 'rm ~/.claude/plans/x.md'", None),
    ("rm ~/.claude/projects/p/memory/*.md", None),
    ("rm -r ~/.claude/projects/p/*", None),
    ("rm ~/.claude/plans/{x,y}.md", None),
    ("rm -rf ~/.claude/projects/p", None),
    ("rm -r ~/genesis/src/genesis/identity", None),
    ("rm -rf ~/genesis", None),
    ("rm src/genesis/identity/USER.md", "genesis"),
    ("rm config/model_routing.local.yaml", "genesis"),
    ("rm secrets.env", "genesis"),
    ("cd ~/genesis && rm secrets.env", None),
    ("cd ~/.claude && rm plans/x.md", None),
    ("rm -- ~/.claude/plans/x.md", None),
    ("pushd ~/.claude && rm CLAUDE.md", None),
    ("cd; rm .claude/plans/x.md", "tmp"),  # a bare cd goes to HOME
    ("shred ~/tmp/alias.md", None),  # shred writes through the link
    ("rm -r ~/tmp/alias-dir/", None),  # the trailing slash descends into the target
    ("shred -n 3 ~/.claude/CLAUDE.md", None),
    ("shred -zn3 ~/.claude/CLAUDE.md", None),
    ("rm ~/.claude/plans/x.md && tr a b <<< x", None),  # a here-string has no body
    ("cd ~/genesis && rm secrets.env || true", None),  # || is not a pipe
]


@pytest.mark.parametrize(("command", "cwd"), FIRES)
def test_fires_on_user_data(home: Path, command: str, cwd: str | None) -> None:
    note = _note(_run(home, command, home / cwd if cwd else None))
    assert note is not None, command
    assert "genesis.trash put" in note
    assert note.startswith("If this ran")


SILENT = [
    ("rm ~/tmp/s.txt", None),
    ("rm ~/.genesis/cc-tmp/f.txt", None),
    ("rm ~/.genesis/locks/l.lock", None),
    ("rm ~/.claude/hooks/zz-probe.sh", None),
    ("rm ~/genesis/src/genesis/x.py", None),
    ("rm ~/genesis/config/model_routing.yaml", None),
    ("rm ~/genesis/src/genesis/identity/SOUL.md", None),
    ("rm -f ~/.claude/plans/missing.md", None),
    ("rm ~/.claude/projects/p", None),  # a directory without -r: rm refuses it
    ("rm -rf ~/wt/a", None),  # a checkout holding no user data
    ("rm ~/proj/config/app.local.yaml", None),  # not a Genesis checkout
    ("git rm ~/.claude/plans/x.md", None),
    ("rmdir ~/.claude/plans", None),
    ("find ~/.claude/plans -delete", None),
    ("git clean -fdx", "genesis"),
    ("ls ~/.claude/plans | xargs rm", None),
    # Read with $X dropped, this would normalise to ~/.claude/plans/x.md.
    ("rm $X/../plans/x.md", ".claude"),
    ("cat <<'EOF' > ~/tmp/x.sh\nrm ~/.claude/CLAUDE.md\nEOF", None),  # heredoc body
    ("cd ~/.claude | true; rm CLAUDE.md", None),  # that cd ran in a subshell
    ("rm ~/.claude/skills/linked", None),  # a link: its target stays
    ("rm ~/.claude/skills/linked.md", None),  # a link to a file, so no directory rule applies
    ("rm ~/.claude/plans", None),  # a directory without -r: rm refuses it
    ("unlink ~/.claude/plans", None),
    ("rm ~/tmp/alias.md", None),  # rm removes the link only
    ("rm -r ~/tmp/alias-dir", None),  # no trailing slash: the link only
    ("shred --random-source ~/.claude/CLAUDE.md ~/tmp/s.txt", None),  # a value, not a target
    (
        'rm -rf ""',
        ".claude",
    ),  # an empty operand removes nothing (with -r, or the dir rule masks it)
    ('cd "$X" && rm -r plans', ".claude"),
    # The subshell's cd does not leak: the second rm runs in HOME, where there is no
    # plans/. Read flat, the cd would aim it at ~/.claude/plans/x.md.
    ("(cd ~/.claude && rm none); rm plans/x.md", None),
    ("bash -c 'cd /nowhere && rm -r plans'", ".claude"),
    ("rm plans/x.md", None),  # relative to HOME, where no plans dir exists
    ("rm ~/.claude/plans/x.md 'unterminated", None),  # blind parse
    ("echo rm ~/.claude/plans/x.md", None),
    ("rm ~/.claude/plans/" + "{a,b}" * 12, None),  # brace bomb
]


@pytest.mark.parametrize(("command", "cwd"), SILENT)
def test_silent(home: Path, command: str, cwd: str | None) -> None:
    assert _note(_run(home, command, home / cwd if cwd else None)) is None, command


def test_relative_operand_resolves_against_the_payload_cwd(home: Path) -> None:
    assert _note(_run(home, "rm plans/x.md", home / ".claude")) is not None
    assert _note(_run(home, "rm plans/x.md", home)) is None


def test_note_names_each_firing_operand_once(home: Path) -> None:
    note = _note(_run(home, "rm ~/.claude/plans/x.md ~/tmp/s.txt ~/.claude/plans/x.md"))
    assert note is not None
    assert note.count("`~/.claude/plans/x.md`") == 1
    assert "s.txt" not in note


def test_genesis_home_is_honored(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "gh"
    (other / "output").mkdir(parents=True)
    (other / "output" / "r.md").write_text("x")
    env = {k: v for k, v in os.environ.items()}
    env.update(HOME=str(home), GENESIS_HOME=str(other))
    payload = {"tool_input": {"command": f"rm {other}/output/r.md"}, "cwd": str(home)}
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert _note(res) is not None


def test_garbage_stdin_exits_zero_silently(home: Path) -> None:
    res = _run(home, "", stdin="not json")
    assert res.returncode == 0
    assert res.stdout == ""


def test_a_crash_inside_the_hook_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    mod = private_module("rm_trash_advisory_crash", HOOK)

    def boom(*_a, **_k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(mod, "_advisory", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_input": {"command": "rm x"}})))
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "synthetic" in captured.err


def test_a_brace_bomb_does_not_silence_the_other_operands(home: Path) -> None:
    note = _note(_run(home, "rm ~/.claude/CLAUDE.md ~/.claude/plans/" + "{a,b}" * 12))
    assert note is not None
    assert "`~/.claude/CLAUDE.md`" in note


@pytest.mark.parametrize("var", ["GENESIS_OUTPUT_DIR", "GENESIS_PLANS_DIR", "SECRETS_PATH"])
def test_path_overrides_are_honoured(home: Path, tmp_path: Path, var: str) -> None:
    """Genesis resolves these locations through override variables
    (src/genesis/env.py); a relocated one is user data too."""
    moved = tmp_path / "moved"
    target = moved / "r.md"
    target.parent.mkdir()
    target.write_text("x")
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOME"}
    env.update(HOME=str(home), **{var: str(target if var == "SECRETS_PATH" else moved)})
    payload = {"tool_input": {"command": f"rm {target}"}, "cwd": str(home)}
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert _note(res) is not None, var


def test_claude_home_override_is_honoured(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "claude-elsewhere"
    (other / "plans").mkdir(parents=True)
    (other / "plans" / "p.md").write_text("x")
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOME"}
    env.update(HOME=str(home), CLAUDE_HOME=str(other))
    payload = {"tool_input": {"command": f"rm {other}/plans/p.md"}, "cwd": str(home)}
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert _note(res) is not None


def test_glob_expansion_stops_at_the_visit_bound(home: Path, monkeypatch) -> None:
    """A pattern that matches nothing still costs a listing of every directory it
    reaches; the bound must stop that, not just the matches it yields."""
    mod = _cost_module(home, monkeypatch)
    for i in range(30):
        (home / "tmp" / f"d{i}").mkdir()
    monkeypatch.setattr(mod, "_MAX_VISITS", 10)
    with pytest.raises(mod._OverBudget):
        mod._existing(str(home / "tmp" / "*" / "*.nomatch"), float("inf"))


def test_the_budget_is_checked_before_listing_any_directory(home: Path, monkeypatch) -> None:
    mod = _cost_module(home, monkeypatch)
    listed: list[object] = []
    real = mod.os.scandir
    monkeypatch.setattr(mod.os, "scandir", lambda p: listed.append(p) or real(p))
    monkeypatch.setattr(mod, "_BUDGET_S", -1.0)
    assert mod._advisory("rm ~/tmp/*/*.nomatch", str(home)) is None
    assert listed == []


def test_glob_expansion_follows_the_shell_rules(home: Path, monkeypatch) -> None:
    mod = _cost_module(home, monkeypatch)
    (home / "tmp" / ".hidden.md").write_text("x")
    found = mod._existing(str(home / "tmp" / "*.md"), float("inf"))
    assert str(home / "tmp" / "alias.md") in found
    assert str(home / "tmp" / ".hidden.md") not in found  # * does not match a leading dot
    assert mod._existing(str(home / "tmp" / ".*.md"), float("inf")) == [
        str(home / "tmp" / ".hidden.md")
    ]


def _cost_module(home: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GENESIS_HOME", raising=False)
    return private_module("rm_trash_advisory_cost", HOOK)


def test_matches_per_operand_are_capped(home: Path, monkeypatch) -> None:
    """Every command waits for its hooks: a glob must not cost one check per match
    (`rm -rf ~/tmp/*/*` once took 16.5s)."""
    big = home / "tmp" / "big"
    big.mkdir()
    for i in range(50):
        (big / f"f{i}").write_text("x")
    mod = _cost_module(home, monkeypatch)
    monkeypatch.setattr(mod, "_MAX_MATCHES", 5)
    seen: list[str] = []
    real = mod._hits
    monkeypatch.setattr(mod, "_hits", lambda p, r, t: seen.append(p) or real(p, r, t))
    assert mod._advisory(f"rm -rf {big}/*", str(home)) is None
    assert len(seen) == 5


def test_the_time_budget_stops_the_scan(home: Path, monkeypatch) -> None:
    mod = _cost_module(home, monkeypatch)
    assert mod._advisory("rm ~/.claude/CLAUDE.md", str(home)) is not None
    monkeypatch.setattr(mod, "_BUDGET_S", -1.0)
    assert mod._advisory("rm ~/.claude/CLAUDE.md", str(home)) is None


def test_wired_as_a_bash_advisory() -> None:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
    ]
    assert "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook hooks/rm_trash_advisory.py" in commands


def test_carries_no_exit_2() -> None:
    src = HOOK.read_text()
    for blocked in ("return 2", "sys.exit(2)", "os._exit(2)"):
        assert blocked not in src
