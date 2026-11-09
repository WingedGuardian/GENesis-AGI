"""Transactional public-state projection on existing native peer receipts."""

FORMAT = "%Y-%m-%dT%H:%M:%fZ"
PUBLIC_STATES = ("submitted", "working", "auth_required", "completed", "failed", "canceled")


def object_json(column):
    """Lazy guards keep malformed/non-object documents out of JSON extraction."""
    return f"CASE WHEN json_valid({column}) THEN CASE WHEN json_type({column})='object' THEN {column} ELSE '{{}}' END ELSE '{{}}' END"


PENDING = (
    "EXISTS(SELECT 1 FROM approval_requests WHERE status='pending' AND "  # noqa: S608 — static schema expressions only
    f"json_extract({object_json('context')},'$.agent_session_id')=cc_sessions.id)"
)
_META = object_json("metadata")
_NATIVE = f"json_extract({_META},'$.native_state')"
_STATE = (
    "CASE "
    f"WHEN status='active' AND {PENDING} THEN 'auth_required' "
    f"WHEN status='active' AND {_NATIVE} IN ('succeeded','failed','cancelled') THEN 'working' "
    f"WHEN json_type({_META},'$.native_state') IS NULL THEN 'submitted' "
    f"WHEN {_NATIVE}='pending' THEN 'submitted' "
    f"WHEN {_NATIVE}='succeeded' THEN 'completed' "
    f"WHEN {_NATIVE}='failed' THEN 'failed' "
    f"WHEN {_NATIVE}='cancelled' THEN 'canceled' ELSE 'working' END"
)
# SQL validation covers the entire owned population, including rows outside a
# requested page. Canonical round-trip rejects malformed and normalized dates.
VALID = (
    f"a2a_state IN {PUBLIC_STATES} AND length(a2a_status_at)=24 "
    "AND substr(a2a_status_at,1,4) BETWEEN '0001' AND '9999' "
    f"AND strftime('{FORMAT}',a2a_status_at,'+0 seconds')=a2a_status_at"
)


def _refresh(where):
    return (
        "UPDATE cc_sessions SET "  # noqa: S608 — callers supply fixed trigger predicates only
        f"a2a_state=CASE WHEN source_tag='agent_api' THEN {_STATE} END, "
        "a2a_status_at=CASE WHEN source_tag!='agent_api' THEN NULL "
        f"WHEN a2a_state IS NOT ({_STATE}) OR a2a_status_at IS NULL "
        f"THEN strftime('{FORMAT}','now') ELSE a2a_status_at END WHERE {where};"
    )


async def install(db):
    """Install inside the caller's transaction; numbered runner owns its envelope."""
    columns = {row[1] for row in await (await db.execute("PRAGMA table_info(cc_sessions)")).fetchall()}
    for column in ("a2a_state", "a2a_status_at"):
        if column not in columns:
            await db.execute(f"ALTER TABLE cc_sessions ADD COLUMN {column} TEXT")
    for suffix, event in (
        ("insert", "INSERT"), ("update", "UPDATE OF metadata,status,source_tag,id"),
    ):
        await db.execute(
            f"CREATE TRIGGER IF NOT EXISTS peer_status_session_{suffix} AFTER {event} ON cc_sessions "
            f"BEGIN {_refresh('id=NEW.id')} END"
        )
    old = f"json_extract({object_json('OLD.context')},'$.agent_session_id')"
    new = f"json_extract({object_json('NEW.context')},'$.agent_session_id')"
    for suffix, event, ids in (
        ("insert", "INSERT", new),
        ("update", "UPDATE OF status,context", f"{old},{new}"),
        ("delete", "DELETE", old),
    ):
        await db.execute(
            f"CREATE TRIGGER IF NOT EXISTS peer_status_approval_{suffix} AFTER {event} ON approval_requests "
            f"BEGIN {_refresh(f'id IN ({ids}) AND source_tag=\'agent_api\'')} END"
        )
    await db.execute(
        f"UPDATE cc_sessions SET a2a_state=COALESCE(a2a_state,({_STATE})), "  # noqa: S608 — static projection expression
        f"a2a_status_at=COALESCE(a2a_status_at,strftime('{FORMAT}',"
        "COALESCE(NULLIF(completed_at,''),NULLIF(last_activity_at,''),started_at),'+0 seconds')) "
        "WHERE source_tag='agent_api' AND (a2a_state IS NULL OR a2a_status_at IS NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_peer_status_page ON cc_sessions(source_tag,a2a_status_at DESC,id DESC)"
    )


async def install_base(db):
    """Base-schema upgrades preserve any earlier uncommitted caller work."""
    await db.execute("SAVEPOINT peer_status_install")
    try:
        await install(db)
    except BaseException:
        await db.execute("ROLLBACK TO peer_status_install")
        await db.execute("RELEASE peer_status_install")
        raise
    await db.execute("RELEASE peer_status_install")
