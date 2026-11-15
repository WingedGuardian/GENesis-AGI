"""Actual local stdio/module callback, with confirmation refusal injected safely."""
import asyncio
import sys

import pytest

from genesis.mcp.local_modules import ModuleCleanupUnconfirmed
from genesis.modules.external.adapter import ExternalProgramAdapter
from genesis.modules.external.config import ProgramConfig
from tests.test_mcp.test_local_module_owner import CHILD, owner, ready
from tests.test_mcp.test_local_module_owner import owned_children as owned_children


@pytest.mark.parametrize('phase', ['readiness', 'operation'])
@pytest.mark.parametrize('ending', ['caller', 'owner'])
async def test_actual_adapter_preserves_cleanup_uncertainty(tmp_path, monkeypatch, owned_children, phase, ending):
    import genesis.mcp.local_modules as local_modules
    from genesis.mcp.health import module_ops

    scope = owner(cleanup_timeout_s=2)
    adapter = ExternalProgramAdapter(ProgramConfig.from_dict({
        'name': 'fixture', 'enabled': True,
        'ipc': {'method': 'stdio', 'command': [sys.executable, '-I', '-S', '-u', '-c', CHILD,
                                              str(tmp_path/'ready'), str(tmp_path/'release')], 'timeout': 5},
        'health_check': {'endpoint': 'held' if phase == 'readiness' else 'health'},
        'operations': {'held': {'path': 'held'}},
    }), owner=scope)
    confirm = local_modules.confirm_process_exit
    refuse = True

    async def controlled_confirm(*args, **kwargs):
        if refuse:
            raise RuntimeError('injected confirmation refusal; actual owned child remains')
        return await confirm(*args, **kwargs)

    monkeypatch.setattr(local_modules, 'confirm_process_exit', controlled_confirm)
    monkeypatch.setattr(module_ops, '_get_adapters', lambda: {'fixture': adapter})
    monkeypatch.setattr(module_ops, '_resource_owner', scope)
    task = asyncio.create_task(module_ops._impl_module_call('fixture', 'held'))
    try:
        await ready(tmp_path/'ready')
        if ending == 'caller':
            task.cancel()
        else:
            next(iter(scope._units.values())).cancel_scope.cancel()
        result, = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, ModuleCleanupUnconfirmed), type(result).__name__
        assert not task.cancelled() and any(r.uncertain for r in scope._resources)
        assert not scope.closed
    finally:
        # Restoration/physical settlement is AFTER the observed verdict.
        refuse = False
        (tmp_path/'release').touch()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scope.close()


@pytest.mark.parametrize('boundary', ['operation', 'adapter-health', 'stdio-health'])
@pytest.mark.parametrize('failure', ['ordinary', 'ownership', 'uncertain'])
async def test_selected_error_boundaries_preserve_only_cleanup_uncertainty(boundary, failure, monkeypatch):
    from types import SimpleNamespace

    from genesis.mcp.local_modules import ModuleOwnershipError

    scope = owner()
    adapter = ExternalProgramAdapter(ProgramConfig.from_dict({
        'name': 'fixture', 'enabled': True,
        'ipc': {'method': 'stdio', 'command': ['unused-owned-fixture']},
        'health_check': {'endpoint': 'health'},
        'operations': {'held': {'path': 'held'}},
    }), owner=scope)
    adapter._healthy = True
    adapter.ipc._process = SimpleNamespace(returncode=None)
    error = {'ordinary': ValueError('business failure'),
             'ownership': ModuleOwnershipError('admission refusal'),
             'uncertain': ModuleCleanupUnconfirmed('unconfirmed cleanup')}[failure]

    async def refuse(*args, **kwargs):
        raise error

    if boundary == 'adapter-health':
        monkeypatch.setattr(adapter.ipc, 'health_check', refuse)
        call = adapter.check_health
    else:
        monkeypatch.setattr(adapter.ipc, 'send', refuse)
        call = (lambda: adapter.execute_operation('held')) if boundary == 'operation' else (
            lambda: adapter.ipc.health_check('health', 200))
    try:
        if failure == 'uncertain':
            with pytest.raises(ModuleCleanupUnconfirmed) as caught:
                await call()
            assert caught.value is error
        else:
            result = await call()
            assert ('error' in result) if boundary == 'operation' else result is False
        assert not scope._resources and not scope._units
    finally:
        await scope.close()
