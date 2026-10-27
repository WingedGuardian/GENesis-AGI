"""The dashboard keeps an earlier update's restart guard (#3139).

~/.genesis/update_state.json is what stops deploy_code_only.sh and the live engine
restarting onto code an update merged and never activated. The progress poll used
to delete it whenever no update was running, the supervised start deleted it
unconditionally, and the direct start ran an update.sh that refused it into a log
nobody reads. Now the poll only reports it (Dismiss is offered for any state it
cannot judge), and /apply refuses on both paths, by scripts/lib/update_state_guard.py.

Every test runs in the shared sandbox (tests/_update_routes_sandbox.py): HOME and
every path constant in a temporary home, and the routes module's Popen refusing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from flask import Flask

from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.routes import updates

REPO = Path(__file__).resolve().parents[2]
DEPLOY_CODE_ONLY = REPO / "scripts" / "deploy_code_only.sh"

pytestmark = [
    pytest.mark.skipif(sys.platform.startswith("win"), reason="git + POSIX paths"),
    pytest.mark.usefixtures("updates_sandbox"),
]


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def w(updates_sandbox, monkeypatch):
    root = updates_sandbox.root
    _git(root, "tag", "pre")
    monkeypatch.setattr(updates, "update_in_progress", lambda: False)
    return {
        "root": root,
        "head": _git(root, "rev-parse", "HEAD"),
        "state": updates._STATE_FILE,
        "conflicts": updates._CONFLICT_FILE,
        "sandbox": updates_sandbox,
    }


def _write(w, content, *, conflict=False, old=True) -> bytes:
    if isinstance(content, dict) and content.get("deploy_head") == "HEAD":
        content = {**content, "deploy_head": w["head"]}
    w["state"].write_text(content if isinstance(content, str) else json.dumps(content))
    if conflict:
        w["conflicts"].write_text(json.dumps({"deploy_head": w["head"]}))
    if old:
        os.utime(w["state"], (1, 1))
    return w["state"].read_bytes()


CONFLICT_EXIT = {
    "phase": "merging",
    "rollback_tag": "pre",
    "deploy_head": "HEAD",
    "pid": 2147480000,
}

GUARDING = {
    "conflict-exit": (CONFLICT_EXIT, True),
    "merged-not-activated": (
        {"phase": "merging", "rollback_tag": "gone", "pid": 2147480000},
        False,
    ),
    "bootstrap": ({"phase": "bootstrap", "rollback_tag": "pre", "pid": 2147480000}, False),
    "post-merge-fetching": (
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "HEAD", "post_merge": True},
        False,
    ),
    "unknown-phase": ({"phase": "merged", "pid": 2147480000}, False),
    "no-phase": ({"pid": 2147480000}, False),
    "malformed": ("{not json", False),
    "array": ("[1, 2]", False),
}


# ── the progress poll ────────────────────────────────────────────────────────


def _deploy_code_only_refuses(w) -> subprocess.CompletedProcess:
    """deploy_code_only.sh's own refusal block, run with this sandbox's HOME."""
    text = DEPLOY_CODE_ONLY.read_text()
    begin = text.index("# BEGIN update-state-refusal")
    block = text[begin : text.index("# END update-state-refusal", begin)]
    script = (
        'die() { echo "ERROR: $*" >&2; exit 1; }\n'
        f'_SELF_DIR="{REPO / "scripts"}"\nGENESIS_ROOT="{w["root"]}"\n'
        'UPDATE_STATE_FILE="$HOME/.genesis/update_state.json"\n' + block + "echo PROCEEDS\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_issue_acceptance_the_poll_keeps_a_conflict_exit_and_deploys_still_refuse(client, w):
    """#3139's acceptance: a conflict-exit state (phase merging, dead pid, old
    mtime) survives /updates/progress, and deploy_code_only.sh still refuses."""
    before = _write(w, CONFLICT_EXIT, conflict=True)
    r = client.get("/api/genesis/updates/progress")
    assert r.status_code == 200
    body = r.get_json()
    assert w["state"].read_bytes() == before, "the poll deleted the restart guard"
    assert body["stale"] is True and body["guards"] is True
    assert body["guard"] == "conflict_pending"
    d = _deploy_code_only_refuses(w)
    assert d.returncode == 1 and "PROCEEDS" not in d.stdout, d.stdout + d.stderr
    assert "merge conflicts" in d.stderr


@pytest.mark.parametrize("name", list(GUARDING))
def test_the_poll_never_deletes_a_guarding_state_and_offers_dismiss(client, w, name):
    """Reviews 4255463447/4255463459: an unknown, absent or malformed phase is
    still stale, so the card shows Dismiss; nothing is deleted."""
    content, conflict = GUARDING[name]
    before = _write(w, content, conflict=conflict)
    r = client.get("/api/genesis/updates/progress")
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert w["state"].read_bytes() == before
    assert body["stale"] is True, body
    assert body["guards"] is True, body
    assert body["state_unreadable"] is (name in ("malformed", "array"))


def test_the_poll_reports_a_leftover_that_guards_nothing_and_keeps_it(client, w):
    before = _write(
        w, {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "HEAD", "post_merge": False}
    )
    body = client.get("/api/genesis/updates/progress").get_json()
    assert body["stale"] is True and body["guards"] is False and body["guard"] == "safe"
    assert w["state"].read_bytes() == before, "the poll is read-only"


def test_the_poll_does_not_judge_a_running_update(client, w, monkeypatch):
    monkeypatch.setattr(updates, "update_in_progress", lambda: True)
    _write(w, {"phase": "merging", "rollback_tag": "gone"})

    def _never(*a, **k):
        raise AssertionError("the guard ran during a live update")

    monkeypatch.setattr(updates, "_state_guard_verdict", _never)
    body = client.get("/api/genesis/updates/progress").get_json()
    assert body["in_progress"] is True and body["stale"] is False and body["guard"] is None


def test_the_poll_with_no_state_file(client, w, monkeypatch):
    def _never(*a, **k):
        raise AssertionError("the guard ran with no state file")

    monkeypatch.setattr(updates, "_state_guard_verdict", _never)
    body = client.get("/api/genesis/updates/progress").get_json()
    assert body["stale"] is False and body["guards"] is False and body["state_unreadable"] is False


def test_the_poll_passes_the_conflict_file_it_reads(client, w, monkeypatch, tmp_path):
    """The predicate reads the conflict record the dashboard reads, not a sibling."""
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"deploy_head": w["head"]}))
    monkeypatch.setattr(updates, "_CONFLICT_FILE", elsewhere)
    _write(w, CONFLICT_EXIT)
    assert client.get("/api/genesis/updates/progress").get_json()["guard"] == "conflict_pending"


# ── /apply ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("supervised", [True, False], ids=["supervised", "direct"])
@pytest.mark.parametrize("name", list(GUARDING))
def test_apply_refuses_over_a_guarding_state_on_both_paths(client, w, name, supervised):
    content, conflict = GUARDING[name]
    before = _write(w, content, conflict=conflict)
    r = client.post("/api/genesis/updates/apply", json={"supervised": supervised})
    assert r.status_code == 409, r.data
    error = r.get_json()["error"]
    assert ("merge conflicts" if name == "conflict-exit" else "--post-merge") in error
    assert "Dismiss" in error
    assert w["state"].read_bytes() == before
    assert w["sandbox"].launched == [], "a refused Apply launched a process"


def test_supervised_apply_over_a_leftover_that_guards_nothing_proceeds(client, w):
    _write(
        w, {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "HEAD", "post_merge": False}
    )
    with pytest.raises(AssertionError, match="real process launch"):
        client.post("/api/genesis/updates/apply", json={"supervised": True})
    assert not w["state"].exists(), "the supervised start clears a harmless leftover"
    assert w["sandbox"].launched and w["sandbox"].launched[0][0] == "systemd-run"


def test_a_guard_that_cannot_be_loaded_keeps_the_state(client, w, monkeypatch, tmp_path):
    monkeypatch.setattr(updates, "_STATE_GUARD_SCRIPT", tmp_path / "absent.py")
    before = _write(w, {"phase": "done"})
    r = client.post("/api/genesis/updates/apply", json={"supervised": True})
    assert r.status_code == 409 and w["state"].read_bytes() == before


# ── the tiers and the card ───────────────────────────────────────────────────


def test_tier_1_is_told_exit_5_is_a_refusal_it_reports(updates_sandbox):
    """SF2: "refused:" is what the orchestrator's refused() reads to stop escalation."""
    text = " ".join(updates._TIER1_PROMPT.split())
    assert "If exit 5" in text
    assert 'Write to {summary_file}: "refused:' in text
    assert "never delete or edit ~/.genesis/update_state.json" in text
    assert "Do not write {escalation_file}" in text


