"""Pipeline orchestrator (architecture §7.1, §4.2).

Runs the 4 sequential, re-runnable stages for one audit run against the
Postgres-backed job queue:
    normalize → match → report → (quote stub)
Each stage is a plain async Python function that (1) does deterministic data
work, (2) calls the LLM only via the thin LLMClient seam for judgment, and
(3) records a row in audit_steps with pinned model/prompt/versions + token
telemetry (§7.4). Stages are idempotent: re-running process_run skips already-
succeeded jobs, and findings/artifacts are replaced rather than appended, so a
run can be re-executed after a failure or crash.
"""
from __future__ import annotations
import json

from ..config import Settings
from ..storage import get_storage
from . import normalize as normalize_mod
from . import repo, report as report_mod, matcher as matcher_mod
from . import quote as quote_mod
from . import cost_gate, cost_gate_repo
from .llm import get_llm
from .repo import STAGE_TO_RUN_STATUS
from .rules import get_ruleset

AGENTS = {
    "normalize": normalize_mod.AGENT,
    "match": matcher_mod.AGENT,
    "report": report_mod.AGENT,
    "quote": quote_mod.AGENT,
}
TEMPLATES = {
    "normalize": normalize_mod.NORMALIZE_TEMPLATE_ID,
    "match": matcher_mod.MATCH_TEMPLATE_ID,
    "report": report_mod.REPORT_TEMPLATE_ID,
    "quote": quote_mod.QUOTE_TEMPLATE_ID,
}
STAGES = list(AGENTS)


def artifact_key(run_id: str, stage: str) -> str:
    # flat, filesystem-safe storage key (LocalEncryptedStorage rejects '/')
    return f"run-{run_id}-{stage}.json"


# --- individual stage bodies ---------------------------------------------------
async def _run_normalize(settings: Settings, storage, run: dict, llm) -> tuple:
    upload = await repo.get_upload(settings, run["upload_id"])
    if upload is None:
        raise RuntimeError("upload no longer available")
    kind = normalize_mod.kind_from_path(upload["filename"])
    raw = storage.get(upload["storage_key"])
    view = normalize_mod.normalize(raw, kind=kind, source=upload["filename"])
    out_ref = artifact_key(run["run_id"], "normalize")
    storage.put(out_ref, json.dumps(view).encode("utf-8"), content_type="application/json")
    return out_ref, 0, 0


async def _run_match(settings: Settings, storage, run: dict, llm, rule_set) -> tuple:
    view = json.loads(storage.get(artifact_key(run["run_id"], "normalize")) or b"{}")
    results = await matcher_mod.match_view(rule_set, view, llm)
    await repo.replace_findings(
        settings,
        run_id=run["run_id"],
        tenant_id=run["tenant_id"],
        standard=run["standard"],
        standard_version=run["rule_set_version"],
        results=results,
    )
    out_ref = artifact_key(run["run_id"], "match")
    storage.put(out_ref, json.dumps(results).encode("utf-8"), content_type="application/json")
    tin = sum((r.get("llm_judgment") or {}).get("tokens_in", 0) for r in results)
    tout = sum((r.get("llm_judgment") or {}).get("tokens_out", 0) for r in results)
    return out_ref, tin, tout


async def _run_report(settings: Settings, storage, run: dict, llm, rule_set) -> tuple:
    # §8 standardized report, derived deterministically from the stored findings
    # + audit trail (idempotent; the LLM seam is used only to pin model versions
    # — no call is made). Stored encrypted via the StorageBackend for free
    # re-download, and referenced by the audit_steps output_artifact_ref.
    findings = await repo.get_findings(settings, run["run_id"])
    report = await report_mod.synthesize(
        rule_set, findings, llm, run_id=run["run_id"],
        artifacts=[{
            "name": "report",
            "ref": report_mod.report_artifact_key(run["run_id"]),
            "content_type": "application/json",
            "format": f"veritas-report-{report_mod.REPORT_VERSION}",
        }],
    )
    out_ref = report_mod.report_artifact_key(run["run_id"])
    storage.put(out_ref, json.dumps(report).encode("utf-8"), content_type="application/json")
    # Report assembly is deterministic (no LLM); surface token telemetry already
    # recorded against the judgment findings that fed this report.
    tin = sum((f.get("llm_judgment") or {}).get("tokens_in", 0) for f in findings)
    tout = sum((f.get("llm_judgment") or {}).get("tokens_out", 0) for f in findings)
    return out_ref, tin, tout


