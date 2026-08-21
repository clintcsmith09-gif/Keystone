"""Thin LLM provider seam (architecture §7.3, zero-retention/no-training policy).

The pipeline uses an LLM ONLY for judgment (check_type='judgment' rules and
llm_assist=True rules) — every deterministic data check runs without AI. This
module is the single seam where a real provider plugs in (Phase 1); at MVP the
``noop`` provider returns config-driven scripted responses so the whole pipeline
is fully offline and deterministic in tests. No provider is ever called, no
third-party spend, and no customer data leaves the service.

§13 Q2 (zero-retention/no-training): any real provider added later MUST be one
that guarantees no-training / zero-retention of submitted payloads; the pinning
of model_id/model_version/prompt_template_id in audit_steps is what makes the
audit trail reproducible.
"""
from __future__ import annotations
import abc
from dataclasses import dataclass

from ..config import Settings


@dataclass(frozen=True)
class LLMResult:
    """A completed LLM invocation, with measured token counts for §7.4/§11.3."""
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


def _approx_tokens(text: str) -> int:
    """Rough + deterministic token estimate (words) — no external tokenizer
    dependency at MVP. Real providers report exact counters through the same
    interface, and audit_steps records whatever comes back."""
    return len(text.split()) if text else 0


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


def get_llm(settings: Settings | None = None) -> LLMClient:
    """Factory over the provider seam. MVP only knows 'noop'; a real provider is
    added here (config-guarded) in Phase 1."""
    from ..config import get_settings

    settings = settings or get_settings()
    # Guard: unknown provider must never silently fall back to a real network
    # call. MVP hard-wires noop until Phase 1 wires the guarded provider map.
    if settings.llm_provider != "noop":
        raise ValueError(
            f"llm_provider={settings.llm_provider!r} is not configured at MVP; "
            "only 'noop' (offline) is available until Phase 1."
        )
    return NoopLLMClient(
        model_id=settings.llm_model_id,
        model_version=settings.llm_model_version,
        response="{}",
    )
