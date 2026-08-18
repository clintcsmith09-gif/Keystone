"""StorageBackend interface (architecture §6.4).

The pipeline stores and reads artifacts (uploads, normalized manifests, findings,
reports) only through this interface, so the MVP local encrypted-disk backend can be
swapped for an S3-compatible managed tier later with a config change, not a rewrite.

The interface is deliberately synchronous: the MVP does not need to hold the event
loop open for object IO, and FastAPI runs sync endpoints in a threadpool.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


class StorageError(Exception):
    """Base error for storage backends."""


class StorageIntegrityError(StorageError):
    """Raised when stored data fails integrity verification (e.g. GCM tag check)."""


@dataclass(frozen=True)
class StorageObject:
    """Metadata for a stored object; callers fetch bytes separately via get()."""

    key: str
    content_type: str
    size_bytes: int


class StorageBackend(abc.ABC):
    @abc.abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> StorageObject:
        """Store ``data`` under ``key`` (opaque, backend-validated). Returns metadata."""

    @abc.abstractmethod
    def get(self, key: str) -> bytes:
        """Fetch raw bytes; raises StorageIntegrityError if integrity fails, KeyError if missing."""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """True if an object with this key exists."""

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Hard-delete the object (overwrite + unlink in the encrypted-disk backend)."""
