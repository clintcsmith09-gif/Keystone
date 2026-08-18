"""Test fixtures. Sets a hermetic environment BEFORE importing the app:
storage root in a temp dir, a dev master key, and (optionally) a database URL.

DB-gated tests (test_migrations.py) are skipped unless VERITAS_TEST_DATABASE_URL
is set — point it at a scratch database, never production.
"""
from __future__ import annotations

import base64
import os
import secrets

import pytest

# --- hermetic env before any app import -------------------------------------
_TMP_STORAGE = "/tmp/veritas-test-storage"
os.environ.setdefault("VERITAS_STORAGE_ROOT", _TMP_STORAGE)
os.environ.setdefault(
    "VERITAS_MASTER_KEY",
    base64.b64encode(secrets.token_bytes(32)).decode(),
)
os.environ.setdefault("VERITAS_ENVIRONMENT", "test")
os.environ.setdefault("VERITAS_DATABASE_URL", "postgresql://localhost/veritas_test")


@pytest.fixture()
def master_key() -> str:
    return os.environ["VERITAS_MASTER_KEY"]


@pytest.fixture()
def storage_root() -> str:
    return _TMP_STORAGE
