"""A refusal over hand-edited systemd units (update.sh exit 4) on the dashboard path.

The status card reports edited units from the real checker, and the supervised
update never escalates a refusal to a tier that could "fix" the edit away, even
when a session writes an escalation anyway.
"""

from __future__ import annotations

import json
import os
import string
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.dashboard.routes import updates

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "lib" / "managed_units.py"


def _git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.mark.parametrize("prompt", ["_TIER1_PROMPT", "_TIER2_PROMPT", "_TIER3_PROMPT"])
def test_every_tier_is_told_a_refusal_is_not_its_to_fix(prompt):
    text = getattr(updates, prompt)
    assert "exit 4" in text.lower() or "EXITS 4" in text
    assert "never pass --take-template" in text
    assert '"refused: <the units it names>"' in text


def _orchestrate(tmp_path: Path, tier1_summary: str, genesis_root: Path | None = None) -> list[str]:
    """Run the real orchestrator with a fake `claude` that writes the summary
    and ALSO asks for tier 2, as a session that ignored its prompt would."""
    summary, escalation, calls = tmp_path / "summary", tmp_path / "escalation", tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$4" >> {calls}\n'
        f'if [ "$4" = haiku ]; then printf %s {tier1_summary!r} > {summary}; echo tier2_needed > {escalation}; fi\n'
    )
    fake.chmod(0o755)
    fields = {f for _, f, _, _ in string.Formatter().parse(updates._ORCHESTRATOR_TEMPLATE) if f}
    values = dict.fromkeys(fields, "x")
    values.update(
        summary_file=str(summary),
        escalation_file=str(escalation),
        pid_file=str(tmp_path / "pid"),
        genesis_root=str(genesis_root or tmp_path),
        tier1_prompt="t1",
        tier2_prompt="t2",
    )
    script = tmp_path / "orch.py"
    script.write_text(updates._ORCHESTRATOR_TEMPLATE.format(**values))
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    subprocess.run(
        [sys.executable, str(script)], check=True, capture_output=True, env=env, timeout=60
    )
    return calls.read_text().split()


