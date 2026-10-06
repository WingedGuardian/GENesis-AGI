"""Contract for the branch leak scan and its shared implementation.

Owner rule: a branch is never public without being checked. `ci.yml` runs only
on pull requests to main and pushes to main, so
`.github/workflows/branch-leak-scan.yml` runs the leak scan on every push to
every non-main branch. Both workflows call ONE script,
`scripts/ci/leak_scan.sh`, so the scan cannot drift between them.

These tests load the YAML with PyYAML (no network) and pin:
  * the new workflow triggers on push to every branch except main, and on
    nothing else, and runs no test suite;
  * both workflows call the same script with the same set of steps;
  * ci.yml's job id stays `leak-detector` and checks.json still requires it,
    while the branch job does NOT publish a check under that name;
  * the scanner pins, checksum and --redact survived the move into the script.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_CI = _WORKFLOWS / "ci.yml"
_BRANCH = _WORKFLOWS / "branch-leak-scan.yml"
_SCRIPT = _ROOT / "scripts" / "ci" / "leak_scan.sh"
_CHECKS = _ROOT / ".github" / "rulesets" / "checks.json"

_STEP_CALL = re.compile(r"^bash scripts/ci/leak_scan\.sh ([a-z-]+)$")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
    # YAML 1.1 resolves a bare `on:` key to boolean True.
    return doc.get("on") or doc.get(True)


def _script_steps(job: dict) -> list[str]:
    """The leak_scan.sh subcommands a job calls, in order."""
    out = []
    for step in job.get("steps", []):
        run = str(step.get("run", "")).strip()
        m = _STEP_CALL.match(run)
        if m:
            out.append(m.group(1))
    return out


def _script_subcommands() -> set[str]:
    text = _SCRIPT.read_text(encoding="utf-8")
    case_body = text[text.index('case "${1-}" in') :]
    return set(re.findall(r"^\s+([a-z-]+)\)\s+step_", case_body, re.M))


def test_branch_workflow_exists_and_triggers_on_non_main_pushes_only():
    assert _BRANCH.is_file()
    on = _on(_load(_BRANCH))
    assert set(on) == {"push"}, f"unexpected triggers: {sorted(on)}"
    assert on["push"] == {"branches-ignore": ["main"]}


def test_branch_workflow_runs_only_the_leak_scan():
    jobs = _load(_BRANCH)["jobs"]
    assert list(jobs) == ["branch-leak-scan"]
    text = _BRANCH.read_text(encoding="utf-8")
    assert "pytest" not in text


def test_branch_job_is_not_named_leak_detector():
    """`leak-detector` is a required check matched by name alone; a push to a
    PR's branch puts this job's check on the same head commit."""
    job = _load(_BRANCH)["jobs"]["branch-leak-scan"]
    assert job.get("name", "branch-leak-scan") != "leak-detector"
    assert _load(_BRANCH)["name"] != "CI"


def test_branch_workflow_never_cancels_or_replaces_a_scan():
    """No concurrency group at all: a cancelled (or replaced pending) run is a
    push whose commits may never be scanned, because a force-push can rewrite
    them out of the next run's merge-base..HEAD range after they were public."""
    doc = _load(_BRANCH)
    assert "concurrency" not in doc
    assert "concurrency" not in doc["jobs"]["branch-leak-scan"]
    assert doc["permissions"] == {"contents": "read"}


def test_both_workflows_call_the_same_script_steps():
    """The branch job runs every leak-detector step, plus the history scan right
    after the tree scan. The required check is unchanged; the history step is
    branch-only for now."""
    ci_steps = _script_steps(_load(_CI)["jobs"]["leak-detector"])
    branch_steps = _script_steps(_load(_BRANCH)["jobs"]["branch-leak-scan"])
    assert ci_steps, "leak-detector no longer calls scripts/ci/leak_scan.sh"
    i = ci_steps.index("gitleaks") + 1
    assert branch_steps == [*ci_steps[:i], "gitleaks-history", *ci_steps[i:]]
    assert set(branch_steps) == _script_subcommands()


def test_branch_history_scan_uses_the_branch_range():
    steps = _load(_BRANCH)["jobs"]["branch-leak-scan"]["steps"]
    (hist,) = [s for s in steps if str(s.get("run", "")).endswith("leak_scan.sh gitleaks-history")]
    assert hist["env"]["LEAK_SCAN_RANGE"] == "branch"
    assert hist["env"]["EVENT_NAME"] == "${{ github.event_name }}"


