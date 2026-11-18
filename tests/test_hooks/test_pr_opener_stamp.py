"""The PR-opener stamp: what it writes after a create or an edit, and that it never blocks.

No network and no gh: the module's one process launcher, ``_run``, is replaced by a
fake GitHub that stores PR bodies, records every edit into an edit history (newest
first, as GitHub lists it) and can play a bot that edits a new PR's body. The clock
is fake too, so the settle wait costs no real time. Repos and ids are synthetic.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import private_module

_REPO = Path(__file__).resolve().parents[2]
_HOOKS = _REPO / "scripts" / "hooks"
_HOOK = _HOOKS / "pr_opener_stamp.py"
sys.path.insert(0, str(_HOOKS))
hook = private_module("pr_opener_stamp_under_test", _HOOK)

NOW = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)
JUST_NOW = (NOW - timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
LAST_WEEK = (NOW - timedelta(days=7)).isoformat().replace("+00:00", "Z")
URL = "https://github.com/acme/widgets/pull/900"
OLD = "https://github.com/acme/widgets/pull/5"
ME = "0a1b2c3d"
CREATE = "gh pr create --title t --body-file body.md"
BADGE = "<!-- devin-review-badge-begin -->\nbadge\n<!-- devin-review-badge-end -->"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _GitHub:
    """Fake gh. ``bot`` maps a read count to a body edit a bot makes before that read;
    ``clobber`` makes a bot write back the body it read before our first write."""

    def __init__(self, prs=None, bot=None, clobber=False, fail=None):
        self.prs = prs or {}
        self.reads = 0
        self.writes: list[tuple[str, str]] = []
        self.calls: list[list[str]] = []
        self.bot, self.clobber, self.fail = bot or {}, clobber, fail
        self._stale = None

    def add(self, url, body, author="acme", created=JUST_NOW, history=()):
        self.prs[url] = {"body": body, "author": author, "created": created, "edits": list(history)}

    def _edit(self, url, body, who):
        pr = self.prs[url]
        if not pr["edits"]:
            pr["edits"].append(
                {"editedAt": pr["created"], "diff": pr["body"], "editor": {"login": pr["author"]}}
            )
        pr["edits"].insert(0, {"editedAt": "later", "diff": body, "editor": {"login": who}})
        pr["body"] = body

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if self.fail is not None:
            if isinstance(self.fail, BaseException):
                raise self.fail
            return subprocess.CompletedProcess(argv, 1, "", self.fail)
        if argv[1:3] == ["pr", "view"]:
            url = argv[3]
            self.reads += 1
            if self.reads in self.bot:
                self._edit(url, self.bot[self.reads](self.prs[url]["body"]), "a-bot")
            pr = self.prs[url]
            if self.clobber and self._stale is None:
                self._stale = pr["body"]
            out = {
                "number": int(url.rsplit("/", 1)[1]),
                "author": {"login": pr["author"]},
                "createdAt": pr["created"],
                "body": pr["body"],
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        if argv[1:3] == ["pr", "edit"]:
            url, path = argv[3], argv[argv.index("--body-file") + 1]
            body = Path(path).read_text(encoding="utf-8")
            self.writes.append((url, body))
            self._edit(url, body, "acme")
            if self.clobber and len(self.writes) == 1:
                self._edit(url, self._stale, "a-bot")  # a bot writes back a stale copy
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["api", "graphql"]:
            num = int(next(a for a in argv if a.startswith("number=")).split("=", 1)[1])
            url = next(u for u in self.prs if u.endswith(f"/pull/{num}"))
            nodes = self.prs[url]["edits"]
            data = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "userContentEdits": {"totalCount": len(nodes), "nodes": nodes}
                        }
                    }
                }
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        raise AssertionError(f"unexpected gh call {argv}")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(hook.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(hook.time, "sleep", clock.sleep)
    for k in ("GENESIS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ME + "-1111-2222-3333-444455556666")
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    # The synthetic repos are this checkout's remotes; a real `git remote -v` is
    # tested in the lookup's own suite.
    real_lookup = hook._lookup

    def lookup():
        module = real_lookup()
        module.local_repos = lambda cwd=None: LOCAL_REPOS
        return module

    monkeypatch.setattr(hook, "_lookup", lookup)
    return clock


LOCAL_REPOS = {"acme/widgets"}


def _overlay(tmp_path, text):
    path = tmp_path / "home" / "config" / "pr_opener_stamp.local.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _run(
    monkeypatch,
    capsys,
    command,
    gh,
    stdout="",
    event="PostToolUse",
    git_op=None,
    error=None,
    extra=None,
):
    monkeypatch.setattr(hook, "_run", gh)
    payload = {
        "hook_event_name": event,
        "tool_name": "Bash",
        "session_id": "ffffffff-x",
        "tool_input": {"command": command},
        "duration_ms": 4000,
    }
    if event == "PostToolUse":
        payload["tool_response"] = {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
            **(extra or {}),
        }
        if git_op:
            payload["tool_response"]["gitOperation"] = git_op
    else:
        payload["error"] = error or f"Exit code 1\n\n{stdout}"
    hook._process(payload, now=NOW)
    out = capsys.readouterr().out.strip()
    if not out:
        return None
    wrapped = json.loads(out)
    assert "permissionDecision" not in json.dumps(wrapped)
    assert wrapped["hookSpecificOutput"]["hookEventName"] == event
    return wrapped["hookSpecificOutput"]["additionalContext"]


def _created(url=URL):
    return {
        "push": {"branch": "b"},
        "pr": {"number": int(url.rsplit("/", 1)[1]), "url": url, "action": "created"},
    }


def _edited(url=URL):
    return {"pr": {"number": int(url.rsplit("/", 1)[1]), "url": url, "action": "edited"}}


def _line(prefix=ME):
    return f"Opened-by-session: {prefix}"


# -- the create path ------------------------------------------------------------------


def test_a_created_pr_gets_the_line_appended(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, "## What\n\nx\n")
    note = _run(monkeypatch, capsys, CREATE, gh, stdout=URL, git_op=_created())
    assert gh.prs[URL]["body"] == f"## What\n\nx\n\n{_line()}\n"
    assert f"Stamped PR #900 with `{_line()}`" in note
    assert "add that line to the file" in note


def test_without_git_operation_a_url_line_created_in_this_call_is_stamped(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, "body")
    _run(monkeypatch, capsys, CREATE, gh, stdout=f"pushed\n{URL}\n")
    assert hook._lookup().stamped(gh.prs[URL]["body"]) == ME


def test_a_pr_an_earlier_step_printed_is_never_written(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(OLD, "old body", created=LAST_WEEK)
    note = _run(monkeypatch, capsys, f"gh pr view 5 --json url -q .url; {CREATE}", gh, stdout=OLD)
    assert gh.writes == [] and note is None


def test_the_push_hint_url_is_not_a_pr(monkeypatch, capsys):
    gh = _GitHub()
    out = "remote:      https://github.com/acme/widgets/pull/new/feat/x\n"
    assert _run(monkeypatch, capsys, CREATE, gh, stdout=out) is None
    assert gh.calls == []


def test_a_command_without_gh_starts_no_process(monkeypatch, capsys):
    gh = _GitHub()
    assert _run(monkeypatch, capsys, "ls -la", gh, stdout=URL) is None
    assert gh.calls == []


@pytest.mark.parametrize("action", ["commented", "closed", "reopened", "merged"])
def test_other_pr_actions_write_nothing(monkeypatch, capsys, action):
    gh = _GitHub()
    gh.add(URL, "body")
    op = {"pr": {"number": 900, "url": URL, "action": action}}
    assert _run(monkeypatch, capsys, "gh pr comment 900 -b x", gh, git_op=op) is None
    assert gh.calls == []


def test_a_pr_into_another_owners_repo_is_left_alone(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, "body", author="someone-else")
    assert _run(monkeypatch, capsys, CREATE, gh, git_op=_created()) is None
    assert gh.writes == []


def test_the_same_line_already_there_is_not_written_again(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, f"body\n\n{_line()}\n")
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.writes == [] and "already carries" in note


def test_another_sessions_line_from_a_reused_body_file_is_replaced(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, f"body\n\n{_line('99999999')}\n\nmore")
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.prs[URL]["body"] == f"body\n\n{_line()}\n\nmore"


def test_an_example_in_a_fence_is_neither_replaced_nor_counted(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, f"```\n{_line('99999999')}\n```")
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.prs[URL]["body"] == f"```\n{_line('99999999')}\n```\n\n{_line()}\n"


def test_the_line_goes_before_a_bot_section(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, f"## What\n\nx\n\n{BADGE}\n")
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.prs[URL]["body"] == f"## What\n\nx\n\n{_line()}\n\n{BADGE}\n"


def test_a_failed_compound_call_that_created_a_pr_is_stamped(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, "body")
    _run(
        monkeypatch,
        capsys,
        f"{CREATE} && false",
        gh,
        event="PostToolUseFailure",
        error=f"Exit code 1\n{URL}\n",
    )
    assert hook._lookup().stamped(gh.prs[URL]["body"]) == ME


def test_a_backgrounded_create_gets_a_note_and_no_write(monkeypatch, capsys):
    gh = _GitHub()
    note = _run(monkeypatch, capsys, CREATE, gh, extra={"backgroundTaskId": "b1"})
    assert gh.calls == []
    assert "background" in note and f"`{_line()}`" in note


# -- bots that edit a new PR's body ---------------------------------------------------


def _with_badge(body):
    return body.rstrip("\n") + "\n\n" + BADGE + "\n"


def test_with_a_settle_marker_the_write_waits_for_the_bots_edit(
    monkeypatch, capsys, tmp_path, _isolated
):
    _overlay(tmp_path, 'settle_markers: ["<!-- devin-review-badge-end -->"]\n')
    gh = _GitHub(bot={3: _with_badge})  # the bot's edit lands before the 3rd read
    gh.add(URL, "## What\n\nx\n")
    start = _isolated.t
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    body = gh.prs[URL]["body"]
    assert BADGE in body and _line() in body
    assert body.index(_line()) < body.index(BADGE)
    assert len(gh.writes) == 1
    assert _isolated.t - start < 20  # it stopped waiting once the badge appeared


def test_a_settle_marker_that_never_comes_ends_the_wait_and_still_writes(
    monkeypatch, capsys, tmp_path, _isolated
):
    _overlay(tmp_path, 'settle_markers: ["<!-- never -->"]\nsettle_wait_s: 10\n')
    gh = _GitHub()
    gh.add(URL, "body")
    start = _isolated.t
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert hook._lookup().stamped(gh.prs[URL]["body"]) == ME
    assert 9 <= _isolated.t - start <= 25


def test_no_settle_marker_configured_means_no_wait(monkeypatch, capsys, _isolated):
    gh = _GitHub()
    gh.add(URL, "body")
    start = _isolated.t
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    # Only the one re-read pause after the write.
    assert _isolated.t - start <= hook._RECHECK_S * 2 + 0.1


def test_a_bot_that_writes_back_a_stale_body_is_repaired_once(monkeypatch, capsys):
    gh = _GitHub(clobber=True)
    gh.add(URL, "body")
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert hook._lookup().stamped(gh.prs[URL]["body"]) == ME
    assert len(gh.writes) == 2 and "Stamped" in note


# -- the edit path: a rewrite that drops the line ------------------------------------


def _stamped_pr(gh, opener="11111111"):
    gh.add(URL, "original")
    gh._edit(URL, f"original\n\n{_line(opener)}\n", "acme")  # the stamp at create


def test_a_rewrite_that_drops_the_line_gets_the_original_opener_back(monkeypatch, capsys):
    gh = _GitHub()
    _stamped_pr(gh)
    gh._edit(URL, "rewritten from a local file", "acme")
    note = _run(monkeypatch, capsys, "gh pr edit 900 --body-file b.md", gh, git_op=_edited())
    assert hook._lookup().stamped(gh.prs[URL]["body"]) == "11111111"  # never ME, the editor
    assert "restored" in note and "11111111" in note


def test_a_rewrite_that_keeps_the_line_costs_one_read(monkeypatch, capsys):
    gh = _GitHub()
    _stamped_pr(gh)
    assert _run(monkeypatch, capsys, "gh pr edit 900 --add-label x", gh, git_op=_edited()) is None
    assert gh.writes == [] and len(gh.calls) == 1


def test_a_pr_never_stamped_is_not_stamped_by_an_edit(monkeypatch, capsys):
    gh = _GitHub()
    gh.add(URL, "opened before the hook")
    gh._edit(URL, "edited", "acme")
    assert _run(monkeypatch, capsys, "gh pr edit 900 -b edited", gh, git_op=_edited()) is None
    assert gh.writes == []


# -- failures are named, never raised -------------------------------------------------


def test_a_gh_timeout_is_reported_with_the_line_to_add_by_hand(monkeypatch, capsys):
    gh = _GitHub(fail=subprocess.TimeoutExpired(["gh"], 1))
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert "Could not stamp PR #900" in note and f"`{_line()}`" in note


def test_a_spent_budget_is_reported_not_overrun(monkeypatch, capsys, _isolated):
    gh = _GitHub()
    gh.add(URL, "body")
    real = gh.__call__

    def slow(argv, timeout):
        _isolated.t += 30  # every call eats 30 s of the 45 s budget
        return real(argv, timeout)

    note = _run(monkeypatch, capsys, CREATE, slow, git_op=_created())
    # The read and the write fit; the re-read does not. The write landed, so the
    # note says so rather than "could not stamp" (an agent would hand-edit a stale body).
    assert len(gh.writes) == 1
    assert note.startswith("[pr-opener-stamp] Stamped PR #900")
    assert "could not run (the hook ran out of time)" in note


def test_a_budget_spent_before_the_write_says_nothing_was_written(monkeypatch, capsys, _isolated):
    gh = _GitHub()
    gh.add(URL, "body")
    real = gh.__call__

    def slower(argv, timeout):
        _isolated.t += 44.5  # the read alone eats the budget
        return real(argv, timeout)

    note = _run(monkeypatch, capsys, CREATE, slower, git_op=_created())
    assert gh.writes == []
    assert "Could not stamp PR #900" in note and "ran out of time" in note


# -- round 0 (architect review): attribution the lookup reads back -------------------


def test_the_lookup_reads_the_corrected_id_after_a_copied_body_is_fixed(monkeypatch, capsys):
    # B1: a rework opens with another PR's body (another session's line). The hook
    # replaces it; the lookup must then name THIS session from the history left
    # behind, and a later rewrite that drops the line must restore THIS session.
    gh = _GitHub()
    created = (NOW - timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    gh.add(URL, f"copied\n\n{_line('99999999')}\n", created=created)
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    lookup = hook._lookup()
    for e in gh.prs[URL]["edits"]:  # the fake's "later" stamps: inside the window
        if e["editedAt"] == "later":
            e["editedAt"] = (NOW + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    assert lookup.opener(gh.prs[URL]["body"], gh.prs[URL]["edits"], [], created)[0] == ME
    gh._edit(URL, "rewritten without the line", "acme")
    gh.prs[URL]["edits"][0]["editedAt"] = (
        (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "eeeeeeee-x")
    _run(monkeypatch, capsys, "gh pr edit 900 --body-file b.md", gh, git_op=_edited())
    assert lookup.stamped(gh.prs[URL]["body"]) == ME


def test_a_fresh_pr_found_only_by_url_keeps_another_sessions_stamp(monkeypatch, capsys):
    # S1: `gh pr create ... && gh pr list --json url` prints another session's PR
    # created in the same window; its stamp is never replaced.
    gh = _GitHub()
    gh.add(URL, "mine")
    gh.add(URL.replace("900", "901"), f"theirs\n\n{_line('77777777')}\n")
    out = f"{URL}\n{URL.replace('900', '901')}\n"
    _run(monkeypatch, capsys, f"{CREATE} && gh pr list", gh, stdout=out, git_op=_created())
    assert hook._lookup().stamped(gh.prs[URL.replace("900", "901")]["body"]) == "77777777"
    assert hook._lookup().stamped(gh.prs[URL]["body"]) == ME


def test_a_confirmed_create_of_an_old_pr_is_not_written(monkeypatch, capsys):
    # S2: Claude Code's record says "created", GitHub says last week.
    gh = _GitHub()
    gh.add(URL, f"old\n\n{_line('77777777')}\n", created=LAST_WEEK)
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.writes == [] and "created before this call" in note


def test_a_bot_marker_inside_a_fence_is_not_an_insertion_point(monkeypatch, capsys):
    # S3: the doc example of a badge in a fence is skipped; the line goes before the
    # real badge, in one write.
    gh = _GitHub()
    body = f"x\n\n```\n{BADGE}\n```\n\n{BADGE}\n"
    gh.add(URL, body)
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.prs[URL]["body"] == f"x\n\n```\n{BADGE}\n```\n\n{_line()}\n\n{BADGE}\n"
    assert len(gh.writes) == 1


def test_a_whitespace_only_body_becomes_just_the_line():
    assert hook.with_line("  \n \n", ME, [], hook._lookup()) == f"{_line()}\n"


def test_a_missing_lookup_module_is_said_to_the_agent(monkeypatch, capsys):
    def broken():
        raise FileNotFoundError("pr_opener.py")

    monkeypatch.setattr(hook, "_lookup", broken)
    gh = _GitHub()
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert "Could not stamp PR #900" in note and "could not be loaded" in note
    assert gh.calls == []


def test_no_session_id_anywhere_is_said(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    gh = _GitHub()
    gh.add(URL, "body")
    monkeypatch.setattr(hook, "_run", gh)
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": CREATE},
        "tool_response": {"stdout": URL, "gitOperation": _created()},
    }
    hook._process(payload, now=NOW)
    note = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "no session id" in note and gh.writes == []


def test_main_returns_0_on_a_bad_payload(monkeypatch, capsys):
    monkeypatch.setattr(hook, "read_payload", lambda: (_ for _ in ()).throw(ValueError("x")))
    assert hook.main() == 0


# -- the session id, config and insertion rules ---------------------------------------


@pytest.mark.parametrize(
    ("env", "payload_sid", "want"),
    [
        (
            {"GENESIS_SESSION_ID": "AAAAAAAA-1", "CLAUDE_CODE_SESSION_ID": "bbbbbbbb-2"},
            "c",
            "aaaaaaaa",
        ),
        (
            {"CLAUDE_CODE_SESSION_ID": "bbbbbbbb-2", "CLAUDE_SESSION_ID": "cccccccc"},
            None,
            "bbbbbbbb",
        ),
        ({"CLAUDE_SESSION_ID": "cccccccc-3"}, None, "cccccccc"),
        ({}, "dddddddd-4", "dddddddd"),
        ({"GENESIS_SESSION_ID": "not-hex"}, "dddddddd-4", None),
        ({}, None, None),
    ],
)
def test_the_session_prefix_follows_prepare_commit_msg_then_the_payload(
    monkeypatch, env, payload_sid, want
):
    for k in ("GENESIS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    payload = {"session_id": payload_sid} if payload_sid else {}
    assert hook.session_prefix(payload) == want


def test_the_env_order_matches_prepare_commit_msg_exactly():
    text = (_HOOKS / "prepare-commit-msg").read_text()
    raw = re.search(r'local raw="\$\{(.+)\}"', text).group(1)
    order = re.findall(r"([A-Z_]+_SESSION_ID)", raw)
    assert order == ["GENESIS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"]
    src = _HOOK.read_text()
    assert '("GENESIS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")' in src


def test_the_overlay_sets_markers_and_a_bad_layer_changes_nothing(tmp_path):
    assert hook._config()["settle_markers"] == []
    _overlay(tmp_path, "settle_markers: [m1]\nsettle_wait_s: 99\n")
    conf = hook._config()
    assert conf["settle_markers"] == ["m1"] and conf["settle_wait_s"] == 20  # 99 is out of range
    _overlay(tmp_path, ":: not yaml [")
    assert hook._config()["settle_markers"] == []


def test_the_public_config_waits_for_nothing_and_names_the_bot_sections():
    import yaml

    conf = yaml.safe_load((_REPO / "config" / "pr_opener_stamp.yaml").read_text())
    assert conf["settle_markers"] == []
    assert "<!-- devin-review-badge-begin -->" in conf["insert_before"]


def test_with_line_keeps_crlf_bodies_readable_and_an_empty_body_gets_just_the_line():
    lookup = hook._lookup()
    assert hook.with_line("", ME, [], lookup) == f"{_line()}\n"
    assert lookup.stamped(hook.with_line("a\r\nb\r\n", ME, [], lookup)) == ME


# -- wiring and the real process ------------------------------------------------------


def test_it_is_wired_once_on_each_post_bash_event_with_a_60s_timeout():
    settings = json.loads((_REPO / ".claude" / "settings.json").read_text())
    for event in ("PostToolUse", "PostToolUseFailure"):
        found = [
            (m.get("matcher"), h.get("timeout"))
            for m in settings["hooks"][event]
            for h in m["hooks"]
            if "pr_opener_stamp.py" in h.get("command", "")
        ]
        assert found == [("^Bash$", 60)], event
    for event, matchers in settings["hooks"].items():
        if event not in ("PostToolUse", "PostToolUseFailure"):
            assert all("pr_opener_stamp" not in json.dumps(m) for m in matchers), event


def test_the_real_process_is_silent_and_exits_0_on_an_ordinary_call():
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "x"},
    }
    out = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PATH": "/nonexistent"},
    )
    assert (out.returncode, out.stdout) == (0, "")


def test_the_real_process_emits_a_post_tool_use_wrapper(tmp_path):
    # No gh on PATH: the create cannot be stamped, and the note says so in the
    # wrapper Claude Code reads for this event.
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": ME + "-x",
        "tool_input": {"command": CREATE},
        "tool_response": {"stdout": URL, "gitOperation": _created()},
    }
    env = {k: v for k, v in os.environ.items() if not k.endswith("SESSION_ID")}
    env.update(PATH=str(tmp_path), GENESIS_HOME=str(tmp_path / "home"))
    out = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert out.returncode == 0
    wrapped = json.loads(out.stdout)["hookSpecificOutput"]
    assert wrapped["hookEventName"] == "PostToolUse"
    assert "Could not stamp PR #900" in wrapped["additionalContext"]


def test_an_old_pr_that_already_carries_this_sessions_line_says_so(monkeypatch, capsys):
    # MEASURED on a real PR: the age refusal fired before the "already carries"
    # check and told the agent to add a line the body already had.
    gh = _GitHub()
    gh.add(URL, f"old\n\n{_line()}\n", created=LAST_WEEK)
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.writes == [] and "already carries" in note


# -- security review ------------------------------------------------------------------


def test_a_pr_url_from_a_repo_this_checkout_does_not_have_is_never_written(monkeypatch, capsys):
    # SF1: fetched text printed by a create command names a stranger's fresh PR,
    # authored by its own owner. Not one of this checkout's remotes: no read, no write.
    gh = _GitHub()
    stranger = "https://github.com/stranger/project/pull/7"
    gh.add(stranger, "theirs", author="stranger")
    gh.add(URL, "mine")
    out = f"{stranger}\n{URL}\n"
    _run(monkeypatch, capsys, f"curl -s example.test; {CREATE}", gh, stdout=out)
    assert all(w[0] != stranger for w in gh.writes)
    assert hook._lookup().stamped(gh.prs[URL]["body"]) == ME


def test_a_confirmed_create_never_scans_its_output_for_other_prs(monkeypatch, capsys):
    # SF3: another session's fresh, not-yet-stamped PR printed in the output.
    gh = _GitHub()
    other = URL.replace("900", "901")
    gh.add(URL, "mine")
    gh.add(other, "theirs, not stamped yet")
    _run(
        monkeypatch,
        capsys,
        f"{CREATE} && gh pr list",
        gh,
        stdout=f"{URL}\n{other}\n",
        git_op=_created(),
    )
    assert [w[0] for w in gh.writes] == [URL]
    assert all(other not in " ".join(c) for c in gh.calls)


def test_blank_lines_in_output_do_not_stall_the_url_scan():
    # SF2: `\s*` across lines was MEASURED at 74 s on 100,000 newlines.
    import time as _t

    text = " \n" * 100_000 + "x"
    start = _t.perf_counter()
    assert list(hook._PR_URL.finditer(text)) == []
    assert _t.perf_counter() - start < 1.0


def test_a_body_ending_inside_an_unclosed_fence_gets_the_line_on_top(monkeypatch, capsys):
    # SF4: appended inside the open fence, the line would never count.
    gh = _GitHub()
    gh.add(URL, "x\n\n```\nlog output, never closed\n")
    _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    body = gh.prs[URL]["body"]
    assert body.startswith(f"{_line()}\n\n")
    assert hook._lookup().stamped(body) == ME and len(gh.writes) == 1


def test_a_note_names_the_full_url_and_the_lookup_command(monkeypatch, capsys):
    gh = _GitHub(fail="boom")
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert f"gh pr edit {URL} --body-file" in note
    assert "scripts/pr_opener.py 900 --repo acme/widgets" in note


def test_unreadable_remotes_stop_the_stamp_and_say_so(monkeypatch, capsys):
    real = hook._lookup

    def lookup():
        module = real()

        def broken(cwd=None):
            raise RuntimeError("git remote failed")

        module.local_repos = broken
        return module

    monkeypatch.setattr(hook, "_lookup", lookup)
    gh = _GitHub()
    gh.add(URL, "body")
    note = _run(monkeypatch, capsys, CREATE, gh, git_op=_created())
    assert gh.writes == [] and "git remotes could not be read" in note
