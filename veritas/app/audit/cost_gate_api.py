"""Cost-gate owner + telemetry API (architecture §11.3, §13 Q2).

Owner surface mirrors the quote review queue (§9.2): there is no dollar
threshold and no auto-start — a monthly-capped audit waits for an explicit owner
action. The monthly aggregate view (token telemetry across audits) is exposed
here too, so per-stage token measurement is surfaced and sign-off is auditable.
"""
from __future__ import annotations
import uuid

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from ..config import get_settings
from . import cost_gate, cost_gate_repo

owner_router = APIRouter(prefix="/owner", tags=["owner-cost-gate"])
telemetry_router = APIRouter(prefix="/telemetry", tags=["cost-telemetry"])


def _actor(payload: dict) -> str:
    actor = str(payload.get("actor", "owner")).strip()
    return actor or "owner"


@owner_router.get("/cost-gate")
async def owner_cost_gate_summary() -> JSONResponse:
    """Owner dashboard: monthly aggregate spend vs cap (token telemetry), the
    available headroom/warning state, pending cap-queued audits, and unacked
    notifications."""
    settings = get_settings()
    # The owner surface spans tenants at MVP (single tenant), so list globally.
    usage = await cost_gate_repo.monthly_spend(settings, tenant_id="")
    monthly = cost_gate.monthly_decision(
        settings, used_in=usage["tokens_in"], used_out=usage["tokens_out"])
    pending = await cost_gate_repo.list_pending_approvals(settings)
    notifications = await cost_gate_repo.list_notifications(settings)
    per_audit = await cost_gate_repo.monthly_usage_by_audit(settings, tenant_id="")
    return JSONResponse({
        "monthly": {
            "month": usage["month"],
            "tokens_in": usage["tokens_in"],
            "tokens_out": usage["tokens_out"],
            "steps": usage["steps"],
            "budget_in": monthly.budget_in,
            "budget_out": monthly.budget_out,
            "state": monthly.action,
            "reason": monthly.reason,
            "cost_usd": cost_gate.estimated_cost_usd(
                settings, usage["tokens_in"], usage["tokens_out"]),
        },
        "per_audit": per_audit,
        "pending_approvals": pending,
        "notifications": notifications,
    }, status_code=200)


@owner_router.get("/notifications")
async def owner_notifications() -> JSONResponse:
    settings = get_settings()
    n = await cost_gate_repo.list_notifications(settings)
    return JSONResponse({"notifications": n, "count": len(n)}, status_code=200)


@owner_router.post("/notifications/{notification_id}/ack")
async def owner_ack_notification(notification_id: uuid.UUID) -> JSONResponse:
    settings = get_settings()
    ok = await cost_gate_repo.acknowledge_notification(
        settings, str(notification_id))
    if not ok:
        return JSONResponse({"detail": "notification not found or already acked"}, status_code=404)
    return JSONResponse({"acknowledged": str(notification_id)}, status_code=200)


@owner_router.post("/approvals/{run_id}/approve")
async def owner_approve_gated_run(
    run_id: uuid.UUID, payload: dict = Body(default={}),
) -> JSONResponse:
    settings = get_settings()
    run = await cost_gate_repo.approve_cap_queued_run(
        settings, run_id=str(run_id), actor=_actor(payload))
    if run is None:
        return JSONResponse({"detail": "audit run not found"}, status_code=404)
    return JSONResponse(run, status_code=200)


@telemetry_router.get("/monthly")
async def monthly_telemetry(tenant_id: str | None = Query(default=None)) -> JSONResponse:
    """Per-stage token measurement aggregated across audits for a month. Owner
    surface at MVP: when tenant_id is omitted, aggregates across all tenants."""
    settings = get_settings()
    tid = str(tenant_id) if tenant_id else ""
    usage = await cost_gate_repo.monthly_spend(settings, tenant_id=tid)
    per_audit = await cost_gate_repo.monthly_usage_by_audit(settings, tenant_id=tid)
    return JSONResponse({
        "month": usage["month"],
        "tenant_id": tid or None,
        "tokens_in": usage["tokens_in"],
        "tokens_out": usage["tokens_out"],
        "steps": usage["steps"],
        "per_audit": per_audit,
    }, status_code=200)
