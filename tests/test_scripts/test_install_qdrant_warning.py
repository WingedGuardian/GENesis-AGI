"""install.sh: a SUCCESSFUL Qdrant install must not report a setup warning.

The defect this pins, found by the first run of the fresh-install CI check in
this same PR: the "Qdrant not found → install it" branch set ``SETUP_WARNINGS=1``
unconditionally at the end of the branch, outside the inner success/failure
if-chain. So a clean install printed

    + Qdrant 1.14.0 installed to /usr/local/bin/
    Genesis REQUIRES Qdrant for vector storage.

— the second line contradicting the first — and left the warning flag set. Under
``GENESIS_INSTALL_STRICT=1`` that is a guaranteed nonzero exit on every genuinely
fresh machine, i.e. the check could never be green.

Why it survived: a box that already has Qdrant takes the FIRST branch and never
reaches this code. Every developer box already has Qdrant. Only a fresh machine
reaches it, and nothing had installed Genesis on a fresh machine in ~12 weeks.

The branch is extracted from the shipped script and executed, so these tests
exercise the real control flow rather than a restatement of it. The only textual
substitution is the hardcoded ``/tmp`` path, redirected into the sandbox so a
test run cannot collide with a real download or with a concurrent test; the
if/else structure and the warn decision — what is actually under test — are
untouched.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL = REPO_ROOT / "scripts" / "install.sh"


def _extract_qdrant_block() -> str:
    """Pull the shipped Qdrant infrastructure block verbatim.

    Anchored on the two comments that bracket it, so a rename inside the block
    cannot silently shrink what is under test — the extraction fails loudly.
    """
    text = INSTALL.read_text()
    m = re.search(
        r"^# Qdrant \(required\)\n(.*?)^# Ollama \(optional\)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert m, "Qdrant block anchors not found in install.sh — extraction is stale"
    block = m.group(1)
    assert "_qdrant_installed" in block, "extracted block is missing the success flag"
    return block


def _run(tmp_path: Path, *, download_ok: bool, binary_in_archive: bool, sudo_ok: bool):
    """Execute the shipped block with stubbed curl/tar/sudo. Returns CompletedProcess."""
    sandbox_tmp = tmp_path / "tmp"
    sandbox_tmp.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    # curl: the reachability probe must FAIL (so the block takes the install
    # branch); the download then succeeds or fails per scenario.
    (fake_bin / "curl").write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do case "$a" in *collections) exit 7;; esac; done\n'
        f"exit {0 if download_ok else 22}\n"
    )
    # tar: materialise the extracted binary, or not.
    (fake_bin / "tar").write_text(
        "#!/bin/bash\n"
        + (f'echo stub > "{sandbox_tmp}/qdrant"\n' if binary_in_archive else "")
        + "exit 0\n"
    )
    (fake_bin / "sudo").write_text("#!/bin/bash\n" + ('exec "$@"\n' if sudo_ok else "exit 1\n"))
    for f in fake_bin.iterdir():
        f.chmod(0o755)

    block = _extract_qdrant_block().replace("/tmp/", f"{sandbox_tmp}/")
    # /usr/local/bin is not writable in the sandbox; send the sudo-success path
    # somewhere it can actually land, without touching the branch logic.
    usr_local = tmp_path / "usrlocal"
    usr_local.mkdir()
    block = block.replace("/usr/local/bin/qdrant", f"{usr_local}/qdrant")

    script = f"""set -euo pipefail
SETUP_WARNINGS=0
SETUP_WARNING_LOG=""
setup_warn() {{ SETUP_WARNINGS=1; SETUP_WARNING_LOG="$SETUP_WARNING_LOG $1"; }}
{block}
echo "RESULT_WARNINGS=$SETUP_WARNINGS"
echo "RESULT_REASONS=$SETUP_WARNING_LOG"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(home),
            "QDRANT_URL": "http://localhost:6333",
            "QDRANT_VERSION": "1.14.0",
        },
        timeout=60,
    )


def test_successful_install_records_no_setup_warning(tmp_path):
    """THE regression. A clean install must leave the flag at 0."""
    r = _run(tmp_path, download_ok=True, binary_in_archive=True, sudo_ok=True)
    assert r.returncode == 0, r.stderr
    assert "RESULT_WARNINGS=0" in r.stdout, r.stdout
    assert "installed to" in r.stdout, "the success path did not actually run"
    assert "REQUIRES Qdrant" not in r.stdout, (
        "a successful install still printed the 'you need Qdrant' advice — "
        "the line that contradicts the '+ installed' line above it"
    )


def test_successful_install_without_sudo_also_records_no_warning(tmp_path):
    """The ~/.local/bin fallback is equally a success and must not warn either —
    the flag has to be set on the shared success path, not in one sub-branch."""
    r = _run(tmp_path, download_ok=True, binary_in_archive=True, sudo_ok=False)
    assert r.returncode == 0, r.stderr
    assert "RESULT_WARNINGS=0" in r.stdout, r.stdout
    assert ".local/bin" in r.stdout, "the no-sudo path did not run"


