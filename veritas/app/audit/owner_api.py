"""Owner review queue (architecture §9.2) — HARD GATE (§13 Q5).

Owner-only routes under /api/v1/owner. There is NO dollar-threshold exception:
every quote that a client requests goes to this queue, and only an explicit
owner action moves it out of pending_owner. Actions are logged to
owner_actions (who, when, action, before, after).

At MVP there is no full auth layer; these routes are the owner surface, and the
owning identity is supplied as an ``actor`` on each action (the seam a real
token/session replaces in Phase 1 without changing this surface).
"""
from __future__ import annotations
import uuid

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..config import get_settings
from . import quotes_repo

router = APIRouter(prefix="/owner", tags=["owner"])


def _not_found(detail: str = "quote not found") -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=404)


def _actor(payload: dict) -> str:
    actor = str(payload.get("actor", "owner")).strip()
    return actor or "owner"


@router.get("/queue")
async def owner_queue() -> JSONResponse:
    """List every pending_owner quote with its audit summary (§9.2)."""
    settings = get_settings()
    q = await quotes_repo.list_pending(settings)
    return JSONResponse({"pending": q, "count": len(q)}, status_code=200)


@router.post("/queue/{quote_id}/approve")
async def owner_approve(quote_id: uuid.UUID, payload: dict = Body(default={})) -> JSONResponse:
    settings = get_settings()
    quote = await quotes_repo.approve_quote(
        settings, quote_id=str(quote_id), actor=_actor(payload)
    )
    if quote is None:
        return _not_found()
    return JSONResponse(
        {"quote_id": quote["quote_id"], "status": quote["status"],
         "client_visible": True, "detail": "Quote approved and released to the client."},
        status_code=200,
    )


@router.post("/queue/{quote_id}/reject")
async def owner_reject(quote_id: uuid.UUID, payload: dict = Body(default={})) -> JSONResponse:
    settings = get_settings()
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        return JSONResponse(
            {"detail": "reason is required to reject a quote"}, status_code=422
        )
    quote = await quotes_repo.reject_quote(
        settings, quote_id=str(quote_id), actor=_actor(payload), reason=reason
    )
    if quote is None:
        return _not_found()
    return JSONResponse(
        {"quote_id": quote["quote_id"], "status": quote["status"],
         "client_visible": False, "reject_reason": quote["reject_reason"],
         "detail": "Quote rejected. The client may request a revised quote."},
        status_code=200,
    )


@router.post("/queue/{quote_id}/edit")
async def owner_edit(quote_id: uuid.UUID, payload: dict = Body(default={})) -> JSONResponse:
    settings = get_settings()
    amount_usd = payload.get("amount_usd")
    if amount_usd is not None:
        try:
            amount_usd = float(amount_usd)
        except (TypeError, ValueError):
            return JSONResponse(
                {"detail": "amount_usd must be a number"}, status_code=422
            )
        if amount_usd < 0:
            return JSONResponse(
                {"detail": "amount_usd cannot be negative"}, status_code=422
            )
    body = payload.get("body")
    if body is not None and not isinstance(body, dict):
        return JSONResponse({"detail": "body must be an object"}, status_code=422)
    quote = await quotes_repo.edit_quote(
        settings, quote_id=str(quote_id), actor=_actor(payload),
        amount_usd=amount_usd, body=body,
    )
    if quote is None:
        return _not_found()
    return JSONResponse(
        {"quote_id": quote["quote_id"], "status": quote["status"],
         "amount_usd": quote["amount_usd"],
         "detail": "Quote edited and returned to the owner queue for approval."},
        status_code=200,
    )
