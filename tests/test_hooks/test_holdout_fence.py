"""Tests for scripts/hooks/holdout_fence.py (advisory fence around a holdout).

The hook runs as a real subprocess (the hook contract) with HOME and
GENESIS_HOME pointed at a temporary directory holding a synthetic holdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "hooks" / "holdout_fence.py"
STEP_LINE = "from genesis.autonomy.approval_gate import AutonomousCliApprovalGate"
EXPECT_LINE = "the exact expected output line that is well over forty characters"

HOLDOUT_MD = f"""# Holdout

## H-01 first
Protects: something
Touches: scripts/hooks/x.py
Expect-before: PASS
Steps:
```sh
{STEP_LINE}
```
Expect:
```
{EXPECT_LINE}
```
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    gh = tmp_path / ".genesis"
    (gh / "holdout").mkdir(parents=True)
    (gh / "holdout" / "HOLDOUT.md").write_text(HOLDOUT_MD)
    return tmp_path


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOME"}
    env["HOME"] = str(home)
    env["GENESIS_HOME"] = str(home / ".genesis")
    return env


def _log_rows(log: Path) -> list[dict]:
    return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []


def _rows(home: Path) -> list[dict]:
    logs = home / ".genesis" / "logs"
    return _log_rows(logs / "holdout-exposures.jsonl") + _log_rows(
        logs / "holdout-fence-activity.jsonl"
    )


def _run(home: Path, event: str, payload: dict) -> tuple[str, list[dict]]:
    proc = subprocess.run(
        [sys.executable, str(HOOK), event],
        input=json.dumps(payload),
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, _rows(home)


def _note(stdout: str) -> str:
    if not stdout.strip():
        return ""
    return json.loads(stdout)["hookSpecificOutput"]["additionalContext"]


def _pre(tool: str, tool_input: dict, **extra) -> dict:
    return {"session_id": "s1", "tool_name": tool, "tool_input": tool_input, "cwd": "/", **extra}


def _post(tool: str, response, tool_input=None, **extra) -> dict:
    return {
        "session_id": "s1",
        "tool_name": tool,
        "tool_input": tool_input or {},
        "tool_response": response,
        **extra,
    }


GH = "~/.genesis"


@pytest.mark.parametrize(
    "tool,ti,kind",
    [
        ("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}, "read"),
        (
            "Edit",
            {"file_path": f"{GH}/holdout/HOLDOUT.md", "old_string": "a", "new_string": "b"},
            "write",
        ),
        ("Glob", {"pattern": f"{GH}/hold*/*.md"}, "read"),
        ("Glob", {"pattern": f"{GH}/**/*.md"}, "read"),  # it lists HOLDOUT.md
        ("Glob", {"pattern": "holdout/*", "path": GH}, "read"),
        ("Glob", {"pattern": "**/HOLDOUT.md", "path": GH}, "read"),
        ("Glob", {"pattern": f"{GH}/{{holdout,logs}}/*"}, "read"),
        ("Grep", {"pattern": "x", "path": f"{GH}/holdout"}, "read"),
        ("Grep", {"pattern": "x", "path": GH, "glob": "holdout/*.md"}, "read"),
        ("Grep", {"pattern": "x", "path": GH, "glob": "HOLDOUT.md"}, "read"),
        ("Bash", {"command": f"cat {GH}/holdout/HOLDOUT.md"}, "access"),
        ("Bash", {"command": 'cat "$HOME/.genesis/holdout/HOLDOUT.md" | head'}, "access"),
        ("Bash", {"command": f"cd {GH} && cat holdout/HOLDOUT.md"}, "access"),
        ("Bash", {"command": "python3 x.py < ~/.genesis/holdout/HOLDOUT.md"}, "access"),
        ("Bash", {"command": "echo hi > ~/.genesis/holdout/new.md"}, "access"),
        ("Bash", {"command": "sudo -n cat ~/.genesis/holdout/HOLDOUT.md"}, "access"),
        ("Bash", {"command": "rg needle ~/.genesis/holdout"}, "access"),
    ],
)
def test_operands_reaching_the_holdout_are_logged_and_advised(home, tool, ti, kind):
    out, rows = _run(home, "pre", _pre(tool, ti))
    assert "HOLDOUT FENCE" in _note(out)
    assert [r["kind"] for r in rows] == [kind]
    assert rows[0]["tool"] == tool and rows[0]["session_id"] == "s1"


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Grep", {"pattern": "x", "path": GH}),
        ("Glob", {"pattern": "**/*.zzz", "path": GH}),
        ("Bash", {"command": "rg pattern ~/.genesis"}),
        ("Bash", {"command": "grep -rn x ~"}),  # grep -r does walk hidden directories
        ("Bash", {"command": f"grep -rn x {GH}"}),
        ("Bash", {"command": f"find {GH} -name '*.zzz'"}),
    ],
)
def test_broad_searches_above_the_holdout_are_logged_without_a_note(home, tool, ti):
    out, rows = _run(home, "pre", _pre(tool, ti))
    assert out.strip() == ""
    assert [r["kind"] for r in rows] == ["broad"]


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Agent", {"subagent_type": "Explore", "prompt": "Never open ~/.genesis/holdout/."}),
        ("Bash", {"command": "rg -n '~/.genesis/holdout' docs/"}),
        ("Bash", {"command": "gh pr comment 1 --body 'never read ~/.genesis/holdout'"}),
        ("Bash", {"command": "echo ~/.genesis/holdout"}),
        ("Grep", {"pattern": "~/.genesis/holdout", "path": "src"}),
    ],
)
def test_a_mention_is_recorded_as_a_mention_without_a_note(home, tool, ti):
    out, rows = _run(home, "pre", _pre(tool, ti))
    assert out.strip() == ""
    assert [r["kind"] for r in rows] == ["mention"]


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Read", {"file_path": f"{GH}/holdoutish.md"}),
        ("Read", {"file_path": "/etc/hostname"}),
        ("Bash", {"command": f"ls {GH}"}),
        ("Bash", {"command": f"ls -r {GH}"}),
        ("Bash", {"command": "echo holdout"}),
        ("Bash", {"command": "echo ~/.genesis/holdout-old"}),
        ("Grep", {"pattern": "holdout", "path": "src"}),
    ],
)
def test_unrelated_calls_are_silent(home, tool, ti):
    out, rows = _run(home, "pre", _pre(tool, ti))
    assert out.strip() == "" and rows == []


