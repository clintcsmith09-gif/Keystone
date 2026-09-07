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

    # --- Phase 0.6 cost gate (§11.3) — token-budget by default ----------------
    # The gate computes deterministically on token budgets. There is no real
    # provider wired at MVP, so it never spends money and never calls an LLM to
    # decide. When a provider + its price are configured (Phase 1), the same
    # machinery extends to dollars (see cost_gate_price_per_million_*).
    cost_gate_enabled: bool = True
    # Per-audit hard token budgets (§11.3 defaults: 250k in / 75k out).
    cost_gate_max_tokens_in: int = 250_000
    cost_gate_max_tokens_out: int = 75_000
    # --- deterministic pre-run upper-bound estimate knobs (§11.3) -------------
    # est_in  = max(1,row_est)*tokens_per_row
    #         + file_size_bytes*tokens_per_byte
    #         + judgment_rule_count*tokens_per_judgment_rule
    # est_out = judgment_rule_count*tokens_out_per_judgment_rule
    # both scaled by cost_gate_estimate_safety_factor. row_est is a deterministic
    # upper bound on rows derived from file size and bytes_per_row (capped).
    cost_gate_estimate_tokens_per_byte: float = 0.5
    cost_gate_estimate_tokens_per_row: int = 40
    cost_gate_estimate_tokens_per_judgment_rule: int = 300
    cost_gate_estimate_tokens_out_per_judgment_rule: int = 100
    cost_gate_estimate_bytes_per_row: int = 200
    cost_gate_estimate_row_cap: int = 1_000_000
    cost_gate_estimate_safety_factor: float = 1.25
    # --- monthly aggregate cap (§11.3): default 20% of monthly revenue ---------
    # Monthly revenue is not a live number at MVP (default 0 = no revenue model),
    # so the operational default is a monthly token budget. When BOTH
    # monthly_revenue_usd > 0 AND a provider price is configured, the token caps
    # are derived from ratio of monthly revenue instead (the dollar extension).
    monthly_revenue_usd: float = 0.0
    cost_gate_monthly_cap_ratio: float = 0.20
    cost_gate_monthly_warn_ratio: float = 0.80
    cost_gate_monthly_tokens_in: int = 2_000_000
    cost_gate_monthly_tokens_out: int = 600_000
    # Provider price per 1M tokens (0 = not configured; gate stays token-budget).
    cost_gate_price_per_million_in: float = 0.0
    cost_gate_price_per_million_out: float = 0.0

    @property
    def storage_root_resolved(self) -> Path:
        return self.storage_root.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Tests can clear the cache / set env first."""
    return Settings()
