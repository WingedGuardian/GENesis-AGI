"""Historical integrity authority cannot bypass the current restore validator."""

import hashlib
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'archive_epoch_subject', Path(__file__).parents[2] / 'scripts/lib/transcript_archive.py'
)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home, scratch, captures, root = [tmp_path / name for name in ('gpg', 'scratch', 'captures', 'root')]
    for path in (home, scratch, captures, root):
        path.mkdir(mode=0o700)
    monkeypatch.setenv('GNUPGHOME', str(home))
    yield tmp_path, scratch, captures, root
    subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'],
                   capture_output=True, timeout=5, check=False)


def old_authority(sandbox, name, timestamp):
    tmp, scratch, captures, _root = sandbox
    plain = tmp / 'source.tar'
    with tarfile.open(plain, 'w', format=tarfile.PAX_FORMAT) as outgoing:
        member = tarfile.TarInfo(name)
        member.size = 1
        member.pax_headers = {'path': name, 'genesis.mtime_ns': timestamp,
                              'genesis.sha256': hashlib.sha256(b'x').hexdigest()}
        outgoing.addfile(member, io.BytesIO(b'x'))
    cipher = captures / archive.object_name(name)
    archive.crypt(plain, cipher, b'synthetic-epoch-key')
    document = {'version': 1, 'sources': {}, 'captures': {cipher.name: archive.checksum(cipher)}}
    archive.publish_capture_index(captures, scratch, b'synthetic-epoch-key', document)
    return document, cipher


@pytest.mark.parametrize('name,timestamp', [('x\0y', '1'), ('valid.jsonl', str(10**100))])
@pytest.mark.parametrize('operation', ['attest', 'backup', 'manifest'])
def test_old_digest_attestation_requires_current_validation(sandbox, name, timestamp, operation):
    tmp, scratch, captures, root = sandbox
    document, cipher = old_authority(sandbox, name, timestamp)
    if operation == 'attest':
        _changed, failures = archive.attest_captures(captures, scratch, b'synthetic-epoch-key', document)
        assert failures
        assert cipher.name in document['captures'], 'retain historical inventory despite failed validation'
    elif operation == 'backup':
        assert archive.backup(root, captures, scratch, b'synthetic-epoch-key') is True
    else:
        target = tmp / 'manifest.gpg'
        with pytest.raises(ValueError):
            archive.pool_manifest(captures, target, 'snapshot', scratch, b'synthetic-epoch-key')
        assert not target.exists()


def test_valid_legacy_authority_migrates_durably_once_without_rewriting_cipher(sandbox, monkeypatch):
    _tmp, scratch, captures, _root = sandbox
    document, cipher = old_authority(sandbox, 'valid.jsonl', '1')
    original = (archive.checksum(cipher), cipher.stat().st_mtime_ns)
    checkpoint = archive.CaptureCheckpoint(captures, scratch, b'synthetic-epoch-key', document, True)
    changed, failures = archive.attest_captures(captures, scratch, b'synthetic-epoch-key', document, checkpoint)
    assert changed and failures == []
    assert checkpoint.first_authorization_published
    saved, authenticated = archive.capture_index(captures, scratch, b'synthetic-epoch-key')
    assert authenticated and saved['validation_contract'] == archive._VALIDATION_CONTRACT
    assert saved['validated_captures'][cipher.name] == saved['captures'][cipher.name]
    real_crypt = archive.crypt
    def no_revalidation(source, target, password, decrypt=False):
        assert not (decrypt and source == cipher), 'completed validation must resume from checkpoint'
        return real_crypt(source, target, password, decrypt)
    monkeypatch.setattr(archive, 'crypt', no_revalidation)
    assert archive.attest_captures(captures, scratch, b'synthetic-epoch-key', saved) == (False, [])
    assert (archive.checksum(cipher), cipher.stat().st_mtime_ns) == original


@pytest.mark.parametrize('contract', ['previous-validator', 'different-platform'])
def test_changed_contract_never_authorizes_unvalidated_retained_bytes(sandbox, contract):
    _tmp, scratch, captures, _root = sandbox
    document, cipher = old_authority(sandbox, 'valid.jsonl', str(10**100))
    document['validation_contract'] = contract
    document['validated_captures'] = {cipher.name: document['captures'][cipher.name]}
    _changed, failures = archive.attest_captures(captures, scratch, b'synthetic-epoch-key', document)
    assert failures and cipher.name in document['captures']
    assert cipher.name not in document['validated_captures']