async def _run_quote(settings: Settings, storage, run: dict, llm) -> tuple:
    # Phase 0.5: Quote Agent drafts a priced quote deterministically (§9.1, no
    # LLM). Status stays 'draft' — it becomes client-visible only after the
    # owner explicitly approves it in the review queue (§9.1 hard gate). The
    # pipeline merely records the draft; the client quote-request endpoint
    # routes it to pending_owner.
    agent = quote_mod.DeterministicQuoteAgent(
        model_id=llm.model_id, model_version=llm.model_version
    )
    report = json.loads(storage.get(artifact_key(run["run_id"], "report")) or b"{}")
    findings = await repo.get_findings(settings, run["run_id"])
    view = json.loads(storage.get(artifact_key(run["run_id"], "normalize")) or b"{}")
    volume = {"rows": view.get("row_count", 0), "files": 1}
    payload = await agent.quote(
        run=run, report=report, findings=findings, volume=volume
    )
    await repo.insert_quote_stub(
        settings, run_id=run["run_id"], tenant_id=run["tenant_id"], payload=payload
    )
    out_ref = artifact_key(run["run_id"], "quote")
    storage.put(out_ref, json.dumps(payload).encode("utf-8"), content_type="application/json")
    return out_ref, payload.get("tokens_in", 0), payload.get("tokens_out", 0)


_STAGE_BODIES = {
    "normalize": lambda s, storage, run, llm, rs: _run_normalize(s, storage, run, llm),
    "match": _run_match,
    "report": _run_report,
    "quote": lambda s, storage, run, llm, rs: _run_quote(s, storage, run, llm),
}


async def _record_gate(
    settings: Settings, *, run: dict, kind: str, decision: cost_gate.Decision,
    notify_kind: str | None = None, notify_message: str | None = None,
) -> None:
    await cost_gate_repo.record_gate_event(
        settings, tenant_id=run["tenant_id"], run_id=run["run_id"], kind=kind,
        decision=decision.action, tokens_in=decision.tokens_in,
        tokens_out=decision.tokens_out, budget_in=decision.budget_in,
        budget_out=decision.budget_out, detail=decision.reason,
    )
    if notify_kind:
        await cost_gate_repo.add_notification(
            settings, tenant_id=run["tenant_id"], kind=notify_kind,
            message=notify_message or decision.reason, run_id=run["run_id"],
        )


async def _gate_pre_run(settings: Settings, run: dict, rule_set) -> str | None:
    """Phase 0.6 cost gate — pre-run. Returns a terminal status for the run when
    it must NOT start ('cost_gate_halted' | 'awaiting_owner_approval'), else None
    to allow the run to proceed."""
    upload = await repo.get_upload(settings, run["upload_id"]) or {}
    file_size = int(upload.get("size_bytes") or 0)

    # §11.3a — deterministic upper-bound estimate vs per-audit budget.
    decision = cost_gate.pre_run_decision(settings, rule_set, file_size)
    if decision.action == "halt":
        await repo.halt_for_cost_gate(settings, run["run_id"], reason=decision.reason)
        await _record_gate(settings, run=run, kind="pre_run", decision=decision,
                           notify_kind="cost_gate_halted")
        return "cost_gate_halted"

    # §11.3c — monthly aggregate cap.
    usage = await cost_gate_repo.monthly_spend(
        settings, tenant_id=run["tenant_id"])
    monthly = cost_gate.monthly_decision(
        settings, used_in=usage["tokens_in"], used_out=usage["tokens_out"])
    if monthly.action == "block":
        # A cap-queued run only proceeds once the owner grants it (approved_via_gate).
        if not run.get("approved_via_gate"):
            await repo.queue_for_owner_approval(
                settings, run["run_id"], reason=monthly.reason)
            await _record_gate(settings, run=run, kind="monthly", decision=monthly,
                               notify_kind="monthly_cap")
            return "awaiting_owner_approval"
        # Consume the one-time owner grant so a future re-run is gated again.
        await repo.consume_gate_approval(settings, run["run_id"])
    elif monthly.action == "warn":
        # Owner is warned once at >=80% of the monthly budget (§11.3c).
        await _record_gate(settings, run=run, kind="monthly", decision=monthly,
                           notify_kind="monthly_warn")
    return None


