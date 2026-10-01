"""Embedding provider with configurable backend chains.

Two chain configurations for split read/write paths. Both lead with the
cloud backend; they differ only in the rate tier:
  Storage (writes): DeepInfra → Ollama (ordinary tier)
  Recall (reads):   DeepInfra → Ollama (priority tier — deadline-bound)
Ollama is the fallback rung in both, so a cloud outage degrades rather than fails.

Every chain is confined to ONE vector space (see CANONICAL_VECTOR_SPACE):
DeepInfra and the local Ollama model are both Qwen3-Embedding-0.6B at 1024-d,
and DashScope (a different model) is only used where it is the sole backend.
Cache keys are text-based (SHA256 of "<space prefix>:{text}"), NOT
provider-dependent within a space — two instances sharing the same L2 diskcache
dir see each other's entries, and never another space's.

Two-level cache: L1 in-process dict (fast, per-process) backed by
L2 diskcache on disk (shared across all MCP server processes).
Embeddings are deterministic for a given model+text, so long TTLs are safe.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.provider_activity import ProviderActivityTracker

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = Path.home() / ".genesis" / "embedding_cache"

_HTTPX_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.HTTPStatusError,
)

# ---------------------------------------------------------------------------
# Connection reuse tuning for embedding backends
# ---------------------------------------------------------------------------
# The recall embedder is a long-lived singleton whose backends each hold one
# AsyncClient for their lifetime, so the underlying TLS connection *can* be
# reused across proactive recalls. httpx's default keepalive_expiry is only 5s,
# though — between the sparse per-prompt recalls that drive proactive memory,
# the warm connection to DeepInfra/DashScope expires and each cold embed pays a
# fresh TLS handshake (the ~2357ms embed spikes seen in prod). Extending
# keepalive_expiry keeps the connection warm across that gap; the connection
# counts stay small because a single recall issues one embed at a time.
_EMBED_KEEPALIVE_EXPIRY_S = 60.0
_EMBED_MAX_KEEPALIVE_CONNECTIONS = 8
_EMBED_MAX_CONNECTIONS = 16


def _embed_limits() -> httpx.Limits:
    """httpx connection-pool limits tuned for warm-connection reuse."""
    return httpx.Limits(
        max_connections=_EMBED_MAX_CONNECTIONS,
        max_keepalive_connections=_EMBED_MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=_EMBED_KEEPALIVE_EXPIRY_S,
    )


def _http2_available() -> bool:
    """True only if the optional ``h2`` package is importable.

    httpx raises if ``http2=True`` is requested without ``h2`` installed, so we
    gate on import rather than adding a hard dependency.
    """
    try:
        import h2  # noqa: F401
    except ImportError:
        return False
    return True


def _build_embed_client(timeout: float, *, http2: bool = False) -> httpx.AsyncClient:
    """Build an AsyncClient with warm-reuse limits (and HTTP/2 when available).

    ``http2`` is only honoured for TLS (https) cloud backends where it is
    negotiated via ALPN and falls back cleanly to HTTP/1.1. It is left off for
    cleartext local endpoints (Ollama), where enabling it would force h2 with
    prior knowledge and could break servers that only speak HTTP/1.1.
    """
    return httpx.AsyncClient(
        timeout=timeout,
        limits=_embed_limits(),
        http2=http2 and _http2_available(),
    )


class EmbeddingUnavailableError(Exception):
    """Raised when all embedding backends are unavailable."""


# ---------------------------------------------------------------------------
# Vector-space identity
# ---------------------------------------------------------------------------
# Two backends are interchangeable ONLY if they produce vectors in the same
# coordinate space — the same model weights at the same output dimension.
# Matching dimension alone proves nothing: two different 1024-d models place the
# same text at unrelated coordinates, so a vector written by one and queried by
# the other scores as noise, and nothing raises. Every backend therefore names
# its space, and a chain is only ever built from backends that share ONE.
#
# The canonical space is Qwen3-Embedding-0.6B at 1024-d, which the local Ollama
# model (``qwen3-embedding:0.6b*``) and the DeepInfra model
# (``Qwen/Qwen3-Embedding-0.6B``) both are. DashScope's ``text-embedding-v4`` is
# a DIFFERENT model whose compatibility with the 0.6B space has never been
# validated, so it names its own space and is never mixed into a canonical
# chain.
CANONICAL_VECTOR_SPACE = "qwen3-embedding-0.6b@1024"

# Cache keys are prefixed with the provider's vector space, so a vector cached
# by one space can never be served to a provider in another. The keys used to
# be "qwen3-embedding:{text}" for EVERY backend, which let a DashScope fallback
# vector be cached under the same key a Qwen3 provider reads. Those legacy
# entries are deliberately NOT reused (a one-time cold cache is the price of
# never serving a foreign-space vector); they expire on the L2 TTL.
_EMPTY_CHAIN_CACHE_PREFIX = "no-backends"

_LOGGED_EXCLUSIONS: set[tuple[str | None, tuple[str, ...]]] = set()


def _ollama_vector_space(model: str) -> str:
    # The Ollama library tag ("qwen3-embedding:0.6b-fp16") and a pull straight
    # from the upstream repo ("hf.co/Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0") are
    # the same weights; both must count, or DeepInfra is dropped from the chain
    # for an install that is in fact in the canonical space.
    m = model.strip().lower()
    if m.startswith("qwen3-embedding:0.6b") or "qwen3-embedding-0.6b" in m:
        return CANONICAL_VECTOR_SPACE
    return f"ollama:{model}"


def _dashscope_vector_space(model: str, dimensions: int) -> str:
    # Its own space, never the canonical one: see CANONICAL_VECTOR_SPACE.
    return f"dashscope:{model}@{dimensions}"


def _deepinfra_vector_space(model: str) -> str:
    if model.strip().lower() == "qwen/qwen3-embedding-0.6b":
        return CANONICAL_VECTOR_SPACE
    return f"deepinfra:{model}"


def backend_vector_space(backend: object) -> str | None:
    """The backend's declared vector space, or None if it declares none.

    Only a non-empty ``str`` counts: test doubles built on ``MagicMock`` answer
    every attribute lookup with another mock, and an empty string names nothing.
    ``EmbeddingProvider`` refuses a backend for which this returns None.
    """
    space = getattr(backend, "vector_space", None)
    return space if isinstance(space, str) and space.strip() else None


class EmbeddingBackend(Protocol):
    """Protocol for embedding backends in the provider chain."""

    @property
    def name(self) -> str: ...
    @property
    def vector_space(self) -> str: ...
    async def embed(self, text: str) -> list[float]: ...
    async def is_available(self) -> bool: ...


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


class OllamaBackend:
    """Local Ollama embedding backend (qwen3-embedding, fp16 recommended).

    Uses a 60s timeout (Ollama can be slow under GPU contention or cold model
    loading) and retries once on ReadTimeout before propagating the failure.
    """

    _AVAIL_TTL = 120.0  # seconds — cache is_available() result

    def __init__(
        self,
        url: str,
        model: str = "qwen3-embedding:0.6b-fp16",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._model = model
        # Local cleartext endpoint — warm-reuse limits, but HTTP/2 stays off
        # (h2 with prior knowledge would break HTTP/1.1-only Ollama servers).
        self._client = client or _build_embed_client(60.0)
        self._avail_cache: bool | None = None
        self._avail_cache_at: float = 0.0

    @property
    def name(self) -> str:
        return "ollama_embedding"

    @property
    def vector_space(self) -> str:
        return _ollama_vector_space(self._model)

    async def embed(self, text: str) -> list[float]:
        last_exc: Exception | None = None
        for attempt in range(2):  # 1 retry on timeout
            try:
                resp = await self._client.post(
                    f"{self._url.rstrip('/')}/api/embed",
                    json={"model": self._model, "input": text, "keep_alive": -1},
                )
                resp.raise_for_status()
                return resp.json()["embeddings"][0]
            except httpx.ReadTimeout as exc:
                last_exc = exc
                if attempt == 0:
                    import asyncio
                    await asyncio.sleep(1.0)  # brief backoff before retry
                    continue
                raise
            except Exception:
                raise
        raise last_exc  # type: ignore[misc]  # unreachable, but satisfies type checker

    async def is_available(self) -> bool:
        now = time.monotonic()
        if (
            self._avail_cache is not None
            and (now - self._avail_cache_at) < self._AVAIL_TTL
        ):
            return self._avail_cache
        try:
            resp = await self._client.get(
                f"{self._url.rstrip('/')}/api/tags", timeout=5.0,
            )
            result = resp.status_code == 200
        except _HTTPX_ERRORS:
            result = False
        self._avail_cache = result
        self._avail_cache_at = now
        return result


class DeepInfraBackend:
    """DeepInfra cloud embedding backend (OpenAI-compatible API)."""

    def __init__(
        self,
        api_key: str,
        model: str = "Qwen/Qwen3-Embedding-0.6B",
        client: httpx.AsyncClient | None = None,
        service_tier: str | None = None,
    ) -> None:
        """``service_tier`` opts this backend into a paid scheduling tier.

        DeepInfra QUEUES default-tier requests when a model is under load — their
        words: "requests queue up and some get shed with an HTTP 429" (Priority
        Service Tier announcement, 2026-06-29). A queued request still returns a
        clean 200, just late, so the symptom is pure latency with no error
        anywhere to key on.

        MEASURED 2026-09-04 on this model, three runs at each size:

            input     default     priority
            25 tok     8,646ms      602ms
            120 tok   13,317ms      684ms
            600 tok    7,830ms      613ms

        Priority being FLAT across input size is the diagnostic: compute for a
        0.6B embedding model is sub-second, so the seconds on default were
        admission queue, not inference. Against the recall route's 4.5s deadline
        the default tier produced a 100% 503 rate — 20 of 20 through the live
        endpoint — because the route cancels long before the queue clears.

        ``None`` (the default) sends no field and bills at the normal rate.
        Callers opt in; this class never assumes a paid tier on someone's behalf.

        CAVEAT, measured and unresolved: the announcement says the response echoes
        ``service_tier`` "when (and only when) priority was actually applied", and
        that billing follows the echo. Our embeddings responses carry NO such
        field — the announcement lists only chat/completions under "Supported
        Endpoints" and mentions embeddings for billing alone. The latency split
        above is strong evidence the tier is honoured, but it cannot be confirmed
        from the response, so the first invoice is the check.
        """
        self._api_key = api_key
        self._model = model
        self._client = client or _build_embed_client(30.0, http2=True)
        self._service_tier = service_tier
        self._tier_echo_checked = False

    @property
    def name(self) -> str:
        return "deepinfra_embedding"

    @property
    def vector_space(self) -> str:
        return _deepinfra_vector_space(self._model)

    async def embed(self, text: str) -> list[float]:
        payload: dict[str, object] = {"model": self._model, "input": [text]}
        if self._service_tier:
            payload["service_tier"] = self._service_tier
        resp = await self._client.post(
            "https://api.deepinfra.com/v1/openai/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        if self._service_tier and not self._tier_echo_checked:
            # ONE debug line, once per backend instance, to settle a question the
            # latency evidence cannot: the provider documents that the response
            # echoes `service_tier` "when (and only when) priority was actually
            # applied", and that billing follows that echo — but our embeddings
            # responses carry no such field today. If it ever appears, this is how
            # we find out we are (or are not) getting what we pay for. Costs one
            # dict lookup on the first call and nothing thereafter.
            self._tier_echo_checked = True
            try:
                echoed = resp.json().get("service_tier", "<absent>")
            except Exception:  # noqa: BLE001 — diagnostics must never break embed
                echoed = "<unreadable>"
            logger.debug(
                "deepinfra embed: requested service_tier=%s, response echoed %s",
                self._service_tier, echoed,
            )
        return resp.json()["data"][0]["embedding"]

    async def is_available(self) -> bool:
        return True  # Cloud API — assume available, let embed() fail if not


class DashScopeBackend:
    """Alibaba DashScope cloud embedding backend (OpenAI-compatible API).

    Uses text-embedding-v4 with explicit dimensions=1024 for vector space
    compatibility. NOTE: text-embedding-v4 may run the 8B variant —
    validate cosine similarity with local 0.6B before trusting as fallback.
    Until that is done it declares its own vector space, so ``build_chain``
    never mixes it into a Qwen3 chain; it is used only where it is the ONLY
    configured backend family, and then consistently for writes and reads.
    """

    DEFAULT_MODEL = "text-embedding-v4"
    DEFAULT_DIMENSIONS = 1024

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._client = client or _build_embed_client(30.0, http2=True)

    @property
    def name(self) -> str:
        return "dashscope_embedding"

    @property
    def vector_space(self) -> str:
        return _dashscope_vector_space(self._model, self._dimensions)

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "input": [text],
                "dimensions": self._dimensions,
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    async def is_available(self) -> bool:
        return True  # Cloud API — assume available, let embed() fail if not


# ---------------------------------------------------------------------------
# Main embedding provider
# ---------------------------------------------------------------------------


class EmbeddingProvider:
    """Embedding provider with backend chain and two-level cache.

    Backend chain order is set by ``build_chain``; every backend in one
    provider must share a vector space (a mixed chain raises ValueError).
    If all backends fail, raises EmbeddingUnavailableError.
    Caller (MemoryStore) falls to FTS5-only and queues for later embedding.
    """

    def __init__(
        self,
        *,
        backends: list[EmbeddingBackend] | None = None,
        activity_tracker: ProviderActivityTracker | None = None,
        event_bus: GenesisEventBus | None = None,
        cache_dir: Path | None = _DEFAULT_CACHE_DIR,
    ) -> None:
        self._backends = backends if backends is not None else self._build_default_chain()
        # One provider = one vector space. A chain that mixes spaces would
        # write whichever model answered into a collection queried by another,
        # which corrupts retrieval silently, so it is refused outright rather
        # than logged: it can only come from a caller assembling backends by
        # hand, and that is a programming error, not a runtime condition.
        #
        # Declaring a space is MANDATORY, with no exemption. An earlier version
        # skipped backends that declared none, so an undeclared backend could
        # sit beside a Qwen3 one and write its own model's vectors into the same
        # collection, and every undeclared provider shared one cache namespace.
        # Test doubles declare a space like any other backend.
        undeclared = [
            getattr(b, "name", repr(b))
            for b in self._backends
            if not backend_vector_space(b)
        ]
        if undeclared:
            msg = (
                f"EmbeddingProvider backends {undeclared} declare no vector space; "
                "every backend must name the space its vectors are in"
            )
            raise ValueError(msg)
        spaces = {backend_vector_space(b) for b in self._backends}
        if len(spaces) > 1:
            msg = (
                "EmbeddingProvider backends span more than one vector space "
                f"({sorted(s for s in spaces if s)}); a chain must use one "
                "embedding model"
            )
            raise ValueError(msg)
        self._vector_space: str | None = next(iter(spaces), None)
        # An empty chain can never produce (or cache) a vector, so its prefix is
        # never used to store anything; it only has to be well-defined.
        self._cache_prefix = self._vector_space or _EMPTY_CHAIN_CACHE_PREFIX
        # The backend that produced the most recent remote vector. Observed, not
        # inferred from chain order: a fallback rung answers during an outage.
        self._last_backend: EmbeddingBackend | None = None
        self._cache: dict[str, tuple[list[float], float]] = {}
        self._cache_ttl: float = 86400.0  # 24 hours
        self._cache_max: int = 2048
        self._tracker = activity_tracker
        self._event_bus = event_bus

        # Observability counters
        self._l1_hits: int = 0
        self._l2_hits: int = 0
        self._misses: int = 0
        self._remote_calls: int = 0
        self._consecutive_backend_failures: dict[str, int] = {}

        # L2 shared disk cache
        self._disk_cache = None
        if cache_dir is not None:
            try:
                import json as _json

                import diskcache
                import diskcache.core

                class _SafeDisk(diskcache.Disk):
                    """Disk using JSON instead of pickle (CVE-2025-69872)."""

                    def store(self, value, read, key=diskcache.core.UNKNOWN):
                        if isinstance(value, (list, dict)):
                            value = _json.dumps(value)
                        return super().store(value, read, key=key)

                    def fetch(self, mode, filename, value, read):
                        if mode == diskcache.core.MODE_PICKLE:
                            return None
                        data = super().fetch(mode, filename, value, read)
                        if isinstance(data, str):
                            try:
                                return _json.loads(data)
                            except (ValueError, _json.JSONDecodeError):
                                return data
                        return data

                cache_dir.mkdir(parents=True, exist_ok=True)
                self._disk_cache = diskcache.Cache(
                    str(cache_dir), size_limit=100_000_000,  # 100 MB
                    disk=_SafeDisk,
                )
            except Exception:
                logger.warning(
                    "Failed to initialize diskcache at %s, using L1-only",
                    cache_dir, exc_info=True,
                )

        backend_names = [b.name for b in self._backends]
        logger.info("Embedding provider initialized: chain=%s", backend_names)

    @staticmethod
    def build_chain(
        *,
        ollama_first: bool = False,
        priority_tier: bool = False,
        fresh_collection: bool = False,
    ) -> list[EmbeddingBackend]:
        """Build backend chain with configurable priority order.

        Args:
            ollama_first: If True, Ollama leads. If False (the DEFAULT),
                         cloud leads and Ollama is the fallback rung.
                         The default used to be True, on the reasoning that a
                         write has no deadline so the slower local backend is
                         free. It is not free: local embedding is inference, and
                         on a GPU-less host every write burns cores the rest of
                         the system is contending for. MEASURED 2026-09-26
                         through this chain, 20 calls each: Ollama p50 2395.8ms
                         / p95 2952.9ms, DeepInfra p50 207.8ms / p95 399.0ms —
                         11.5x at p50. FIFTEEN callers construct an
                         EmbeddingProvider with no explicit chain and so inherit
                         this value — six in ``src/`` and nine in ``scripts/``,
                         the highest-traffic being
                         ``scripts/genesis_mcp_server.py``, which is the
                         provider behind ``memory_store`` / ``reference_store``
                         / ``knowledge_ingest`` for every session. That is why
                         the DEFAULT is what had to move rather than one call
                         site. They are NOT all write paths, and an earlier
                         revision said they were: the procedural novelty gate
                         and the session-awareness drift lane embed queries, and
                         the standalone memory MCP used this one provider for
                         ``memory_recall`` too until it was given a separate
                         priority-tier recall provider like the runtime's.
            (vector space): whatever the order, the chain only ever contains
                         backends in ONE vector space — by default the space of
                         the backend that led the pre-flip storage order (Ollama
                         if enabled, else the first cloud backend; see
                         ``fresh_collection`` for the exception), because that is
                         the space the existing corpus was written in. The
                         cloud-first flip therefore reorders backends WITHIN
                         that space; it never changes which model writes. A
                         backend in another space (today: DashScope beside any
                         Qwen3 backend) is left out and logged, not appended.
                         That anchor is INFERRED from configuration, not read
                         from the collection: nothing records which space a
                         collection was written in (issue #2502 tracks a
                         recorded per-collection marker checked at startup).
            fresh_collection: True ONLY for a caller writing into a brand-new,
                         EMPTY collection (the LongMemEval ephemeral store).
                         There is no corpus to match, so the chain anchors to
                         the leader of the requested order instead of the
                         historical storage leader. Never set it for a live
                         collection: it would let the order choose the model.
            priority_tier: If True, the DeepInfra backend requests the paid
                         priority scheduling tier (1.5x rate). Defaults to
                         False so no caller is billed the premium implicitly —
                         the DECISION belongs at the call site, where it is
                         visible, not buried in a builder default.

        The two flags are deliberately independent even though today only the
        recall chain sets both. Ordering is about which backend answers;
        ``priority_tier`` is about what that answer COSTS, and conflating them
        would hide a billing decision behind a routing one.
        """
        import os

        from genesis.env import (
            dashscope_api_key,
            deepinfra_api_key,
            ollama_enabled,
            ollama_url,
        )

        ollama_backends: list[EmbeddingBackend] = []
        if ollama_enabled():
            model = os.environ.get(
                "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b-fp16",
            )
            ollama_backends.append(OllamaBackend(url=ollama_url(), model=model))

        cloud_backends: list[EmbeddingBackend] = []
        excluded_desc: list[str] = []
        di_key = deepinfra_api_key()
        if di_key:
            cloud_backends.append(
                DeepInfraBackend(
                    api_key=di_key,
                    service_tier="priority" if priority_tier else None,
                )
            )
        ds_key = dashscope_api_key()
        ds_space = (
            _dashscope_vector_space(
                DashScopeBackend.DEFAULT_MODEL, DashScopeBackend.DEFAULT_DIMENSIONS,
            )
            if ds_key
            else None
        )

        # The spaces the configured backends produce, in the historical storage
        # order (local, then DeepInfra, then DashScope) and in the order this
        # caller asked for. Computed BEFORE DashScope is constructed so an
        # excluded DashScope never builds an HTTP client (several callers build
        # a provider per call).
        local_spaces = [b.vector_space for b in ollama_backends]
        cloud_spaces = [b.vector_space for b in cloud_backends] + (
            [ds_space] if ds_space else []
        )
        historical_spaces = local_spaces + cloud_spaces
        requested_spaces = (
            local_spaces + cloud_spaces if ollama_first else cloud_spaces + local_spaces
        )

        # The anchor is the ONE space the chain may contain.
        #   Default (an existing collection): the space of the historical
        #   storage leader, because that is the space the corpus was written
        #   in. Order is a per-caller preference; the space is a property of
        #   the collection and must not depend on it — otherwise the storage
        #   and recall chains could disagree on the model.
        #   fresh_collection=True (a brand-new, EMPTY collection): there is no
        #   corpus to match, so the chain anchors to the leader of the order
        #   the caller asked for.
        spaces_for_anchor = requested_spaces if fresh_collection else historical_spaces
        anchor = spaces_for_anchor[0] if spaces_for_anchor else None

        if ds_key:
            if ds_space == anchor:
                cloud_backends.append(DashScopeBackend(api_key=ds_key))
            else:
                excluded_desc.append(f"dashscope_embedding ({ds_space})")

        if ollama_first:
            ordered = ollama_backends + cloud_backends
        else:
            ordered = cloud_backends + ollama_backends

        chain = [b for b in ordered if b.vector_space == anchor]
        excluded_desc += [
            f"{b.name} ({b.vector_space})" for b in ordered if b.vector_space != anchor
        ]
        # Once per process per distinct exclusion: several callers build a
        # provider per call, and the configuration does not change between them.
        exclusion_key = (anchor, tuple(excluded_desc))
        if excluded_desc and exclusion_key not in _LOGGED_EXCLUSIONS:
            _LOGGED_EXCLUSIONS.add(exclusion_key)
            logger.warning(
                "Embedding chain excludes %s: vector space differs from the "
                "corpus space %s (mixing models in one collection corrupts "
                "retrieval)",
                excluded_desc, anchor,
            )

        if not chain:
            logger.warning(
                "No embedding backends configured. Set GENESIS_ENABLE_OLLAMA=true, "
                "API_KEY_DEEPINFRA, or API_KEY_QWEN in secrets.env."
            )

        return chain

    @staticmethod
    def _build_default_chain() -> list[EmbeddingBackend]:
        """Build the default backend chain (cloud first, Ollama as fallback)."""
        return EmbeddingProvider.build_chain(ollama_first=False)

    @property
    def tracker(self) -> ProviderActivityTracker | None:
        """Activity tracker for recording call metrics."""
        return self._tracker

    # -- Cache layer (unchanged from original) --

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(f"{self._cache_prefix}:{text}".encode()).hexdigest()

    @property
    def vector_space(self) -> str | None:
        """The single vector space every backend in this chain produces."""
        return self._vector_space

    @property
    def last_backend(self) -> EmbeddingBackend | None:
        """The backend that produced this provider's most recent remote vector.

        None until the first remote call succeeds. Per instance, so per
        process: another process's provider has its own.
        """
        return self._last_backend

    def _cache_get(self, text: str) -> list[float] | None:
        key = self._cache_key(text)

        # L1: in-process dict
        entry = self._cache.get(key)
        if entry is not None:
            vec, ts = entry
            if time.monotonic() - ts <= self._cache_ttl:
                self._l1_hits += 1
                return vec
            del self._cache[key]

        # L2: shared diskcache
        if self._disk_cache is not None:
            try:
                vec = self._disk_cache.get(key)
                if vec is not None:
                    self._l2_hits += 1
                    self._l1_put(key, vec)
                    return vec
            except Exception:
                logger.debug("diskcache get failed for key %s", key[:12], exc_info=True)

        self._misses += 1
        return None

    def _l1_put(self, key: str, vec: list[float]) -> None:
        if len(self._cache) >= self._cache_max:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        self._cache[key] = (vec, time.monotonic())

    def _cache_put(self, text: str, vec: list[float]) -> None:
        key = self._cache_key(text)
        self._l1_put(key, vec)
        if self._disk_cache is not None:
            try:
                self._disk_cache.set(key, vec, expire=604800)
            except Exception:
                logger.debug("diskcache set failed for key %s", key[:12], exc_info=True)

    def cache_stats(self) -> dict:
        return {
            "l1_size": len(self._cache),
            "l2_size": len(self._disk_cache) if self._disk_cache is not None else 0,
            "l1_hits": self._l1_hits,
            "l2_hits": self._l2_hits,
            "misses": self._misses,
            "remote_calls": self._remote_calls,
        }

    @property
    def backends(self) -> list[EmbeddingBackend]:
        """Backend chain in priority order."""
        return list(self._backends)

    def chain_health(self) -> list[dict]:
        """Return backend chain health in neural-monitor chain_health format.

        Each entry has: provider, state (derived from activity tracker error
        rate), failures, type, model, has_api_key — matching the shape
        expected by the call_sites snapshot for rendering chain health dots.
        """
        result = []
        for backend in self._backends:
            state = "closed"  # default healthy
            if self._tracker:
                summary = self._tracker.summary(backend.name)
                if isinstance(summary, dict) and summary.get("calls", 0) > 0:
                    if summary["error_rate"] > 0.5:
                        state = "open"
                    elif summary["error_rate"] > 0.1:
                        state = "half_open"
            result.append({
                "provider": backend.name,
                "state": state,
                "failures": self._consecutive_backend_failures.get(backend.name, 0),
                "type": "embedding",
                "model": getattr(backend, "_model", "unknown"),
                "has_api_key": True,
            })
        return result

    # -- Public API --

    async def is_available(self) -> bool:
        """Check if at least one embedding backend is reachable."""
        for backend in self._backends:
            try:
                if await backend.is_available():
                    return True
            except Exception:
                continue
        return False

    async def embed(self, text: str) -> list[float]:
        """Embed single text, returns 1024-dim vector."""
        cached = self._cache_get(text)
        if cached is not None:
            if self._tracker:
                self._tracker.record(
                    "embedding", latency_ms=0, success=True, cache_hit=True,
                )
            return cached

        vec = await self._embed_remote(text)
        self._cache_put(text, vec)
        return vec

    async def _embed_remote(self, text: str) -> list[float]:
        """Try each backend in chain order. First success wins."""
        self._remote_calls += 1
        if self._remote_calls % 100 == 0:
            stats = self.cache_stats()
            logger.debug(
                "Embedding cache: L1=%d/%d L2=%d hits=%d+%d misses=%d remote=%d",
                stats["l1_size"], self._cache_max, stats["l2_size"],
                stats["l1_hits"], stats["l2_hits"], stats["misses"],
                stats["remote_calls"],
            )

        errors: list[tuple[str, Exception]] = []

        for backend in self._backends:
            t0 = time.monotonic()
            try:
                vec = await backend.embed(text)
                latency = (time.monotonic() - t0) * 1000
                if self._tracker:
                    self._tracker.record(
                        backend.name, latency_ms=latency, success=True,
                    )
                # Reset failure counter on success
                self._consecutive_backend_failures[backend.name] = 0
                self._last_backend = backend
                if errors:
                    # Log fallback event if primary failed
                    failed_names = [name for name, _ in errors]
                    details = "; ".join(
                        f"{n}: {type(e).__name__}" + (f" ({e})" if str(e) else "")
                        for n, e in errors
                    )
                    await self._emit_embedding_event(
                        "embedding.fallback",
                        f"{'→'.join(failed_names)}→{backend.name} fallback ({details})",
                        "warning",
                    )
                return vec
            except Exception as exc:
                latency = (time.monotonic() - t0) * 1000
                if self._tracker:
                    self._tracker.record(
                        backend.name, latency_ms=latency, success=False,
                    )
                fails = self._consecutive_backend_failures.get(backend.name, 0) + 1
                self._consecutive_backend_failures[backend.name] = fails
                # Log full traceback only on first few failures; suppress spam
                # after repeated failures from the same backend (portability).
                exc_desc = f"{type(exc).__name__}" + (f": {exc}" if str(exc) else " (no details)")
                if fails <= 3:
                    logger.warning(
                        "Embedding backend '%s' failed (%d consecutive): %s",
                        backend.name, fails, exc_desc, exc_info=True,
                    )
                elif fails % 50 == 0:
                    logger.warning(
                        "Embedding backend '%s' still failing (%d consecutive): %s",
                        backend.name, fails, exc_desc,
                    )
                else:
                    logger.debug(
                        "Embedding backend '%s' failed (%d consecutive): %s",
                        backend.name, fails, exc_desc,
                    )
                errors.append((backend.name, exc))

        # All backends failed
        failed_names = [name for name, _ in errors]
        await self._emit_embedding_event(
            "embedding.failed",
            f"All embedding backends failed: {', '.join(failed_names)}",
            "error",
        )
        msg = f"All embedding backends failed: {failed_names}"
        raise EmbeddingUnavailableError(msg)

    async def _emit_embedding_event(
        self, event_type: str, message: str, severity: str,
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from genesis.observability.types import Severity, Subsystem

            sev = Severity(severity)
            await self._event_bus.emit(
                Subsystem.PROVIDERS, sev, event_type, message,
            )
        except Exception:
            logger.debug("Failed to emit embedding event", exc_info=True)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        return [await self.embed(t) for t in texts]

    @staticmethod
    def enrich(content: str, memory_type: str, tags: list[str]) -> str:
        """Contextual enrichment: prepend type and tags before embedding."""
        if tags:
            return f"{memory_type}: {' '.join(tags)}: {content}"
        return f"{memory_type}: {content}"