def test_missing_history_retained_across_validation_migration(sandbox):
    _tmp, scratch, captures, _root = sandbox
    document, cipher = old_authority(sandbox, 'valid.jsonl', '1')
    cipher.unlink()
    checkpoint = archive.CaptureCheckpoint(captures, scratch, b'synthetic-epoch-key', document, True)
    _changed, failures = archive.attest_captures(captures, scratch, b'synthetic-epoch-key', document, checkpoint)
    checkpoint.flush()
    assert failures
    saved, authenticated = archive.capture_index(captures, scratch, b'synthetic-epoch-key')
    assert authenticated and cipher.name in saved['captures']
    assert cipher.name not in saved['validated_captures']


@pytest.mark.parametrize('operation', ['index', 'attest', 'backup', 'manifest'])
def test_cached_retry_requires_index_parent_fence(sandbox, monkeypatch, operation):
    tmp, scratch, captures, root = sandbox
    key = b'synthetic-epoch-key'
    document, cipher = old_authority(sandbox, 'valid.jsonl', '1')
    real_replace, real_fence = archive.os.replace, archive._fsync_directory
    replaced = False

    def index_replace(source, destination):
        nonlocal replaced
        real_replace(source, destination)
        if destination == captures / archive._INDEX_NAME:
            replaced = True

    def failed_fence(path):
        if replaced and path == captures:
            raise OSError('synthetic visible-index parent fence failure')
        real_fence(path)

    with monkeypatch.context() as failure:
        failure.setattr(archive.os, 'replace', index_replace)
        failure.setattr(archive, '_fsync_directory', failed_fence)
        checkpoint = archive.CaptureCheckpoint(captures, scratch, key, document, True)
        with pytest.raises(archive.CaptureCheckpointError):
            archive._attest_capture(cipher, scratch, key, document, checkpoint)
    assert replaced
    target = tmp / 'manifest.gpg'

    def invoke():
        if operation == 'index':
            return archive.capture_index(captures, scratch, key)
        if operation == 'attest':
            return archive.attest_captures(captures, scratch, key, document)
        if operation == 'backup':
            return archive.backup(root, captures, scratch, key)
        return archive.pool_manifest(captures, target, 'snapshot', scratch, key)

    with monkeypatch.context() as failure:
        failure.setattr(archive, '_fsync_directory', failed_fence)
        with pytest.raises(OSError, match='parent fence failure'):
            invoke()
    assert not target.exists()
    assert document['captures'][cipher.name] == document['validated_captures'][cipher.name]
    fences = []
    real_crypt = archive.crypt

    def successful_fence(path):
        fences.append(path)
        real_fence(path)

    def no_capture_decryption(source, destination, password, decrypt=False):
        assert not (decrypt and source == cipher), 'fence retry must not restart validation'
        return real_crypt(source, destination, password, decrypt)

    monkeypatch.setattr(archive, '_fsync_directory', successful_fence)
    monkeypatch.setattr(archive, 'crypt', no_capture_decryption)
    result = invoke()
    assert captures in fences
    if operation == 'index':
        assert result[1] is True
    elif operation == 'attest':
        assert result == (False, [])
    elif operation == 'backup':
        assert result is False
    else:
        assert len(result) == 1


@pytest.mark.parametrize("operation", ["attest", "backup", "manifest"])
def test_previous_v2_cached_literal_cannot_bypass_passphrase_authentication(sandbox, operation):
    tmp, scratch, captures, root = sandbox
    key = b"synthetic-epoch-key"
    document, cipher = old_authority(sandbox, "valid.jsonl", "1")
    plain = tmp / "source.tar"
    # Reproduce a real literal OpenPGP packet accepted by the previous validator.
    subprocess.run(["gpg", "--no-options", "--batch", "--yes", "--store", "--output", str(cipher), str(plain)], capture_output=True, check=True)
    digest = archive.checksum(cipher)
    document["captures"][cipher.name] = digest
    document["validation_contract"] = (
        f"restore-v2-time_t-{archive.sysconfig.get_config_var('SIZEOF_TIME_T')}-"
        f"{archive.sys.getfilesystemencoding()}-{archive.sys.getfilesystemencodeerrors()}"
    )
    document["validated_captures"] = {cipher.name: digest}
    archive.publish_capture_index(captures, scratch, key, document)
    original = (cipher.read_bytes(), cipher.stat().st_mtime_ns)
    if operation == "attest":
        _, failures = archive.attest_captures(captures, scratch, key, document)
        assert failures and cipher.name not in document["validated_captures"]
        assert document["captures"][cipher.name] == digest
    elif operation == "backup":
        assert archive.backup(root, captures, scratch, key) is True
    else:
        target = tmp / "manifest.gpg"
        with pytest.raises(ValueError):
            archive.pool_manifest(captures, target, "snapshot", scratch, key)
        assert not target.exists()
    assert (cipher.read_bytes(), cipher.stat().st_mtime_ns) == original
    saved, authenticated = archive.capture_index(captures, scratch, key)
    assert authenticated and saved["captures"][cipher.name] == digest
