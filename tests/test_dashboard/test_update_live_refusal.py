"""The dashboard's update routes refuse on `live` (#2978, PR C1a).

/apply runs update.sh and /resolve starts a Tier 3 session; both merge into the
checkout, which on `live` would drop every candidate the deploy manifest pins.
Each route asks scripts/lib/live_checkout.py (the real file, run afresh) and
answers 409 on `live` or when the answer cannot be read, starting nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.routes import updates

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + POSIX paths")


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    root = tmp_path / "root"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "seed",
        ],
        check=True,
    )
    script = root / "scripts" / "update.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\nexit 0\n")
    escalation = tmp_path / "escalation.json"
    escalation.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(updates, "_GENESIS_ROOT", root)
    monkeypatch.setattr(updates, "_UPDATE_SCRIPT", script)
    # raising=False: verify-RED runs this file against a routes module that has no
    # such constant, and must fail on the 409 assertions, not in the fixture.
    monkeypatch.setattr(
        updates,
        "_LIVE_CHECKOUT_SCRIPT",
        REPO / "scripts" / "lib" / "live_checkout.py",
        raising=False,
    )
    monkeypatch.setattr(updates, "_ESCALATION_FILE", escalation)
    monkeypatch.setattr(updates, "_PID_FILE", tmp_path / "update.pid")
    monkeypatch.setattr(updates, "update_in_progress", lambda: False)
    return {"home": home, "root": root}


def _put_on_live(w, manifest: str | None) -> None:
    subprocess.run(["git", "-C", str(w["root"]), "checkout", "-qb", "live"], check=True)
    path = w["home"] / ".genesis" / "deploy_manifest.json"
    if manifest == "here":
        repo = str((w["root"] / ".git").resolve())
        path.write_text(json.dumps({"version": 2, "repo": repo, "candidates": []}))
    elif manifest is not None:
        path.write_text(manifest)


_STARTED = {"status": "triggered", "stub": True}


@pytest.fixture()
def starters():
    """Every way the two routes can start work, replaced by a recorder."""
    calls = []

    def record(name):
        def _f(*a, **k):
            calls.append(name)
            if name == "spawn":

                class P:
                    pid = 4242
                    returncode = 0

                    def wait(self):
                        return 0

                return P()
            return updates.jsonify(_STARTED)

        return _f

    with (
        patch.object(updates, "_apply_supervised", record("supervised")),
        patch.object(updates, "_apply_direct", record("direct")),
        patch.object(updates, "_spawn_detached_cc", record("spawn")),
    ):
        yield calls


ROUTES = ["/api/genesis/updates/apply", "/api/genesis/updates/resolve"]


@pytest.mark.parametrize("route", ROUTES)
def test_on_live_the_route_refuses_and_names_the_rebuild(client, world, starters, route):
    _put_on_live(world, "here")
    r = client.post(route, json={})
    assert r.status_code == 409
    body = r.get_json()["error"]
    assert "scripts/deploy_candidates rebuild" in body
    assert "scripts/deploy_code_only.sh restart" in body
    assert starters == [], "a refused route started work"


@pytest.mark.parametrize("route", ROUTES)
def test_an_unreadable_verdict_refuses(client, world, starters, route):
    _put_on_live(world, "{not json")
    r = client.post(route, json={})
    assert r.status_code == 409
    assert "Cannot tell" in r.get_json()["error"]
    assert starters == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_missing_predicate_refuses(client, world, starters, route, tmp_path):
    with patch.object(updates, "_LIVE_CHECKOUT_SCRIPT", tmp_path / "absent.py", create=True):
        r = client.post(route, json={})
    assert r.status_code == 409
    assert starters == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "body",
    [
        "def (\n",  # python exits 1: a SyntaxError
        "raise SystemExit('boom')\n",  # python exits 1: an uncaught exit
        "print('live')\nraise SystemExit(1)\n",  # word and code disagree
    ],
)
def test_a_broken_predicate_on_live_refuses(client, world, starters, route, body, tmp_path):
    """Python's own failure exit (1) is the code `other` uses: the route must act
    only when the printed word agrees with it."""
    _put_on_live(world, "here")
    broken = tmp_path / "broken.py"
    broken.write_text(body)
    with patch.object(updates, "_LIVE_CHECKOUT_SCRIPT", broken, create=True):
        r = client.post(route, json={})
    assert r.status_code == 409
    assert starters == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("manifest", [None, "elsewhere"])
def test_a_live_branch_no_manifest_builds_is_still_refused(
    client, world, starters, route, manifest, tmp_path
):
    """The predicate answers `other` here (the deploy scripts then refuse by their
    branch rule); the route has no such rule, so it refuses `live` itself."""
    if manifest == "elsewhere":
        other = tmp_path / "other"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        repo = str((other / ".git").resolve())
        manifest = json.dumps({"version": 2, "repo": repo, "candidates": []})
    _put_on_live(world, manifest)
    r = client.post(route, json={})
    assert r.status_code == 409
    assert "no deploy manifest builds" in r.get_json()["error"]
    assert starters == []


@pytest.mark.parametrize("route", ROUTES)
def test_an_inherited_git_dir_does_not_hide_the_live_branch(
    client, world, starters, route, tmp_path, monkeypatch
):
    """GIT_DIR in the server's environment names a repository on main; the
    branch read must still be about the deployed checkout."""
    _put_on_live(world, None)
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    r = client.post(route, json={})
    assert r.status_code == 409
    assert starters == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("manifest", [None, "{not json"])
def test_on_main_the_route_proceeds_as_before(client, world, starters, route, manifest):
    if manifest is not None:
        (world["home"] / ".genesis" / "deploy_manifest.json").write_text(manifest)
    r = client.post(route, json={})
    assert r.status_code == 200, r.get_json()
    assert starters, "the route on main started nothing"
