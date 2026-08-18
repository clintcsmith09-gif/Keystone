"""StorageBackend + envelope-encryption tests (architecture §6.4)."""
from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from app.storage import StorageIntegrityError, get_storage
from app.storage.local_encrypted import LocalEncryptedStorage, parse_master_key

# opaque key shapes the upload path will produce (UUIDs)
KEY = "3f0d5a2e-8b1e-4c9f-a7d3-6e2b0c4d9f1a"


@pytest.fixture()
def storage() -> LocalEncryptedStorage:
    return get_storage()


def test_round_trip(storage):
    payload = '{"ledger": [1, 2, 3], "note": "héllo wörld"}'.encode("utf-8")
    obj = storage.put(KEY, payload, content_type="application/json")
    assert obj.size_bytes == len(payload)
    assert storage.exists(KEY)
    assert storage.get(KEY) == payload


def test_get_missing_raises_keyerror(storage):
    with pytest.raises(KeyError):
        storage.get("no-such-key")


def test_at_rest_is_ciphertext_not_plaintext(storage):
    payload = b"SUPER-SECRET-LEDGER-ROW"
    storage.put(KEY, payload)
    raw = (storage.root / KEY).read_bytes()
    assert payload not in raw
    assert raw[:4] == b"VRT1"  # magic header present


def test_tamper_detected(storage):
    storage.put(KEY, b"authentic bytes")
    path = storage.root / KEY
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0xFF  # flip a ciphertext bit
    path.write_bytes(bytes(blob))
    with pytest.raises(StorageIntegrityError):
        storage.get(KEY)


def test_wrong_master_key_detected(storage, tmp_path):
    import base64
    import secrets

    # Encrypt with a different master key, then try to read with the fixture's key.
    other_key = base64.b64encode(secrets.token_bytes(32)).decode()
    other = LocalEncryptedStorage(root=tmp_path, master_key=other_key)
    other.put(KEY, b"secret")
    raw = (tmp_path / KEY).read_bytes()
    (storage.root / KEY).write_bytes(raw)
    with pytest.raises(StorageIntegrityError):
        storage.get(KEY)


def test_delete_removes_and_overwrites(storage):
    storage.put(KEY, b"x" * 1000)
    path = storage.root / KEY
    assert path.exists()
    storage.delete(KEY)
    assert not storage.exists(KEY)
    assert not path.exists()


def test_unsafe_key_rejected(storage):
    for bad in ["../escape", "a/b", "a\\b", "", "x" * 256]:
        with pytest.raises(ValueError):
            storage.put(bad, b"data")


def test_master_key_validation():
    import base64, secrets

    good = base64.b64encode(secrets.token_bytes(32)).decode()
    assert len(parse_master_key(good)) == 32
    assert len(parse_master_key(secrets.token_hex(32))) == 32
    for bad in ["", "too-short", "Z" * 100]:
        with pytest.raises(ValueError):
            parse_master_key(bad)
