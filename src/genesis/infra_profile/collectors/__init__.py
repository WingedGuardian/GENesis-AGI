"""Collector registry for the infrastructure body schema.

Each collector is an async callable returning a ``SectionResult``. The service
runs them via ``asyncio.gather(return_exceptions=True)`` (the
``guardian/collector.py`` fan-out shape): one failing collector degrades its
own section and never takes down the refresh.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from genesis.infra_profile.collectors.container import (
    collect_cpu,
    collect_kernel,
    collect_limits,
    collect_memory,
    collect_network,
    collect_os,
    collect_storage,
    collect_systemd,
    collect_time,
    collect_versions,
    collect_virt,
)
from genesis.infra_profile.collectors.qdrant_facts import collect_qdrant
from genesis.infra_profile.collectors.sqlite_facts import collect_sqlite

from genesis.infra_profile.types import SectionResult

# Container-plane collectors, in render order. Names must be unique — they key
# the profile's sections dict.
CONTAINER_COLLECTORS: list[Callable[[], Awaitable[SectionResult]]] = [
    collect_os,
    collect_virt,
    collect_cpu,
    collect_memory,
    collect_storage,
    collect_kernel,
    collect_sqlite,
    collect_qdrant,
    collect_network,
    collect_systemd,
    collect_versions,
    collect_limits,
    collect_time,
]