@pytest.mark.parametrize(
    "command",
    [
        "ls ${HOME}/.genesis/holdout",
        "du -sh ~/.genesis/holdout",
        "stat ~/.genesis/holdout/HOLDOUT.md",
        "ls ~/.genesis/*",
    ],
)
def test_metadata_commands_are_listings_without_a_note(home, command):
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert out.strip() == ""
    assert [r["kind"] for r in rows] == ["listing"]
    assert not (home / ".genesis" / "logs" / "holdout-exposures.jsonl").exists()


@pytest.mark.parametrize(
    "command",
    [
        "grep -E 'a|b' ~/.genesis/holdout/HOLDOUT.md",
        "X=$(cat ~/.genesis/holdout/HOLDOUT.md)",
        'echo "$(cat ~/.genesis/holdout/HOLDOUT.md)"',
        "nl ~/.genesis/holdout/HOLDOUT.md",
        "perl -ne print ~/.genesis/holdout/HOLDOUT.md",
        "git diff --no-index a ~/.genesis/holdout/HOLDOUT.md",
        "python3 - <<'PY'\nprint(open('~/.genesis/holdout/HOLDOUT.md').read())\nPY",
    ],
)
def test_reads_by_any_verb_are_exposures(home, command):
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert "HOLDOUT FENCE" in _note(out)
    assert [r["kind"] for r in rows] == ["access"]


def test_a_heredoc_body_written_to_a_file_is_a_mention(home):
    command = "cat > notes.md <<'EOF'\nnever read ~/.genesis/holdout\nEOF"
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert out.strip() == ""
    assert [r["kind"] for r in rows] == ["mention"]


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Grep", {"pattern": "x", "path": "~", "glob": "*.md"}),
        ("Glob", {"pattern": "**/*.md", "path": "~"}),
        ("Glob", {"pattern": "~/*/holdout/*.md"}),  # "*" never matches ".genesis"
    ],
)
def test_wildcards_do_not_enter_the_hidden_genesis_directory(home, tool, ti):
    out, rows = _run(home, "pre", _pre(tool, ti))
    assert out.strip() == "" and rows == []


def test_the_ripgrep_ignore_turns_a_parent_search_broad(home):
    (home / ".genesis" / ".ignore").write_text("holdout/\n")
    out, rows = _run(home, "pre", _pre("Grep", {"pattern": "x", "path": GH, "glob": "*.md"}))
    assert out.strip() == ""
    assert [r["kind"] for r in rows] == ["broad"]
    out, _ = _run(home, "pre", _pre("Grep", {"pattern": "x", "path": f"{GH}/holdout"}))
    assert "HOLDOUT FENCE" in _note(out)  # pointed at it directly, ripgrep searches it


@pytest.mark.parametrize(
    "command",
    [
        "cat /tmp/copy.md; ls scripts/holdout_runner.py",
        "python3 scripts/holdout_runner.py --pr 7 && cat /tmp/copy.md",
    ],
)
def test_naming_the_runner_beside_a_read_does_not_exempt_it(home, command):
    _, rows = _run(home, "post", _post("Bash", {"stdout": EXPECT_LINE}, {"command": command}))
    assert rows and rows[0]["kind"] == "content"


def test_spawning_the_writer_is_not_logged(home):
    out, rows = _run(
        home,
        "pre",
        _pre("Agent", {"subagent_type": "holdout-writer", "prompt": "edit ~/.genesis/holdout"}),
    )
    assert out.strip() == "" and rows == []


@pytest.mark.parametrize(
    "identity",
    [{"agent_id": "a1", "agent_type": "holdout-writer"}, {"agent_type": "holdout-writer"}],
)
def test_the_writer_is_allowed_and_logged_by_path_only(home, identity):
    heredoc = f"cat >> ~/.genesis/holdout/HOLDOUT.md <<'X'\n{EXPECT_LINE}\nX"
    out, rows = _run(home, "pre", _pre("Bash", {"command": heredoc}, **identity))
    assert out.strip() == ""
    assert [r["kind"] for r in rows] == ["allowed"]
    assert EXPECT_LINE not in json.dumps(rows)
    assert rows[0]["detail"] == "~/.genesis/holdout"


