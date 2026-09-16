#!/usr/bin/env python3
"""PreToolUse hook — blocks Write/Edit to CRITICAL protected paths in
AUTONOMOUS (dispatched) sessions.

Called by CC CLI via .claude/settings.json PreToolUse hook.
Reads the CC hook payload from stdin (via hook_input), extracts file_path,
checks against CRITICAL patterns from config/protected_paths.yaml.

Enforcement scope (matches the config's documented intent — CRITICAL paths
"cannot be modified from any relay/chat channel. Only modifiable from direct
CC CLI sessions"): every relay/chat channel reaches the filesystem through a
Genesis-DISPATCHED CC session, which cc/invoker.py stamps with
``GENESIS_CC_SESSION=1``. So the block applies to dispatched sessions only; a
direct interactive CLI session (no stamp — the user is present and sovereign)
is allowed through.

LOAD-CONTEXT CAVEAT (verified 2026-08-01): this is a PROJECT hook, and CC
discovers project settings by git-root detection. A default dispatched session
runs with cwd ``~/.genesis/background-sessions`` (outside any git repo), so CC
does NOT load project hooks there — this guard only *loads* for an in-repo /
worktree cwd. Its practical reach is therefore an in-repo dispatched session
(loads AND stamped → blocks). Autonomous file ops that never enter the repo cwd
are governed by the autonomy/protection layer, not this bridge. The gate is
still correct where it loads; it is not a claim of universal dispatched-session
coverage.

Path matching is TAIL-based: a repo-relative pattern like
``src/genesis/autonomy/protection.py`` matches that suffix under ANY checkout
root (main repo, a linked worktree, a fresh clone) — CC sends ABSOLUTE paths,
and a naive relative fnmatch silently never fired for the repo-relative
patterns (found inert 2026-08-01). Absolute patterns (``/etc/netplan/**``)
match as-given.

Exit codes:
  0 — allow (interactive session, or path is not CRITICAL)
  2 — block (dispatched session + CRITICAL path)

Emits SteerMessage for unified enforcement feedback when the genesis package
is importable; blocking NEVER depends on it (stdlib fallback message
otherwise — a fresh/broken install must not fail open, audit B4).
"""

import os
import sys
from fnmatch import fnmatch
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
try:
    from hook_input import field, read_payload, run_guard  # noqa: E402
except Exception:  # noqa: BLE001 — an unimportable hook_input must not VANISH.
    if __name__ != "__main__":
        raise
    # NOTHING TO FALL BACK ON: hook_input is the module that would recover us —
    # read_payload and degraded_exit both live there — so nothing imported can help.
    # An unguarded import here exits 1, which Claude Code reads as NON-BLOCKING, and
    # the CRITICAL-path Write/Edit gate simply disappears. So this block decides the
    # verdict itself, using only `os` and `sys`, which are bound at module top and
    # survive whatever broke.
    #
    # THE VERDICT IS THE GUARD'S OWN SCOPE QUESTION, asked with the one input that
    # cannot fail. This guard blocks CRITICAL-path Write/Edit in AUTONOMOUS sessions
    # ONLY; an interactive session is allowed through by design (see _is_dispatched
    # below — the user is present and sovereign). Refusing unconditionally here is
    # therefore not the conservative choice, it is STRICTLY MORE than this guard was
    # ever scoped to do: it blocks the interactive owner, whom it would always have
    # allowed, from editing anything at all.
    #
    # That costs the repair path. Every Bash-firing guard that is not declared advisory
    # refuses every command in this same state, so an interactive session that also
    # cannot Edit can neither run a command nor fix the file that is broken.
    #
    # The SIZE of that refusal is deliberately not written here. Four hand-written
    # copies of it existed across this repo and every one of them went wrong: three by
    # miscounting (`grep degraded_exit(` counts the HELPER, not the condition — on this
    # leg `degraded_exit` is unreachable and each guard refuses from its own import
    # handler), and the fourth by going stale when someone wired one more Bash hook,
    # with nobody being wrong at all. It is now derived on every test run by
    # tests/test_hooks/test_import_time_degraded.py::
    # test_every_bash_hook_declares_its_degrade_direction, which also fails if a guard
    # is ever wired in a spelling that enumeration cannot resolve. This box is headless,
    # with no
    # operator at a console. Blocking here does not protect the machine; it bricks it.
    # Restoring the allow surrenders nothing, because the healthy guard permits exactly
    # this call (locked by test_healthy_pretool_check_allows_the_interactive_critical_write).
    #
    # A DISPATCHED session still refuses: nobody is present to approve, and the
    # unattended direction is where fail-closed belongs.
    # Read inside its own guard, defaulting to the REFUSING side. This module's
    # rule is that nothing here may raise — an escaping exception exits 1, which CC
    # reads as non-blocking, reintroducing the fail-open this block exists to stop —
    # and this is the one statement that DECIDES the verdict. No raise is reachable
    # through `os.environ.get` on a literal ASCII key, so this is defence in depth
    # rather than a live defect; it costs three lines and removes the question.
    _dispatched = True  # an unreadable environment is not a waiver
    try:  # noqa: SIM105 — contextlib would widen this block's imports; see above.
        _dispatched = os.environ.get("GENESIS_CC_SESSION") == "1"
    except BaseException:  # noqa: BLE001 — an unreadable env cannot decide a verdict.
        pass
    # TWO CHANNELS, because Claude Code DISCARDS stderr from an exit-0 hook: on the
    # allow path stderr reaches nobody and a degraded guard would look exactly like a
    # healthy one. hookSpecificOutput.additionalContext on STDOUT is what survives
    # exit 0. hook_input._degraded_say is the canonical renderer and is unreachable
    # here by construction, so the JSON is a fixed literal — no serializer, no
    # interpolation, nothing that needs escaping, and therefore nothing that can fail.
    # Degrade the renderer, never the verdict.
    try:
        if _dispatched:
            sys.stderr.write(
                "GUARD DEGRADED (pretool_check): shared hook_input could not be "
                "imported; BLOCKING this dispatched session until the hook tree is "
                "repaired.\n"
            )
            sys.stderr.flush()
        else:
            sys.stdout.write(
                '{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
                '"additionalContext": "GUARD DEGRADED (pretool_check): shared '
                "hook_input could not be imported, so the CRITICAL-path Write/Edit "
                "check did not run for this call. ALLOWING: this guard only ever "
                "blocks dispatched sessions, and refusing an interactive one would "
                "take away the edit that repairs scripts/hooks/hook_input.py. Repair "
                'the hook tree; Bash stays blocked until you do."}}\n'
            )
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001 — a diagnostic cannot decide a verdict.
        pass
    os._exit(2 if _dispatched else 0)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "protected_paths.yaml"