def test_no_leak_scan_logic_left_inline_in_either_workflow():
    """Every run step of both jobs is a plain script call — nothing inline to drift."""
    for path, job_id in ((_CI, "leak-detector"), (_BRANCH, "branch-leak-scan")):
        job = _load(path)["jobs"][job_id]
        runs = [str(s["run"]).strip() for s in job["steps"] if "run" in s]
        assert runs and all(_STEP_CALL.match(r) for r in runs), (path.name, runs)


def test_both_workflows_fetch_full_history():
    for path, job_id in ((_CI, "leak-detector"), (_BRANCH, "branch-leak-scan")):
        checkout = _load(path)["jobs"][job_id]["steps"][0]
        assert checkout["uses"].startswith("actions/checkout@")
        assert checkout["with"]["fetch-depth"] == 0


def test_branch_private_scan_uses_branch_range_and_secret():
    steps = _load(_BRANCH)["jobs"]["branch-leak-scan"]["steps"]
    (priv,) = [s for s in steps if str(s.get("run", "")).endswith("leak_scan.sh private")]
    env = priv["env"]
    assert env["LEAK_SCAN_RANGE"] == "branch"
    assert env["EVENT_NAME"] == "${{ github.event_name }}"
    assert env["PRIV"] == "${{ secrets.GENESIS_PRIVATE_PATTERNS }}"
    assert "dependabot[bot]" in priv["if"]


def test_ci_private_scan_does_not_use_branch_range():
    steps = _load(_CI)["jobs"]["leak-detector"]["steps"]
    (priv,) = [s for s in steps if str(s.get("run", "")).endswith("leak_scan.sh private")]
    assert "LEAK_SCAN_RANGE" not in priv["env"]


def test_required_check_name_preserved():
    jobs = _load(_CI)["jobs"]
    assert "leak-detector" in jobs
    assert "name" not in jobs["leak-detector"], "a job `name:` would rename the check"
    assert _load(_CI)["name"] == "CI"
    contexts = [
        c["context"]
        for rule in json.loads(_CHECKS.read_text(encoding="utf-8"))["rules"]
        if rule["type"] == "required_status_checks"
        for c in rule["parameters"]["required_status_checks"]
    ]
    assert "leak-detector" in contexts


def test_script_keeps_pins_checksum_and_redact():
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "pip install 'detect-secrets==1.5.0'" in text
    assert "GITLEAKS_VERSION=8.22.1" in text
    assert (
        "GITLEAKS_SHA256=2f92ab3b8e08319ac30836c32b90818e01519c3a4982771e4f45a7f5607872f7" in text
    )
    assert "sha256sum -c -" in text
    assert re.search(r'"\$gl" detect --no-git --redact -c \.gitleaks\.toml', text)
    # --remerge-diff: a merge's conflict resolution is read, not skipped.
    assert re.search(
        r'"\$gl" git --redact -c \.gitleaks\.toml --log-opts="--remerge-diff \$range"', text
    )
    # Every gitleaks invocation redacts: these logs are public.
    calls = re.findall(r'"\$gl" (?:detect|git) [^\n]*', text)
    assert len(calls) == 2 and all("--redact" in c for c in calls)
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