def test_another_subagent_type_is_not_allowed(home):
    out, rows = _run(
        home,
        "pre",
        _pre(
            "Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}, agent_id="a1", agent_type="Explore"
        ),
    )
    assert "HOLDOUT FENCE" in _note(out)
    assert rows[0]["agent_type"] == "Explore" and rows[0]["kind"] == "read"


def test_globs_are_matched_without_walking_the_disk(home):
    """A pattern over a large tree must cost nothing: no expansion on disk."""
    started = time.monotonic()
    _run(home, "pre", _pre("Grep", {"pattern": "x", "path": "/", "glob": "**/*.zzz"}))
    _run(home, "pre", _pre("Glob", {"pattern": "/**/*.zzz"}))
    _run(home, "pre", _pre("Bash", {"command": "ls /usr/**/zzz"}))
    assert time.monotonic() - started < 10


@pytest.mark.parametrize(
    "response",
    [{"stdout": f"x\n{EXPECT_LINE}\n"}, f"{EXPECT_LINE}", [{"type": "text", "text": EXPECT_LINE}]],
)
def test_expected_output_in_a_result_is_logged_and_advised(home, response):
    out, rows = _run(home, "post", _post("Bash", response))
    note = _note(out)
    assert "HOLDOUT FENCE" in note and "H-01" in note
    assert rows[0]["kind"] == "content" and rows[0]["detail"] == "H-01"
    assert EXPECT_LINE not in json.dumps(rows)


def test_an_mcp_result_is_scanned(home):
    _, rows = _run(
        home,
        "post",
        _post("mcp__genesis-memory__memory_recall", json.dumps({"result": EXPECT_LINE})),
    )
    assert rows and rows[0]["kind"] == "content"


def test_whitespace_changes_do_not_hide_expected_output(home):
    mangled = EXPECT_LINE.replace(" ", "   \n  ")
    _, rows = _run(home, "post", _post("Read", {"file": {"content": mangled}}))
    assert rows and rows[0]["kind"] == "content"


def test_a_step_line_shared_with_repository_code_is_not_a_leak(home):
    _, rows = _run(home, "post", _post("Read", {"content": f"{STEP_LINE}\nmore code"}))
    assert rows == []


def test_a_listing_naming_the_directory_is_logged(home):
    holdout = str(home / ".genesis" / "holdout")
    _, rows = _run(home, "post", _post("Bash", {"stdout": f"{holdout}/HOLDOUT.md"}))
    assert rows[0]["detail"] == "directory path"


def _exposures(home: Path) -> list[dict]:
    return _log_rows(home / ".genesis" / "logs" / "holdout-exposures.jsonl")


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        (f"du -sh {GH}/*", "listing"),
        (f"ls -d {GH}/holdout", "listing"),
        (f"stat {GH}/holdout", "listing"),
        (f"echo {GH}/holdout", "mention"),
        (f"printf '%s\\n' {GH}/holdout", "mention"),
        (f"du -sh {GH}/* | sort -h", "listing"),
        (f"du -h --max-depth=1 {GH}", "broad"),
        (f"find {GH} -maxdepth 1", "broad"),
        (f"ls -d {GH}/holdout; find {GH} -maxdepth 1 -newermt 2026-11-01", "listing"),
        (f"cd {GH} && du -sh *", "listing"),
        (f"cd {GH}/holdout && ls -la && pwd", "listing"),
        (f"du -sh {GH}/* | LC_ALL=C sort -h", "listing"),
        (f"du -sh {GH}/* 2>/dev/null | sort -h", "listing"),
        (f"ls -d {GH}/holdout 2>&1", "listing"),
        (f"ls -d {GH}/holdout &>/dev/null; echo {GH}/holdout", "listing"),
        (f'ls -d {GH}/holdout 2>"/dev/null"', "listing"),
        (f"ls -d {GH}/holdout 2>&-", "listing"),
        (f"/bin/ls -d {GH}/holdout", "listing"),
        (f"/usr/bin/du -sh {GH}/*", "listing"),
        (f'"/bin/ls" -d {GH}/holdout', "listing"),
        (f"ls -d {GH}/holdout >/dev/stderr; echo {GH}/holdout", "listing"),
        (f"ls -d {GH}/{{logs,holdout,config}}", "listing"),
        (f"ls -d {GH}/holdout; printf '%s,-%s\\n' a b", "listing"),
        (f"find {GH} -maxdepth 1 -printf '%p -> %l\\n'", "broad"),
        ('echo "$GENESIS_HOME/holdout"', "mention"),
        ("echo ${GENESIS_HOME}/holdout; ls -d $HOME/.genesis/holdout", "listing"),
        (f"du -sh --exclude='*.pyc' {GH}/*", "listing"),
        (f"ls -d {GH}/holdout | cut -d/ -f1-4", "listing"),
        (f"find {GH} -maxdepth 2 | sort -t/ -k3", "broad"),
        (f"ls --full-time -d {GH}/holdout", "listing"),
        (f"find {GH} -maxdepth 2 -name '*.md'", "broad"),
        (f"du -sh {GH}/* | sort -h | tail -n 5", "listing"),
        (f"ls -d {GH}/holdout | cut -d / -f 1-6 | tr a-z A-Z", "listing"),
        (f"echo checking; ls -d {GH}/holdout", "listing"),
    ],
)
def test_a_path_printed_by_a_names_only_command_is_activity(home, command, kind):
    holdout = str(home / ".genesis" / "holdout")
    out, rows = _run(
        home, "post", _post("Bash", {"stdout": f"4.0K\t{holdout}\n"}, {"command": command})
    )
    assert _note(out) == "" and _exposures(home) == []
    assert [(r["event"], r["kind"], r["detail"]) for r in rows] == [
        ("post", kind, "directory path")
    ]


