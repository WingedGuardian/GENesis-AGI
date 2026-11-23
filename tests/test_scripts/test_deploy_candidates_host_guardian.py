"""Readiness: where a host guardian is configured, the deployed commit its last
probe recorded must contain the host guardian's `live` ban (HOST_REQUIRED, #3546).
REVERT_CODE runs on the host from that commit. It is required by commit, not by
the readiness marker: update.sh redeploys the host only when a guardian path
changed, so a marker requirement would never clear on an up-to-date host. Every
way the probe cannot show it is a refusal that names it (#2978)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.test_scripts._deploy_candidates_world import REPO


@pytest.fixture
def armed(dc, dc_ready, monkeypatch):
    """The scratch base stands in for the real #3546 commit."""
    w = dc_ready
    monkeypatch.setattr(
        dc.gate, "HOST_REQUIRED", ("the live ban", w.rev("refs/remotes/origin/main"))
    )
    return w


def _guardian(w, deployed: object | None = None, *, raw: str | None = None) -> None:
    genesis = w.home / ".genesis"
    (genesis / "guardian_remote.yaml").write_text("host: the-host\n")
    if raw is not None:
        (genesis / "host_gateway_state.json").write_text(raw)
    elif deployed is not None:
        (genesis / "host_gateway_state.json").write_text(
            json.dumps(
                {
                    "checked_at": "2026-11-20T00:00:00+00:00",
                    "version": {"deployed_commit": deployed},
                }
            )
        )


def _refused(w, dc, capsys, needle: str) -> None:
    w.candidate("feat/a", {"x.txt": "x\n"})
    capsys.readouterr()
    assert w.add(dc, "feat/a") == 1
    err = capsys.readouterr().err
    assert "not ready" in err and needle in err, err
    assert "genesis-cc-align.service" in err and "scripts/update.sh" in err, err


def test_no_host_guardian_configured_adds_no_check(dc, armed, capsys):
    armed.candidate("feat/a", {"x.txt": "x\n"})
    assert armed.add(dc, "feat/a") == 0, capsys.readouterr().err