# Hardcoded fallback — protects the most dangerous paths even when config is
# missing or corrupted.  Fail-closed: if we can't load the full config, at
# least these patterns are still enforced.
_FALLBACK_CRITICAL = [
    "*/secrets.env",
    ".claude/settings.json",
    "src/genesis/autonomy/protection.py",
    "config/protected_paths.yaml",
    "scripts/systemd/*.template",
]


def _load_critical_patterns() -> list[str]:
    """Load CRITICAL path patterns from config, falling back to hardcoded list.

    ``yaml`` is imported HERE, not at module top level: an import-time failure
    (venv-less install, missing PyYAML) would crash the script before run_guard
    could catch it — historically exit 1 = silent fail-open. Degrading to the
    fallback set keeps the most dangerous paths enforced instead.
    """
    try:
        import yaml

        # `or {}`: an empty YAML file parses to None — degrade to the fallback
        # instead of crashing on None.get (the pre-2026-08 fail-open bug).
        data = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception as exc:
        print(
            f"WARNING: protected_paths.yaml load failed ({exc}), using fallback",
            file=sys.stderr,
        )
        return list(_FALLBACK_CRITICAL)
    patterns = []
    for rule in data.get("critical", []):
        patterns.append(rule["pattern"])
    return patterns or list(_FALLBACK_CRITICAL)


def _matches(path: str, patterns: list[str]) -> str | None:
    """Return the matching pattern if ``path`` matches any CRITICAL pattern.

    A pattern is tried against the full path AND every '/'-suffix of it, so a
    repo-relative pattern hits the file under any checkout root. CC delivers
    absolute paths — matching only the verbatim string left every repo-relative
    pattern inert (verified live 2026-08-01: settings.json / protection.py /
    protected_paths.yaml never matched). Suffix matching deliberately
    over-matches a same-named path outside any checkout — acceptable, because
    the block applies only to dispatched sessions, where conservative is right.
    """
    normalized = path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    candidates = [normalized] + ["/".join(parts[i:]) for i in range(len(parts))]
    for pattern in patterns:
        for cand in candidates:
            if fnmatch(cand, pattern):
                return pattern
        # Handle ** recursive glob (fnmatch's * doesn't cross '/' semantics
        # here are fine, but a bare prefix check keeps legacy behavior).
        if "**" in pattern:
            prefix = pattern.split("**")[0]
            for cand in candidates:
                if cand.startswith(prefix):
                    return pattern
    return None


def _is_dispatched() -> bool:
    """True in a Genesis-dispatched (autonomous/relay) CC session.

    cc/invoker.py stamps ``GENESIS_CC_SESSION=1`` on every dispatched session;
    a user-launched interactive session does not carry it. Mirrors the same
    check in git_push_guard.
    """
    return os.environ.get("GENESIS_CC_SESSION") == "1"


def _block_message(matched: str, file_path: str) -> str:
    """The block text — via SteerMessage when genesis is importable, else a
    plain-text equivalent. Blocking must never depend on the genesis package
    (audit B4: the import lived on the block path, so a fresh/broken install
    crashed → exit 1 → CC ran the Write anyway)."""
    suggestion = (
        "CRITICAL paths cannot be modified from an autonomous/dispatched "
        "session. Ask the user to make this change from an interactive "
        "Claude Code session."
    )
    try:
        from genesis.autonomy.steering import SteerMessage
        from genesis.autonomy.types import ApprovalDecision, EnforcementLayer

        return SteerMessage(
            layer=EnforcementLayer.PERMISSION_GATE,
            rule_id="critical_protected_path",
            decision=ApprovalDecision.BLOCK,
            severity="critical",
            title="CRITICAL protected path",
            context=f"Matches pattern '{matched}'",
            suggestion=suggestion,
            tool_name="Write",
            file_path=file_path,
        ).to_stderr()
    except Exception:
        return (
            f"BLOCKED [critical_protected_path]: {file_path} matches CRITICAL "
            f"pattern '{matched}'.\n  Fix: {suggestion}"
        )


def main() -> int:
    file_path = field(read_payload(), "file_path")
    if not file_path:
        return 0

    if not _is_dispatched():
        # Direct interactive CLI session — the user is present and sovereign;
        # CRITICAL protection targets relay/autonomous channels only (see the
        # config header). Allow.
        return 0

    patterns = _load_critical_patterns()
    matched = _matches(file_path, patterns)
    if matched:
        print(_block_message(matched, file_path), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    run_guard(main, "pretool_check")
