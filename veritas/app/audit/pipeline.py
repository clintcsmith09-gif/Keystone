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


def _estimate_cost(rule_set, settings: Settings) -> float:
    """Rough upfront LLM cost estimate for the §11.3 gate (offline, order-of-
    magnitude). Judgment/assist rules incur LLM calls; deterministic rules do
    not, so token cost does not scale with file size."""
    if settings.cost_gate_max_usd <= 0:
        return 0.0
    judgment = sum(1 for r in rule_set.rules if r.check_type == "judgment" or r.llm_assist)
    # crude: ~200 in + 100 out tokens per judgment call, at the MVP placeholder price.
    tokens = judgment * 300
    price_per_token = 0.000003  # §11 placeholder, replaced by real pricing in 0.5
    return tokens * price_per_token


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

    # §11.3 cost gate (disabled at MVP unless a threshold is configured).
    if settings.cost_gate_max_usd > 0:
        est = _estimate_cost(rule_set, settings)
        if est > settings.cost_gate_max_usd:
            await repo.set_run_status(settings, run_id, "cost_gate_halted")
            await repo.mark_run_completed(settings, run_id, status="cost_gate_halted")
            return {"run_id": run_id, "status": "cost_gate_halted", "cost_estimate_usd": est}

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
