"""Authenticated report/trail download endpoints (architecture §8, §5, §10.3).
Exposes the standardized Veritas Compliance Report (JSON or Markdown) and the
audit trail for a completed run.

Tenant isolation (§10.3): a client request that presents an ``X-Tenant-Id``
header is only allowed to see its own tenant's audits (anything else → 404 so
the existence of other tenants' runs is never disclosed). An owner/auditor
request without ``X-Tenant-Id`` may read any audit. At MVP there is no full
auth layer; the header is the tenant-scoping seam that a real token/JWT
replaces in Phase 1 without changing this surface.

Re-download is free: the report is read from the encrypted artifact the report
stage stored on completion (storage.get) — never regenerated, never re-run.
"""
from __future__ import annotations
import json
import uuid
from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from ..config import get_settings
from ..storage import get_storage
from . import repo
from . import report as report_mod
from .report import to_markdown

router = APIRouter(prefix="/audits", tags=["audit-reports"])

TENANT_HEADER = "X-Tenant-Id"  # client tenant scope (Phase 1 → real token)


def _client_tenant(x_tenant_id: str | None) -> str | None:
    """Resolve the requesting actor. Returns a tenant id (client scope) or None
    (owner scope, can see all). Invalid UUIDs are treated as owner-none."""
    if x_tenant_id is None:
        return None
    tid = x_tenant_id.strip()
    try:
        return str(uuid.UUID(tid))
    except ValueError:
        return None


def _allowed(run: dict, tenant: str | None) -> bool:
    """Owner scope (tenant is None) sees everything; a client scope must match."""
    if tenant is None:
        return True
    return run["tenant_id"] == tenant


async def _fetch_report_json(run_id: str) -> dict | None:
    settings = get_settings()
    storage = get_storage(settings)
    raw = storage.get(report_mod.report_artifact_key(run_id))
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _not_found(detail: str = "audit run not found") -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=404)


@router.get("/{run_id}/report")
async def get_report(
    run_id: uuid.UUID,
    format: str = Query("json", pattern="^(json|md)$"),
    x_tenant_id: str | None = Header(default=None, alias=TENANT_HEADER),
) -> Response:
    settings = get_settings()
    run = await repo.get_run(settings, str(run_id))
    if run is None or not _allowed(run, _client_tenant(x_tenant_id)):
        return _not_found()
    report = await _fetch_report_json(str(run_id))
    if report is None:
        return _not_found("report artifact not available for this run")
    if format == "md":
        return PlainTextResponse(
            to_markdown(report),
            media_type="text/markdown",
            headers={"X-Content-Type-Options": "nosniff"},
        )
    return JSONResponse(report, status_code=200)


@router.get("/{run_id}/trail")
async def get_trail(
    run_id: uuid.UUID,
    x_tenant_id: str | None = Header(default=None, alias=TENANT_HEADER),
) -> Response:
    """Audit trail for a run: run + steps + quote (consistent with the start/
    get-audit-run surface, §7.4). Same tenant isolation as the report."""
    settings = get_settings()
    run = await repo.get_run(settings, str(run_id))
    if run is None or not _allowed(run, _client_tenant(x_tenant_id)):
        return _not_found()
    steps = await repo.get_steps(settings, str(run_id))
    findings = await repo.get_findings(settings, str(run_id))
    return JSONResponse(
        {"run": run, "steps": steps, "findings": findings}, status_code=200
    )
