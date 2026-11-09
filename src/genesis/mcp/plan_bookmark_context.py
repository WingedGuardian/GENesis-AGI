"""Explicit caller input, admitted by authenticated shared middleware only."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from genesis.caller_context import caller_context
from genesis.mcp.shared_paths import SharedPathError

PENDING_META = "genesis/planBookmarkPending"
PENDING_VERSION = 1
_SCOPE: ContextVar[tuple | None] = ContextVar("genesis_plan_bookmark_input", default=None)


def pending_path(value):
    """Validate the lexical contract; neither probe nor resolve caller files."""
    if (not isinstance(value, dict) or set(value) != {"version", "path"}
            or type(value["version"]) is not int or value["version"] != PENDING_VERSION
            or not isinstance(value["path"], str) or "\0" in value["path"]):
        raise SharedPathError("Invalid pending bookmark metadata")
    path = Path(value["path"])
    if (not path.is_absolute() or path.name != "plan_bookmark_pending.json"
            or path.parent.name != ".genesis"):
        raise SharedPathError("Invalid pending bookmark path")
    return path


def prepare_pending_params(method, params, path):
    """Opted-in peers add their input without overwriting client metadata."""
    if method != "tools/call" or not isinstance(params, dict):
        return params
    metadata = params.get("_meta")
    if metadata is not None and (not isinstance(metadata, dict) or PENDING_META in metadata):
        raise SharedPathError("Pending bookmark metadata is reserved for the forwarder")
    return {**params, "_meta": {**(metadata or {}), PENDING_META: {
        "version": PENDING_VERSION, "path": str(path),
    }}}


@contextmanager
def request_pending_bookmark(service, caller, metadata, *, versioned):
    """Bind only positive authenticated eligibility, resetting even on cancel."""
    path = None
    if isinstance(metadata, dict) and PENDING_META in metadata:
        if service != "memory" or caller.profile is not None or not versioned:
            raise SharedPathError("Pending bookmarks require an authenticated memory forwarder")
        path = pending_path(metadata[PENDING_META])
    token = _SCOPE.set((path, caller) if path is not None else None)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_pending_bookmark():
    scope = _SCOPE.get()
    # Losing just the caller scope must not retain an earlier admitted input.
    return scope[0] if scope is not None and caller_context() is scope[1] else None
