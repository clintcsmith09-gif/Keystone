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
    # Retention default for uploads (§10.4: 30 days; the nightly purge job lands
    # with Phase 0 operations — the timestamp is set at insert time regardless).
    retention_days: int = 30
    # Parse-safety gate (§6.2): the third gate parses the file in a sandboxed
    # subprocess with hard memory/CPU limits. Limits are per-upload and generous
    # enough for the 100 MB cap; a file that over-runs is quarantined.
    parse_memory_limit_mb: int = 1536
    parse_cpu_seconds: int = 60
    # Safety-parse row/sheet caps: the gate proves the file parses cleanly with a
    # bounded probe — the pipeline's normalize stage does the full deep parse.
    parse_row_cap: int = 100_000
    # --- audit job queue (§7.2) -----------------------------------------------
    # Crash-safe retry: a leased job is reclaimable after lease_timeout even if
    # the worker died mid-stage; retries back off exponentially (2^n * base).
    job_lease_timeout_seconds: int = 60
    job_max_attempts: int = 3
    job_retry_base_seconds: int = 1
    # --- LLM provider seam (§7.3): thin interface, NOT wired to a provider. ----
    # MVP is fully offline: provider 'noop' returns config-driven scripted
    # responses so every stage is deterministically testable. Real provider
    # hookup is Phase 1 and stays behind this seam. No third-party spend now.
    llm_provider: str = "noop"
    llm_model_id: str = "noop-llm"
    llm_model_version: str = "0.1.0"
    # §11.3 pre-run cost gate: halt the run when the upfront LLM cost estimate
    # would exceed this threshold (USD). 0 = gate disabled at MVP.
    cost_gate_max_usd: float = 0.0

    @property
    def storage_root_resolved(self) -> Path:
        return self.storage_root.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Tests can clear the cache / set env first."""
    return Settings()