@pytest.mark.parametrize(
    "command",
    [
        f"grep -rn needle {GH}",
        f"find {GH} -maxdepth 1 -exec head -3 {{}} +",
        f"ls -d {GH}/holdout; echo x >> {GH}/holdout/HOLDOUT.md",
        f"ls -d {GH}/holdout; grep -rl needle {GH}",
        # An abbreviated long option, or an option word the shell rewrites.
        f"ls -d {GH}/holdout; du --files0={GH}/holdout/HOLDOUT.md",
        f"ls -d {GH}/holdout; du --fi={GH}/holdout/HOLDOUT.md",
        f"ls -d {GH}/holdout | wc --files0={GH}/holdout/HOLDOUT.md",
        f"cd {GH}; du --files0=holdout/HOLDOUT.md; ls -d {GH}/holdout",
        f"ls -d {GH}/holdout; du -{{-files0-from={GH}/holdout/HOLDOUT.md,s}}",
        f"ls -d {GH}/holdout; du -s${{IFS}}--files0-from={GH}/holdout/HOLDOUT.md",
        f"ls -d {GH}/holdout; ls | head -v${{IFS}}{GH}/holdout/HOLDOUT.md",
        f"ls -d {GH}/holdout; cd {GH}/holdout; ls | head -q${{IFS}}HOLDOUT.md",
        f"ls -d {GH}/holdout; cd {GH}/holdout; ls | head -n4${{IFS}}HOLDOUT.md",
        # Names-only in shape, but nothing shows where the path came from.
        'printf "%s\\n" "$X"',
        'echo "$SOMEWHERE"',
        f"ls {GH}/holdout; grep -r needle {GH}",
        f"cat {GH}/holdout/HOLDOUT.md",
        'cat "$D/HOLDOUT.md"',
        # A path the operand matcher cannot see: piped to xargs, built by eval,
        # read by substitution, a redirect or a file-reading option.
        f"find {GH}/holdout -type f | xargs head -5",
        f"find {GH}/holdout -type f -print0 | xargs -0 tail -n +1",
        f"find {GH}/holdout -type f | xargs grep -H Protects",
        f"ls -1 {GH}/holdout/*.md | xargs -n1 -I{{}} sh -c 'echo {{}}; cat {{}}'",
        f"tree --fromfile {GH}/holdout/HOLDOUT.md",
        f"echo {GH}/holdout $(< {GH}/holdout/HOLDOUT.md)",
        f'echo {GH}/holdout; eval "head -5 {GH}/holdout/HOLDOUT.md"',
        f"echo {GH}/holdout/HOLDOUT.md | xargs -t head -5",
        f"git diff /dev/null {GH}/holdout/HOLDOUT.md",
        f"du --files0-from={GH}/holdout/HOLDOUT.md; echo {GH}/holdout",
        f'echo {GH}/holdout; sort < "$F"',
        f"env X=1 ls -d {GH}/holdout",
        f"ls -d {GH}/holdout; head -n 3 notes.txt",
        f"ls -d {GH}/holdout; head -n 3 -- notes.txt",
        f"ls -d {GH}/holdout; head -- -notes",
        # An option that writes, reads or runs something, in any spelling.
        f"du -sh {GH}/* | sort -o{GH}/holdout/new.txt && ls -d {GH}/holdout",
        f"du -sh {GH}/* | sort --output={GH}/holdout/new.txt && ls -d {GH}/holdout",
        f"cd {GH}/holdout; ls | sort -ronew.txt; ls -d {GH}/holdout",
        f"du --exclude-from={GH}/holdout/HOLDOUT.md {GH}",
        f"du --exclude-from {GH}/holdout/HOLDOUT.md {GH}",
        f"du -shX {GH}/holdout/HOLDOUT.md {GH}",
        f"du -sh {GH}/* | sort -R --random-source={GH}/holdout/HOLDOUT.md",
        f"du -sh {GH}/* | sort --compress-program=/bin/cat",
        f"du -sh {GH}/* | sort -T {GH}/holdout",
        f"find -files0-from {GH}/holdout/HOLDOUT.md",
        f"fd --ignore-file {GH}/holdout/HOLDOUT.md . {GH}",
        f"fd . {GH}/holdout -x cat",
        f"cd {GH}/holdout; du --files0=HOLDOUT.md; ls -d {GH}/holdout",
        f"ls -d {GH}/holdout; ls --hide=/x {GH}",
        f"PATH=/tmp/x ls -d {GH}/holdout",
        # A redirect to a file, wherever it points: its target cannot be resolved.
        f"cd {GH}/holdout; printf 'new' > HOLDOUT.md; ls -d {GH}/holdout",
        f"cd {GH} && echo x >> holdout/HOLDOUT.md && find {GH} -maxdepth 1 -type d",
        f"ls -d {GH}/holdout; echo x > link/HOLDOUT.md",
        f"ls -d {GH}/holdout; echo x >& out.txt",
        f"ls -d {GH}/holdout; echo a#b > out.txt",
        f"ls -d {GH}/holdout >| out.txt; ls -d {GH}/holdout",
        f"du -sh {GH}/* | sort -h > sizes.txt; ls -d {GH}/holdout",
        f"ls -d {GH}/holdout 2> errors.txt",
        # An option or a path that only an expansion or a brace produces.
        f"find {GH}/holdout {{-mindepth,1,-delete}}",
        f"du {{-s,-X,HOLDOUT.md}} {GH}/holdout",
        f"ls -d {GH}/holdout; find {GH}/holdout -mindepth 1 {{,}}-delete",
        f"cd {GH}/holdout; du -s {{,}}--exclude-from=HOLDOUT.md .; ls -d {GH}/holdout",
        f"ls -d {GH}/holdout; find {GH}/holdout -maxdepth 0 {{,}}''-delete",
        f"ls -d {GH}/holdout; echo -delete; find {GH}/holdout -mindepth 1 $_",
        f"echo $USER; du -sh {GH}/*",
        f"ls -d {GH}/holdout; ls -d $PWD",
        # A command given by a path outside the system directories.
        f"./du; ls -d {GH}/holdout",
        f"/tmp/x/ls -d {GH}/holdout",
        # An expansion that evaluates text: only a plain $NAME or ${NAME} is allowed.
        f"printf -v X '%b' '\\x24(head -5 f)'; echo {GH}/holdout \"${{X@P}}\"",
        f"echo {GH}/holdout $[b]",
        f"echo {GH}/holdout ${{a[b]}}",
        f"echo {GH}/holdout ${{HOME:b}}",
        f"printf -v 'a[b]' x; echo {GH}/holdout",
        f"ls -d {GH}/holdout; sort -k 2 notes.txt",
        f"ls -d {GH}/holdout; sort -m - notes.txt",
    ],
)
def test_a_path_from_any_other_call_stays_an_exposure(home, command):
    holdout = str(home / ".genesis" / "holdout")
    stdout = f"{holdout}/HOLDOUT.md:Protects: a short line\n"
    out, rows = _run(home, "post", _post("Bash", {"stdout": stdout}, {"command": command}))
    assert "HOLDOUT FENCE" in _note(out)
    assert [(r["kind"], r["detail"]) for r in _exposures(home)] == [("content", "directory path")]


