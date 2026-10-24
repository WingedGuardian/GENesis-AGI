"""Update existing hook-registered rows; never create a parallel session writer."""

from datetime import UTC, datetime

import aiosqlite


async def update_session_row(session_id: str, model: str | None, closed: bool) -> None:
    from genesis.cc.foreground_reaper import _pid_dead
    from genesis.db.connection import connect_aiosqlite_rw
    from genesis.db.crud.cc_sessions import checkpoint_dark, get_by_id, update_model_effort
    from genesis.env import genesis_db_path

    async with connect_aiosqlite_rw(genesis_db_path()) as db:
        db.row_factory = aiosqlite.Row
        row = await get_by_id(db, session_id)
        if not row or row["cc_session_id"] != session_id or row["session_type"] != "foreground":
            raise LookupError("live agent has no matching foreground registration")
        if closed:
            # SDK close can suppress its last process-wait timeout. Use the
            # reaper's positive death evidence, not disconnect's return alone.
            pid = row.get("pid")
            if not isinstance(pid, int) or pid <= 1 or not _pid_dead(pid, row):
                raise RuntimeError("child death unconfirmed; leaving registration active for reaper")
            if row["status"] == "active" and not await checkpoint_dark(
                db, session_id, checkpointed_at=datetime.now(UTC).isoformat(),
                expected_last_activity=row.get("last_activity_at"),
            ):
                raise RuntimeError("registration changed during checkpoint; left unchanged")
        elif model:
            if not await update_model_effort(db, session_id, model=model):
                raise LookupError("live agent registration disappeared during model update")
