"""Tests for session_context ContextVar-based session tracking."""

import asyncio

import pytest

from genesis.observability.session_context import (
    get_session_id,
    session_scope,
    set_session_id,
)


class TestGetSet:
    def test_default_is_none(self):
        assert get_session_id() is None

    def test_set_and_get(self):
        set_session_id("sess-abc")
        try:
            assert get_session_id() == "sess-abc"
        finally:
            set_session_id(None)

    def test_set_none_clears(self):
        set_session_id("sess-123")
        set_session_id(None)
        assert get_session_id() is None


class TestSessionScope:
    def test_scope_sets_and_restores(self):
        assert get_session_id() is None
        with session_scope("sess-xyz"):
            assert get_session_id() == "sess-xyz"
        assert get_session_id() is None

    def test_nested_scopes(self):
        with session_scope("outer"):
            assert get_session_id() == "outer"
            with session_scope("inner"):
                assert get_session_id() == "inner"
            assert get_session_id() == "outer"
        assert get_session_id() is None

    def test_scope_restores_on_exception(self):
        with pytest.raises(ValueError), session_scope("will-fail"):
            assert get_session_id() == "will-fail"
            raise ValueError("boom")
        assert get_session_id() is None


class TestAsyncPropagation:
    @pytest.mark.asyncio
    async def test_contextvar_propagates_to_asyncio_task(self):
        """ContextVar should propagate to asyncio.create_task children."""
        captured = []

        async def child():
            captured.append(get_session_id())

        set_session_id("parent-sess")
        try:
            task = asyncio.create_task(child())
            await task
            assert captured == ["parent-sess"]
        finally:
            set_session_id(None)


    @pytest.mark.asyncio
    async def test_sibling_tasks_are_isolated(self):
        """Setting session_id in one task must not affect a sibling task."""
        results: dict[str, str | None] = {}

        async def task_a():
            set_session_id("task-a")
            await asyncio.sleep(0.01)
            results["a"] = get_session_id()

        async def task_b():
            await asyncio.sleep(0.01)
            results["b"] = get_session_id()

        await asyncio.gather(
            asyncio.create_task(task_a()),
            asyncio.create_task(task_b()),
        )
        assert results["a"] == "task-a"
        assert results["b"] is None


class TestEmitIntegration:
    @pytest.mark.asyncio
    async def test_emit_uses_contextvar_when_no_explicit_session_id(self):
        """emit() should auto-populate session_id from ContextVar."""
        from datetime import UTC, datetime

        from genesis.observability.events import GenesisEventBus
        from genesis.observability.types import Severity, Subsystem

        bus = GenesisEventBus(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))

        # Enable persistence to capture the dict that would be written
        original_queue = asyncio.Queue(maxsize=500)
        bus._write_queue = original_queue

        with session_scope("ctx-sess-001"):
            await bus.emit(
                Subsystem.ROUTING, Severity.INFO, "test.event", "hello",
            )

        # The dict queued for DB write should have our session_id
        item = original_queue.get_nowait()
        assert item["session_id"] == "ctx-sess-001"

    @pytest.mark.asyncio
    async def test_explicit_session_id_takes_precedence(self):
        """Explicit session_id in details should override ContextVar."""
        from datetime import UTC, datetime

        from genesis.observability.events import GenesisEventBus
        from genesis.observability.types import Severity, Subsystem

        bus = GenesisEventBus(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
        bus._write_queue = asyncio.Queue(maxsize=500)

        with session_scope("ctx-sess"):
            await bus.emit(
                Subsystem.ROUTING, Severity.INFO, "test.event", "hello",
                session_id="explicit-sess",
            )

        item = bus._write_queue.get_nowait()
        assert item["session_id"] == "explicit-sess"

    @pytest.mark.asyncio
    async def test_no_session_produces_none(self):
        """Without ContextVar or explicit session_id, should be None."""
        from datetime import UTC, datetime

        from genesis.observability.events import GenesisEventBus
        from genesis.observability.types import Severity, Subsystem

        bus = GenesisEventBus(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
        bus._write_queue = asyncio.Queue(maxsize=500)

        await bus.emit(
            Subsystem.ROUTING, Severity.INFO, "test.event", "hello",
        )

        item = bus._write_queue.get_nowait()
        assert item["session_id"] is None
