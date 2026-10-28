import json
import os
from pathlib import Path
import pytest
from repolaunch import credentials


def test_no_file_without_credentials(tmp_path):
    assert credentials.load_credentials(tmp_path) == {}


def test_encrypted_file_round_trip_and_no_plaintext(tmp_path, monkeypatch):
    secret = 'test-only-fixture-secret'
    monkeypatch.setattr(credentials, '_crypt', lambda raw, decrypt: bytes(value ^ 0x9a for value in raw))
    values = {'api_key': secret, 'base_url': 'https://api.example/v1', 'model': 'org/model'}
    credentials.save_credentials(tmp_path, values)
    assert secret.encode() not in (tmp_path/'credentials.enc').read_bytes()
    assert credentials.load_credentials(tmp_path) == values
    assert len(list(tmp_path.iterdir())) == 1


def test_failure_preserves_existing_file(tmp_path, monkeypatch):
    source = tmp_path/'credentials.enc'
    source.write_bytes(b'previous encrypted blob')
    def fail(*args, **kwargs):
        raise credentials.CredentialError('safe failure')
    monkeypatch.setattr(credentials, '_crypt', fail)
    with pytest.raises(credentials.CredentialError):
        credentials.save_credentials(tmp_path, {'api_key':'fixture'})
    assert source.read_bytes() == b'previous encrypted blob'


@pytest.mark.parametrize('values', [{'unknown':'value'},{'api_key':12},{'api_key':'bad\nkey'}, {'api_key':'x'*4097}])
def test_invalid_settings_never_encrypt_or_write(tmp_path, values, monkeypatch):
    monkeypatch.setattr(credentials, '_crypt', lambda *args, **kwargs: pytest.fail('must validate before encryption'))
    with pytest.raises(credentials.CredentialError):
        credentials.save_credentials(tmp_path, values)
    assert not (tmp_path/'credentials.enc').exists()


def test_rejects_corrupt_and_oversized_file(tmp_path):
    source=tmp_path/'credentials.enc'
    for blob in [b'plaintext credential', b'x'*70000]:
        source.write_bytes(blob)
        with pytest.raises(credentials.CredentialError):
            credentials.load_credentials(tmp_path)


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_real_dpapi_in_memory_round_trip():
    raw=b'RepoLaunch-public-test-fixture'
    try:
        encrypted=credentials._crypt(raw,decrypt=False)
    except credentials.CredentialError:
        pytest.skip('DPAPI not available in restricted execution context')
    assert raw not in encrypted
    assert credentials._crypt(encrypted,decrypt=True) == raw
