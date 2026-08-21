"""Stage 4 — Quote Agent (optional stage; INTERFACE + STUB at Phase 0.3).

Architecture §9: the Quote Agent autonomously drafts a client quote from an
audit, and EVERY quote must pass the owner review queue before it is client-
visible (no dollar-threshold exception, §13 Q5). Real quote logic (itemization
from quote_rules.yaml + pricing) lands in Phase 0.5.

This Phase 0.3 module defines the interface (``QuoteAgent.quote``) and a stub
implementation that records a minimal quote step for the audit trail WITHOUT
pricing or owner-queue routing. It is deliberately inert: it never creates a
billable quote, never reaches a real client, and never calls an LLM for money
decisions.
"""
from __future__ import annotations
import abc
from datetime import datetime, timezone

QUOTE_TEMPLATE_ID = "quote-draft-v1"
AGENT = "quote_agent"


class QuoteAgent(abc.ABC):
    """Interface for drafting a quote from an audit run's report."""

    @abc.abstractmethod
    async def quote(self, *, run: dict, report: dict) -> dict:
        """Return a draft-quote payload (idempotent; see stub)."""


class QuoteStub(QuoteAgent):
    """Phase 0.3 stub: records intent only. Produces NO priced line items and
    does NOT route to the owner queue — that behavior arrives in Phase 0.5."""

    def __init__(self, model_id: str = "noop-llm", model_version: str = "0.1.0") -> None:
        self.model_id = model_id
        self.model_version = model_version

    async def quote(self, *, run: dict, report: dict) -> dict:
        return {
            "status": "draft",
            "amount_usd": None,  # never priced by the stub
            "currency": "USD",
            "body": {"itemization": []},
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "note": "Quote Agent stub — real itemization lands in Phase 0.5; "
                    "nothing here is client-visible or owner-queued.",
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_template_id": QUOTE_TEMPLATE_ID,
            "tokens_in": 0,
            "tokens_out": 0,
        }
