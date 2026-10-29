"""Internal opt-in initialization for request-owned MCP services.

No transport, CLI flag or live configuration selects this path. The caller owns
the manager and must close it after stopping requests. Providers are injected.
"""

from importlib import import_module

from genesis.db.request_database import RequestDatabase

_INITIALIZERS = {
    "memory": ("genesis.mcp.memory", "init"),
    "health": ("genesis.mcp.health", "init_health_mcp"),
    "recon": ("genesis.mcp.recon_mcp", "init_recon_mcp"),
    "outreach": ("genesis.mcp.outreach_mcp", "init_outreach_mcp"),
}


async def initialize_request_service(name: str, *, db: RequestDatabase, activity_tracker=None, **dependencies):
    """Prepare service schema in a bootstrap unit, then return the actual server.

    ``db`` must already exist with its normal schema. Outreach's existing
    standalone table preparation is awaited here, never scheduled unowned.
    Health receives a service already bound to this same facade. Memory callers
    choose pending-bookmark policy explicitly before any shared activation.
    A supplied tracker must already use this facade; its owning bootstrap binds
    and optionally warms it once, before registration. Rebinding here could move
    a pending flush away from the manager that owns it.
    Bootstrap owns an independent unit and must not run inside an active unit
    owned by this task. Registration starts only after that unit finalizes.
    """
    if not isinstance(db, RequestDatabase):
        raise TypeError("initialize_request_service requires RequestDatabase")
    if activity_tracker is not None and getattr(activity_tracker, "_db", None) is not db:
        raise ValueError("Activity tracker must already use the supplied RequestDatabase")
    module_name, initializer = _INITIALIZERS[name]
    module = import_module(module_name)
    if name == "health":
        if getattr(dependencies.get("service"), "_db", None) is not db:
            raise ValueError("Health service must use the supplied RequestDatabase")
    else:
        dependencies["db"] = db
    async with db.unit_of_work(independent=True):
        if name == "outreach":
            from genesis.db.crud.pending_outreach import ensure_table

            await ensure_table(db)
    getattr(module, initializer)(activity_tracker=activity_tracker, **dependencies)
    return module.mcp