def test_a_host_guardian_with_the_live_ban_is_ready(dc, armed, capsys):
    w = armed
    _guardian(w, w.rev("refs/remotes/origin/main")[:9])  # the probe stores 9 hex
    w.candidate("feat/a", {"x.txt": "x\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr().err


def test_a_host_guardian_past_the_live_ban_is_ready(dc, armed, capsys):
    """A host deployed from a LATER commit (no guardian path changed since) passes:
    the marker itself is never required on the host."""
    w = armed
    later = w.advance_main({"z.txt": "z\n"}, "later work on main")
    w.git(w.root, "fetch", "-q", "origin")
    _guardian(w, later[:9])
    w.candidate("feat/a", {"x.txt": "x\n"})
    assert w.add(dc, "feat/a") == 0, capsys.readouterr().err


def test_a_host_guardian_before_the_live_ban_is_not_ready(dc, armed, capsys, monkeypatch):
    w = armed
    old = w.rev("refs/remotes/origin/main")
    ban = w.advance_main({"z.txt": "z\n"}, "the live ban")
    w.git(w.root, "fetch", "-q", "origin")
    monkeypatch.setattr(dc.gate, "HOST_REQUIRED", ("the live ban", ban))
    monkeypatch.setattr(dc.gate, "REQUIRED_MERGED", ())
    _guardian(w, old[:12])
    _refused(w, dc, capsys, "predates the live ban")


def test_a_probe_older_than_the_guardian_config_is_not_ready(dc, armed, capsys):
    """The probe names no host: a config changed after it (a new host or key) may
    make it describe another guardian, so it must be re-taken first."""
    w = armed
    _guardian(w, w.rev("refs/remotes/origin/main")[:9])
    genesis = w.home / ".genesis"
    state = genesis / "host_gateway_state.json"
    t = state.stat().st_mtime
    os.utime(state, (t - 60, t - 60))
    os.utime(genesis / "guardian_remote.yaml", (t, t))
    _refused(w, dc, capsys, "predates")


def test_a_guardian_config_that_cannot_be_checked_is_not_ready(dc, armed, capsys, monkeypatch):
    """Only an ABSENT config means "no host guardian"; any other stat error refuses."""
    w = armed
    _guardian(w, w.rev("refs/remotes/origin/main")[:9])
    real = dc.gate.os.stat
    target = str(w.home / ".genesis" / "guardian_remote.yaml")

    def stat(path, *a, **kw):
        if str(path) == target:
            raise PermissionError(13, "Permission denied")
        return real(path, *a, **kw)

    monkeypatch.setattr(dc.gate.os, "stat", stat)
    _refused(w, dc, capsys, "could not be checked")


def test_a_main_that_deleted_the_marker_disarms_rebuild(dc, armed, capsys):
    """The serving merge-base still has the marker, but the rebuild builds on the
    fetched origin/main, which deleted it: no candidate goes live on it."""
    w = armed
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    w.advance_main({"scripts/lib/deploy_live.sh": None}, "disarm live")
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "no longer has scripts/lib/deploy_live.sh" in err, err
    assert w.live_merges() == []


def test_rebuild_refuses_on_the_host_check_as_add_does(dc, armed, capsys):
    w = armed
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    _guardian(w)  # configured, never probed
    capsys.readouterr()
    assert w.run(dc, "rebuild") == 1
    err = capsys.readouterr().err
    assert "not ready" in err and "deployed version is unknown" in err, err
    assert w.live_merges() == []


@pytest.mark.parametrize(
    "case,needle",
    [
        ("no-probe", "deployed version is unknown"),
        ("not-json", "names no version.deployed_commit"),
        ("no-key", "names no version.deployed_commit"),
        ("deep-json", "names no version.deployed_commit"),
        ("not-a-name", "names no commit as the host guardian's"),
        ("unknown-object", "names no object in this clone"),
        ("a-tree", "names no commit (only other objects)"),
        ("oversize", "was not read"),
        ("symlink", "deployed version is unknown"),
        ("fifo", "is not a regular file"),
    ],
)
def test_every_unreadable_probe_is_a_named_refusal(dc, armed, capsys, case, needle):
    w = armed
    tip = w.rev("refs/remotes/origin/main")
    state = w.home / ".genesis" / "host_gateway_state.json"
    if case == "no-probe":
        _guardian(w)
    elif case == "not-json":
        _guardian(w, raw="{not json")
    elif case == "no-key":
        _guardian(w, raw=json.dumps({"version": {}}))
    elif case == "deep-json":
        _guardian(
            w, raw="[" * 20000 + "]" * 20000
        )  # under the 64 KiB cap, past the recursion limit
    elif case == "not-a-name":
        _guardian(w, "--upload-pack=x")
    elif case == "unknown-object":
        _guardian(w, "0123456789abcdef0123")
    elif case == "a-tree":
        _guardian(w, w.git(w.root, "rev-parse", f"{tip}^{{tree}}").stdout.strip())
    elif case == "oversize":
        _guardian(w, raw=json.dumps({"version": {"deployed_commit": tip}, "pad": "x" * 70000}))
    elif case == "symlink":
        real = w.tmp / "real_state.json"
        real.write_text(json.dumps({"version": {"deployed_commit": tip}}))
        _guardian(w)
        state.symlink_to(real)
    elif case == "fifo":
        _guardian(w)
        os.mkfifo(state)  # an open without O_NONBLOCK would hang here
    _refused(w, dc, capsys, needle)


def test_this_repository_now_carries_the_marker(dc):
    """The arming PR adds the marker; deleting it disarms every install."""
    res = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--error-unmatch", dc.gate.PR_C_MARKER],
        capture_output=True,
    )
    assert res.returncode == 0


def test_the_real_live_ban_is_named_by_its_full_commit(dc):
    label, sha = dc.gate.HOST_REQUIRED
    assert "#3546" in label and len(sha) == 40
