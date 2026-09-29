"""Which genesis-server subsystems regressed across a restart?

Run by scripts/update.sh, and kept in its own file so every deploy path that restarts
the server judges it the same way. Reads the bootstrap manifest the runtime
writes as the last step of GenesisRuntime.bootstrap(), accepts it ONLY when it was
written by the pid systemd runs as genesis-server (SERVER_PID), and compares it
with the pre-restart baseline (MANIFEST_BEFORE, owned by SERVER_PID_BEFORE).

Prints a comma-separated list: regressed subsystem names, `<name>:gone`, and
`check:*` sentinels for anything it could not establish. EMPTY means "checked,
nothing regressed" and nothing else. stdlib only; the venv may be mid-reinstall.

Environment: SERVER_PID, SERVER_PID_BEFORE, MANIFEST_BEFORE. The manifest path is
~/.genesis/bootstrap_manifest.json. The reasoning behind each rule is in
update.sh's health block, which calls this.
"""

import json
import os
import sys


def rank(value):
    """ok > degraded > everything else. Ordering only — never a pass/fail test."""
    s = str(value)
    return 2 if s == "ok" else 1 if s == "degraded" else 0


def owner_ok(doc, pid_want):
    """True IFF this document was written by pid_want.

    Identity, not recency. The file is user-global and the server is not its only
    writer (bridge, interactive terminal), so "written recently" cannot establish
    whose it is — any writer can land at any moment. "Written by the process
    systemd is running as genesis-server" is a yes/no fact.
    """
    return isinstance(doc, dict) and str(doc.get("pid")) == pid_want


def payload(doc):
    """The non-empty manifest mapping, or None. Kept SEPARATE from ownership so the
    two failures get distinct sentinels — "someone else wrote this" and "this is
    ours but says nothing" send a reader to completely different places."""
    m = doc.get("manifest") if isinstance(doc, dict) else None
    return m if isinstance(m, dict) and m else None


try:
    pid_want = (os.environ.get("SERVER_PID") or "").strip()
    if not pid_want or pid_want == "0":
        print("check:no-server-pid")
        sys.exit(0)
    with open(os.path.expanduser("~/.genesis/bootstrap_manifest.json")) as fh:
        doc = json.load(fh)
    if not owner_ok(doc, pid_want):
        # Written by the bridge, a terminal, or a previous boot. Unknown — and
        # unknown is reported, never treated as a clean bill of health.
        print("check:manifest-not-this-server")
        sys.exit(0)
    after = payload(doc)
    if after is None:
        print("check:manifest-empty")
        sys.exit(0)

    before, baseline_known = {}, False
    raw = (os.environ.get("MANIFEST_BEFORE") or "").strip()
    pid_before = (os.environ.get("SERVER_PID_BEFORE") or "").strip()
    # `!= "0"` is load-bearing: "0" is what systemd reports for a STOPPED unit, and
    # it is a TRUTHY string, so `raw and pid_before` alone would accept it and then
    # fail the comparison silently — reporting "no baseline" (which reads like a
    # first deploy) instead of "I read a stopped unit". Same falsy-check family that
    # review already caught here once.
    if raw and pid_before and pid_before != "0":
        try:
            d = json.loads(raw)
            if owner_ok(d, pid_before):
                b = payload(d)
                if b is not None:
                    before, baseline_known = b, True
        except Exception:
            pass

    bad = []
    if not baseline_known:
        # First deploy on this install, or the pre-restart manifest was not the old
        # server's. The check still runs, but it can only see hard failures — say so
        # rather than emitting a confident-looking empty result.
        bad.append("check:no-baseline")
    for name, status in sorted(after.items()):
        if rank(status) == 0:
            bad.append(name)  # hard failure, baseline or not
        elif not baseline_known:
            continue
        elif name not in before:
            # Arrived on THIS deploy already not-ok. The likeliest real regression:
            # a newly added init step whose module swallows its own exception and
            # so records "degraded" rather than "failed:".
            if rank(status) < 2:
                bad.append(name)
        elif rank(status) < rank(before[name]):
            bad.append(name)  # regressed across the restart
    if baseline_known:
        # Present before, absent after. A manifest key is written on BOTH branches of
        # _run_init_step, so an absent key means the step never ran at all — a
        # deleted or newly-skipped subsystem. A legitimate rename costs one false
        # positive, once; a silent drop costs the signal entirely.
        for name in sorted(set(before) - set(after)):
            if rank(before[name]) == 2:
                bad.append(name + ":gone")
    print(",".join(bad))
except Exception as exc:
    # Name the cause: this token is the only artefact the check leaves behind, and
    # a bare "unreadable" makes the one signal it exists to emit undiagnosable.
    print("check:manifest-unreadable(" + type(exc).__name__ + ")")
