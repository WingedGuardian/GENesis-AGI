"""cc_exit_capture.sh — durable record of why a CC session died.

A CC session runs as tmux ``... exec claude`` (cc-slot.sh). When claude exits the
pane vanishes with its output, so a dying session leaves no trace — the
2026-08-19 death was undiagnosable for exactly this reason. cc-slot.sh now drops
the inner ``exec`` and calls cc_exit_capture.sh on claude's return to log the
exit status (signal-decoded) + a pane-scrollback tail.

Two layers: (1) the script's status/hint/rotation logic, tested directly with
TMUX_PANE unset (no tmux needed); (2) the FULL flow — the exact cc-slot inner
command shape with a fake crashing ``claude`` — in a scratch tmux server on a
private ``-L`` socket, so the real cc-* sessions are never touched.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

_CAPTURE = Path(__file__).resolve().parents[2] / "scripts" / "cc_exit_capture.sh"


def _run_capture(home: Path, slot: str, ec: str, tmux_pane: str | None = None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("TMUX_PANE", None)
    if tmux_pane is not None:
        env["TMUX_PANE"] = tmux_pane
    return subprocess.run(
        [str(_CAPTURE), slot, ec],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _log_for(home: Path, slot: str) -> Path:
    return home / ".genesis" / "logs" / f"cc_exit_{slot}.log"


# ── Layer 1: script logic, no tmux ───────────────────────────────────────


def test_records_exit_status_and_clean_hint(tmp_path):
    home = tmp_path / "home"
    r = _run_capture(home, "1", "0")
    assert r.returncode == 0, r.stderr
    body = _log_for(home, "1").read_text()
    assert "cc-1 claude exited status=0" in body
    assert "clean exit" in body


def test_decodes_fatal_signals(tmp_path):
    home = tmp_path / "home"
    cases = {
        "134": "SIGABRT",  # V8/Node fatal abort
        "137": "SIGKILL",  # OOM kill
        "139": "SIGSEGV",
        "143": "SIGTERM",
    }
    for ec in cases:
        _run_capture(home, "2", ec)
    body = _log_for(home, "2").read_text()
    for ec, marker in cases.items():
        assert f"status={ec}" in body, body
        assert marker in body, f"missing {marker} decode:\n{body}"


def test_never_fails_when_home_unwritable(tmp_path):
    """Best-effort: a capture problem must never surface a non-zero exit (it would
    otherwise change the session's exit code). Point HOME at a non-dir."""
    bad = tmp_path / "afile"
    bad.write_text("not a dir")
    env = dict(os.environ)
    env["HOME"] = str(bad)
    env.pop("TMUX_PANE", None)
    r = subprocess.run(
        [str(_CAPTURE), "1", "137"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0, f"capture must exit 0 even when it cannot log: {r.stderr}"


def test_log_is_owner_only(tmp_path):
    """The captured tail can contain credentials/prompts → the log must be 0600
    so another local account can't read it."""
    home = tmp_path / "home"
    _run_capture(home, "1", "0")
    mode = stat.S_IMODE(_log_for(home, "1").stat().st_mode)
    assert mode == 0o600, f"log mode {oct(mode)} (must be 0600)"


def test_pre_existing_log_is_tightened(tmp_path):
    """A log that predates this hardening (mode 0644) is tightened to 0600 on the
    next append — not left world-readable."""
    home = tmp_path / "home"
    log = _log_for(home, "2")
    log.parent.mkdir(parents=True)
    log.write_text("older entry\n")
    os.chmod(log, 0o644)
    _run_capture(home, "2", "137")
    mode = stat.S_IMODE(log.stat().st_mode)
    assert mode == 0o600, f"pre-existing log not tightened: {oct(mode)}"


def test_log_self_rotates(tmp_path):
    home = tmp_path / "home"
    log = _log_for(home, "3")
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(f"old line {i}" for i in range(5000)) + "\n")
    _run_capture(home, "3", "0")
    lines = log.read_text().splitlines()
    assert len(lines) <= 2000 + 5, f"log not rotated: {len(lines)} lines"
    assert "cc-3 claude exited status=0" in log.read_text()


# ── Layer 2: full flow in a scratch tmux (never the live sessions) ───────


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_full_flow_captures_crash_in_scratch_tmux(tmp_path):
    """Mimic cc-slot's exact inner command with a fake crashing claude, in a
    private tmux server. Assert the exit status AND the pane's dying words are
    captured, and the session actually ends (exit-code preservation)."""
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    marker = f"CRASH_MARKER_{uuid.uuid4().hex[:8]}"
    fake_claude = tmp_path / "fakeclaude.sh"
    fake_claude.write_text(f"#!/usr/bin/env bash\necho '{marker}'\nexit 134\n")
    fake_claude.chmod(0o755)

    sock = f"ccexit-test-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    # The exact shape cc-slot.sh builds: run claude (no exec), capture on return,
    # preserve the exit code. HOME is set so capture logs into our sandbox.
    inner = (
        f"HOME={home} '{fake_claude}'; __ec=$?; "
        f"HOME={home} '{_CAPTURE}' 7 $__ec >/dev/null 2>&1; exit $__ec"
    )
    try:
        subprocess.run(
            ["tmux", "-L", sock, "new-session", "-d", "-s", "t", inner],
            check=True,
            capture_output=True,
            text=True,
        )
        # Condition-based wait (bounded): the session ends when the inner cmd exits.
        deadline = time.monotonic() + 15
        gone = False
        while time.monotonic() < deadline:
            r = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "t"], capture_output=True)
            if r.returncode != 0:
                gone = True
                break
            time.sleep(0.2)
        assert gone, "session never ended — exit-code preservation broken"
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    body = _log_for(home, "7").read_text()
    assert "cc-7 claude exited status=134" in body, body
    assert "SIGABRT" in body, body
    assert marker in body, f"pane dying words not captured:\n{body}"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_scrollback_credentials_are_redacted(tmp_path):
    """A credential printed into the pane must not reach the log verbatim.

    The motivating shape: a CLI that prints a freshly-minted long-lived token
    to stdout. The capture takes 200 lines of raw scrollback, so without
    redaction the token is persisted in plaintext. Benign output in the same
    scrollback must survive, or the log stops being useful for diagnosis.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    benign = f"BENIGN_MARKER_{uuid.uuid4().hex[:8]}"
    secret = "sk-ant-oat01-" + "T" * 60  # synthetic, never a real credential
    fake_claude = tmp_path / "fakeclaude.sh"
    fake_claude.write_text(
        f"#!/usr/bin/env bash\necho '{benign}'\necho 'Your token:'\necho '{secret}'\nexit 0\n"
    )
    fake_claude.chmod(0o755)

    sock = f"ccexit-red-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    inner = (
        f"HOME={home} '{fake_claude}'; __ec=$?; "
        f"HOME={home} '{_CAPTURE}' 8 $__ec >/dev/null 2>&1; exit $__ec"
    )
    try:
        subprocess.run(
            ["tmux", "-L", sock, "new-session", "-d", "-s", "t", inner],
            check=True, capture_output=True, text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            r = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "t"], capture_output=True)
            if r.returncode != 0:
                break
            time.sleep(0.2)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    body = _log_for(home, "8").read_text()
    assert secret not in body, "credential persisted verbatim into the exit log"
    assert "T" * 60 not in body, "credential body persisted into the exit log"
    assert "cc-8 claude exited status=0" in body, body
    assert benign in body, f"redaction ate benign scrollback:\n{body}"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_a_credential_split_by_the_terminal_wrap_is_still_redacted(tmp_path):
    """The terminal's soft wrap was a redaction hole.

    A token longer than the pane is wide is stored by tmux as several physical
    rows. Captured without ``-J`` those rows come back as separate lines, so
    only the FIRST fragment still carries the vendor prefix the pattern needs —
    the remaining fragments are unmatchable and the token is persisted in
    reconstructable form. This drives the pane deliberately narrow so the token
    cannot fit on one row, which is the ordinary case at any real pane width
    for a long-lived token.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    benign = f"BENIGN_MARKER_{uuid.uuid4().hex[:8]}"
    tail_marker = "W" * 30  # lands in a LATER wrapped row, never the first
    secret = "sk-ant-oat01-" + "T" * 60 + tail_marker
    fake_claude = tmp_path / "fakeclaude.sh"
    fake_claude.write_text(
        f"#!/usr/bin/env bash\necho '{benign}'\necho '{secret}'\nexit 0\n"
    )
    fake_claude.chmod(0o755)

    sock = f"ccexit-wrap-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    inner = (
        f"HOME={home} '{fake_claude}'; __ec=$?; "
        f"HOME={home} '{_CAPTURE}' 9 $__ec >/dev/null 2>&1; exit $__ec"
    )
    try:
        subprocess.run(
            ["tmux", "-L", sock, "new-session", "-d", "-s", "t",
             "-x", "40", "-y", "10", inner],
            check=True, capture_output=True, text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            r = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "t"], capture_output=True)
            if r.returncode != 0:
                break
            time.sleep(0.2)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    body = _log_for(home, "9").read_text()
    assert tail_marker not in body, (
        "a wrapped credential's later rows were persisted unredacted:\n" + body
    )
    assert "T" * 30 not in body, "wrapped credential body persisted:\n" + body
    assert benign in body, f"redaction ate benign scrollback:\n{body}"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_a_credential_split_by_the_input_cap_is_not_written_out(tmp_path):
    """The byte cap must not hand a pattern half a value.

    Several patterns are recognised by a TERMINATOR: a URL credential needs its
    `@host`. A byte cut lands anywhere, so a capture truncated mid-URL presents
    `postgres://user:password` with the `@` just past the cut — matching
    nothing, and writing the password out. Cutting on a line boundary instead
    drops the over-long line whole, so no pattern is ever shown part of one.

    The cap is both lowered AND COMPUTED so the cut is guaranteed to land
    between the password and its `@`. An earlier version of this test picked a
    round number, the cut landed in the padding before the URL even started,
    and it passed against the unfixed code — it proved nothing.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    first_line = "BENIGNLINE"
    pad = "x" * 50
    secret = "s3cr3tpassword"
    url_head = f"postgres://user:{secret}"
    # feed is "<first_line>\n<pad> <url_head>@host/db"; cut immediately after
    # the password and before the '@' that the pattern needs to match.
    cap = len(first_line) + 1 + len(pad) + 1 + len(url_head)
    fake_claude = tmp_path / "fakeclaude.sh"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        f"echo '{first_line}'\n"
        f"echo '{pad} {url_head}@host/db'\n"
        "exit 0\n"
    )
    fake_claude.chmod(0o755)

    sock = f"ccexit-cap-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    inner = (
        f"HOME={home} '{fake_claude}'; __ec=$?; "
        f"HOME={home} GENESIS_CC_TAIL_CAP={cap} '{_CAPTURE}' 4 $__ec >/dev/null 2>&1; "
        "exit $__ec"
    )
    try:
        subprocess.run(
            ["tmux", "-L", sock, "new-session", "-d", "-s", "t",
             "-x", "300", "-y", "12", inner],
            check=True, capture_output=True, text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            r = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "t"], capture_output=True)
            if r.returncode != 0:
                break
            time.sleep(0.2)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    body = _log_for(home, "4").read_text()
    assert secret not in body, (
        f"a credential split by the input cap was written out:\n{body}"
    )
    assert "cc-4 claude exited status=0" in body, body


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_an_overcap_capture_keeps_the_newest_output(tmp_path):
    """When the cap bites, the log must keep the NEWEST bytes — the dying words.

    The newest rows sit at the BOTTOM of the capture, and the whole point of
    this log is why the session died. A cap taken from the top keeps the oldest
    scrollback, discards the dying words, and stamps a marker claiming the
    opposite ("earlier output dropped"). So three assertions: the last line
    printed survives, the first line printed is gone, and the marker is there.

    The cap (700) is far above the newest lines' byte count and far below the
    oldest filler's, so the verdict cannot hinge on pane geometry (trailing
    blank rows cost at most a few bytes against a wide margin).
    """
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    fake_claude = tmp_path / "fakeclaude.sh"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        # ~2,800 bytes of numbered old filler, then the dying words.
        "for i in $(seq -w 0 49); do printf 'OLD-%s %s\\n' \"$i\" "
        "\"$(printf 'x%.0s' $(seq 1 50))\"; done\n"
        "echo 'DYINGWORDS123'\n"
        "exit 0\n"
    )
    fake_claude.chmod(0o755)

    sock = f"ccexit-newest-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    inner = (
        f"HOME={home} '{fake_claude}'; __ec=$?; "
        f"HOME={home} GENESIS_CC_TAIL_CAP=700 '{_CAPTURE}' 7 $__ec >/dev/null 2>&1; "
        "exit $__ec"
    )
    try:
        subprocess.run(
            ["tmux", "-L", sock, "new-session", "-d", "-s", "t",
             "-x", "200", "-y", "12", inner],
            check=True, capture_output=True, text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            r = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "t"], capture_output=True)
            if r.returncode != 0:
                break
            time.sleep(0.2)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    body = _log_for(home, "7").read_text()
    assert "DYINGWORDS123" in body, f"the dying words were truncated away:\n{body}"
    assert "OLD-00 " not in body, f"oldest scrollback survived an over-cap capture:\n{body}"
    assert "earlier output dropped" in body, f"truncation marker missing:\n{body}"


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_tail_is_withheld_when_scrubber_is_unavailable(tmp_path):
    """Fail CLOSED: with no usable scrubber the tail is WITHHELD, never raw.

    Verified by running a copy of the script whose sibling ``hooks/`` directory
    does not exist — the same state a partial checkout would produce. The
    exit-status diagnosis must still be written, since that is the primary
    reason the log exists.
    """
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    # A copy with NO hooks/ sibling -> secret_scrub.py unreadable -> scrub fails.
    isolated = tmp_path / "nohooks"
    isolated.mkdir()
    capture_copy = isolated / "cc_exit_capture.sh"
    capture_copy.write_text(Path(_CAPTURE).read_text())
    capture_copy.chmod(0o755)

    secret = "sk-ant-oat01-" + "W" * 60  # synthetic
    fake_claude = tmp_path / "fakeclaude2.sh"
    fake_claude.write_text(f"#!/usr/bin/env bash\necho '{secret}'\nexit 0\n")
    fake_claude.chmod(0o755)

    sock = f"ccexit-fc-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    inner = (
        f"HOME={home} '{fake_claude}'; __ec=$?; "
        f"HOME={home} '{capture_copy}' 9 $__ec >/dev/null 2>&1; exit $__ec"
    )
    try:
        subprocess.run(
            ["tmux", "-L", sock, "new-session", "-d", "-s", "t", inner],
            check=True, capture_output=True, text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            r = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "t"], capture_output=True)
            if r.returncode != 0:
                break
            time.sleep(0.2)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    body = _log_for(home, "9").read_text()
    assert secret not in body, "raw tail written when the scrubber was unavailable"
    assert "withheld" in body, f"no withheld-tail marker:\n{body}"
    assert "cc-9 claude exited status=0" in body, body


def test_scrub_filter_decodes_bytes_leniently():
    """The scrub filter must not abort on an undecodable byte.

    Scope, honestly: ``tmux capture-pane -p`` normalises stray bytes out, so
    this is NOT reachable through the tmux path today (measured — a raw 0xff
    never survives capture). It guards the FILTER itself, which is a plain
    stdin->stdout stage: a strict decode there turns one bad byte into a
    discarded 200-line diagnostic, in exactly the crash case the tail exists
    for. The snippet is extracted from the script rather than restated, so this
    fails if the real decode ever regresses.
    """
    src = Path(_CAPTURE).read_text()
    m = re.search(r"python3 -c '(.*?)' \"\$_CC_HOOKS_DIR\"", src, re.S)
    assert m, "could not locate the scrub filter snippet in the script"
    snippet = m.group(1)

    hooks_dir = str(Path(_CAPTURE).resolve().parent / "hooks")
    secret = "sk-ant-oat01-" + "R" * 60  # synthetic
    payload = b"bad \xff byte KEEPME\n" + secret.encode() + b"\n"
    r = subprocess.run(
        [sys.executable, "-c", snippet, hooks_dir], input=payload, capture_output=True
    )
    assert r.returncode == 0, f"filter aborted on a non-UTF-8 byte: {r.stderr.decode()}"
    out = r.stdout.decode()
    assert "KEEPME" in out, "surrounding diagnostic text was lost"
    assert secret not in out, "credential survived the lenient decode path"


