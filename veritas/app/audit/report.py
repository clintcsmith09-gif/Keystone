"""Stage 3 — Report Synthesizer (architecture §7.1, §8).

Deterministic aggregation of the matched findings into a standardized report
(§8 schema): summary counts, breakdown by severity, and the per-rule result list.
Optionally calls the LLM seam for an executive summary (template report-synth-v1)
so the audit trail records model pinning + token telemetry — but the report body
itself is fully deterministic and does not depend on the LLM response.
"""
from __future__ import annotations
from datetime import datetime, timezone

from .llm import LLMClient

REPORT_TEMPLATE_ID = "report-synth-v1"
AGENT = "report_synthesizer"
REPORT_ARTIFACT_SUFFIX = ".report.json"


def summarize(results: list[dict]) -> dict:
    total = len(results)
    counts = {"passed": 0, "failed": 0, "needs_review": 0, "info": 0}
    by_severity: dict[str, dict] = {}
    for r in results:
        status = r["status"]
        sev = r["severity"]
        counts[status] = counts.get(status, 0) + 1
        bucket = by_severity.setdefault(sev, {"passed": 0, "failed": 0, "needs_review": 0, "total": 0})
        bucket[status] = bucket.get(status, 0) + 1
        bucket["total"] += 1
    return {"total": total, **counts, "by_severity": by_severity}


async def synthesize(rule_set, results: list[dict], llm: LLMClient) -> dict:
    summary = summarize(results)
    report = {
        "standard": rule_set.standard,
        "standard_version": rule_set.version,
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "findings": [
            {
                "rule_id": r["rule_id"],
                "category": r.get("category", "uncategorized"),
                "severity": r["severity"],
                "status": r["status"],
                "recommendation": r.get("recommendation"),
            }
            for r in results
        ],
    }
    # LLM-assisted executive summary (offline at MVP → tokens telemetry only).
    prompt = (
        f"Summarize compliance findings for {rule_set.standard} v{rule_set.version}: "
        f"{summary}. Return a short executive summary JSON."
    )
    llm_out = await llm.complete(prompt, template_id=REPORT_TEMPLATE_ID)
    report["executive_summary"] = {
        "model_id": llm.model_id,
        "model_version": llm.model_version,
        "prompt_template_id": REPORT_TEMPLATE_ID,
        "tokens_in": llm_out.tokens_in,
        "tokens_out": llm_out.tokens_out,
    }
    report["meta"] = {
        "model_id": llm.model_id,
        "model_version": llm.model_version,
        "prompt_template_id": REPORT_TEMPLATE_ID,
    }
    return report