def test_tier_2_has_an_exit_5_branch_that_stops(updates_sandbox):
    text = " ".join(updates._TIER2_PROMPT.split())
    branch = text[text.index("If update.sh EXITS 5") : text.index("If SCRIPT ERROR")]
    assert '"refused:' in branch and "tier3_needed" in branch and "Stop." in branch
    assert "never delete or edit ~/.genesis/update_state.json" in branch


def test_tier_3_never_deletes_the_restart_guard(updates_sandbox):
    text = " ".join(updates._TIER3_PROMPT.split())
    assert "Never delete or edit ~/.genesis/update_state.json" in text


def test_the_card_hides_retry_and_explains_a_guarding_state():
    html = (
        Path(updates.__file__).parent.parent / "templates" / "partials" / "tabs" / "backup.html"
    ).read_text()
    assert '<template x-if="!$store.genesisDashboard.updateProgress?.guards">' in html
    assert "updateProgress?.stale" in html
    js = (Path(updates.__file__).parent.parent / "webui" / "js" / "dashboard.js").read_text()
    dismiss = js[js.index("async dismissUpdate()") :][:600]
    assert "this.updateProgress?.guards" in dismiss and "showConfirm" in dismiss


def test_tier_1_sends_a_retry_that_exits_5_to_the_refusal_step():
    """N2: the exit-1 retry can come back as exit 5; it must stop, not escalate."""
    text = " ".join(updates._TIER1_PROMPT.split())
    step5 = text[text.index("5. If exit 1") : text.index("6. If exit 4")]
    assert "If the retry exits 4 or 5" in step5 and '"refused:" summary and stop' in step5