@pytest.mark.parametrize(
    ("tool", "ti"),
    [
        ("mcp__genesis-memory__memory_recall", {"query": "x"}),
        ("mcp__some-server__run", {"command": f"ls -d {GH}/holdout"}),
        ("Agent", {"subagent_type": "Explore", "prompt": f"ls {GH}/holdout"}),
    ],
)
def test_a_path_in_a_result_this_hook_cannot_classify_stays_an_exposure(home, tool, ti):
    holdout = str(home / ".genesis" / "holdout")
    _run(home, "post", _post(tool, f"see {holdout}/HOLDOUT.md", ti))
    assert [r["kind"] for r in _exposures(home)] == ["content"]


def test_expected_output_from_a_listing_command_is_still_content(home):
    holdout = str(home / ".genesis" / "holdout")
    out, _ = _run(
        home,
        "post",
        _post("Bash", {"stdout": f"{holdout}\n{EXPECT_LINE}\n"}, {"command": f"ls {GH}/holdout"}),
    )
    assert "H-01" in _note(out)
    assert [(r["kind"], r["detail"]) for r in _exposures(home)] == [("content", "H-01")]


def _listing(home: Path, stdout: str) -> tuple[str, list[dict]]:
    return _run(
        home, "post", _post("Bash", {"stdout": stdout}, {"command": f"ls -la {GH}/holdout"})
    )


def test_a_line_of_a_holdout_file_in_a_listing_result_is_content(home):
    """The result is checked itself, whatever the command looked like."""
    holdout = str(home / ".genesis" / "holdout")
    out, _ = _listing(home, f"{holdout}\nProtects:   something\n")
    assert "HOLDOUT FENCE" in _note(out)
    assert [(r["kind"], r["detail"]) for r in _exposures(home)] == [("content", "directory path")]


def test_a_very_short_line_of_a_holdout_file_is_not_its_text(home):
    holdout = str(home / ".genesis" / "holdout")
    out, rows = _listing(home, f"{holdout}\n```sh\n")
    assert _note(out) == "" and _exposures(home) == []
    assert [r["kind"] for r in rows] == ["listing"]


@pytest.mark.parametrize("extra", ["subdirectory", "symlink"])
def test_holdout_content_the_hook_cannot_vouch_for_keeps_the_exposure(home, extra):
    holdout = home / ".genesis" / "holdout"
    if extra == "subdirectory":
        (holdout / "more").mkdir()
    else:
        (holdout / "link.md").symlink_to(home / "elsewhere.md")
    _listing(home, f"{holdout}\n")
    assert [r["kind"] for r in _exposures(home)] == ["content"]


