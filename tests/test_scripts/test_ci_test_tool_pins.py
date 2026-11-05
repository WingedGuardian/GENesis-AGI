"""CI runs the test tools at exact versions; user installs keep pyproject's ranges.

`.github/test-tools/requirements.txt` pins the `[test]` extra's tools and is
applied as a pip constraint on every CI install that runs or collects tests, so
a new pytest release cannot turn main red between a PR's CI and its merge (it
did on 2026-10-27). Dependabot bumps that folder alone, weekly.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parents[2]
PINS = REPO / ".github/test-tools/requirements.txt"
CI = REPO / ".github/workflows/ci.yml"
DEPENDABOT = REPO / ".github/dependabot.yml"


def _pins() -> dict[str, Requirement]:
    reqs = [
        Requirement(line.strip())
        for line in PINS.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return {canonicalize_name(r.name): r for r in reqs}


def _test_extra() -> dict[str, Requirement]:
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    reqs = [Requirement(s) for s in project["optional-dependencies"]["test"]]
    return {canonicalize_name(r.name): r for r in reqs}


def _step_runs(job: str) -> str:
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    return "\n".join(str(s.get("run", "")) for s in jobs[job]["steps"])


def test_every_pin_is_exact():
    for name, req in _pins().items():
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", f"{name} is not pinned exactly"


#: The tools pinned by the owner's ruling (2026-10-28): the test runner and the
#: two plugins whose releases change how the suite runs. The rest of `[test]`
#: (hypothesis, aioresponses, ...) still floats.
PINNED = {"pytest", "pytest-asyncio", "pytest-timeout"}


def test_the_pins_are_the_named_test_tools():
    assert set(_pins()) == PINNED


def test_every_pinned_tool_is_in_the_test_extra():
    # A pin for a tool the extra no longer declares would constrain nothing.
    assert set(_pins()) <= set(_test_extra())


def test_each_pin_satisfies_pyprojects_range():
    extra = _test_extra()
    for name, req in _pins().items():
        version = next(iter(req.specifier)).version
        assert extra[name].specifier.contains(version, prereleases=True), (
            f"{name}=={version} is outside pyproject's {extra[name].specifier}"
        )


def test_the_test_job_installs_under_the_pins():
    assert (
        'pip install -e ".[test,transcript-analytics]" -c .github/test-tools/requirements.txt'
        in _step_runs("test")
    )


def test_collected_floor_installs_both_trees_under_the_pins():
    run = _step_runs("collected-floor")
    assert (
        'pip install -e ".[test,transcript-analytics]" -c .github/test-tools/requirements.txt'
        in run
    )
    # The base tree may predate the pins file, so it is copied out of the PR
    # tree BEFORE the switch to the base, and the base venv reads the copy.
    copy = run.index('cp .github/test-tools/requirements.txt "$RUNNER_TEMP/test-tools.txt"')
    switch = run.index('git checkout --quiet --force --detach "$BASE"')
    base_install = run.index('-c "$RUNNER_TEMP/test-tools.txt"')
    assert copy < switch < base_install


#: Jobs that install the test extra unpinned ON PURPOSE: the audit reports on
#: the tree a fresh user install resolves, and the dispatch-only benchmark
#: measures the floating tree (runner-benchmark.yml records what it resolved).
FLOATING_BY_DESIGN = {("ci.yml", "dependency-audit"), ("runner-benchmark.yml", "bench")}


def _test_extra_installs():
    """(workflow, job, command) for every install of the test extra in every
    workflow, shell comments skipped and ``\\`` continuations joined."""
    install = re.compile(r'pip install\b[^\n]*-e "\.\[[^\]]*\btest\b')
    for wf in sorted((REPO / ".github/workflows").glob("*.yml")):
        for job, spec in (yaml.safe_load(wf.read_text()).get("jobs") or {}).items():
            for step in spec.get("steps", []):
                for line in str(step.get("run", "")).replace("\\\n", " ").splitlines():
                    if not line.lstrip().startswith("#") and install.search(line):
                        yield wf.name, job, line.strip()


def test_no_ci_test_install_floats():
    floating = {
        (wf, job) for wf, job, cmd in _test_extra_installs() if not re.search(r"\s-c\s", cmd)
    }
    assert floating == FLOATING_BY_DESIGN


def test_dependabot_bumps_the_pins_folder_and_nothing_else_in_pip():
    updates = yaml.safe_load(DEPENDABOT.read_text())["updates"]
    pip = [u for u in updates if u["package-ecosystem"] == "pip"]
    assert [u["directory"] for u in pip] == ["/.github/test-tools"]
    assert PINS.parent == REPO / pip[0]["directory"].lstrip("/")
