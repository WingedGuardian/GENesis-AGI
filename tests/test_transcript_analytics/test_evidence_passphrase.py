"""Encrypted evidence refuses a passphrase GPG would truncate."""

import pytest

from genesis.transcript_analytics import evidence


@pytest.mark.parametrize('password', ['prefix\nsuffix', 'prefix\n', '\nprefix'])
def test_archive_evidence_refuses_multiline_key_without_spawning(monkeypatch, tmp_path, password):
    monkeypatch.setenv('GENESIS_BACKUP_PASSPHRASE', password)
    calls = []
    monkeypatch.setattr(evidence.subprocess, 'Popen', lambda *args, **kwargs: calls.append(args))
    result = evidence._archive_window(tmp_path / 'payload.gpg', 'source.jsonl', 1, 0, 100)
    assert 'single line' in result['unavailable']
    assert calls == []
    assert password not in str(result)
