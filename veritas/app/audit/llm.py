"""Thin LLM provider seam (architecture §7.3, zero-retention/no-training policy).

The pipeline uses an LLM ONLY for judgment (check_type='judgment' rules and
llm_assist=True rules) — every deterministic data check runs without AI. This
module is the single seam where a real provider plugs in; the ``noop`` provider
returns config-driven scripted responses so the whole pipeline is fully offline
and deterministic in tests, and a real provider (``anthropic``) slots in behind
the SAME ``LLMClient`` interface with no pipeline changes.

§13 Q2 (zero-retention/no-training) — Anthropic position, as documented for ops:
  * Anthropic's standard commercial terms do NOT train on API data by default —
    submitted prompts/completions are not used for model training unless the
    customer separately opts in.
  * Zero Data Retention (ZDR) is an ACCOUNT-LEVEL setting for API customers and
    is NOT toggled per request via an API flag. Ops must enable ZDR on the
    Anthropic account for the API key used here (dashboard / account terms)
    BEFORE running real customer audits on the ``anthropic`` provider; the
    veritas client itself sends no request parameters that defeat retention.
  * We deliberately do NOT enable prompt caching / response streaming / beta
    headers on this client, keeping the request surface minimal and the
    retention posture easy to reason about.
Pinning of model_id/model_version/prompt_template_id in audit_steps plus real
per-call token counts and latency make the audit trail reproducible (§7.4).
"""
from __future__ import annotations
import abc
import time
from dataclasses import dataclass

from ..config import Settings

try:  # pragma: no cover - import cost is a deliberate lazy import (see below)
    import anthropic
    _ANTHROPIC_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - only reachable if dep missing
    anthropic = None  # type: ignore[assignment]
    _ANTHROPIC_IMPORT_ERROR = exc


@dataclass(frozen=True)
class LLMResult:
    """A completed LLM invocation, with measured token counts for §7.4/§11.3."""
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


def _approx_tokens(text: str) -> int:
    """Rough + deterministic token estimate (words) — no external tokenizer
    dependency at MVP. Real providers report exact counters through the same
    interface, and audit_steps records whatever comes back."""
    return len(text.split()) if text else 0


class LLMProviderError(RuntimeError):
    """A provider call failed in a way the pipeline can act on. ``kind`` is one
    of: timeout | connection | rate_limit | server_error | auth | http_<code> |
    unknown. The pipeline's job-queue retry/backoff treats any
    LLMProviderError as retryable, so a transient provider failure never
    produces a hollow report — the stage fails, gets requeued with backoff, and
    only succeeds once the provider responds."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class LLMClient(abc.ABC):
    """Interface every provider implements. Pinned model identity is fixed at
    construction so a run's audit trail can never be silently upgraded (§7.4)."""

    model_id: str
    model_version: str

    @abc.abstractmethod
    async def complete(self, prompt: str, *, template_id: str) -> LLMResult:
        """Run a single completion for the given pinned prompt template."""


class NoopLLMClient(LLMClient):
    """Offline, deterministic stand-in. Returns a config-driven scripted response
    (or an empty one) and measures approximate tokens — enough to exercise the
    pipeline and populate the audit trail without any network or spend."""

    def __init__(
        self,
        model_id: str = "noop-llm",
        model_version: str = "0.1.0",
        response: str = "",
    ) -> None:
        self.model_id = model_id
        self.model_version = model_version
        self._response = response

    async def complete(self, prompt: str, *, template_id: str) -> LLMResult:
        # Record how much input we WOULD have sent (deterministic for cost/token
        # telemetry) and return the scripted response.
        return LLMResult(
            text=self._response,
            tokens_in=_approx_tokens(prompt),
            tokens_out=_approx_tokens(self._response),
        )


