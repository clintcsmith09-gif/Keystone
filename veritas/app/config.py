"""Environment-based configuration (never secrets in code).

All settings come from environment variables prefixed with ``VERITAS_`` and are
documented in ``.env.example``. No secret has a default that is usable in
production; the master key is validated by the storage layer on construction.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard cap from architecture §6.2 / §13 Q4: 100 MB per upload at MVP.
DEFAULT_UPLOAD_MAX_BYTES = 100 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VERITAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://localhost/veritas"
    storage_root: Path = Path("./data/objects")
    master_key: str = ""  # validated by the storage backend; empty = storage refuses to start
    environment: str = "dev"
    upload_max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES

    @property
    def storage_root_resolved(self) -> Path:
        return self.storage_root.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Tests can clear the cache / set env first."""
    return Settings()
