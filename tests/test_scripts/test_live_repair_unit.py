"""The boot repair's unit line in genesis-server's and genesis-bridge's units (#3492 D2a-2).

The ExecStartPre runs the installed kit with --boot and maps its exit: 0
starts, 75 or a signal death refuses the start, and every other outcome (no
kit, a crash, the self-check's 3) starts with one journal line and, when an
update state exists, one queued page. The line is taken from each template,
unescaped as systemd does, and run with /bin/sh against a scratch HOME.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SYSTEMD = REPO / "scripts" / "systemd"
QUEUE_LIB = REPO / "scripts" / "lib" / "alert_queue.sh"
TEMPLATES = ("genesis-server.service.template", "genesis-bridge.service.template")

FAKE_KIT = """import os, sys
rc = os.environ.get("FAKE_RC", "0")
with open(os.path.join(os.environ["HOME"], "kit_argv.json"), "w") as fh:
    fh.write(__import__("json").dumps(sys.argv[1:]))
if rc in ("kill", "usr1", "term"):
    os.kill(os.getpid(), {"kill": 9, "usr1": 10, "term": 15}[rc])
sys.exit(int(rc))
"""


def _line(template: str) -> str:
    lines = [
        ln for ln in (SYSTEMD / template).read_text().splitlines() if "live-repair/current" in ln
    ]
    assert len(lines) == 1, f"{template}: one boot repair line expected, found {len(lines)}"
    assert lines[0].startswith("ExecStartPre=/bin/sh -c "), lines[0][:60]
    return lines[0]


def _argv(template: str, home: Path, repo: Path) -> list[str]:
    """The ExecStartPre's argv as systemd builds it ($$ is a literal $)."""
    value = _line(template).split("=", 1)[1]
    value = value.replace("__HOME_EXEC__", str(home)).replace("__REPO_EXEC__", str(repo))
    return [word.replace("$$", "$") for word in shlex.split(value)]


