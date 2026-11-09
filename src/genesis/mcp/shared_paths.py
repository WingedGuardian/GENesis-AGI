"""Caller-file interpretation and ephemeral identity binding for shared calls."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from genesis.caller_context import in_shared_service

BASE_ENDPOINT = "/mcp"
FORWARDER_ENDPOINT = "/mcp/v1"
IDENTITY_META = "genesis/callerPath"


class SharedPathError(ValueError):
    """A caller-file request cannot be interpreted without guessing."""


@dataclass(frozen=True)
class _Rule:
    argument: str
    mode: str
    optional_empty: bool = False


_RULES = {
    ("health", "evo_run"): _Rule("golden_set_path", "literal", True),
    ("health", "experiment_run"): _Rule("golden_set_path", "literal", True),
    ("health", "skill_replay_run"): _Rule("suite_path", "literal", True),
    ("health", "task_submit"): _Rule("plan_path", "plan"),
    ("memory", "document_index"): _Rule("path", "user"),
    ("memory", "document_query"): _Rule("path_or_doc_id", "document"),
    ("memory", "knowledge_ingest_source"): _Rule("source", "source"),
    ("memory", "knowledge_ingest_batch"): _Rule("directory", "directory"),
    ("memory", "resume_review"): _Rule("resume_source", "resume"),
}


@cache
def _registry():
    # Stateless processors only; their fetch/extract/provider paths stay lazy.
    from genesis.knowledge.processors.registry import build_default_registry

    return build_default_registry()


def forwarder_url(base):
    """Derive one protocol endpoint after trusted base configuration validation."""
    if not isinstance(base, str) or not base.endswith(BASE_ENDPOINT):
        raise SharedPathError("Invalid shared MCP base endpoint")
    return base + "/v1"


def _unchanged(rule, value):
    return ((rule.optional_empty and value == "")
            or (rule.mode == "plan" and not value.strip())
            or (rule.mode == "document" and value.startswith("pi-"))
            or (rule.mode == "source" and _registry().get_url_processor(value) is not None))


def prepare_params(service, method, params):
    """Copy only known caller-file arguments; retain all unrelated wire values."""
    if method != "tools/call" or not isinstance(params, dict):
        return params
    rule = _RULES.get((service, params.get("name")))
    arguments, metadata = params.get("arguments"), params.get("_meta")
    if rule is None or not isinstance(arguments, dict):
        return params
    if isinstance(metadata, dict) and IDENTITY_META in metadata:
        raise SharedPathError("Caller file metadata is reserved for the forwarder")
    value = arguments.get(rule.argument)
    if not isinstance(value, str) or _unchanged(rule, value):
        return params
    path = Path(value.strip() if rule.mode == "plan" else value)
    if rule.mode == "resume" and not path.exists():
        return params
    if rule.mode in {"user", "document", "plan"}:
        path = path.expanduser()
    actual = str(path.absolute())
    if actual == value:
        return params
    prepared = {**params, "arguments": {**arguments, rule.argument: actual}}
    if rule.mode in {"source", "directory"}:
        prepared["_meta"] = {**(metadata or {}), IDENTITY_META: {
            "argument": rule.argument, "original": value, "actual": actual,
        }}
    return prepared


@dataclass(frozen=True)
class _Scope:
    tool: str
    rule: _Rule | None
    actual: object
    original: str | None


_SCOPE: ContextVar[_Scope | None] = ContextVar("genesis_shared_file_scope", default=None)


def _original(rule, value, hint, versioned):
    if (not versioned or rule is None or rule.mode not in {"source", "directory"}
            or not isinstance(hint, dict) or set(hint) != {"argument", "original", "actual"}
            or hint["argument"] != rule.argument or not isinstance(hint["original"], str)
            or not isinstance(hint["actual"], str) or hint["actual"] != value
            or not isinstance(value, str) or not Path(value).is_absolute()):
        raise SharedPathError("Invalid caller file identity metadata")
    if rule.mode == "source" and _registry().get_url_processor(hint["original"]) is not None:
        raise SharedPathError("URL identity cannot replace a file source")
    return hint["original"]


@contextmanager
def request_paths(service, tool, arguments, metadata, *, versioned, metadata_available=True):
    """Admit the actual HTTP interpretation and reset its request-local binding."""
    rule = _RULES.get((service, tool))
    if rule is not None and not metadata_available:
        raise SharedPathError("Shared file request metadata is unavailable")
    value = (arguments or {}).get(rule.argument) if rule is not None else None
    original = None
    if isinstance(metadata, dict) and IDENTITY_META in metadata:
        original = _original(rule, value, metadata[IDENTITY_META], versioned)
    if rule is not None and isinstance(value, str) and not _unchanged(rule, value):
        absolute = Path(value).is_absolute()
        if not absolute and (rule.mode != "resume" or not versioned):
            raise SharedPathError("Shared file calls require an absolute path or updated forwarder")
    token = _SCOPE.set(_Scope(tool, rule, value, original))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _current(tool, argument, value):
    scope = _SCOPE.get()
    if scope is None:
        if in_shared_service():
            raise SharedPathError("Shared file request context is unavailable")
        return None
    if scope.tool != tool or scope.rule is None or scope.rule.argument != argument or scope.actual != value:
        raise SharedPathError("Shared file request context does not match this input")
    return scope


def original_identity(tool, argument, value):
    scope = _current(tool, argument, value)
    return scope.original if scope is not None else None


def ingestion_input(tool, argument, value):
    """Keep original identity and add a read option only for rewritten input."""
    original = original_identity(tool, argument, value)
    options = {} if original is None else {
        "read_path" if argument == "source" else "read_directory": value,
    }
    return (value if original is None else original), options


def probe_resume_file(value):
    scope = _current("resume_review", "resume_source", value)
    return scope is None or Path(value).is_absolute()
