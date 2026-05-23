"""Tests for AgentProvider protocol conformance."""

from __future__ import annotations

import inspect

from genesis.cc.invoker import CCInvoker
from genesis.cc.protocol import AgentProvider


def test_ccinvoker_is_agent_provider():
    """CCInvoker must satisfy the AgentProvider runtime-checkable protocol."""
    invoker = CCInvoker(claude_path="/usr/bin/claude")
    assert isinstance(invoker, AgentProvider)


def test_protocol_has_three_methods():
    """AgentProvider protocol defines exactly run, run_streaming, interrupt."""
    protocol_methods = {
        name
        for name, _ in inspect.getmembers(AgentProvider, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert protocol_methods == {"run", "run_streaming", "interrupt"}


def test_run_signature_matches():
    """CCInvoker.run() signature matches AgentProvider.run()."""
    proto_sig = inspect.signature(AgentProvider.run)
    impl_sig = inspect.signature(CCInvoker.run)
    # Both should accept (self, invocation: CCInvocation) -> CCOutput
    proto_params = list(proto_sig.parameters.keys())
    impl_params = list(impl_sig.parameters.keys())
    assert proto_params == impl_params


def test_run_streaming_signature_matches():
    """CCInvoker.run_streaming() signature matches AgentProvider.run_streaming()."""
    proto_sig = inspect.signature(AgentProvider.run_streaming)
    impl_sig = inspect.signature(CCInvoker.run_streaming)
    proto_params = list(proto_sig.parameters.keys())
    impl_params = list(impl_sig.parameters.keys())
    assert proto_params == impl_params


def test_interrupt_signature_matches():
    """CCInvoker.interrupt() signature matches AgentProvider.interrupt()."""
    proto_sig = inspect.signature(AgentProvider.interrupt)
    impl_sig = inspect.signature(CCInvoker.interrupt)
    proto_params = list(proto_sig.parameters.keys())
    impl_params = list(impl_sig.parameters.keys())
    assert proto_params == impl_params
