"""Guardrail: the slot door must not make tmux log an error on every login.

WHAT THIS FILE MEASURES, AND WHAT IT DOES NOT
---------------------------------------------
The observable here is the SERVER's message log (`show-messages`): one
`can't find session` entry per absent `has-session` probe. That is the thing
the fix actually removes, and it is what the operator's own `show-messages`
output showed.

It is NOT a measurement of the status line, and an earlier version of this
docstring said it was. The claim then was that a tmux error is "painted on
every attached client's status line", which would make the `2>/dev/null` on
the old call sites useless. MEASURED and REFUTED on tmux 3.4, 2026-09-25: with
a client attached through a pty, three absent probes added three log entries
and ZERO bytes to that client's stream, while a `display-message` control on
the same client painted at row 24 in black-on-yellow, 1/1. For an ssh
RemoteCommand door the issuing client has no session, so tmux routes the error
to that client's own stderr -- which the redirect did suppress.

The fix is still right: an erroring probe on a path where absent is the
expected answer is per-login log noise, and the error's destination depends on
the invocation shape (from inside a pane the same command writes to the pane).
The claim about its blast radius was simply too large.

THE PROBES THIS PINS
--------------------
Same shape as the lobby door's (#2298), pinned here because the slot door is
the OTHER entry point the operator's terminals run on every SSH connection.

``cc-slot.sh`` probed with ``has-session`` in three places, and the ones that
matter fire on SUCCESS paths, where "absent" is the ANSWER rather than a fault:

  - the free-slot search (``while has-session ...; do SLOT++; done``) exits by
    FAILING, so allocating any slot at all emits exactly one message;
  - the two pre-create existence checks emit one each whenever the slot is new,
    which is every first connection to a slot.

WHAT THIS FILE ASSERTS, AND WHAT IT DOES NOT
--------------------------------------------
The behavioural test runs the SHIPPED ``_session_exists`` text -- extracted from
``cc-slot.sh`` itself, not a copy pasted into this file -- against a real tmux
server with a real client attached, and asserts it adds no message. It carries
its own POSITIVE CONTROL: the same probe written with ``has-session`` must add
one. Without that control a zero is unfalsifiable, because a test that never
attached a client scores zero for both spellings.

It deliberately does NOT drive ``cc-slot.sh`` end to end. That script allocates
capacity, consults an OOM floor, and launches an agent; standing a faithful one
up in a unit test would be a second implementation of the thing under test. The
static companion closes that gap from the other side by asserting no
``has-session`` call site survives anywhere in the file, so a fourth one added
later fails here rather than on the operator's status line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SLOT = REPO / "scripts" / "cc-slot.sh"

_needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("script") is None,
    reason="needs tmux and script(1) to attach a real client",
)


def _extract_helper() -> str:
    """Return the shipped `_session_exists` definition, verbatim.

    Extracted rather than retyped: a copy in this file would pass forever while
    the real function rotted. If the definition is ever renamed or reshaped,
    this raises instead of silently testing nothing.
    """
    text = SLOT.read_text()
    m = re.search(r"^_session_exists\(\) \{\n.*?^\}$", text, re.M | re.S)
    assert m, "cc-slot.sh no longer defines _session_exists() at column 0"
    return m.group(0)


def _helper_lines() -> set[str]:
    """The lines of the shipped `_session_exists` body, for exempting its own
    fallback from the has-session scan without hardcoding a line number."""
    return {ln for ln in _extract_helper().splitlines()}


def _tmux(sock: str, *args: str):
    env = dict(os.environ)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = "/tmp"
    return subprocess.run(
        ["tmux", "-L", sock, *args], env=env, capture_output=True, text=True, timeout=60
    )


def _misses(sock: str) -> int:
    return _tmux(sock, "show-messages").stdout.count("can't find session")


@_needs_tmux
def test_the_shipped_probe_adds_no_message_while_has_session_does(tmp_path):
    sock = f"ccslotprobe{os.getpid()}"

    env = dict(os.environ)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = "/tmp"
    # Runners set no TERM, and tmux needs terminfo to attach even through the
    # pty script(1) provides. Without this the client never attaches, the guard
    # below skips, and the test goes dormant on CI while reading as a clean run
    # -- MEASURED on #2298, where exactly that happened.
    env["TERM"] = "xterm-256color"

    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/bin/bash\nset -euo pipefail\n"  # the same flags cc-slot.sh runs under
        f'tmux() {{ command tmux -L {sock} "$@"; }}\n'
        f"{_extract_helper()}\n"
        '_session_exists "$1"\n'
    )
    probe.chmod(0o755)

    client = None
    client_fh = None
    try:
        _tmux(sock, "new-session", "-d", "-s", "cc-1", "sleep", "120")
        time.sleep(0.5)

        client_log = tmp_path / "client.log"
        client_fh = client_log.open("wb")
        client = subprocess.Popen(
            ["script", "-qec", f"tmux -L {sock} attach -t cc-1", "/dev/null"],
            env=env,
            stdout=client_fh,
            stderr=subprocess.STDOUT,
        )
        # POLL, do not sleep a fixed interval. A fixed 2s wait is a coin-flip
        # on a loaded CI runner, and losing it produces a `pytest.skip` -- which
        # the skip CEILING cannot distinguish from any other test skipping, so a
        # compensating change elsewhere would let the ceiling CERTIFY this test
        # as dormant. That is the exact failure #2298's ci.yml note warns about.
        _deadline = time.monotonic() + 30
        while time.monotonic() < _deadline:
            if _tmux(sock, "list-clients").stdout.strip():
                break
            time.sleep(0.25)

        # Guard-the-guard: tmux logs these against the SERVER, but
        # `show-messages` needs a target client -- with none attached it exits 1
        # with `no current client` and this test can read nothing. (Not, as this
        # comment previously said, that the messages are only recorded when a
        # client is attached: MEASURED, they are logged either way, and
        # attaching afterwards reveals entries logged while nothing was.)
        if not _tmux(sock, "list-clients").stdout.strip():
            client.kill()
            client.wait(timeout=5)
            client_fh.close()
            why = client_log.read_text(errors="replace").strip() or "(said nothing)"
            pytest.skip(f"could not attach a client on this runner: {why}")

        # POSITIVE CONTROL FIRST. If this does not move, the measurement is
        # broken and the zero below would be meaningless.
        before = _misses(sock)
        subprocess.run(
            ["tmux", "-L", sock, "has-session", "-t", "=cc-absent"],
            env=env,
            capture_output=True,
            timeout=60,
        )
        time.sleep(0.3)
        control = _misses(sock) - before
        assert control == 1, (
            "positive control failed: `tmux has-session` on a missing session "
            f"added {control} message(s), expected 1. The measurement is not "
            "working, so the shipped-probe result below cannot be trusted."
        )

        # THE CLAIM: the shipped probe answers the same question silently.
        before = _misses(sock)
        got = subprocess.run([str(probe), "cc-absent"], env=env, capture_output=True, timeout=60)
        time.sleep(0.3)
        added = _misses(sock) - before

        assert added == 0, (
            f"the shipped _session_exists made tmux log {added} error(s) to the "
            "server message log; the whole point of it is that it logs none."
        )
        # ...and it must still be CORRECT, or silence is trivially achievable.
        assert got.returncode != 0, "probe reported a missing session as present"
        present = subprocess.run([str(probe), "cc-1"], env=env, capture_output=True, timeout=60)
        assert present.returncode == 0, "probe reported an existing session as absent"
    finally:
        if client is not None:
            client.kill()
        if client_fh is not None:
            client_fh.close()
        _tmux(sock, "kill-server")


def test_no_has_session_call_site_survives_in_the_slot_door():
    """Static half: runs anywhere, including a runner with no tmux.

    Comments are stripped before matching, because the fix deliberately KEEPS
    the words `has-session` in prose explaining why it is wrong -- counting
    string occurrences instead of call sites is how the first reading of this
    got the number wrong (3 strings, 2 calls).
    """
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(SLOT.read_text().splitlines(), 1)
        # `#.*$` also truncates at the `#` that opens a tmux FORMAT string, so
        # a line like `-f "#{==:...}"` is cut short. Harmless for this search --
        # nothing it could hide contains `tmux has-session` -- but the stripping
        # is not general, and the `(?<!#)` lookbehind is dead once it has run.
        # The enumeration is also not closed: a line-continued call
        # (`tmux \` / newline / `has-session`) or an indirected binary
        # (`"$TMUX_BIN" has-session`) would not be seen either.
        if re.search(r"\btmux has-session\b", re.sub(r"#.*$", "", line))
        # ONE call site is legitimate: _session_exists falls back to the legacy
        # probe when the filtered query ERRORS, because answering correctly then
        # matters more than the log entry. Scoping by the helper's own body
        # rather than by a line number, so the exemption cannot drift onto some
        # other call that happens to move into that range.
        and line not in _helper_lines()
    ]
    assert not offenders, (
        "cc-slot.sh calls `tmux has-session`, which logs a server-side error on "
        "the absent path -- on a login path where absent is the expected "
        "answer. Use _session_exists instead:\n  " + "\n  ".join(offenders)
    )
    assert "_session_exists()" in SLOT.read_text(), (
        "cc-slot.sh must define _session_exists -- the silent replacement for "
        "has-session. Its absence means the probes went somewhere unreviewed."
    )


def _run_helper_with_fake_tmux(tmp_path, name, *, filter_supported, present):
    """Run the SHIPPED helper against a fake tmux with a chosen capability.

    Extracted, not retyped, for the same reason as everywhere else in this file:
    a copy would pass forever while the real function rotted.
    """
    bin_dir = tmp_path / f"bin-{name}-{filter_supported}-{present}"
    bin_dir.mkdir()
    names = "cc-1\ncc-2" if present else "cc-9"
    fake = bin_dir / "tmux"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "list-sessions" ]; then\n'
        f"    if [ {'0' if filter_supported else '1'} -ne 0 ]; then\n"
        "        echo 'unknown option -- f' >&2; exit 1\n"
        "    fi\n"
        "    want=''\n"
        "    for a in \"$@\"; do\n"
        '        case "$a" in "#{==:#{session_name},"*)\n'
        "            want=${a#\\#\\{==:\\#\\{session_name\\},}; want=${want%\\}} ;;\n"
        "        esac\n"
        "    done\n"
        f"    printf '%s\\n' '{names}' | grep -xF -- \"$want\" || true\n"
        "    exit 0\n"
        "fi\n"
        'if [ "$1" = "has-session" ]; then\n'
        "    want=$3; want=${want#=}\n"
        f"    printf '%s\\n' '{names}' | grep -qxF -- \"$want\" || "
        "{ echo \"can't find session: $want\" >&2; exit 1; }\n"
        "    exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    fake.chmod(0o755)

    probe = tmp_path / f"probe-{name}-{filter_supported}-{present}.sh"
    probe.write_text("#!/bin/bash\nset -euo pipefail\n" + _extract_helper() + '\n_session_exists "$1"\n')
    probe.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run([str(probe), name], env=env, capture_output=True, timeout=60)


def test_a_filtered_query_that_ERRORS_is_not_read_as_absent(tmp_path):
    """The correctness half of the fix, and the half a silent fallback loses.

    `list-sessions -f` and `#{==:}` postdate `has-session`, so on an older tmux
    the filtered query errors while the server is perfectly healthy. Collapsing
    that into "absent" would let manual mode hand out an OCCUPIED slot, and
    would push a reattach through the capacity gate it exists to bypass --
    denying the login, or reclaiming another session, instead of attaching to
    the slot sitting right there. Raised by review on #2341.

    Run as a 2x2 rather than just the fallback arm. If the filter-supported
    cells agreed with the filter-erroring ones by accident -- because the
    helper was answering from the fallback all along -- a single-arm test would
    pass while measuring nothing.
    """
    for name, filt, present, want_rc in (
        ("cc-1", True, True, 0),     # filter works, session present
        ("cc-1", True, False, 1),    # filter works, session absent
        ("cc-1", False, True, 0),    # filter ERRORS, session present  <- the fix
        ("cc-1", False, False, 1),   # filter ERRORS, session absent
    ):
        got = _run_helper_with_fake_tmux(
            tmp_path, name, filter_supported=filt, present=present
        )
        assert got.returncode == want_rc, (
            f"filter_supported={filt} present={present}: expected rc={want_rc}, "
            f"got {got.returncode}; stderr={got.stderr!r}"
        )
