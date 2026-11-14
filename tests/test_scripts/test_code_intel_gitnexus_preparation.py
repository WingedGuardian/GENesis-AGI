"""Actual held resolver uses harmless fixture Node only after caller admission."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import package
from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_gitnexus_runtime import frozen
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def operation(namespace, name):
    result = namespace.get(name)
    assert callable(result), 'GN reporter needs its bound '+name+' seam'
    return result


def test_fresh_source_recapture_keeps_one_node_prefix_and_spawns_nothing(selection):
    snapshot, modules, value, namespace = frozen(selection)
    # Origin checks belong to an actual execution, not the shared pure
    # capsule fixture used by the independent queue-free decoder tests.
    queue = modules['queue']
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
    queue.write_marker(str(snapshot.root), 'gitnexus', value['reservation']['run_mode'])
    claimed = queue.claim_native(queue.marker_hash(str(snapshot.root)), provider='gitnexus')
    value['context'] = claimed['context']
    value['reservation']['unit'] = queue._native.expected_unit(value['context'])
    assert queue.reserve_launch(value['context'], value['reservation']) == 'reserved'
    source = operation(namespace, 'current_source')(snapshot, modules, value)
    assert source == value['reservation']['source'] and not selection[4].exists()
    entry = Path(value['provider_options']['entry']['path'])
    entry.write_text(entry.read_text()+'// changed installed entry\n')
    with pytest.raises(ValueError):
        namespace['current_source'](snapshot, modules, value)
    assert not selection[4].exists()


def test_canonical_resolver_confirms_the_actual_pinned_files(selection):
    snapshot, modules, value, namespace = frozen(selection)
    environment = {**value['environment_recipe']['frozen'], 'INVOCATION_ID': '1'*32}
    operation(namespace, 'confirm')(snapshot, modules, value, environment)
    assert selection[4].read_text() == 'executed'


def test_canonical_resolver_cannot_substitute_another_packaged_install(selection):
    snapshot, modules, value, namespace = frozen(selection)
    confirm = operation(namespace, 'confirm')
    different = package(Path(selection[2]['HOME']) / 'different-provider')
    environment = {**value['environment_recipe']['frozen'], 'INVOCATION_ID': '1'*32,
                   'GITNEXUS_BIN': str(different)}
    with pytest.raises(ValueError):
        confirm(snapshot, modules, value, environment)


@pytest.mark.parametrize('wire', [b'', b'node\0entry', b'node\0entry\0extra\0', b'\xff\0entry\0'])
def test_resolver_transport_requires_complete_exact_fields(selection, monkeypatch, wire):
    snapshot, modules, value, namespace = frozen(selection)
    confirm = operation(namespace, 'confirm')
    monkeypatch.setitem(confirm.__globals__, 'subprocess', SimpleNamespace(
        run=lambda *args, **kwargs: SimpleNamespace(stdout=wire)))
    with pytest.raises((ValueError, UnicodeError)):
        confirm(snapshot, modules, value, value['environment_recipe']['frozen'])