def _checkout(tmp_path: Path, monkeypatch, *, incoming: str | None = None) -> Path:
    """A checkout with an ``origin`` remote whose tracking ref is what update.sh
    fetched: HEAD, plus one commit adding ``incoming`` as a new template."""
    repo = tmp_path / "genesis"
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "systemd").mkdir(parents=True)
    (repo / "scripts" / "lib" / "managed_units.py").write_text(CHECKER.read_text())
    (repo / "scripts" / "systemd" / "base.service.template").write_text("[Service]\nExecStart=/b\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", str(tmp_path / "upstream.git"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    fetched = "HEAD"
    if incoming is not None:
        _git(repo, "checkout", "-qb", "incoming")
        (repo / "scripts" / "systemd" / f"{incoming}.template").write_text(
            "[Service]\nExecStart=/new\n"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "adds a unit")
        _git(repo, "checkout", "-q", "main")
        fetched = "incoming"
    _git(repo, "update-ref", "refs/remotes/origin/main", fetched)
    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home / ".config" / "systemd" / "user"


def test_a_refusal_is_never_escalated_even_when_a_session_asks(tmp_path, monkeypatch):
    _checkout(tmp_path, monkeypatch)
    calls = _orchestrate(
        tmp_path, "refused: genesis-server.service", genesis_root=tmp_path / "genesis"
    )
    assert calls == ["haiku"]


def test_an_ordinary_failure_still_escalates(tmp_path, monkeypatch):
    """Control: the stop above keys on the refusal, not on escalation in general."""
    _checkout(tmp_path, monkeypatch)
    calls = _orchestrate(tmp_path, "error: pip failed", genesis_root=tmp_path / "genesis")
    assert calls == ["haiku", "sonnet"]


@pytest.mark.parametrize("edited", [True, False])
def test_a_unit_only_the_fetched_commit_adds_still_stops_escalation(tmp_path, monkeypatch, edited):
    """update.sh refuses before the merge, so HEAD lacks the new template; the
    gate must also check the commit update.sh fetched (#3138 review)."""
    units = _checkout(tmp_path, monkeypatch, incoming="fresh.service")
    if edited:
        (units / "fresh.service").write_text("[Service]\nExecStart=/mine\n")
    calls = _orchestrate(tmp_path, "Resolved everything.", genesis_root=tmp_path / "genesis")
    assert calls == (["haiku"] if edited else ["haiku", "sonnet"])


def test_an_unresolvable_update_remote_stops_escalation(tmp_path):
    """No remote can be read (not even a checkout): doubt never escalates."""
    assert _orchestrate(tmp_path, "error: pip failed") == ["haiku"]


def _status_report(tmp_path: Path, monkeypatch, unit_text: str | None) -> dict:
    repo, units = tmp_path / "genesis", tmp_path / "units"
    (repo / "scripts" / "systemd").mkdir(parents=True)
    (repo / "scripts" / "systemd" / "demo.service.template").write_text(
        "[Service]\nExecStart=__VENV__/x\n"
    )
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "t")
    units.mkdir()
    if unit_text is not None:
        (units / "demo.service").write_text(unit_text)
    monkeypatch.setattr(updates, "_GENESIS_ROOT", repo)
    monkeypatch.setattr(updates, "_UNIT_DIR", units)
    monkeypatch.setattr(updates, "_MANAGED_UNITS_SCRIPT", CHECKER)
    return updates._managed_units_report()


def _stamp(text: str) -> str:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(CHECKER), "stamp"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_the_status_card_names_an_edited_stamped_unit(tmp_path, monkeypatch):
    report = _status_report(tmp_path, monkeypatch, _stamp("[Service]\nExecStart=/v/x\n") + "X=1\n")
    assert report == {"edited": ["demo.service"], "unchecked": [], "error": None}


def test_an_unstamped_unit_that_matches_is_clean_and_one_that_differs_is_unchecked(
    tmp_path, monkeypatch
):
    assert _status_report(tmp_path, monkeypatch, "[Service]\nExecStart=/v/x\n")["unchecked"] == []
    other = tmp_path / "second"
    other.mkdir()
    report = _status_report(other, monkeypatch, "[Service]\nExecStart=/mine\n")
    assert report == {"edited": [], "unchecked": ["demo.service"], "error": None}


def test_a_checker_failure_reads_as_unknown_never_as_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_MANAGED_UNITS_SCRIPT", tmp_path / "missing.py")
    report = updates._managed_units_report()
    assert report["error"] and report["edited"] == []


def test_the_backup_tab_renders_the_report():
    html = (
        Path(updates.__file__).parent.parent / "templates" / "partials" / "tabs" / "backup.html"
    ).read_text()
    assert "managed_units?.edited?.length" in html
    assert "--take-template" in html


def test_a_session_that_overwrote_the_summary_still_cannot_escalate(tmp_path, monkeypatch):
    """The gate re-runs the checker: a refusal holds even when the summary is prose."""
    repo = tmp_path / "genesis"
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "systemd").mkdir(parents=True)
    (repo / "scripts" / "lib" / "managed_units.py").write_text(CHECKER.read_text())
    (repo / "scripts" / "systemd" / "demo.service.template").write_text("[Service]\nExecStart=/x\n")
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "t")
    home = tmp_path / "home"
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    monkeypatch.setenv("HOME", str(home))
    calls = _orchestrate(tmp_path, "Resolved everything; see notes.", genesis_root=repo)
    assert calls == ["haiku"]


@pytest.fixture()
def progress(tmp_path, monkeypatch):
    from flask import Flask

    from genesis.dashboard._blueprint import blueprint

    app = Flask(__name__)
    app.register_blueprint(blueprint)
    gh = tmp_path / "gh"
    gh.mkdir()
    monkeypatch.setenv("GENESIS_HOME", str(gh))
    state = tmp_path / "update_state.json"
    summary = tmp_path / "summary.txt"
    monkeypatch.setattr(updates, "_STATE_FILE", state)
    monkeypatch.setattr(updates, "_SUMMARY_FILE", summary)
    monkeypatch.setattr(updates, "_ESCALATION_FILE", tmp_path / "escalation.txt")
    monkeypatch.setattr(updates, "_CONFLICT_FILE", tmp_path / "conflicts.json")
    state.write_text('{"phase": "merging", "pid": 999999999}')
    os.utime(state, (1, 1))
    return app.test_client(), state, summary


