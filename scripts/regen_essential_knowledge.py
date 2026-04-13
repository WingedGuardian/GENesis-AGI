#!/usr/bin/env python3
"""Regenerate essential knowledge (L1) from memory store.

Standalone script called by the session-end hook via subprocess.Popen.
Extracted from inline -c script that had a SyntaxError (async def after
semicolons is invalid Python).
"""

import asyncio
import sys
from pathlib import Path

# Ensure genesis package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import aiosqlite  # noqa: E402


async def _run() -> None:
    db_path = Path.home() / "genesis" / "data" / "genesis.db"
    db = await aiosqlite.connect(str(db_path))
    try:
        from genesis.memory.essential_knowledge import generate_and_write
        await generate_and_write(db)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_run())
