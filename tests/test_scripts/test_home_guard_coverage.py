"""Guardrail: scripts using ``set -u`` + ``$HOME`` must resolve HOME before use.

A stripped-env invocation (HOME unset) aborts a ``set -u`` script at its first
``${HOME}`` expansion with "HOME: unbound variable". Scripts that both enable
``set -u`` and dereference ``$HOME`` must carry a fallback that resolves HOME
(passwd entry for the current uid, or the operator's home under sudo).

See follow-up 731f3a11 and CC memory ``sandbox_shell_no_home``.
"""

import re
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

# Scripts whose $HOME usage is NOT vulnerable to a host-side HOME-unset, with the
# reason. Verified by reading the surrounding context on 2026-08-10.
_ALLOWLIST = {
    # $HOME is dereferenced inside `incus exec ... --env "HOME=/home/ubuntu"
    # -- bash -c '...'` (container side, explicitly injected), and the one
    # host-side home path resolves via ~${SUDO_USER} (operator home). Neither is
    # affected by the host script's own HOME being unset.
    "host-setup.sh",
    # Never dereferences $HOME at runtime (uses ~${REMOTE_USER}); listed
    # defensively so a future edit that adds a bare $HOME is a conscious choice.
    "generate-ssh-config.sh",
}

_SET_U = re.compile(r"^\s*set\s+-[a-z]*u", re.M)
_HOME_USE = re.compile(r"\$\{?HOME\b")
# A guard that resolves HOME when unset: the passwd-fallback form used by
# store_cc_token.sh / install.sh / bootstrap.sh, or the ~${SUDO_USER} operator
# form, or a ${HOME:-...} default.
_GUARD = re.compile(
    r"getent passwd.*cut -d: -f6"  # passwd-uid fallback
    r"|~\$\{SUDO_USER"  # operator-home form
    r"|HOME=\"\$\{HOME:-",  # inline default form
    re.S,
)


def _candidate_scripts():
    for p in sorted(SCRIPTS_DIR.glob("*.sh")):
        text = p.read_text()
        if _SET_U.search(text) and _HOME_USE.search(text):
            yield p, text


def test_home_guarded_scripts_have_fallback():
    """Every set -u script that uses $HOME resolves HOME-unset (or is allowlisted)."""
    missing = [
        p.name
        for p, text in _candidate_scripts()
        if p.name not in _ALLOWLIST and not _GUARD.search(text)
    ]
    assert not missing, (
        "scripts enable `set -u` and dereference $HOME but lack a HOME-unset "
        f"guard: {sorted(missing)}"
    )


def test_guard_snippet_resolves_home_when_unset():
    """The passwd-fallback guard resolves HOME under `env -i` + `set -u`."""
    guard = (
        'if [ -z "${HOME:-}" ]; then '
        'HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""; '
        '[ -n "$HOME" ] || { echo unresolved >&2; exit 1; }; '
        "export HOME; fi; "
        'printf %s "$HOME"'
    )
    out = subprocess.run(
        ["env", "-i", "bash", "-uc", guard],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f"guard aborted: {out.stderr!r}"
    assert out.stdout.strip(), "guard did not resolve a HOME value"