def _tagged_repo(tmp_path: Path, monkeypatch, *, merged: bool) -> str:
    """A checkout whose rollback tag is HEAD, or one commit behind it (merged)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "a")
    _git(repo, "tag", "pre-update-x")
    if merged:
        _git(repo, "commit", "-q", "--allow-empty", "-m", "merged")
    monkeypatch.setattr(updates, "_GENESIS_ROOT", repo)
    return "pre-update-x"


@pytest.mark.parametrize(
    ("phase", "text", "tag", "kept"),
    [
        # From "merging" on the file is the restart guard, whatever the summary
        # says and whether or not one exists (a direct CLI run writes none).
        ("merging", "refused: genesis-server.service", "head", True),
        ("merging", None, "head", True),
        ("merging", "error: merge conflict", "head", True),
        ("bootstrap", "error: pip", "head", True),
        ("health_check", None, "head", True),
        # "fetching" is retired only when git says nothing merged.
        ("fetching", None, "head", False),
        ("fetching", "error: fetch", "head", False),
        # A killed --post-merge run also writes "fetching", over merged code.
        ("fetching", None, "merged", True),
        ("fetching", None, None, True),
    ],
)
def test_the_progress_poll_never_deletes_a_post_merge_state_file(
    progress, tmp_path, monkeypatch, phase, text, tag, kept
):
    """update_state.json past the merge stops deploy_code_only.sh and the live
    engine restarting onto merged code bootstrap never ran on (#3139); a
    dashboard poll must not delete it."""
    client, state, summary = progress
    data = {"phase": phase, "pid": 999999999}
    if tag is not None:
        data["rollback_tag"] = _tagged_repo(tmp_path, monkeypatch, merged=(tag == "merged"))
    state.write_text(json.dumps(data))
    os.utime(state, (1, 1))
    if text is not None:
        summary.write_text(text)
    assert client.get("/api/genesis/updates/progress").status_code == 200
    assert state.exists() is kept


def test_a_supervised_update_will_not_start_over_a_post_merge_guard(progress):
    """Starting the dashboard pipeline used to delete update_state.json first."""
    _client, state, _summary = progress
    state.write_text(json.dumps({"phase": "merging", "pid": 999999999}))
    from flask import Flask

    with Flask(__name__).app_context():
        body, code = updates._apply_supervised(state.parent / "pid")
    assert code == 409
    assert state.exists()
    assert "--post-merge" in body.get_json()["error"]


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '"text"', "{}", '{"pid": 1}'],
    ids=["malformed", "array", "string", "empty-object", "no-phase"],
)
def test_a_state_file_the_guard_cannot_read_still_guards(progress, content):
    """A file that exists but is not a readable state with a phase is doubt, and
    doubt keeps the guard: the supervised pipeline refuses instead of deleting it."""
    _client, state, _summary = progress
    state.write_text(content)
    from flask import Flask

    with Flask(__name__).app_context():
        _body, code = updates._apply_supervised(state.parent / "pid")
    assert code == 409
    assert state.read_text() == content


@pytest.mark.parametrize("content", ["[1, 2]", "{not json"], ids=["array", "malformed"])
def test_the_progress_poll_survives_a_state_file_it_cannot_read(progress, content):
    client, state, _summary = progress
    state.write_text(content)
    os.utime(state, (1, 1))
    assert client.get("/api/genesis/updates/progress").status_code == 200
    assert state.read_text() == content


@pytest.mark.parametrize("name", ["_TIER1_PROMPT", "_TIER2_PROMPT", "_TIER3_PROMPT"])
def test_every_tier_is_told_never_to_delete_the_restart_guard(name):
    """update.sh refuses a plain run over an earlier update's guard; a tier told
    only to 'fix and retry' could delete the file to get past it."""
    text = " ".join(getattr(updates, name).split())
    assert "never delete or edit ~/.genesis/update_state.json" in text.lower()