@pytest.mark.parametrize(
    ("download_ok", "binary_in_archive", "label"),
    [(False, False, "download failed"), (True, False, "binary missing from archive")],
)
def test_failed_install_still_warns(tmp_path, download_ok, binary_in_archive, label):
    """The other direction — a precision fix that blinded the real warning would
    be worse than the bug. A genuinely failed install must still warn, and the
    reason must name Qdrant so the strict-mode exit is diagnosable."""
    r = _run(tmp_path, download_ok=download_ok, binary_in_archive=binary_in_archive, sudo_ok=True)
    assert r.returncode == 0, r.stderr
    assert "RESULT_WARNINGS=1" in r.stdout, f"{label}: should still warn\n{r.stdout}"
    assert "REQUIRES Qdrant" in r.stdout, f"{label}: lost the operator-facing advice"
    assert "Qdrant" in r.stdout.split("RESULT_REASONS=")[1], (
        f"{label}: the recorded reason does not name Qdrant, so a strict-mode "
        "failure would again be a bare flag with no cause"
    )


def test_every_warning_setter_goes_through_setup_warn():
    """Structural guardrail: no bare ``SETUP_WARNINGS=1`` assignment may remain.

    The reason log only works if every setter registers a cause. A future edit
    that adds a bare assignment would set the flag with no reason, and the strict
    exit would print a header over an empty list — silently back to the
    undiagnosable state this replaced. Asserted on the shipped text because there
    is no runtime moment at which all eight paths are exercised.
    """
    text = INSTALL.read_text()

    # Exactly one bare assignment is legitimate: the one INSIDE setup_warn. Cut
    # the helper's body out first rather than relaxing the pattern, so the check
    # still fires on a bare assignment anywhere else — including one added right
    # next to the helper.
    helper = re.search(r"^setup_warn\(\) \{\n(?:.*?\n)*?^\}\n", text, re.MULTILINE)
    assert helper, "setup_warn helper not found — has the mechanism been removed?"
    assert "SETUP_WARNINGS=1" in helper.group(0), "setup_warn no longer sets the flag"
    outside = text[: helper.start()] + text[helper.end() :]

    # Allowed in `outside`: the declaration (=0), the reads, and comment prose.
    offenders = [
        line for line in outside.splitlines() if re.match(r"^\s*SETUP_WARNINGS=1\s*$", line)
    ]
    assert not offenders, (
        'bare SETUP_WARNINGS=1 assignment(s) found — use setup_warn "reason" so '
        f"the strict-mode exit can name the cause: {offenders}"
    )
    assert text.count("setup_warn ") >= 8, (
        "expected every warning site to route through setup_warn; found "
        f"{text.count('setup_warn ')} call(s)"
    )


# ── rendered-template placeholder parity ──────────────────────────────────────


def test_every_template_placeholder_is_substituted_by_the_render_loop():
    """Every ``__TOKEN__`` in any shipped unit template must be in install.sh's sed
    list, or the installer writes a unit with a literal placeholder in it.

    This is stated as parity between two sets rather than as a check for one known
    token, because the specific defect it was written for —
    ``agent-zero.service.template``'s ``WorkingDirectory=__AZ_ROOT__``, absent from
    the sed list, so systemd would reject the rendered unit — is only interesting as
    an instance. Testing for ``__AZ_ROOT__`` by name would pass forever and catch
    nothing new; the next template to add a placeholder is the actual risk.

    It went unnoticed because install.sh never enables agent-zero, so the broken
    unit just sat on disk unusable.
    """
    templates = sorted((REPO_ROOT / "scripts" / "systemd").glob("*.template"))
    assert templates, "no unit templates found — this test is looking in the wrong place"

    install_text = INSTALL.read_text()
    # The sed expressions in the render loop, e.g. -e "s|__HOME__|$HOME|g"
    substituted = set(re.findall(r'-e "s\|(__[A-Z0-9_]+__)\|', install_text))
    assert substituted, "could not find the render loop's sed list — extraction is stale"

    missing: dict[str, set[str]] = {}
    for template in templates:
        tokens = set(re.findall(r"__[A-Z0-9_]+__", template.read_text()))
        unhandled = tokens - substituted
        if unhandled:
            missing[template.name] = unhandled

    assert not missing, (
        "unit template placeholder(s) with no substitution in install.sh's render "
        f"loop — the rendered unit ships the literal token: {missing}. Add each to "
        "the sed list (with a default if the value is optional)."
    )


def test_user_facing_artifacts_use_the_actual_repo_dir_not_a_hardcoded_home_path():
    """The ``genesis`` command and the login auto-cd must reference $REPO_DIR.

    Both hardcoded ``~/genesis``. On any clone elsewhere the wrapper was installed
    dead ("Genesis repo not found") and the login hook pointed at a non-existent
    directory — while every install-test assertion still passed, because nothing
    checked where they pointed. The installer already knows its own location.
    """
    text = INSTALL.read_text()

    wrapper = re.search(r"sudo tee /usr/local/bin/genesis .*?\nWRAPPER", text, re.DOTALL)
    assert wrapper, "genesis wrapper heredoc not found — extraction is stale"
    body = wrapper.group(0)
    assert "$REPO_DIR" in body, "the genesis wrapper must cd to $REPO_DIR"
    assert "~/genesis" not in body, (
        "the genesis wrapper still hardcodes ~/genesis — dead on any other clone"
    )
    assert '"\\$@"' in body or "\\$@" in body, (
        "the wrapper heredoc is unquoted (so $REPO_DIR expands); $@ must be escaped "
        "or the generated script loses its arguments"
    )

    autocd = re.search(r"# Auto-cd to Genesis project on login.*?\nfi", text, re.DOTALL)
    assert autocd, "auto-cd block not found — extraction is stale"
    assert "$REPO_DIR" in autocd.group(0)