def test_every_holdout_file_is_checked_with_whitespace_collapsed(home):
    holdout = home / ".genesis" / "holdout"
    (holdout / "notes.md").write_text("  a second   holdout\tfile line\n")
    _listing(home, f"{holdout}\na second holdout file line\n")
    _listing(home, f"{holdout}\nProtects: something\n")
    assert [r["kind"] for r in _exposures(home)] == ["content", "content"]


def test_a_result_cut_at_the_scan_limit_keeps_the_exposure(home):
    holdout = str(home / ".genesis" / "holdout")
    _listing(home, f"{holdout}\n" + "x" * (2 * 1024 * 1024))
    assert [r["kind"] for r in _exposures(home)] == ["content"]


def _real(home: Path, command: str) -> tuple[str, list[dict]]:
    """Run ``command`` for real in the synthetic home and give its output to the hook."""
    done = subprocess.run(
        ["bash", "-c", command], env=_env(home), capture_output=True, text=True, timeout=60
    )
    response = {"stdout": done.stdout, "stderr": done.stderr, "interrupted": False}
    return _run(home, "post", _post("Bash", response, {"command": command}, cwd=str(home)))


@pytest.mark.parametrize(
    "command",
    [
        "du -sh ~/.genesis/*",
        "du -ah ~/.genesis | sort -h | tail -n 5",
        "find ~/.genesis -maxdepth 1",
        "ls -la ~/.genesis/holdout; stat -c '%n %s' ~/.genesis/holdout/HOLDOUT.md",
    ],
)
def test_real_listings_are_activity(home, command):
    out, rows = _real(home, command)
    assert _note(out) == "" and _exposures(home) == []
    assert rows and all(r["detail"] == "directory path" for r in rows)


@pytest.mark.parametrize(
    "command",
    [
        "find ~/.genesis/holdout -type f | xargs grep -H Protects",
        "ls -d ~/.genesis/holdout; grep -rH Protects ~/.genesis",
        "ls -d ~/.genesis/holdout; head -3 ~/.genesis/holdout/HOLDOUT.md",
    ],
)
def test_real_reads_that_print_the_path_are_content(home, command):
    out, _ = _real(home, command)
    assert "HOLDOUT FENCE" in _note(out)
    assert [r["kind"] for r in _exposures(home)] == ["content"]


def test_a_real_write_through_a_relative_redirect_is_content(home):
    """The file is overwritten and only its directory's path is printed."""
    scenario = home / ".genesis" / "holdout" / "HOLDOUT.md"
    out, _ = _real(
        home, "cd ~/.genesis/holdout; printf 'new\\n' > HOLDOUT.md; ls -d ~/.genesis/holdout"
    )
    assert scenario.read_text() == "new\n"
    assert "HOLDOUT FENCE" in _note(out)
    assert [r["kind"] for r in _exposures(home)] == ["content"]


def test_find_writing_a_file_is_an_access_before_it_runs(home):
    out, rows = _run(home, "pre", _pre("Bash", {"command": f"find {GH}/holdout -fprintf out %p"}))
    assert "HOLDOUT FENCE" in _note(out) and rows[0]["kind"] == "access"


def test_a_sibling_directory_name_is_not_the_holdout(home):
    _, rows = _run(home, "post", _post("Bash", {"stdout": str(home / ".genesis" / "holdout-old")}))
    assert rows == []


@pytest.mark.parametrize(
    "command",
    [
        ".venv/bin/python scripts/holdout_runner.py --pr 7",
        "tail ~/.genesis/logs/holdout-exposures.jsonl",
        "tail ~/.genesis/logs/holdout-fence-activity.jsonl",
    ],
)
def test_the_runner_and_the_log_are_not_scanned(home, command):
    holdout = str(home / ".genesis" / "holdout")
    _, rows = _run(
        home, "post", _post("Bash", {"stdout": f"{holdout} {EXPECT_LINE}"}, {"command": command})
    )
    assert rows == []


def test_the_protects_line_is_not_a_fingerprint(home):
    _, rows = _run(home, "post", _post("Bash", {"stdout": "H-01 PASS — something"}))
    assert rows == []


def test_the_writer_sees_content_without_a_record(home):
    out, rows = _run(
        home, "post", _post("Read", {"content": EXPECT_LINE}, agent_type="holdout-writer")
    )
    assert out.strip() == "" and rows == []


