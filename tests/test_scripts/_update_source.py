"""What the tests read as update.sh's source.

The rollback and the service helpers it needs live in scripts/lib/update_rollback.sh
(#3844), which update.sh sources at startup. update_text() returns both, update.sh
first: a test that takes the first occurrence of a call means update.sh's.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
ROLLBACK_LIB = REPO_ROOT / "scripts" / "lib" / "update_rollback.sh"


def update_text() -> str:
    return UPDATE_SH.read_text() + ROLLBACK_LIB.read_text()


def rollback_body_text() -> str:
    """_do_rollback, whole. Its body holds a column-0 `}` in an embedded python
    program, so a cut at the first one stops early."""
    text = ROLLBACK_LIB.read_text()
    start = text.index("_do_rollback() {\n")
    end = text.index("\n}\n", text.index("# END rollback-state-clear", start)) + len("\n}\n")
    return text[start:end]
