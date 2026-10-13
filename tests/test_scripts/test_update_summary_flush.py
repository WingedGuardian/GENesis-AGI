"""update.sh prints what bootstrap changed outside the checkout on EVERY exit.

A rollback restores the code but leaves units and /etc files as bootstrap wrote
them, so the list matters most on a failed run. The real ``_flush_deploy_summary``
is extracted from scripts/update.sh and armed the way update.sh arms it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPDATE = REPO / "scripts" / "update.sh"


def _func() -> str:
    text = UPDATE.read_text()
    m = re.search(r"^_flush_deploy_summary\(\) \{\n.*?\n\}$", text, re.DOTALL | re.MULTILINE)
    assert m, "_flush_deploy_summary not found with column-0 braces"
    return m.group(0)


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    summary = tmp_path / "summary"
    summary.write_text("written genesis-server.service\nkept demo.timer: edited\n")
    script = f"""
set -Eeuo pipefail
{_func()}
trap '_flush_deploy_summary' EXIT
GENESIS_DEPLOY_SUMMARY={str(summary)!r}
{body}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def test_a_failed_run_still_prints_the_summary(tmp_path):
    r = _run(tmp_path, 'echo "ROLLED BACK"; exit 1')
    assert r.returncode == 1
    out = r.stdout
    assert "before this run stopped (not undone)" in out
    assert "written genesis-server.service" in out and "kept demo.timer" in out
    assert out.index("ROLLED BACK") < out.index("written genesis-server.service")
    assert not (tmp_path / "summary").exists()


def test_success_prints_it_once_under_its_own_heading(tmp_path):
    r = _run(tmp_path, '_flush_deploy_summary "Changed outside the checkout:"; echo DONE')
    assert r.returncode == 0
    assert r.stdout.count("written genesis-server.service") == 1
    assert "Changed outside the checkout:" in r.stdout
    assert "rollback" not in r.stdout


def test_an_exit_before_bootstrap_prints_nothing(tmp_path):
    r = subprocess.run(
        [
            "bash",
            "-c",
            f"set -Eeuo pipefail\n{_func()}\ntrap '_flush_deploy_summary' EXIT\nexit 1\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 1 and r.stdout == ""


def test_every_exit_trap_after_its_definition_flushes():
    text = UPDATE.read_text()
    start = text.index("_flush_deploy_summary() {")
    traps = [
        ln
        for ln in text[start:].splitlines()
        if ln.strip().startswith("trap ") and ln.rstrip().endswith(" EXIT")
    ]
    assert len(traps) >= 2, traps
    for ln in traps:
        assert "_flush_deploy_summary" in ln, ln
