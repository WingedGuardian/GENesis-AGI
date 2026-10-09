"""scripts/lib/managed_units.py: the stamp grammar and the hand-edit verdict.

Driven through the real CLI under ``python3 -I -S`` (how bootstrap.sh, setup-vnc.sh
and update.sh run it) against a scratch git repository, so template history, refs
and reflogs are real.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "lib" / "managed_units.py"

V1 = "[Unit]\nDescription=demo\n\n[Service]\nExecStart=__VENV__/bin/python -m demo --root __REPO_DIR__\nWorkingDirectory=__REPO_DIR__\nEnvironment=PATH=__CC_BIN_DIR__\nRestart=on-failure\n"
V2 = V1.replace("Restart=on-failure", "Restart=always")
V3 = V2.replace("Description=demo", "Description=demo v3")


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "-I", "-S", str(SCRIPT), *args], input=stdin, capture_output=True, text=True
    )


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
    ).stdout


def _render(template: str, venv: str = "/opt/app/.venv", root: str = "/opt/app") -> str:
    return (
        template.replace("__VENV__", venv)
        .replace("__REPO_DIR__", root)
        .replace("__CC_BIN_DIR__", "/opt/cc/bin")
    )


@pytest.fixture
def world(tmp_path: Path):
    repo, units = tmp_path / "repo", tmp_path / "units"
    (repo / "scripts" / "systemd").mkdir(parents=True)
    units.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    tpl = repo / "scripts" / "systemd" / "demo.service.template"
    for text in (V1, V2):
        tpl.write_text(text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "tpl")
    return repo, units


def _check(repo: Path, units: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run("check", "--repo", str(repo), "--unit-dir", str(units), "--upto", "HEAD", *extra)


# ── stamp / verify grammar ───────────────────────────────────────────


def _verify(text: str) -> str:
    import importlib.util

    spec = importlib.util.spec_from_file_location("managed_units_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.verify(text.encode())


def test_stamp_appends_the_hash_of_the_body_with_one_trailing_newline():
    out = _run("stamp", stdin="A=1\nB=2\n\n\n").stdout
    body = "A=1\nB=2\n"
    assert out == f"{body}# genesis-managed v1 sha256={hashlib.sha256(body.encode()).hexdigest()}\n"
    assert _verify(out) == "stamped"


def test_stamp_refuses_a_render_that_already_carries_the_prefix():
    r = _run("stamp", stdin="A=1\n# genesis-managed v1 sha256=abc\n")
    assert r.returncode == 3 and r.stdout == ""


@pytest.mark.parametrize(
    ("mutate", "verdict"),
    [
        (lambda s: s, "stamped"),
        (lambda s: s + "\n\n", "stamped"),  # blank lines after the stamp are tolerated
        (lambda s: s + "Environment=X=1\n", "edited"),  # the `>> unit` edit
        (lambda s: s.replace("A=1", "A=2"), "edited"),
        (lambda s: s.replace("\n", "\r\n"), "edited"),
        (lambda s: "# genesis-managed v1 sha256=" + "0" * 64 + "\n" + s, "edited"),  # two stamps
        (lambda s: s.rsplit("\n", 2)[0] + "\n", "unstamped"),
    ],
)
def test_verify_reads_every_shape_of_edit(mutate, verdict):
    stamped = _run("stamp", stdin="A=1\nB=2\n").stdout
    assert _verify(mutate(stamped)) == verdict


# ── unstamped units: matched against template versions ───────────────


def test_an_unstamped_render_of_the_current_template_is_legacy(world):
    repo, units = world
    (units / "demo.service").write_text(_render(V2))
    r = _check(repo, units, "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)[0]["state"] == "legacy"


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s.replace(
            "-m demo --root /opt/app", "-m demo --root /opt/app --debug"
        ),  # flag after a token
        lambda s: s.replace(
            "WorkingDirectory=/opt/app", "WorkingDirectory=/elsewhere"
        ),  # token rebound
        lambda s: s.replace(
            "PATH=/opt/cc/bin", "PATH=/opt/cc/bin EXTRA=1"
        ),  # a once-only token still matches one run only
        lambda s: s + "Environment=SQLITE_TMPDIR=/x\n",
        lambda s: s.replace("Restart=always", "Restart=no"),
    ],
)
def test_a_hand_edit_of_an_unstamped_unit_refuses(world, edit):
    repo, units = world
    (units / "demo.service").write_text(edit(_render(V2)))
    r = _check(repo, units, "--accept-legacy")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "REFUSED: demo.service" in r.stdout


def test_an_older_render_needs_history_unless_its_revision_is_listed(world):
    repo, units = world
    (units / "demo.service").write_text(_render(V1))
    assert json.loads(_check(repo, units, "--json").stdout)[0]["state"] == "unstamped"
    assert _check(repo, units, "--also", "HEAD~1").returncode == 0
    assert _check(repo, units, "--accept-legacy").returncode == 0


def test_history_includes_a_version_only_a_reflog_still_holds(world):
    repo, units = world
    _git(repo, "checkout", "-qb", "candidate")
    (repo / "scripts" / "systemd" / "demo.service.template").write_text(V3)
    _git(repo, "commit", "-qam", "candidate tpl")
    _git(repo, "checkout", "-q", "trunk")
    _git(repo, "branch", "-qD", "candidate")
    (units / "demo.service").write_text(_render(V3))
    assert _check(repo, units, "--accept-legacy").returncode == 0


def test_a_user_file_where_a_new_template_lands_refuses(world):
    repo, units = world
    (repo / "scripts" / "systemd" / "fresh.timer.template").write_text(
        "[Timer]\nOnCalendar=daily\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "new")
    (units / "demo.service").write_text(_render(V2))
    (units / "fresh.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    r = _check(repo, units, "--accept-legacy")
    assert r.returncode == 4 and "REFUSED: fresh.timer" in r.stdout


def test_take_by_name_or_star_lets_an_edit_through(world):
    repo, units = world
    (units / "demo.service").write_text(_render(V2) + "X=1\n")
    assert _check(repo, units, "--accept-legacy", "--take", "other.service").returncode == 4
    assert (
        _check(repo, units, "--accept-legacy", "--take", "demo.service other.service").returncode
        == 0
    )
    assert _check(repo, units, "--accept-legacy", "--take", "*").returncode == 0


def test_stamped_units_never_consult_history(world):
    repo, units = world
    stamped = _run("stamp", stdin=_render(V2)).stdout
    (units / "demo.service").write_text(stamped)
    assert json.loads(_check(repo, units, "--json").stdout)[0]["state"] == "stamped"
    (units / "demo.service").write_text(stamped + "Environment=A=1\n")
    assert _check(repo, units).returncode == 4  # refuses with no history scan at all


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory"])
def test_a_non_regular_target_is_kept_and_never_refuses(world, kind):
    repo, units = world
    target = units / "demo.service"
    if kind == "symlink":
        (units / "real").write_text("x")
        target.symlink_to(units / "real")
    elif kind == "dangling":
        target.symlink_to(units / "absent")
    else:
        target.mkdir()
    r = _check(repo, units, "--json", "--accept-legacy")
    assert r.returncode == 0
    assert json.loads(r.stdout)[0]["state"] == "nonregular"


def test_a_missing_unit_is_not_a_refusal(world):
    repo, units = world
    assert json.loads(_check(repo, units, "--json").stdout)[0]["state"] == "missing"


def test_an_unreadable_revision_is_an_error_never_clean(world):
    repo, units = world
    r = _run("check", "--repo", str(repo), "--unit-dir", str(units), "--upto", "no-such-rev")
    assert r.returncode not in (0, 4)
    assert "could not check" in r.stderr


def test_no_shipped_template_contains_the_stamp_prefix():
    offenders = [
        p
        for p in (REPO / "scripts" / "systemd").rglob("*.template")
        if "# genesis-managed" in p.read_text()
    ]
    assert offenders == []


def test_every_shipped_template_is_accepted_by_its_own_pattern():
    import importlib.util

    spec = importlib.util.spec_from_file_location("managed_units_t2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for tpl in (REPO / "scripts" / "systemd").rglob("*.template"):
        text = tpl.read_text()
        rendered = mod._TOKEN_RE.sub(lambda m: f"/x/{m.group(1).lower()}", text)
        assert mod.template_pattern(text).fullmatch(rendered.rstrip("\n")), tpl.name


def test_a_revision_that_looks_like_an_option_is_refused(world):
    repo, units = world
    r = _run("check", "--repo", str(repo), "--unit-dir", str(units), "--upto", "--output=/tmp/x")
    assert r.returncode == 2 and "may not start with" in r.stderr
