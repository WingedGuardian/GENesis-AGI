"""genesis-secret-redact's publish guard: a `gh` command carrying a known secrets.env value is refused.

Canary values only; the guard runs as the real hook process with SECRETS_PATH pointed at a scratch file.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "plugins" / "genesis-secret-redact" / "bin" / "publish_guard.py"
KEY = "ck_" + "9a8b7c6d5e4f30211203f4e5d6c7b8a9"


@pytest.fixture
def env(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"CANARY_API_KEY={KEY}\n")
    return {**os.environ, "SECRETS_PATH": str(secrets)}


def _decide(command: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, "-I", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _denied(run: subprocess.CompletedProcess) -> bool:
    if not run.stdout.strip():
        return False
    return json.loads(run.stdout)["hookSpecificOutput"].get("permissionDecision") == "deny"


def _output(run: subprocess.CompletedProcess) -> dict:
    return json.loads(run.stdout)["hookSpecificOutput"] if run.stdout.strip() else {}


@pytest.mark.parametrize(
    "command",
    [
        f'gh pr create --title t --body "uses {KEY}"',
        f"gh pr comment 12 --body 'key={KEY}'",
        f"GH_REPO=o/r gh issue create -t x -b {KEY}",
        f"/usr/local/bin/gh pr edit 3 --body {KEY}",
        f"echo $(gh api repos/o/r/issues/1/comments -f body={KEY})",
        f"cd /tmp && gh pr review 4 --comment -b {KEY}",
    ],
)
def test_a_value_in_the_command_is_refused(env, tmp_path, command):
    run = _decide(command, env, tmp_path)
    assert run.returncode == 0 and _denied(run)
    reason = json.loads(run.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "CANARY_API_KEY" in reason and KEY not in reason


@pytest.mark.parametrize(
    "spelling", ["--body-file {f}", "-F {f}", "--body-file={f}", "-f body=@{f}", "-F body=@{f}"]
)
def test_a_value_in_a_named_file_is_refused(env, tmp_path, spelling):
    body = tmp_path / "body.md"
    body.write_text(f"Summary\n\nthe key is {KEY}\n")
    run = _decide("gh pr create --title t " + spelling.format(f="body.md"), env, tmp_path)
    assert _denied(run)
    assert "body.md" in json.loads(run.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        f"gh secret set CANARY_API_KEY --body {KEY}",
        f"echo {KEY} | gh auth login --with-token",
        f"git commit -m 'note {KEY}'",
        "gh pr create --title t --body 'nothing secret here'",
        "ghost pr create --body x",
    ],
)
def test_other_commands_pass(env, tmp_path, command):
    assert not _denied(_decide(command, env, tmp_path))


def test_a_file_without_values_passes(env, tmp_path):
    (tmp_path / "body.md").write_text("all clean\n")
    assert not _denied(_decide("gh pr create --title t --body-file body.md", env, tmp_path))


def test_no_secrets_file_passes(tmp_path):
    env = {**os.environ, "SECRETS_PATH": str(tmp_path / "absent.env")}
    assert not _denied(_decide(f"gh pr comment 1 --body {KEY}", env, tmp_path))


def test_an_unloadable_resolver_fails_open_and_says_so(tmp_path):
    env = {**os.environ, "SECRETS_PATH": str(tmp_path / "secrets.env"), "GENESIS_REPO_ROOT": ""}
    (tmp_path / "secrets.env").write_text(f"CANARY_API_KEY={KEY}\n")
    bad = tmp_path / "plugin-copy"
    (bad / "bin").mkdir(parents=True)
    (bad / "bin" / "publish_guard.py").write_text(GUARD.read_text())
    (bad / "bin" / "known_values.py").write_text("raise RuntimeError('broken loader')\n")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh pr comment 1 --body {KEY}"},
        "cwd": str(tmp_path),
    }
    run = subprocess.run(
        [sys.executable, "-I", str(bad / "bin" / "publish_guard.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert run.returncode == 0 and run.stdout == ""
    assert "did not run" in run.stderr


def test_the_plugin_wires_the_guard_on_bash():
    hooks = json.loads((GUARD.parent.parent / "hooks" / "hooks.json").read_text())
    commands = [
        h["command"]
        for e in hooks["hooks"]["PreToolUse"]
        if e["matcher"] == "^(Monitor|Bash)$"
        for h in e["hooks"]
    ]
    assert any("bin/publish_guard.py" in c for c in commands)


@pytest.mark.parametrize(
    "wrapper",
    [
        "timeout 60 gh",
        "env gh",
        "command gh",
        "sudo gh",
        "time gh",
        "nice gh",
        "xargs gh",
        "if true; then gh",
        "bash -c 'gh",
        'bash -c "gh',
        "! gh",
        "{ gh",
    ],
)
def test_a_wrapped_gh_is_still_checked(env, tmp_path, wrapper):
    # Narrowing the gate to segment starts let 11 of these 12 through (measured in review).
    assert _denied(_decide(f"{wrapper} pr comment 1 --body {KEY}", env, tmp_path))


def test_a_file_named_after_a_cd_is_found(env, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b2.md").write_text(f"key {KEY}\n")
    assert _denied(_decide("cd sub && gh pr comment 1 --body-file b2.md", env, tmp_path))


def test_monitor_gh_is_checked(env, tmp_path):
    payload = {
        "tool_name": "Monitor",
        "tool_input": {"command": f"gh pr comment 1 --body {KEY}"},
        "cwd": str(tmp_path),
    }
    run = subprocess.run(
        [sys.executable, "-I", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert _denied(run)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'gh pr comment 1 --body-file body.md'",
        "sh -c \"bash -c 'gh issue comment 2 --body-file body.md'\"",
        "timeout 60 bash -lc 'cd . && gh pr create --title t -F body=@body.md'",
        'gh pr comment 1 --body "$(cat body.md)"',
        'gh pr comment 1 --body "$(<body.md)"',
    ],
)
def test_a_file_named_inside_a_wrapped_or_substituted_command_is_checked(env, tmp_path, command):
    (tmp_path / "body.md").write_text(f"the key is {KEY}\n")
    assert _denied(_decide(command, env, tmp_path))


# --- Review rounds 1 and 2: each finding, as the spelling that reached gh unchecked. ---


def _body(tmp_path, name="body.md", where=None):
    folder = tmp_path / where if where else tmp_path
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(f"the key is {KEY}\n")


def test_a_quoted_gh_executable_is_checked(env, tmp_path):
    assert _denied(_decide(f'"gh" pr comment 1 --body {KEY}', env, tmp_path))


def test_a_redirect_inside_a_substitution_is_followed(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide('gh pr comment 1 --body "$(cat<body.md)"', env, tmp_path))


def test_a_quoted_cd_target_with_a_space_is_followed(env, tmp_path):
    _body(tmp_path, where="draft notes")
    run = _decide('cd "draft notes" && gh pr comment 1 --body-file body.md', env, tmp_path)
    assert _denied(run)


def test_relative_cds_are_cumulative(env, tmp_path):
    _body(tmp_path, where="a/b")
    assert _denied(_decide("cd a && cd b && gh pr comment 1 --body-file body.md", env, tmp_path))


def test_an_attached_short_option_value_is_a_path(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide("gh pr comment 1 -Fbody.md", env, tmp_path))


@pytest.mark.parametrize("repo", ["--repo o/r", "-R o/r", "--repo=o/r"])
def test_global_options_before_an_exempt_group_keep_it_exempt(env, tmp_path, repo):
    assert not _denied(_decide(f"gh {repo} secret set K --body {KEY}", env, tmp_path))
    assert _denied(_decide(f"gh {repo} pr comment 1 --body {KEY}", env, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        f"gh pr comment 1 --body '~~deprecated~~ token: {KEY}'",
        f"gh pr comment 1 --body '~~~\nfenced\n~~~ {KEY}'",
        f"gh pr comment 1 -F ~nosuchuser/x --body {KEY}",
    ],
)
def test_a_value_in_the_text_survives_a_tilde_word(env, tmp_path, command):
    assert _denied(_decide(command, env, tmp_path))


def test_a_file_past_the_old_cap_is_scanned(env, tmp_path):
    (tmp_path / "big.md").write_text("z" * (2 * 1024 * 1024) + KEY)
    assert _denied(_decide("gh pr comment 1 --body-file big.md", env, tmp_path))


def test_a_file_over_the_cap_is_allowed_with_a_note_to_the_agent(env, tmp_path):
    (tmp_path / "huge.txt").write_text("z" * (17 * 1024 * 1024) + KEY)
    run = _decide("gh release upload v1 huge.txt", env, tmp_path)
    assert not _denied(run)
    note = _output(run)["additionalContext"]
    assert "huge.txt" in note and "16 MiB" in note and KEY not in note


def test_a_value_in_a_path_is_not_repeated_in_the_refusal(env, tmp_path):
    where = tmp_path / (KEY + "dir")
    where.mkdir()
    (where / "p.md").write_text(f"key {KEY}\n")
    run = _decide("gh pr comment 1 -F p.md", env, where)
    assert _denied(run)
    reason = _output(run)["permissionDecisionReason"]
    assert KEY not in reason and "[secrets.env:CANARY_API_KEY]" in reason


# --- A command the parser cannot prove it read is refused (owner ruling 2026-11-13). ---


def test_a_line_continuation_is_refused_with_the_parsers_reason(env, tmp_path):
    run = _decide("gh pr comment 1 \\\n  --body x", env, tmp_path)
    assert _denied(run)
    reason = _output(run)["permissionDecisionReason"]
    assert "cannot read all of it" in reason and "continu" in reason and "--body-file" in reason
    assert not _denied(_decide("gh pr comment 1 --body x", env, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "cat > notes.md <<EOF\nit's here\nEOF\ngh pr create --title t -F notes.md",
        "echo $'\\x41' && gh issue comment 1 --body x",
        "gh api repos/o/r/issues/1/comments \\\n  -f body=x",
        "gh -R o/r release upload v1 a.zip \\\n  b.zip",
        "gh gist create a.md \\\n  b.md",
        "gh pr view 1 && gh pr merge 1 \\\n  --squash",
        # Uncertain counts as publishing: a verb the text cannot show, an unknown group, a variable.
        'gh "$VERB" 1 \\\n  --body x',
        "gh label create x \\\n  --description y",
        "GH=gh; $GH pr list \\\n  --limit 1",
    ],
)
def test_an_unreadable_publishing_command_is_refused(env, tmp_path, command):
    assert _denied(_decide(command, env, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "cat > notes.md <<EOF\nit's here\nEOF\ngh pr list",
        "echo $'\\x41' && gh pr list",
        "gh pr view 1 \\\n  --json body",
        "gh --repo o/r pr checks 1 \\\n  --watch",
        "gh run list \\\n  --limit 5 && gh workflow view ci",
        "gh issue list \\\n  --state open; gh search prs x",
        "gh repo view o/r \\\n  --json name",
        "gh auth status \\\n  -h github.com",
        "gh secret set K <<'EOF'\nit's x\nEOF",
    ],
)
def test_an_unreadable_read_only_gh_passes_with_a_note(env, tmp_path, command):
    run = _decide(command, env, tmp_path)
    assert not _denied(run)
    note = _output(run)["additionalContext"]
    assert (
        "not checked for secrets.env values" in note and "cannot read all of this command" in note
    )


def test_an_unreadable_read_only_gh_with_a_value_in_the_text_is_refused(env, tmp_path):
    assert _denied(_decide(f"gh pr view 1 \\\n  --json body && echo {KEY}", env, tmp_path))
    assert _denied(_decide(f"gh secret set K <<'EOF'\nit's {KEY}\nEOF", env, tmp_path))


def test_a_readable_read_only_gh_is_decided_as_before(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide("gh pr view 1 && cat body.md", env, tmp_path))
    run = _decide("gh pr view 1 --json body", env, tmp_path)
    assert not _denied(run) and not run.stdout.strip()


# --- Class members the audit found beyond the reported spellings. ---


@pytest.mark.parametrize("exe", ["'g'h", "g\\h"])
def test_a_gh_assembled_by_quote_removal_is_checked(env, tmp_path, exe):
    _body(tmp_path)
    assert _denied(_decide(f"{exe} pr comment 1 -F body.md", env, tmp_path))


def test_gh_run_through_a_variable_is_checked(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide("GH=gh; $GH pr comment 1 -F body.md", env, tmp_path))


def test_gh_run_by_find_exec_is_checked(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide("find . -name body.md -exec gh pr comment 1 -F {} ;", env, tmp_path))


def test_gh_run_by_find_exec_is_never_exempt(env, tmp_path):
    assert _denied(
        _decide(
            f"find . -name x -exec gh pr comment 1 -b {KEY} ';' && gh auth status", env, tmp_path
        )
    )


@pytest.mark.parametrize(
    "command",
    [
        # Quoting that bash removes: the raw text never holds the value, the word gh receives does.
        f"gh pr comment 1 --body '{KEY[:10]}'\"{KEY[10:]}\"",
        'gh pr comment 1 --body "QUOTE_abc\\"defghijklmnopqrs"',
    ],
)
def test_a_value_spelled_with_shell_quoting_is_found_in_the_word(tmp_path, command):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"CANARY_API_KEY={KEY}\nQUOTE_API_KEY='QUOTE_abc\"defghijklmnopqrs'\n")
    env = {**os.environ, "SECRETS_PATH": str(secrets)}
    assert _denied(_decide(command, env, tmp_path))


@pytest.mark.parametrize(
    "command", ["gh pr comment 1 -F - <body.md", 'gh pr comment 1 --body "$(<body.md)"']
)
def test_a_redirect_target_is_a_path(env, tmp_path, command):
    _body(tmp_path)
    assert _denied(_decide(command, env, tmp_path))


def test_an_assignment_value_is_a_path(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide('F=body.md; gh pr comment 1 -F "$F"', env, tmp_path))


def test_a_glob_operand_is_expanded(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide("gh release upload v1 ./b*.md", env, tmp_path))


@pytest.mark.parametrize("spelling", ["$HOME/body.md", "${HOME}/body.md", "~/body.md"])
def test_a_home_relative_path_is_resolved(env, tmp_path, spelling):
    _body(tmp_path)
    assert _denied(
        _decide(f'gh pr comment 1 -F "{spelling}"', {**env, "HOME": str(tmp_path)}, tmp_path)
    )


@pytest.mark.parametrize(
    "command",
    [
        "pushd sub && gh pr comment 1 -F body.md",
        "env -C sub gh pr comment 1 -F body.md",
        "(cd sub && gh pr comment 1 -F body.md)",
    ],
)
def test_other_directory_changes_are_followed(env, tmp_path, command):
    _body(tmp_path, where="sub")
    assert _denied(_decide(command, env, tmp_path))


def test_cd_inside_quoted_text_is_not_a_directory_change(env, tmp_path):
    _body(tmp_path, where="sub")
    assert not _denied(
        _decide('gh pr comment 1 --body "run cd sub first" -F body.md', env, tmp_path)
    )


def test_a_file_piped_into_gh_is_checked(env, tmp_path):
    _body(tmp_path)
    assert _denied(_decide("cat body.md | gh pr comment 1 -F -", env, tmp_path))


def test_gh_in_quoted_data_still_opens_the_check(env, tmp_path):
    # Chosen over-inclusive gate: a mention opens the check, and a value must still be present to refuse.
    _body(tmp_path)
    assert _denied(_decide('echo "gh pr create" && cat body.md', env, tmp_path))


# --- In process: a later failure never discards a value already found. ---


def _guard_module():
    spec = importlib.util.spec_from_file_location("_publish_guard_under_test", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _main(module, monkeypatch, command: str, cwd: Path) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert module.main() == 0


def _verdict(out: str) -> str | None:
    return json.loads(out)["hookSpecificOutput"].get("permissionDecision") if out.strip() else None


def test_a_parser_that_cannot_load_leaves_the_raw_text_check(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SECRETS_PATH", env["SECRETS_PATH"])
    module = _guard_module()

    def broken(root):
        raise ImportError("no parser")

    monkeypatch.setattr(module, "_load_parser", broken)
    _main(module, monkeypatch, f"gh pr comment 1 --body {KEY}", tmp_path)
    out = capsys.readouterr()
    assert _verdict(out.out) == "deny" and KEY not in out.out
    _main(module, monkeypatch, "gh pr comment 1 --body x", tmp_path)
    out = capsys.readouterr()
    assert _verdict(out.out) is None and "did not load" in out.err


def test_a_failing_file_scan_keeps_the_text_hit(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SECRETS_PATH", env["SECRETS_PATH"])
    _body(tmp_path)
    module = _guard_module()

    def broken(path, needles):
        raise LookupError("scan failed")

    monkeypatch.setattr(module, "_scan_file", broken)
    _main(module, monkeypatch, f"gh pr comment 1 -F body.md --body {KEY}", tmp_path)
    assert _verdict(capsys.readouterr().out) == "deny"
    _main(module, monkeypatch, "gh pr comment 1 -F body.md", tmp_path)
    out = capsys.readouterr()
    assert _verdict(out.out) is None and "did not finish" in out.err


# --- Review round 3. ---

CARRIED = [
    'script -qc "gh pr comment 1 -F body.md" /dev/null',
    "watch -n 5 'gh pr comment 1 -F body.md'",
    "flock /tmp/l -c 'gh pr comment 1 -F body.md'",
    "su -c 'gh pr comment 1 -F body.md' someone",
    "tmux new-session -d 'gh pr comment 1 -F body.md'",
    "parallel 'gh pr comment {} -F body.md' ::: 1",
    "systemd-run --user --wait sh -c 'gh pr comment 1 -F body.md'",
    'eval "gh pr comment 1 -F body.md"',
]


@pytest.mark.parametrize("command", CARRIED)
def test_a_gh_command_handed_to_a_launcher_as_one_string_is_file_checked(env, tmp_path, command):
    assert not _denied(_decide(command, env, tmp_path))
    _body(tmp_path)
    assert _denied(_decide(command, env, tmp_path))


def test_an_unreadable_string_handed_to_a_launcher_is_treated_as_unread(env, tmp_path):
    run = _decide("watch -n 5 'gh pr comment 1 \\\n  -F body.md'", env, tmp_path)
    assert _denied(run) and "cannot read all of it" in _output(run)["permissionDecisionReason"]


def test_a_value_in_the_text_is_refused_without_the_file_scan(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SECRETS_PATH", env["SECRETS_PATH"])
    module = _guard_module()

    def never(*args):
        raise AssertionError("the file scan ran after a text hit")

    monkeypatch.setattr(module, "_file_findings", never)
    _main(module, monkeypatch, f"gh pr comment 1 -F /*/*/*/* --body {KEY}", tmp_path)
    assert _verdict(capsys.readouterr().out) == "deny"


def test_the_file_scan_stops_at_its_deadline_with_a_note(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SECRETS_PATH", env["SECRETS_PATH"])
    _body(tmp_path)
    module = _guard_module()
    monkeypatch.setattr(module, "_SCAN_SECONDS", -1.0)
    _main(module, monkeypatch, "gh pr comment 1 -F body.md", tmp_path)
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert "permissionDecision" not in out and "stopped after" in out["additionalContext"]


def test_a_glob_cut_at_its_limit_says_so(env, tmp_path):
    for i in range(70):
        (tmp_path / f"a{i:02d}.md").write_text("clean\n")
    run = _decide("gh release upload v1 ./a*.md", env, tmp_path)
    assert not _denied(run)
    assert "more than 64 files" in _output(run)["additionalContext"]


@pytest.mark.parametrize("target", ['"$HOME/body.md"', '"${HOME}/body.md"', "$HOME/body.md"])
def test_a_home_redirect_target_is_followed(env, tmp_path, target):
    home = tmp_path / "home"
    _body(home)
    run = _decide(f"gh pr comment 1 -F - < {target}", {**env, "HOME": str(home)}, tmp_path)
    assert _denied(run)


def test_every_part_after_an_equals_sign_is_a_path(env, tmp_path):
    _body(tmp_path)
    assert _denied(
        _decide("gh api repos/o/r/issues/1/comments --field=body=@body.md", env, tmp_path)
    )


@pytest.mark.parametrize(
    "command",
    [
        "gh workflow run ci.yml \\\n  -f note=x",
        "gh run rerun 1 \\\n  --failed",
        "gh run cancel 1 \\\n  -R o/r",
    ],
)
def test_unreadable_workflow_and_run_writes_count_as_publishing(env, tmp_path, command):
    assert _denied(_decide(command, env, tmp_path))


def test_an_unreadable_message_that_mentions_gh_is_data_not_a_command(env, tmp_path):
    # Not a launcher: the string is a message, so its apostrophe does not make the command unread.
    command = 'git commit -m "gh pr comment\'s body moved" && gh pr list'
    assert not _denied(_decide(command, env, tmp_path))