@pytest.fixture
def scratch(tmp_path: Path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    (home / ".genesis" / "lib" / "live-repair.d" / "k1").mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copy2(QUEUE_LIB, repo / "scripts" / "lib" / "alert_queue.sh")
    return home, repo


def _kit(home: Path, body: str = FAKE_KIT, lib: str | None = None, boot: bool = True) -> Path:
    kit = home / ".genesis" / "lib" / "live-repair.d" / "k1"
    (kit / "live_move_repair.py").write_text(body)
    if boot:  # a kit from this step on carries live_move_boot.py
        (kit / "live_move_boot.py").write_text("")
    if lib is None:
        shutil.copy2(QUEUE_LIB, kit / "alert_queue.sh")
    else:
        (kit / "alert_queue.sh").write_text(lib)
    (home / ".genesis" / "lib" / "live-repair").mkdir()
    (home / ".genesis" / "lib" / "live-repair" / "current").symlink_to("../live-repair.d/k1")
    return kit


def _start(template: str, home: Path, repo: Path, **env: str):
    full = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", **env}
    return subprocess.run(
        _argv(template, home, repo), capture_output=True, text=True, env=full, timeout=60
    )


def _queued(home: Path) -> list[dict]:
    queue = home / ".genesis" / "alerts" / "queue"
    return (
        [json.loads(p.read_text()) for p in sorted(queue.glob("*.json"))] if queue.exists() else []
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_both_units_bound_their_start(template):
    text = (SYSTEMD / template).read_text()
    assert "\nTimeoutStartSec=90\n" in text
    # After the SQLITE_TMPDIR step: the repair's own temp files land on the big disk.
    assert text.index("SQLITE_TMPDIR %%s") < text.index("live-repair/current")


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize(
    ("rc", "state", "start", "key"),
    [
        ("0", True, 0, None),
        ("75", True, 75, None),
        ("1", True, 0, "live_move_repair:boot:kit:1:k1"),
        ("2", False, 0, None),
        ("3", True, 0, "live_move_repair:boot:kit:3:k1"),
        ("120", True, 0, "live_move_repair:boot:kit:120:k1"),
        ("kill", True, 137, None),
        ("usr1", True, 138, None),  # any signal death refuses (#4501 review)
        ("term", True, 143, None),
    ],
)
def test_the_kit_exit_maps_to_the_start(scratch, template, rc, state, start, key):
    home, repo = scratch
    _kit(home)
    if state:
        (home / ".genesis" / "update_state.json").write_text("{}")
    r = _start(template, home, repo, FAKE_RC=rc)
    assert r.returncode == start, (r.returncode, r.stderr)
    argv = json.loads((home / "kit_argv.json").read_text())
    assert argv == ["--boot", str(home / ".genesis" / "update_state.json"), str(repo)]
    queued = _queued(home)
    assert [q["dedupe_key"] for q in queued] == ([key] if key else []), queued
    if key:
        assert "could not run (exit" in r.stderr, r.stderr
        assert (
            queued[0]["severity"] == "critical"
            and "live_repair_kit.py install" in queued[0]["body"]
        )
        again = _start(template, home, repo, FAKE_RC=rc)
        assert again.returncode == 0 and len(_queued(home)) == 1, "the same kit state paged twice"


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize(("rc", "start"), [("0", 0), ("1", 75), ("75", 75), ("3", 0)])
def test_a_kit_from_before_this_step_refuses_on_its_1(scratch, template, rc, start):
    """A kit with no live_move_boot.py (one a rollback reinstalled from an older
    tag) exits 1 at boot where this kit exits 75: a half-written tree it could
    not heal. That 1 refuses the start too; its other exits map as for any kit."""
    home, repo = scratch
    _kit(home, boot=False)
    (home / ".genesis" / "update_state.json").write_text("{}")
    r = _start(template, home, repo, FAKE_RC=rc)
    assert r.returncode == start, (r.returncode, r.stderr)


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_kit_that_does_not_parse_starts_with_a_page(scratch, template):
    home, repo = scratch
    _kit(home, body="def broken(:\n")
    (home / ".genesis" / "update_state.json").write_text("{}")
    r = _start(template, home, repo)
    assert r.returncode == 0, r.stderr
    assert [q["dedupe_key"] for q in _queued(home)] == ["live_move_repair:boot:kit:1:k1"]


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("kind", ["none", "dangling"])
def test_no_kit_starts_and_pages_through_the_checkouts_lib(scratch, template, kind):
    home, repo = scratch
    if kind == "dangling":
        (home / ".genesis" / "lib" / "live-repair").mkdir(parents=True)
        (home / ".genesis" / "lib" / "live-repair" / "current").symlink_to("../gone")
    (home / ".genesis" / "update_state.json").write_text("{}")
    r = _start(template, home, repo)
    assert r.returncode == 0, r.stderr
    assert [q["dedupe_key"] for q in _queued(home)] == ["live_move_repair:boot:kit:none:"]
    (home / ".genesis" / "update_state.json").unlink()
    shutil.rmtree(home / ".genesis" / "alerts")
    assert _start(template, home, repo).returncode == 0
    assert _queued(home) == [], "no state, yet a page"


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_hanging_queue_lib_never_holds_the_start(scratch, template):
    """The shell's own page is bounded (timeout -k 1 3): a queue lib that hangs
    costs at most 4 s of the start."""
    home, repo = scratch
    _kit(home, lib="queue_alert() { sleep 30; }\n")
    (home / ".genesis" / "update_state.json").write_text("{}")
    start = time.monotonic()
    r = _start(template, home, repo, FAKE_RC="3")
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_page_runs_under_gnutimeout_where_there_is_one(scratch, template, tmp_path):
    """uutils `timeout` (Ubuntu 26.04) sends its KILL to the direct child only, so a
    hung page could outlive its bound; GNU's ships there as `gnutimeout`, and the
    line uses it when present (as scripts/lib/gnu_timeout.sh does for the scripts)."""
    home, repo = scratch
    _kit(home)
    (home / ".genesis" / "update_state.json").write_text("{}")
    bindir = tmp_path / "gnubin"
    bindir.mkdir()
    calls = tmp_path / "gnu_calls"
    (bindir / "gnutimeout").write_text(f'#!/bin/sh\necho "$*" >> {calls}\nexec timeout "$@"\n')
    (bindir / "gnutimeout").chmod(0o755)
    r = _start(template, home, repo, FAKE_RC="3", PATH=f"{bindir}:/usr/local/bin:/usr/bin:/bin")
    assert r.returncode == 0, r.stderr
    assert calls.read_text().startswith("-k 1 3 bash -c "), calls.read_text()
    assert [q["dedupe_key"] for q in _queued(home)] == ["live_move_repair:boot:kit:3:k1"]
