"""Audit run API (architecture §5, §4.2).

Best-effort synchronous run at MVP: starting an audit on a stored upload creates
the run + job rows and drives it through the queue, returning the run + steps +
findings. All processing is offline (noop LLM client) and re-runnable via the
audit_jobs ledger — a real background worker that claims jobs with SKIP LOCKED
can be added without changing this surface or the schema.
"""
from __future__ import annotations
import uuid

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..config import get_settings
from . import repo
from .pipeline import process_run
from .rules import get_ruleset, load_all

router = APIRouter(prefix="/uploads", tags=["audit"])


def _run_payload(run: dict, steps: list, findings: list, quote: dict | None = None) -> dict:
    return {"run": run, "steps": steps, "findings": findings, "quote": quote}


async def _trade_fields(settings, run_id: str) -> dict:
    steps = await repo.get_steps(settings, run_id)
    findings = await repo.get_findings(settings, run_id)
    return steps, findings


@router.post("/{upload_id}/audit")
async def start_audit(
    upload_id: uuid.UUID,
    payload: dict = Body(default={"standard": "ISO-27001"}),
) -> JSONResponse:
    settings = get_settings()
    tenant_id = str(payload.get("tenant_id", "")).strip()
    try:
        tenant_uuid = str(uuid.UUID(tenant_id)) if tenant_id else None
    except ValueError:
        tenant_uuid = None
    if tenant_uuid is None:
        return JSONResponse({"detail": "tenant_id must be a UUID"}, status_code=422)

    standard = str(payload.get("standard", "ISO-27001"))
    rule_set = get_ruleset(standard)
    if rule_set is None:
        return JSONResponse(
            {"detail": f"unknown standard {standard!r}; available: "
             f"{sorted(load_all().keys())}"},
            status_code=422,
        )

    upload = await repo.get_upload(settings, str(upload_id))
    if upload is None or upload["status"] != "stored":
        return JSONResponse({"detail": "upload not found or not yet stored"}, status_code=404)

    run_id = await repo.create_run(
        settings, tenant_id=tenant_uuid, upload_id=str(upload_id),
        standard=rule_set.standard, rule_set_version=rule_set.version,
    )
    if run_id is None:
        return JSONResponse(
            {"detail": "an audit run already exists for this upload"}, status_code=409
        )

    run = await process_run(settings, run_id)
    steps, findings = await _trade_fields(settings, run_id)
    return JSONResponse(_run_payload(run, steps, findings), status_code=201)


@router.get("/audit-runs/{run_id}")
async def get_audit_run(run_id: uuid.UUID) -> JSONResponse:
    settings = get_settings()
    run = await repo.get_run(settings, str(run_id))
    if run is None:
        return JSONResponse({"detail": "audit run not found"}, status_code=404)
    steps, findings = await _trade_fields(settings, str(run_id))
    return JSONResponse(_run_payload(run, steps, findings), status_code=200)
