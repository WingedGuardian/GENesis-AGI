"""check_stale_pending emits its directive FIRST and bounds its output.

The hook returns early on fresh state, so a live run on a healthy install emits
0 bytes — which means "it did not crash" and nothing more. These tests seed a
genuinely stale cognitive state so the emit path actually executes; without that,
the change to this hook would be unexercised and its green would be vacuous.

DB_PATH is derived from Path.home(), so redirecting HOME into tmp_path is enough
to point the hook at a synthetic database. No monkeypatching of module internals,
and no dependency on the operator's real DB.

Install-agnostic: synthetic payloads, subprocess isolation, no network, no live DB.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_HOOK = _REPO / "scripts" / "check_stale_pending.py"

#: Measured harness threshold; imported rather than duplicated would be better,
#: but this test runs the hook as a subprocess and only needs the number.
_CAP = 10_000


def _seed(home: Path, *, days_old: int, items: int, red: int = 0) -> None:
    """Write one cognitive_state row that the hook will consider stale."""
    db = home / "genesis" / "data" / "genesis.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    body = ["**Pending Actions**"]
    body += [f"{i + 1}. **Item {i}** — a pending action that has not moved" for i in range(items)]
    body += ["\U0001f534 a red flag line" for _ in range(red)]
    created = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE cognitive_state (section TEXT, content TEXT, created_at TEXT)")
    conn.execute(
        "INSERT INTO cognitive_state (section, content, created_at) VALUES (?, ?, ?)",
        ("active_context", "\n".join(body), created),
    )
    conn.commit()
    conn.close()


def _run(home: Path) -> str:
    env = dict(os.environ)
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    return proc.stdout


def test_fresh_state_emits_nothing(tmp_path: Path) -> None:
    """The control. Without it, a bug that silences the hook entirely would make
    every other assertion here pass for the wrong reason."""
    _seed(tmp_path, days_old=0, items=3)
    assert _run(tmp_path) == ""


def test_stale_state_actually_emits(tmp_path: Path) -> None:
    """Proves the emit path RUNS — the thing a live run on fresh state cannot."""
    _seed(tmp_path, days_old=9, items=3)
    out = _run(tmp_path)
    assert "STALE PENDING ITEMS" in out
    assert "Item 0" in out


def test_directive_precedes_the_list(tmp_path: Path) -> None:
    """The head survives truncation; the tail does not. A directive after its
    list is what disappears, and it disappears when the list is longest."""
    _seed(tmp_path, days_old=9, items=5, red=2)
    out = _run(tmp_path)
    directive = out.index("ACTION REQUIRED")
    first_item = out.index("Item 0")
    assert directive < first_item, (
        "ACTION REQUIRED must precede the item list — the harness keeps the HEAD "
        "of an over-cap hook's output"
    )


def test_directive_carries_no_positional_reference(tmp_path: Path) -> None:
    """'Raise these' only parses when the list is already above it. Moving the
    directive without rewording would leave a dangling backward reference."""
    _seed(tmp_path, days_old=9, items=2)
    out = _run(tmp_path)
    assert "raise the following" in out
    assert "Raise these" not in out


def test_output_is_bounded_when_the_source_is_huge(tmp_path: Path) -> None:
    """active_context has no length cap in any of its three writers, so the only
    thing standing between it and the harness's cap is this hook's writer."""
    _seed(tmp_path, days_old=9, items=4000)
    out = _run(tmp_path)
    assert out, "a huge stale state must still emit something"
    assert len(out) <= _CAP, f"emitted {len(out)} chars, over the {_CAP} cap"
    assert "ACTION REQUIRED" in out, "the directive must survive the bounding"