async def process_run(settings: Settings, run_id: str, *, worker_id: str = "worker-1",
                      rules_dir=None) -> dict:
    """Drive one audit run end-to-end through the staged job queue."""
    run = await repo.get_run(settings, run_id)
    if run is None:
        return {"run_id": run_id, "status": "not_found"}

    rule_set = get_ruleset(run["standard"], rules_dir)
    if rule_set is None:
        await repo.set_run_status(settings, run_id, "failed")
        await repo.mark_run_completed(settings, run_id, status="failed")
        return {"run_id": run_id, "status": "failed",
                "error": f"no rule set for standard {run['standard']}"}

    # §11.3 cost gate — pre-run: upper-bound estimate vs per-audit budget, plus
    # the monthly aggregate cap. Returns None to proceed, else a terminal status
    # (cost_gate_halted / awaiting_owner_approval) with artifacts preserved.
    gate_status = await _gate_pre_run(settings, run, rule_set)
    if gate_status is not None:
        return await repo.get_run(settings, run_id)

    await repo.mark_run_started(settings, run_id)
    storage = get_storage(settings)
    llm = get_llm(settings)
    run_tokens_in = run_tokens_out = 0

    for stage in STAGES:
        job = await repo.get_job(settings, run_id, stage)
        if job is None:
            continue
        if job["status"] == "succeeded":
            continue  # re-runnable: already completed
        if job["status"] == "failed":
            await repo.mark_run_completed(settings, run_id, status="failed")
            return {"run_id": run_id, "status": "failed",
                    "error": f"stage {stage} exhausted retries"}

        claimed = await repo.claim_job(settings, run_id, stage, worker_id)
        if claimed is None:
            # Another worker holds the lease for this stage — stop; the run is
            # in progress elsewhere (SKIP LOCKED did its job).
            return await repo.get_run(settings, run_id)

        await repo.set_run_status(settings, run_id, STAGE_TO_RUN_STATUS[stage])
        # normalize runs after upload, and its input is the upload artifact.
        if stage == "normalize":
            input_ref = None
        else:
            input_ref = artifact_key(run_id, STAGES[STAGES.index(stage) - 1])
        step_id = await repo.step_begin(
            settings, run_id=run_id, tenant_id=run["tenant_id"], stage=stage,
            agent=AGENTS[stage], model_id=llm.model_id,
            model_version=llm.model_version, prompt_template_id=TEMPLATES[stage],
            input_artifact_ref=input_ref,
        )
        try:
            body = _STAGE_BODIES[stage]
            out_ref, tin, tout = await body(settings, storage, run, llm, rule_set)
            run_tokens_in += tin
            run_tokens_out += tout
            await repo.step_end(settings, step_id, status="succeeded",
                                output_artifact_ref=out_ref, tokens_in=tin, tokens_out=tout)
            await repo.complete_job(settings, claimed["job_id"])

            # §11.3b — mid-flight abort: if the run's measured tokens already
            # exceed the per-audit budget, halt at this stage boundary. Completed
            # stages' artifacts are preserved (idempotent re-run resumes later).
            decision = cost_gate.mid_flight_decision(
                settings, run_tokens_in, run_tokens_out)
            if decision.action == "halt":
                await repo.halt_for_cost_gate(
                    settings, run_id, reason=decision.reason,
                    tokens_in=run_tokens_in, tokens_out=run_tokens_out)
                await _record_gate(settings, run=run, kind="mid_flight",
                                   decision=decision, notify_kind="cost_gate_halted")
                return await repo.get_run(settings, run_id)
        except Exception as exc:  # noqa: BLE001 — any stage failure → retry/backoff
            msg = str(exc) or exc.__class__.__name__
            await repo.step_end(settings, step_id, status="failed", error=msg)
            state = await repo.fail_job(settings, claimed["job_id"],
                                        attempts=claimed["attempts"], error=msg)
            if state["status"] == "failed":
                await repo.mark_run_completed(settings, run_id, status="failed")
                return {"run_id": run_id, "status": "failed", "error": msg}
            # requeued for backoff retry — leave run status as-is (retryable)
            return {"run_id": run_id, "status": "retry_scheduled", "error": msg,
                    "retry_attempts": claimed["attempts"]}

    await repo.set_run_status(settings, run_id, "completed",
                              tokens_in=run_tokens_in, tokens_out=run_tokens_out)
    await repo.mark_run_completed(settings, run_id, status="completed")
    return await repo.get_run(settings, run_id)