def _scratch_tree(tmp_path: Path) -> Path:
    """A minimal tree the real script can run in: the script itself (it cds to
    its own repo root), the portability helper the class step calls, and files
    holding synthetic findings."""
    root = tmp_path / "t"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(_SCRIPT, root / "scripts" / "ci" / "leak_scan.sh")
    shutil.copy(_ROOT / "scripts" / "check_portability.sh", root / "scripts")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text('OWNER = "someone@personal-domain.org"\n', encoding="utf-8")
    (root / "src" / "b.py").write_text('P = "/home/somebody/genesis/data"\n', encoding="utf-8")
    return root


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_email_scan_names_the_place_never_the_address(tmp_path: Path):
    root = _scratch_tree(tmp_path)
    cp = subprocess.run(
        ["bash", str(root / "scripts" / "ci" / "leak_scan.sh"), "email"],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 1
    assert "src/a.py:1" in cp.stdout
    assert "personal-domain" not in cp.stdout + cp.stderr


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_class_scan_names_the_place_never_the_value(tmp_path: Path):
    root = _scratch_tree(tmp_path)
    cp = subprocess.run(
        ["bash", str(root / "scripts" / "ci" / "leak_scan.sh"), "class"],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, "the class scan is advisory"
    assert "::warning::class match at ./src/b.py:1" in cp.stdout
    assert "somebody" not in cp.stdout + cp.stderr


def test_branch_email_and_binary_steps_read_the_branch_history():
    """Codex P1/P2 (round 2): both steps read only the tip. On a branch push
    they now also read the branch range, so they must carry it."""
    steps = _load(_BRANCH)["jobs"]["branch-leak-scan"]["steps"]
    for sub in ("email", "binary"):
        (step,) = [s for s in steps if str(s.get("run", "")).endswith(f"leak_scan.sh {sub}")]
        assert step["env"]["LEAK_SCAN_RANGE"] == "branch", sub
        assert step["env"]["EVENT_NAME"] == "${{ github.event_name }}", sub


def test_ci_email_and_binary_steps_stay_tip_only():
    steps = _load(_CI)["jobs"]["leak-detector"]["steps"]
    for sub in ("email", "binary"):
        (step,) = [s for s in steps if str(s.get("run", "")).endswith(f"leak_scan.sh {sub}")]
        assert "LEAK_SCAN_RANGE" not in (step.get("env") or {}), sub


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "ci@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "ci@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _history_repo(tmp_path: Path) -> Path:
    """A git repo holding the two scripts at a main commit, plus a branch whose
    first commit adds content its second commit deletes, so the tip is clean."""
    import os

    root = tmp_path / "h"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(_SCRIPT, root / "scripts" / "ci" / "leak_scan.sh")
    shutil.copy(_ROOT / "scripts" / "ci" / "leak_scan_added_lines.py", root / "scripts" / "ci")
    env = {**os.environ, **_GIT_ENV, "HOME": str(root)}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "main")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "-q", "-b", "feature")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text('OWNER = "someone@personal-domain.org"\n', encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "t.py").write_text('X = "fixture@personal-domain.org"\n', encoding="utf-8")
    (root / "data.db").write_bytes(b"SQLite format 3\x00")
    git("add", "-A")
    git("commit", "-q", "-m", "add")
    git("rm", "-q", "src/a.py", "data.db")
    git("commit", "-q", "-m", "remove")
    return root


def _run_step(root: Path, step: str, *, branch: bool) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **_GIT_ENV, "HOME": str(root), "EVENT_NAME": "push"}
    env.pop("LEAK_SCAN_RANGE", None)
    if branch:
        env["LEAK_SCAN_RANGE"] = "branch"
    return subprocess.run(
        ["bash", str(root / "scripts" / "ci" / "leak_scan.sh"), step],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def test_email_scan_reads_the_branch_history(tmp_path: Path):
    """An address added and removed inside the branch is public in its history.
    The tip half misses it (control); the branch range catches it, names the
    file and commit, never the address, and still skips tests/."""
    root = _history_repo(tmp_path)
    tip_only = _run_step(root, "email", branch=False)
    assert tip_only.returncode == 0, tip_only.stdout + tip_only.stderr
    cp = _run_step(root, "email", branch=True)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert re.search(r"^src/a\.py:[0-9a-f]{12}$", cp.stdout, re.M)
    assert "tests/t.py" not in cp.stdout
    assert "personal-domain" not in cp.stdout + cp.stderr


def test_binary_scan_reads_the_branch_history(tmp_path: Path):
    """A database added and deleted inside the branch stays downloadable from
    its history. The tip half misses it (control); the branch range catches it."""
    root = _history_repo(tmp_path)
    tip_only = _run_step(root, "binary", branch=False)
    assert tip_only.returncode == 0, tip_only.stdout + tip_only.stderr
    cp = _run_step(root, "binary", branch=True)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert "data.db" in cp.stdout


def test_history_steps_fail_closed_without_a_range(tmp_path: Path):
    """No origin/main to anchor the range: both history halves refuse (exit 3)
    rather than reporting a clean scan of nothing."""
    root = _history_repo(tmp_path)
    subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=root, check=True)
    for step in ("email", "binary"):
        cp = _run_step(root, step, branch=True)
        assert cp.returncode == 3, (step, cp.stdout + cp.stderr)
