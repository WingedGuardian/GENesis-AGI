"""deploy_code_only.sh on `live`, the integration branch the deploy manifest builds.

restart runs on it as it stands; deploy and pull would pull into it, so they
refuse and name the rebuild; a manifest that names another repository leaves the
plain branch rule in force; one that cannot be read refuses every mode but
status. Driven against the real script in the station fixture (#2978, PR C1a).
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.test_scripts._deploy_station import advance_upstream as _advance_upstream
from tests.test_scripts._deploy_station import assert_untouched as _assert_untouched
from tests.test_scripts._deploy_station import git as _git
from tests.test_scripts._deploy_station import restarted as _restarted
from tests.test_scripts._deploy_station import run as _run

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash-only")


def _on_live(st, manifest: object = "here") -> str:
    """Put the fixture checkout on `live` and write the deploy manifest:
    "here" binds it to this checkout, "elsewhere" to another repository, a str is
    written raw, None writes nothing. Returns HEAD."""
    _git(st["root"], "checkout", "-qb", "live")
    path = st["home"] / ".genesis" / "deploy_manifest.json"
    if manifest == "here":
        repo = (st["root"] / ".git").resolve()
        path.write_text(json.dumps({"version": 2, "repo": str(repo), "candidates": []}))
    elif manifest == "elsewhere":
        repo = (st["seed"] / ".git").resolve()
        path.write_text(json.dumps({"version": 2, "repo": str(repo), "candidates": []}))
    elif isinstance(manifest, str):
        path.write_text(manifest)
    return _git(st["root"], "rev-parse", "HEAD")


def test_restart_runs_on_live_as_it_stands(station):
    head = _on_live(station)
    _advance_upstream(station)
    r = _run(station, "restart")
    assert r.returncode == 0, r.stderr
    assert _restarted(station)
    assert _git(station["root"], "rev-parse", "HEAD") == head, "restart moved live"
    assert _git(station["root"], "symbolic-ref", "--short", "HEAD") == "live"
    assert f"Healthy — deployed {head}" in r.stdout


@pytest.mark.parametrize("mode", ["deploy", "pull"])
def test_deploy_and_pull_refuse_on_live_and_name_the_rebuild(station, mode):
    head = _on_live(station)
    _advance_upstream(station)
    r = _run(station, mode)
    _assert_untouched(station, head, r)
    assert "scripts/deploy_candidates rebuild" in r.stderr
    assert "scripts/deploy_code_only.sh restart" in r.stderr


def test_the_default_mode_refuses_on_live_too(station):
    head = _on_live(station)
    _advance_upstream(station)
    r = _run(station)
    _assert_untouched(station, head, r)
    assert "scripts/deploy_candidates rebuild" in r.stderr


@pytest.mark.parametrize("manifest", ["elsewhere", None])
def test_a_live_branch_this_install_does_not_build_is_just_not_main(station, manifest):
    """A manifest naming another repository, or none at all: `live` is then an
    ordinary branch name, refused by the plain branch rule in every mode."""
    head = _on_live(station, manifest)
    r = _run(station, "restart")
    _assert_untouched(station, head, r)
    assert "is on 'live', not main" in r.stderr


@pytest.mark.parametrize("mode", ["restart", "deploy", "pull"])
@pytest.mark.parametrize("payload", ["{not json", json.dumps({"version": 2})])
def test_an_unreadable_manifest_on_live_refuses(station, mode, payload):
    head = _on_live(station, payload)
    r = _run(station, mode)
    _assert_untouched(station, head, r)
    assert "cannot tell whether" in r.stderr


def test_status_still_answers_on_live(station):
    _on_live(station, "{not json")
    r = _run(station, "status")
    assert r.returncode == 0, r.stderr
    assert "bracket:" in r.stdout


def test_main_ignores_a_broken_manifest(station):
    """The manifest is consulted only on `live`: main deploys as before."""
    (station["home"] / ".genesis" / "deploy_manifest.json").write_text("{not json")
    head = _git(station["root"], "rev-parse", "HEAD")
    r = _run(station, "restart")
    assert r.returncode == 0, r.stderr
    assert f"Healthy — deployed {head}" in r.stdout
