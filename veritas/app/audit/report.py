"""Stage 3 — Report Synthesizer (architecture §7.1, §8).
Deterministic aggregation of the stored findings + audit trail into the
standardized deliverable: a **Veritas Compliance Report** (§8 fixed schema).
There is one source of truth — the standardized JSON artifact stored encrypted
via the StorageBackend on completion — and two renderings derived from it
(JSON itself, and a human Markdown view via ``to_markdown``). No drift is
possible because Markdown (and the download endpoint's ?format=md) is always
rendered from the same JSON.

Report assembly is fully deterministic: it reads the already-persisted findings
and never invokes the LLM, so regeneration is idempotent and re-download is
free (no re-run, no third-party spend).
"""
from __future__ import annotations
from datetime import datetime, timezone
from .llm import LLMClient

REPORT_VERSION = "v0.1"
SCHEMA_VERSION = 1
REPORT_TEMPLATE_ID = "report-synth-v1"
AGENT = "report_synthesizer"
REPORT_ARTIFACT_SUFFIX = ".report.json"

# §8 summary buckets must line up with the findings.status CHECK constraint.
_STATUS_KEYS = ("passed", "failed", "needs_review", "info")


def report_artifact_key(run_id: str) -> str:
    """Storage key for the standardized report artifact (LocalEncryptedStorage
    rejects '/', so it is flat — same convention as pipeline.artifact_key)."""
    return f"run-{run_id}-report.json"


def summarize(results: list[dict]) -> dict:
    """§8 summary {passed, failed, needs_review, info} tallied from findings."""
    counts = {"passed": 0, "failed": 0, "needs_review": 0, "info": 0}
    for r in results:
        status = r.get("status")
        if status in counts:
            counts[status] += 1
    return {"total": len(results), **counts}


def derive_data_quality_notes(findings: list[dict]) -> list[dict]:
    """Deterministic, minimal data-quality notes surfaced from the findings'
    evidence (architecture §8 'data_quality_notes'). Scanning evidence catches
    missing columns, empty values and row-count checks without re-parsing."""
    notes: list[dict] = []
    missing_cols: set[str] = set()
    empty_total = 0
    row_checks = 0
    for f in findings:
        ev = f.get("evidence") or {}
        for c in ev.get("missing_columns", []):
            missing_cols.add(str(c))
        if ev.get("empty"):
            empty_total += int(ev["empty"])
        if ev.get("row_count") is not None or ev.get("checked") is not None:
            row_checks += 1
    if missing_cols:
        notes.append({"type": "missing_columns", "columns": sorted(missing_cols)})
    if empty_total:
        notes.append({"type": "empty_values", "count": empty_total})
    if row_checks:
        notes.append({"type": "row_checks_run", "findings_with_row_checks": row_checks})
    return notes


def build_report(
    *,
    run_id: str,
    standard: str,
    rule_set_version: int,
    generated_at: str,
    model_versions: dict,
    results: list[dict],
    data_quality_notes: list[dict] | None = None,
    artifacts: list[dict] | None = None,
) -> dict:
    """Assemble the §8 standardized Veritas Compliance Report from stored
    findings. Pure + deterministic given its inputs (generated_at aside)."""
    summary = summarize(results)
    findings: list[dict] = []
    for r in results:
        finding: dict = {
            "rule_id": r["rule_id"],
            "severity": r["severity"],
            "status": r["status"],
            "evidence": r.get("evidence") or {},
            # §8: llm_judgment is optional — retained as a fixed key (null when
            # no judgment rule ran) so the JSON shape is uniform across findings.
            "llm_judgment": r.get("llm_judgment"),
            "recommendation": r.get("recommendation"),
        }
        findings.append(finding)

    return {
        "report_version": REPORT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "standard": standard,
        "rule_set_version": rule_set_version,
        "generated_at": generated_at,
        "model_versions": model_versions,
        "summary": summary,
        "findings": findings,
        "data_quality_notes": data_quality_notes if data_quality_notes is not None
        else derive_data_quality_notes(results),
        "artifacts": artifacts if artifacts is not None else [],
    }


async def synthesize(
    rule_set,
    results: list[dict],
    llm: LLMClient,
    *,
    run_id: str | None = None,
    generated_at: str | None = None,
    data_quality_notes: list[dict] | None = None,
    artifacts: list[dict] | None = None,
) -> dict:
    """Pipeline entry point: build the standardized report for a rule set +
    findings. ''llm'' is only used to pin model_versions for the audit trail —
    no call is made, the body is fully deterministic and offline."""
    model_versions = {
        AGENT: {"model_id": llm.model_id, "model_version": llm.model_version},
    }
    return build_report(
        run_id=run_id or "",
        standard=rule_set.standard,
        rule_set_version=rule_set.version,
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        model_versions=model_versions,
        results=results,
        data_quality_notes=data_quality_notes,
        artifacts=artifacts,
    )


def to_markdown(report: dict) -> str:
    """Human Markdown rendering of the standardized JSON report. Reads ONLY
    from the report dict, so it can never drift from the JSON artifact."""
    summary = report.get("summary", {})
    lines: list[str] = []
    lines.append(f"# Veritas Compliance Report {report.get('report_version', '')}")
    lines.append("")
    lines.append(f"- **Run ID**: `{report.get('run_id', '')}`")
    lines.append(f"- **Standard**: {report.get('standard', '')} "
                 f"(rule set v{report.get('rule_set_version', '')})")
    lines.append(f"- **Generated**: {report.get('generated_at', '')}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("| --- | ---: |")
    for key in _STATUS_KEYS:
        lines.append(f"| {key} | {summary.get(key, 0)} |")
    lines.append(f"| total | {summary.get('total', 0)} |")
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    lines.append("| Rule | Severity | Status | Recommendation |")
    lines.append("| --- | --- | --- | --- |")
    for f in report.get("findings", []):
        rec = (f.get("recommendation") or "").replace("|", "\\|")
        lines.append(f"| {f.get('rule_id', '')} | {f.get('severity', '')} "
                     f"| {f.get('status', '')} | {rec} |")
    lines.append("")
    if report.get("data_quality_notes"):
        lines.append("## Data Quality Notes")
        lines.append("")
        for note in report["data_quality_notes"]:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(note.items()))
            lines.append(f"- `{note.get('type', '')}` — {detail}")
        lines.append("")
    if report.get("model_versions"):
        lines.append("## Model Versions")
        lines.append("")
        for agent, mv in report["model_versions"].items():
            lines.append(f"- **{agent}**: {mv.get('model_id', '')} "
                         f"(v{mv.get('model_version', '')})")
        lines.append("")
    return "\n".join(lines)
