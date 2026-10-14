"""A refusal over hand-edited systemd units (update.sh exit 4) on the dashboard path.

The status card reports edited units from the real checker, and the supervised
update never escalates a refusal to a tier that could "fix" the edit away: it
reads the record update.sh writes when it refuses (``update_refused.json``),
even when a session wrote prose over the summary or asked for escalation anyway.

Nothing here may reach the install: HOME is a temp dir, the routes module's
Popen raises, and the orchestrator runs with a PATH that holds only a fake
``claude``. (An earlier version of this file let a failing case start real
update orchestrators.)
"""

from __future__ import annotations

import os
import string
import subprocess
import sys
import types
from pathlib import Path

import pytest

from genesis.dashboard.routes import updates

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "lib" / "managed_units.py"


@pytest.fixture(autouse=True)
def _never_the_install(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def _refuse(*args, **kwargs):
        raise AssertionError(f"a test reached a real process launch: {args!r}")

    # The routes module's own view of subprocess, with Popen refused. Patching
    # subprocess.Popen itself would also break subprocess.run everywhere.
    routes_subprocess = types.SimpleNamespace(
        **{name: getattr(subprocess, name) for name in dir(subprocess) if not name.startswith("__")}
    )
    routes_subprocess.Popen = _refuse
    monkeypatch.setattr(updates, "subprocess", routes_subprocess)


def _git(repo: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.mark.parametrize("prompt", ["_TIER1_PROMPT", "_TIER2_PROMPT"])
def test_every_tier_that_runs_a_plain_update_is_told_a_refusal_is_not_its_to_fix(prompt):
    text = getattr(updates, prompt)
    assert "exit 4" in text.lower() or "EXITS 4" in text
    assert "never pass --take-template" in text
    assert '"refused: <the units it names>"' in text


@pytest.mark.parametrize("prompt", ["_TIER2_PROMPT", "_TIER3_PROMPT"])
def test_every_tier_that_runs_post_merge_is_told_a_kept_unit_is_not_its_to_fix(prompt):
    """--post-merge never refuses; it keeps an edited unit and names it."""
    text = " ".join(getattr(updates, prompt).split())
    assert "never pass --take-template" in text
    assert "kept <unit>: edited by hand" in text


def _orchestrate(
    tmp_path: Path,
    tier1_summary: str,
    *,
    refuses_in: str | None = None,
    stale_record: bool = False,
    tier2_asks_tier3: bool = False,
) -> tuple[list[str], str]:
    """Run the real orchestrator with a fake `claude`. Tier 1 writes the summary
    and ALSO asks for tier 2, as a session that ignored its prompt would.
    ``refuses_in`` names the tier whose update.sh run refused: the fake writes
    the record update.sh writes. Returns the tiers run and the final summary."""
    summary, escalation = tmp_path / "summary", tmp_path / "escalation"
    calls, refused = tmp_path / "calls", tmp_path / "update_refused.json"
    if stale_record:
        refused.write_text('{"units": ["old.service"]}')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    record = f'echo \'{{"units": ["demo.service"]}}\' > {refused}'
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$4" >> {calls}\n'
        f'if [ "$4" = haiku ]; then printf %s {tier1_summary!r} > {summary}; '
        f"echo tier2_needed > {escalation}; fi\n"
        + (f'if [ "$4" = {refuses_in} ]; then {record}; fi\n' if refuses_in else "")
        + (
            f'if [ "$4" = sonnet ]; then echo tier3_needed > {escalation}; fi\n'
            if tier2_asks_tier3
            else ""
        )
    )
    fake.chmod(0o755)
    fields = {f for _, f, _, _ in string.Formatter().parse(updates._ORCHESTRATOR_TEMPLATE) if f}
    values = dict.fromkeys(fields, "x")
    values.update(
        summary_file=str(summary),
        escalation_file=str(escalation),
        refused_file=str(refused),
        pid_file=str(tmp_path / "pid"),
        genesis_root=str(tmp_path),
        tier1_prompt="t1",
        tier2_prompt="t2",
    )
    script = tmp_path / "orch.py"
    script.write_text(updates._ORCHESTRATOR_TEMPLATE.format(**values))
    # Only the fake is reachable as `claude`: no real session can start.
    env = {
        **os.environ,
        "HOME": str(tmp_path / "isolated-home"),
        "PATH": f"{bindir}:/usr/bin:/bin",
    }
    subprocess.run(
        [sys.executable, str(script)], check=True, capture_output=True, env=env, timeout=60
    )
    return calls.read_text().split(), (summary.read_text() if summary.exists() else "")


@pytest.mark.parametrize("summary", ["Resolved everything; see notes.", "error: pip failed"])
def test_update_shs_record_stops_escalation_whatever_the_summary_says(tmp_path, summary):
    """The session wrote prose and asked for tier 2; update.sh's record wins."""
    calls, _ = _orchestrate(tmp_path, summary, refuses_in="haiku")
    assert calls == ["haiku"]


def test_a_session_that_reports_a_refusal_is_not_escalated(tmp_path):
    calls, _ = _orchestrate(tmp_path, "refused: genesis-server.service")
    assert calls == ["haiku"]


def test_an_ordinary_failure_still_escalates(tmp_path):
    """Control: the stop above keys on the refusal, not on escalation in general."""
    calls, _ = _orchestrate(tmp_path, "error: pip failed")
    assert calls == ["haiku", "sonnet"]


def test_a_record_left_by_an_earlier_run_does_not_stop_this_one(tmp_path):
    calls, _ = _orchestrate(tmp_path, "error: pip failed", stale_record=True)
    assert calls == ["haiku", "sonnet"]


def test_a_refusal_in_tier_2_stops_tier_3(tmp_path):
    _calls, summary = _orchestrate(
        tmp_path, "error: pip failed", refuses_in="sonnet", tier2_asks_tier3=True
    )
    assert "conflicts_unresolved" not in summary


def test_tier_3_is_reached_without_a_refusal(tmp_path):
    """Control for the test above."""
    _calls, summary = _orchestrate(tmp_path, "error: pip failed", tier2_asks_tier3=True)
    assert "conflicts_unresolved" in summary


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


def test_the_orchestrator_and_update_sh_name_the_same_record():
    """update.sh writes $HOME/.genesis/update_refused.json; the route passes the
    orchestrator the path it reads."""
    script = (REPO / "scripts" / "update.sh").read_text()
    assert 'REFUSED_FILE="$HOME/.genesis/update_refused.json"' in script
    assert updates._REFUSED_FILE == updates._HOME / ".genesis" / "update_refused.json"