def test_no_holdout_directory_is_a_silent_no_op(tmp_path):
    out, rows = _run(tmp_path, "pre", _pre("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}))
    assert out.strip() == "" and rows == []
    assert not (tmp_path / ".genesis").exists()


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[1, 2]",
        '{"tool_input": "x"}',
        '{"tool_name": "Bash", "tool_input": {"command": "cat \'unclosed"}}',
    ],
)
def test_malformed_payloads_are_silent(home, stdin):
    proc = subprocess.run(
        [sys.executable, str(HOOK), "pre"],
        input=stdin,
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_unknown_event_is_silent(home):
    out, rows = _run(home, "sideways", _pre("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}))
    assert out.strip() == "" and rows == []


def test_activity_rotates_and_exposures_never_do(home):
    logs = home / ".genesis" / "logs"
    logs.mkdir(parents=True)
    big = "x" * (5 * 1024 * 1024 + 10) + "\n"
    (logs / "holdout-fence-activity.jsonl").write_text(big)
    (logs / "holdout-exposures.jsonl").write_text(big)
    for payload in (
        _pre("Bash", {"command": "echo ~/.genesis/holdout"}),  # a mention
        _pre("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}),  # an exposure
    ):
        subprocess.run(
            [sys.executable, str(HOOK), "pre"],
            input=json.dumps(payload),
            env=_env(home),
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert (logs / "holdout-fence-activity.jsonl.1").stat().st_size > 5 * 1024 * 1024
    assert [
        json.loads(x)["kind"]
        for x in (logs / "holdout-fence-activity.jsonl").read_text().splitlines()
    ] == ["mention"]
    assert not (logs / "holdout-exposures.jsonl.1").exists()
    assert (
        (logs / "holdout-exposures.jsonl")
        .read_text()
        .endswith(
            '"read", "session_id": "s1", "agent_id": "", "agent_type": "", "tool": "Read", "detail": "~/.genesis/holdout/HOLDOUT.md"}\n'
        )
    )


def test_exposures_and_activity_go_to_separate_logs(home):
    _run(home, "pre", _pre("Bash", {"command": "echo ~/.genesis/holdout"}))
    _run(home, "pre", _pre("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}))
    logs = home / ".genesis" / "logs"
    assert [r["kind"] for r in _log_rows(logs / "holdout-exposures.jsonl")] == ["read"]
    assert [r["kind"] for r in _log_rows(logs / "holdout-fence-activity.jsonl")] == ["mention"]


def test_concurrent_writers_keep_every_row(home):
    payload = json.dumps(_pre("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}))
    procs = [
        subprocess.Popen(
            [sys.executable, str(HOOK), "pre"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            env=_env(home),
        )
        for _ in range(8)
    ]
    for p in procs:
        p.communicate(payload.encode(), timeout=30)
    assert len(_rows(home)) == 8


def test_a_failed_log_write_still_gives_the_note(home):
    (home / ".genesis" / "logs").write_text("a file where the log directory should be")
    out, _ = _run(home, "pre", _pre("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}))
    assert "HOLDOUT FENCE" in _note(out)


def test_long_details_state_the_omission(home):
    cmd = f"cat {GH}/holdout/HOLDOUT.md " + "x" * 400
    _, rows = _run(home, "pre", _pre("Bash", {"command": cmd}))
    assert rows[0]["detail"].endswith("more chars omitted]")


def _wired(event: str) -> str:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    key = "PreToolUse" if event == "pre" else "PostToolUse"
    return next(
        h["command"]
        for m in settings["hooks"][key]
        for h in m["hooks"]
        if "holdout_fence.py" in h["command"]
    )


@pytest.mark.parametrize("event", ["pre", "post"])
def test_the_wired_command_starts_nothing_without_a_holdout(tmp_path, event):
    """The shell check exits before the launcher: here the launcher does not even exist."""
    env = {**_env(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path / "no-such-checkout")}
    proc = subprocess.run(
        ["sh", "-c", _wired(event)], input="{}", env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


@pytest.mark.parametrize("event", ["pre", "post"])
def test_the_wired_command_reaches_the_launcher_with_a_holdout(home, event):
    env = {**_env(home), "CLAUDE_PROJECT_DIR": str(home / "no-such-checkout")}
    proc = subprocess.run(
        ["sh", "-c", _wired(event)], input="{}", env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode != 0  # it tried to start the (missing) launcher


def test_mcp_results_are_wired():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    matcher = next(
        m["matcher"]
        for m in settings["hooks"]["PostToolUse"]
        if any("holdout_fence.py" in h["command"] for h in m["hooks"])
    )
    assert "mcp__.*" in matcher.split("|")


def test_a_command_the_parser_cannot_read_still_counts_if_it_names_the_holdout(home):
    """An unbalanced quote is a parser blind spot; naming the holdout then counts."""
    command = "cat ~/.genesis/holdout/HOLDOUT.md " + "'" + "unclosed"
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert "HOLDOUT FENCE" in _note(out)
    assert [r["kind"] for r in rows] == ["access"]


def test_writer_rows_are_kept_in_the_exposure_log(home):
    _run(
        home,
        "pre",
        _pre("Read", {"file_path": f"{GH}/holdout/HOLDOUT.md"}, agent_type="holdout-writer"),
    )
    logs = home / ".genesis" / "logs"
    assert [r["kind"] for r in _log_rows(logs / "holdout-exposures.jsonl")] == ["allowed"]


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'cd ~/.genesis && cp holdout/HOLDOUT.md /tmp/x'",
        "sh -c 'cd ~/.genesis; cat holdout/HOLDOUT.md > /dev/null'",
        "find ~/.genesis/holdout -name '*.md' -delete",
        "find ~/.genesis/holdout/HOLDOUT.md -exec cat {} +",
        "rm -rf ~/.genesis",
        "touch ~/.genesis/holdout/HOLDOUT.md",
        "dd if=~/.genesis/holdout/HOLDOUT.md of=/tmp/x",
        "python3 -c \"print(open('~/.genesis/holdout/HOLDOUT.md').read())\"",
        'cat "$GENESIS_HOME/holdout/HOLDOUT.md"',
        "echo $((1 << N))\ncat ~/.genesis/holdout/HOLDOUT.md",
    ],
)
def test_review_round_one_shapes_are_exposures(home, command):
    payload = _pre("Bash", {"command": command})
    out, rows = _run(home, "pre", payload)
    assert "HOLDOUT FENCE" in _note(out), command
    assert [r["kind"] for r in rows] == ["access"]


def test_an_unexported_genesis_home_spelling_still_resolves(home):
    env = {**_env(home)}
    env.pop("GENESIS_HOME")
    payload = _pre("Bash", {"command": 'cat "$GENESIS_HOME/holdout/HOLDOUT.md"'})
    proc = subprocess.run(
        [sys.executable, str(HOOK), "pre"],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "HOLDOUT FENCE" in _note(proc.stdout)


@pytest.mark.parametrize(
    "text",
    [
        "kept in ``$GENESIS_HOME/holdout/`` (default ``~/.genesis/holdout``)",
        "Never open, list, search or quote `~/.genesis/holdout/`. Run the runner.",
    ],
)
def test_reading_the_holdout_docs_or_this_hook_is_not_an_exposure(home, text):
    _, rows = _run(
        home,
        "post",
        _post("Read", {"content": text}, {"file_path": "/repo/docs/reference/holdout.md"}),
    )
    assert rows == []


def test_a_single_interpreter_segment_is_not_exempt(home):
    _, rows = _run(
        home, "post", _post("Bash", {"stdout": EXPECT_LINE}, {"command": "python3 -c 'print(1)'"})
    )
    assert rows and rows[0]["kind"] == "content"


def test_the_log_exemption_uses_the_payload_cwd(home):
    logs = home / ".genesis" / "logs"
    payload = _post(
        "Read",
        {"content": str(home / ".genesis" / "holdout")},
        {"file_path": "holdout-exposures.jsonl"},
    )
    payload["cwd"] = str(logs)
    _, rows = _run(home, "post", payload)
    assert rows == []


@pytest.mark.parametrize(
    "command",
    [
        "cd ~/.genesis/holdout && rg approval_gate",
        "cd ~/.genesis/holdout && grep -rn approval_gate",
        "cd ~/.genesis/holdout && find . -name '*.md' -exec cat {} +",
        "rg -f ~/.genesis/holdout/HOLDOUT.md src/",
    ],
)
def test_searches_of_the_current_directory_and_pattern_files_are_exposures(home, command):
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert "HOLDOUT FENCE" in _note(out), command
    assert [r["kind"] for r in rows] == ["access"]


@pytest.mark.parametrize(
    "command",
    [
        "rg -e '~/.genesis/holdout' docs/",
        "rg --regexp='~/.genesis/holdout' docs/",
        "grep -rn -e '~/.genesis/holdout' docs/",
        "grep -E 'holdout|~/.genesis/holdout' docs/x.md",
    ],
)
def test_a_pattern_given_by_option_is_a_mention(home, command):
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert out.strip() == "", command
    assert [r["kind"] for r in rows] == ["mention"]


def test_a_json_encoded_mcp_result_is_decoded_before_matching(home):
    quoted = 'print("the exact expected output line that is well over forty characters")'
    md = home / ".genesis" / "holdout" / "HOLDOUT.md"
    md.write_text(md.read_text().replace(EXPECT_LINE, quoted))
    response = json.dumps({"result": quoted})  # escaped quotes in the serialized string
    _, rows = _run(home, "post", _post("mcp__genesis-memory__memory_recall", response))
    assert rows and rows[0]["kind"] == "content"


@pytest.mark.parametrize(
    "command", ["rg -f /tmp/patterns ~/.genesis/holdout", "grep -f /tmp/p -r ~/.genesis/holdout"]
)
def test_with_a_pattern_file_every_positional_is_a_path(home, command):
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert "HOLDOUT FENCE" in _note(out), command
    assert [r["kind"] for r in rows] == ["access"]


@pytest.mark.parametrize(
    "command",
    [
        "echo 'write it with cat > ~/.genesis/holdout/HOLDOUT.md'",
        "gh pr comment 1 --body 'Avoid cat > ~/.genesis/holdout/HOLDOUT.md'",
    ],
)
def test_redirect_syntax_inside_quotes_is_a_mention(home, command):
    out, rows = _run(home, "pre", _pre("Bash", {"command": command}))
    assert out.strip() == "", command
    assert [r["kind"] for r in rows] == ["mention"]


def test_an_expected_line_that_is_json_still_matches(home):
    json_line = (
        '{"status": "denied", "reason": "this approval requires an explicit owner decision"}'
    )
    md = home / ".genesis" / "holdout" / "HOLDOUT.md"
    md.write_text(md.read_text().replace(EXPECT_LINE, json_line))
    _, rows = _run(home, "post", _post("Bash", {"stdout": json_line}))
    assert rows and rows[0]["kind"] == "content"
