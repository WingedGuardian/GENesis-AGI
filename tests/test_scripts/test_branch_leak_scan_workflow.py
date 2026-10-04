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
from pathlib import Path

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


def test_branch_workflow_concurrency_is_one_run_per_branch():
    doc = _load(_BRANCH)
    conc = doc["concurrency"]
    assert "${{ github.ref }}" in conc["group"]
    assert conc["cancel-in-progress"] is True
    assert doc["permissions"] == {"contents": "read"}


def test_both_workflows_call_the_same_script_steps():
    ci_steps = _script_steps(_load(_CI)["jobs"]["leak-detector"])
    branch_steps = _script_steps(_load(_BRANCH)["jobs"]["branch-leak-scan"])
    assert ci_steps, "leak-detector no longer calls scripts/ci/leak_scan.sh"
    assert ci_steps == branch_steps
    assert set(ci_steps) == _script_subcommands()


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
    assert re.search(r"gitleaks\" detect --no-git --redact -c \.gitleaks\.toml", text)
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