def test_the_orchestrator_log_is_true_for_both_refusals():
    assert "refused over hand-edited units" not in updates._ORCHESTRATOR_TEMPLATE
    assert "an earlier update unfinished" in updates._ORCHESTRATOR_TEMPLATE


# ── the guard's own remedy on the card (#3492 PR C, SF3) ────────────────────


def _half_written(w) -> None:
    """A main merge killed after writing only the file it adds: HEAD at the
    tag, b.txt untracked. The guard calls that a half-written tree."""
    root = w["root"]
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    (root / "b.txt").write_text("b\n")
    _write(w, {"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming, "pid": 1})


def test_the_poll_carries_the_guards_own_remedy(client, w, monkeypatch):
    """The card names the remedy the guard gives, which for a half-written tree
    is not --post-merge (it refuses one); the guard is asked once per poll."""
    _half_written(w)
    calls = []
    real = updates._state_guard_verdict

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(updates, "_state_guard_verdict", counted)
    body = client.get("/api/genesis/updates/progress").get_json()
    assert body["guard"] == "mixed_tree" and body["guards"] is True, body
    assert "b.txt" in body["guard_remedy"], body
    assert "Do NOT run scripts/update.sh --post-merge" in body["guard_remedy"], body
    assert len(calls) == 1, "the poll asked the guard more than once"


def test_the_poll_has_no_remedy_when_nothing_is_guarded(client, w):
    _write(
        w, {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "HEAD", "post_merge": False}
    )
    body = client.get("/api/genesis/updates/progress").get_json()
    assert body["guard"] == "safe" and body["guard_remedy"] is None, body


def test_the_card_shows_the_guards_remedy_and_a_true_dismiss_warning():
    html = (
        Path(updates.__file__).parent.parent / "templates" / "partials" / "tabs" / "backup.html"
    ).read_text()
    assert "updateProgress?.guard_remedy" in html, "the card ignores the guard's remedy"
    js = (Path(updates.__file__).parent.parent / "webui" / "js" / "dashboard.js").read_text()
    dismiss = js[js.index("async dismissUpdate()") :][:900]
    assert "merged code it has not activated (or its record cannot be read)" not in dismiss
    assert "half-written" in dismiss, dismiss
