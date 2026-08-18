"""Storage backend factory. Swap backend by config change (architecture §6.4)."""
from __future__ import annotations

from ..config import Settings, get_settings
from .base import StorageBackend, StorageError, StorageIntegrityError, StorageObject
from .local_encrypted import LocalEncryptedStorage


def get_storage(settings: Settings | None = None) -> StorageBackend:
    """Return the configured StorageBackend for the given (or global) settings.

    MVP ships exactly one backend (local encrypted disk). The factory is the single
    seam where an S3-compatible managed tier plugs in later (Phase 2, §12).
    """
    settings = settings or get_settings()
    return LocalEncryptedStorage(
        root=settings.storage_root_resolved,
        master_key=settings.master_key,
    )


__all__ = ["StorageBackend", "StorageObject", "StorageError", "StorageIntegrityError", "get_storage"]
