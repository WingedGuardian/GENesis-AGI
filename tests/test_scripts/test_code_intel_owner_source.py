"""Private initial owner roles require captured runner source and its real lock."""
import subprocess

import pytest

from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


@pytest.mark.parametrize('profile', ['entry', 'runner'])
def test_finite_source_inventory_binds_owner_and_initial_admission(tmp_path, monkeypatch, profile):
    _, _, snapshot = case(tmp_path, monkeypatch, profile)
    assert {'scripts/lib/code_intel_owner.py', 'scripts/lib/code_intel_owner_admission.py'} <= snapshot.files.keys()


def test_initial_owner_builder_accepts_only_source_only_runner(tmp_path, monkeypatch):
    _, _, snapshot = case(tmp_path, monkeypatch, 'runner')
    command = snapshot.resume_command('owner', '{}', 'fast', '/synthetic/settings.json')
    assert command[-5:] == ['owner', 'runner', '{}', 'fast', '/synthetic/settings.json']
    modules = snapshot.assemble()
    with pytest.raises(ValueError):
        snapshot.with_runtime(runtime(snapshot, modules)).resume_command('owner')


def test_initial_owner_endpoint_refuses_missing_runner_lock_before_queue_or_provider(tmp_path, monkeypatch):
    _, _, snapshot = case(tmp_path, monkeypatch, 'runner')
    command = snapshot.resume_command()
    command[-2] = 'owner'  # Receiving endpoint must validate independently.
    command.extend(['{}', 'fast', '/synthetic/settings.json'])
    with snapshot.sealed() as capsule:
        result = subprocess.run(command, stdin=capsule, capture_output=True, timeout=30)
    assert result.returncode == 75
    assert b'initial owner refused: OSError' in result.stderr
    assert not (tmp_path / 'state').exists()


def test_entry_profile_cannot_build_initial_owner_command(tmp_path, monkeypatch):
    _, _, snapshot = case(tmp_path, monkeypatch, 'entry')
    with pytest.raises(ValueError):
        snapshot.resume_command('owner', '{}', 'fast', '/synthetic/settings.json')