class AnthropicLLMClient(LLMClient):
    """Real Claude API client (Anthropic), behind the same seam as noop.

    Zero-retention/no-training (HARD founder requirement — see module docstring):
      * Anthropic does not train on API data by default; ZDR is an ACCOUNT-LEVEL
        setting. OPS MUST ENABLE ZDR ON THE ANTHROPIC ACCOUNT for this API key
        before real customer runs — there is no per-request flag that does this.
      * This client sends no request parameters that weaken retention: no
        ``store``, no ``metadata``, no cache-control, no beta headers.
    """
    PROVIDER = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-5",
        api_version: str = "2023-06-01",
        max_tokens: int = 1024,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        anthropic_client=None,
    ) -> None:
        """``anthropic_client`` is an injection point for tests ONLY: pass an
        ``anthropic.AsyncAnthropic`` built on an httpx.MockTransport so no test
        ever makes a live API call. Ops code never passes it."""
        if not api_key or not api_key.strip():
            raise LLMProviderError(
                "config",
                "ANTHROPIC_API_KEY is not set: real LLM runs need the Anthropic "
                "API key in the environment (export ANTHROPIC_API_KEY=...). "
                "The noop provider remains the default and is unaffected.",
            )
        if anthropic is None:  # pragma: no cover - anthropic is a hard dep
            raise LLMProviderError(
                "config",
                f"anthropic SDK not importable: {_ANTHROPIC_IMPORT_ERROR}. "
                "Install it (pip install 'anthropic>=0.40,<1') to use provider "
                "'anthropic'; the noop provider is unaffected.",
            )
        self.model_id = model
        self.model_version = api_version  # pinned anthropic-version header
        self._max_tokens = max_tokens
        self._client = anthropic_client or anthropic.AsyncAnthropic(
            api_key=api_key,
            auth_token=None,
            max_retries=max_retries,
            timeout=timeout_seconds,
            # No extra headers: ZDR is an account setting, not a request flag.
        )

    @staticmethod
    def _classify(exc: BaseException) -> LLMProviderError:
        """Normalize provider/SDK exceptions into a retryable LLMProviderError.
        The pipeline's job-queue retry/backoff keys off this type — a transient
        error must surface as a failed stage, never as an empty verdict."""
        try:
            import anthropic as _a
        except Exception:  # pragma: no cover
            _a = None

        if _a is not None:
            if isinstance(exc, _a.RateLimitError):
                return LLMProviderError("rate_limit", f"Anthropic rate limit hit: {exc}")
            if isinstance(exc, _a.InternalServerError):
                return LLMProviderError("server_error", f"Anthropic 5xx: {exc}")
            if isinstance(exc, _a.AuthenticationError):
                return LLMProviderError("auth", f"Anthropic authentication failed (check ANTHROPIC_API_KEY): {exc}")
            if isinstance(exc, _a.APITimeoutError):
                return LLMProviderError("timeout", f"Anthropic request timed out: {exc}")
            if isinstance(exc, _a.APIConnectionError):
                return LLMProviderError("connection", f"Anthropic connection error: {exc}")
            if isinstance(exc, _a.APIStatusError):
                return LLMProviderError(f"http_{exc.status_code}", f"Anthropic API error {exc.status_code}: {exc}")

        try:
            import httpx
        except Exception:  # pragma: no cover
            httpx = None
        if httpx is not None and isinstance(exc, httpx.TimeoutException):
            return LLMProviderError("timeout", f"Anthropic request timed out: {exc}")
        if httpx is not None and isinstance(exc, httpx.HTTPError):
            return LLMProviderError("connection", f"Anthropic HTTP error: {exc}")
        return LLMProviderError("unknown", f"Anthropic provider error: {exc}")

    async def complete(self, prompt: str, *, template_id: str) -> LLMResult:
        started = time.perf_counter()
        try:
            # NOTE: template_id is intentionally NOT sent to the provider — it is
            # pinned in the audit trail (audit_steps.prompt_template_id,
            # findings.llm_judgment.prompt_template_id) so the run is
            # reproducible without leaking internal template ids to the vendor.
            resp = await self._client.messages.create(
                model=self.model_id,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            # Any provider failure raises (never returns a hollow verdict); the
            # pipeline's stage retry/backoff handles the retry.
            raise self._classify(exc) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = resp.usage
        # Count ALL input tokens billed/consumed (incl. cache reads/creations if
        # Anthropic ever adds them) so the cost gate never undercounts (§11.3).
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_in += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        tokens_in += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", "") == "text"
        )
        return LLMResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )


_PROVIDER_FACTORIES: dict[str, str] = {"noop": "noop", "anthropic": "anthropic"}


def get_llm(settings: Settings | None = None) -> LLMClient:
    """Factory over the provider seam. VERITAS_LLM_PROVIDER selects the client:
      * 'noop'      (default) — offline, deterministic, never touches the network.
      * 'anthropic' — real Claude API; requires ANTHROPIC_API_KEY (see
        AnthropicLLMClient). This branch is NEVER constructed unless explicitly
        selected, so the missing-key failure can never break the noop path.
    Unknown values raise instead of silently falling back to a network client
    or to noop — a misconfiguration must be loud."""
    from ..config import get_settings

    settings = settings or get_settings()
    if settings.llm_provider not in _PROVIDER_FACTORIES:
        raise ValueError(
            f"llm_provider={settings.llm_provider!r} is not a known provider; "
            "choose 'noop' (offline) or 'anthropic' (real Claude API)."
        )
    if settings.llm_provider == "anthropic":
        return AnthropicLLMClient(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            api_version=settings.anthropic_api_version,
            max_tokens=settings.anthropic_max_tokens,
            timeout_seconds=settings.anthropic_timeout_seconds,
            max_retries=settings.anthropic_max_retries,
        )
    return NoopLLMClient(
        model_id=settings.llm_model_id,
        model_version=settings.llm_model_version,
        response="{}",
    )
