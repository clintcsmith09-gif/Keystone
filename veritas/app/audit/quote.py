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

from . import quote_rules as qr

QUOTE_TEMPLATE_ID = "quote-draft-v1"
AGENT = "quote_agent"


class QuoteAgent(abc.ABC):
    """Interface for drafting a quote from an audit run's report."""

    @abc.abstractmethod
    async def quote(self, *, run: dict, report: dict, **kwargs) -> dict:
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


def derive_quote_inputs(
    *,
    run: dict,
    report: dict,
    findings: list[dict] | None = None,
    volume: dict | None = None,
    re_audit: bool = False,
) -> qr.QuoteInputs:
    """Build the structured §9.1 audit inputs for pricing from the run + report.

    * standard — run.standard (scope);
    * rules_evaluated — count of findings returned (rule-set coverage);
    * data_rows/files — from the normalized-view volume (defaults to the max
      row_count observed in finding evidence when no volume artifact is given);
    * high_findings / failed_findings — severity/status mix from findings.
    Fully deterministic given the same run + report.
    """
    findings = findings or report.get("findings", [])
    standard = run.get("standard") or report.get("standard") or ""
    rows = 0
    if volume is not None:
        rows = int(volume.get("rows", volume.get("row_count", 0)) or 0)
    else:
        for f in findings:
            ev = f.get("evidence") or {}
            rows = max(rows, int(ev.get("row_count", 0) or 0))
    files = int((volume or {}).get("files", 1) or 1)
    high = sum(1 for f in findings if (f.get("severity") or "").lower() == "high")
    failed = sum(1 for f in findings if (f.get("status") or "").lower() == "failed")
    return qr.QuoteInputs(
        standard=standard,
        rules_evaluated=len(findings),
        data_rows=rows,
        data_files=files,
        high_findings=high,
        failed_findings=failed,
        re_audit=re_audit,
    )


class DeterministicQuoteAgent(QuoteAgent):
    """Phase 0.5 Quote Agent — template-based, deterministic, offline (§9.1).

    Drafts a priced quote autonomously from structured audit inputs using the
    pricing rules in ``quote_rules.yaml``. No LLM and no third-party spend:
    money decisions are pure arithmetic over audited, persisted inputs, so two
    requests for the same audit produce the identical quote.

    The agent never routes to the owner or becomes client-visible itself; the
    caller (quote-request endpoint / pipeline) decides status. The returned
    payload carries the priced itemization ready for the ``quotes`` row.
    """

    def __init__(
        self,
        rules: qr.QuoteRules | None = None,
        model_id: str = "noop-llm",
        model_version: str = "0.1.0",
        rules_path=None,
    ) -> None:
        self.rules = rules or qr.load_quote_rules(rules_path)
        self.model_id = model_id
        self.model_version = model_version

    async def quote(
        self,
        *,
        run: dict,
        report: dict,
        findings: list[dict] | None = None,
        volume: dict | None = None,
        re_audit: bool = False,
        **kwargs,
    ) -> dict:
        ins = derive_quote_inputs(
            run=run, report=report, findings=findings, volume=volume, re_audit=re_audit
        )
        pricing = qr.itemize(self.rules, ins)
        return {
            "status": "draft",
            "amount_usd": pricing["total_usd"],
            "currency": self.rules.currency,
            "body": {
                "itemization": pricing["itemization"],
                "subtotal_usd": pricing["subtotal_usd"],
                "discount_usd": pricing["discount_usd"],
                "total_usd": pricing["total_usd"],
                "audit_scope": {
                    "standard": ins.standard,
                    "rules_evaluated": ins.rules_evaluated,
                    "rule_set_version": run.get("rule_set_version"),
                },
                "data_volume": {"rows": ins.data_rows, "files": ins.data_files},
                "severity_mix": {
                    "high": ins.high_findings,
                    "medium": sum(
                        1 for f in (findings or report.get("findings", []))
                        if (f.get("severity") or "").lower() == "medium"
                    ),
                    "low": sum(
                        1 for f in (findings or report.get("findings", []))
                        if (f.get("severity") or "").lower() == "low"
                    ),
                    "info": sum(
                        1 for f in (findings or report.get("findings", []))
                        if (f.get("severity") or "").lower() == "info"
                    ),
                    "failed": ins.failed_findings,
                },
            },
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "description": (
                f"Compliance audit — {ins.standard or 'unknown standard'}, "
                f"{ins.data_rows:,} row(s); drafted by the Quote Agent. "
                "Subject to owner approval."
            ),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_template_id": QUOTE_TEMPLATE_ID,
            "tokens_in": 0,
            "tokens_out": 0,
        }
