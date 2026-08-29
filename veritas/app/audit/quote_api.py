"""Client quote flow (architecture §5, §9).

POST /api/v1/audits/{run_id}/quote-request — on a completed audit, the client
asks for a quote. The Quote Agent drafts it autonomously (deterministic §9.1),
routes it to the owner queue (pending_owner), and the client polls
GET /api/v1/quotes/{id}.

HARD GATE §9.1: a client NEVER sees a non-approved quote. The client route
returns an "under owner review" payload (no price, no line items) until the
owner approves it. Only the owner scope (no X-Tenant-Id) sees a draft/pending/
rejected quote in full. Tenant isolation: a client may only act on its own
tenant's run/quote (§10.3) — anything else is a 404.
"""
from __future__ import annotations
import json
import uuid

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..storage import get_storage
from . import repo, report as report_mod, quotes_repo
from .pipeline import artifact_key
from .quote import DeterministicQuoteAgent
from .llm import get_llm
from .report_api import _client_tenant

router = APIRouter(tags=["quotes"])


def _not_found(detail: str = "not found") -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=404)


def _quote_owner_view(quote: dict, actions: list | None = None) -> dict:
    return {"quote": quote, "owner_actions": actions or []}


def _quote_client_unapproved(quote: dict) -> dict:
    """Client-safe payload for a quote that is not yet approved — NO price, NO
    line items (§9.1: no price leak before approval)."""
    return {
        "quote_id": quote["quote_id"],
        "audit_run_id": quote["audit_run_id"],
        "status": quote["status"],
        "client_visible": False,
        "detail": "This audit is under owner review. The quote will appear here "
                  "once the owner approves it.",
    }


def _quote_client_approved(quote: dict) -> dict:
    return {
        "quote_id": quote["quote_id"],
        "audit_run_id": quote["audit_run_id"],
        "status": quote["status"],
        "client_visible": True,
        "currency": quote["currency"],
        "amount_usd": quote["amount_usd"],
        "body": quote["body"],
        "approved_at": quote["client_visible_at"] or quote["decided_at"],
    }


async def _draft_payload(settings, run: dict) -> dict:
    """Run the deterministic Quote Agent over the run's persisted audit inputs
    (report artifact + findings + normalized volume). Fully offline (§9.1)."""
    storage = get_storage(settings)
    raw = storage.get(report_mod.report_artifact_key(run["run_id"]))
    report = json.loads(raw.decode("utf-8")) if raw else {}
    findings = await repo.get_findings(settings, run["run_id"])
    nraw = storage.get(artifact_key(run["run_id"], "normalize"))
    volume = None
    if nraw:
        try:
            view = json.loads(nraw.decode("utf-8"))
            volume = {"rows": view.get("row_count", 0), "files": 1}
        except (UnicodeDecodeError, json.JSONDecodeError):
            volume = None
    llm = get_llm(settings)
    re_audit = await quotes_repo.is_re_audit(
        settings, tenant_id=run["tenant_id"], run_id=run["run_id"],
        eligible_days=0,  # discount is context-configurable; default off at MVP
    )
    agent = DeterministicQuoteAgent(
        model_id=llm.model_id, model_version=llm.model_version
    )
    return await agent.quote(
        run=run, report=report, findings=findings, volume=volume, re_audit=re_audit
    )


@router.post("/audits/{run_id}/quote-request")
async def request_quote(
    run_id: uuid.UUID,
    payload: dict = Body(default={"tenant_id": ""}),
) -> JSONResponse:
    settings = get_settings()
    tenant = str(payload.get("tenant_id", "")).strip()
    try:
        tenant_uuid = str(uuid.UUID(tenant)) if tenant else None
    except ValueError:
        tenant_uuid = None
    if tenant_uuid is None:
        return JSONResponse({"detail": "tenant_id must be a UUID"}, status_code=422)

    run = await repo.get_run(settings, str(run_id))
    if run is None or run["tenant_id"] != tenant_uuid:
        return _not_found("audit run not found")
    if run["status"] != "completed":
        return JSONResponse(
            {"detail": "a quote can only be requested on a completed audit"},
            status_code=409,
        )

    drafted = await _draft_payload(settings, run)
    quote = await quotes_repo.request_quote(
        settings, run_id=run["run_id"], tenant_id=tenant_uuid, payload=drafted
    )
    return JSONResponse(
        {
            "quote_id": quote["quote_id"],
            "audit_run_id": str(run_id),
            "status": quote["status"],
            "client_visible": False,
            "detail": "Quote drafted and routed to the owner for review.",
        },
        status_code=201,
    )


@router.get("/quotes/{quote_id}")
async def get_quote(
    quote_id: uuid.UUID,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> JSONResponse:
    """Client poll: GET /api/v1/quotes/{id}. Owner scope (no X-Tenant-Id) sees
    the full quote + action log; a client sees it only once approved."""
    settings = get_settings()
    quote = await quotes_repo.get_quote(settings, str(quote_id))
    if quote is None:
        return _not_found("quote not found")

    tenant = _client_tenant(x_tenant_id)
    if tenant is not None:
        # Client scope: only this tenant's quote, and only when approved.
        if quote["tenant_id"] != tenant:
            return _not_found("quote not found")
        if quote["status"] == "approved":
            return JSONResponse(_quote_client_approved(quote), status_code=200)
        return JSONResponse(_quote_client_unapproved(quote), status_code=200)

    # Owner scope: full detail + audit log.
    actions = await quotes_repo.list_owner_actions(settings, quote_id=str(quote_id))
    return JSONResponse(_quote_owner_view(quote, actions), status_code=200)
