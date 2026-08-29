"""Database operations for the quote flow + owner review queue (architecture §9).

Covers the quotes state machine — draft → pending_owner → approved | rejected
(edit returns to pending_owner) — plus the append-only owner_actions audit log
(who, when, action, before, after) and the owner queue listing. No client ever
reads a non-approved quote through the client surface (the API layer enforces
that; this module only persists state). Tenant isolation is enforced by the
caller: client-requested ops pass an explicit tenant, owner ops pass none and
therefore see every tenant.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import psycopg

from ..config import Settings
from .repo import _connect, _as_json

_QUOTE_COLS = (
    "id, tenant_id, audit_run_id, status, amount_usd, currency, body, "
    "requested_at, drafted_at, decided_at, reject_reason, client_visible_at, created_at"
)


def _row_to_quote(row) -> dict:
    return {
        "quote_id": str(row[0]),
        "tenant_id": str(row[1]),
        "audit_run_id": str(row[2]),
        "status": row[3],
        "amount_usd": float(row[4]) if row[4] is not None else None,
        "currency": row[5],
        "body": _as_json(row[6]),
        "requested_at": row[7].isoformat() if row[7] else None,
        "drafted_at": row[8].isoformat() if row[8] else None,
        "decided_at": row[9].isoformat() if row[9] else None,
        "reject_reason": row[10],
        "client_visible_at": row[11].isoformat() if row[11] else None,
        "created_at": row[12].isoformat() if row[12] else None,
    }


def _snapshot(quote: dict) -> dict:
    """JSON-serializable snapshot for owner_actions.before/after."""
    return {
        "quote_id": quote["quote_id"],
        "status": quote["status"],
        "amount_usd": quote["amount_usd"],
        "currency": quote["currency"],
        "body": quote["body"],
    }


async def get_quote(settings: Settings, quote_id: str) -> dict | None:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            f"SELECT {_QUOTE_COLS} FROM quotes WHERE id = %s", (quote_id,)
        )
        rec = await row.fetchone()
    return _row_to_quote(rec) if rec else None


async def get_quote_by_run(settings: Settings, run_id: str) -> dict | None:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            f"SELECT {_QUOTE_COLS} FROM quotes WHERE audit_run_id = %s", (run_id,)
        )
        rec = await row.fetchone()
    return _row_to_quote(rec) if rec else None


async def request_quote(
    settings: Settings,
    *,
    run_id: str,
    tenant_id: str,
    payload: dict,
) -> dict:
    """Draft → pending_owner (§9.1 step 3). Upserts the run's single quote with
    the Quote Agent's priced payload and routes it to the owner queue. Idempotent:
    a second request recomputes the same deterministic draft and re-queues it.
    Returns the updated quote row."""
    now = datetime.now(timezone.utc)
    amount = payload.get("amount_usd")
    body = json.dumps(payload.get("body", {}))
    currency = payload.get("currency", "USD")
    async with await _connect(settings) as conn:
        existing = await conn.execute(
            "SELECT id FROM quotes WHERE audit_run_id = %s", (run_id,)
        )
        rec = await existing.fetchone()
        if rec is not None:
            await conn.execute(
                """
                UPDATE quotes
                   SET amount_usd = %s, currency = %s, body = %s,
                       status = 'pending_owner',
                       requested_at = %s, drafted_at = %s,
                       decided_at = NULL, reject_reason = NULL, client_visible_at = NULL
                 WHERE id = %s
                """,
                (amount, currency, body, now, now, rec[0]),
            )
            quote_id = str(rec[0])
        else:
            row = await conn.execute(
                """
                INSERT INTO quotes
                    (tenant_id, audit_run_id, status, amount_usd, currency,
                     body, requested_at, drafted_at)
                VALUES (%s, %s, 'pending_owner', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (tenant_id, run_id, amount, currency, body, now, now),
            )
            quote_id = str((await row.fetchone())[0])
    return await get_quote(settings, quote_id)


async def _write_owner_action(
    conn, *, tenant_id: str, actor: str, action: str, target_id: str,
    before: dict, after: dict,
) -> None:
    await conn.execute(
        """
        INSERT INTO owner_actions (tenant_id, actor, action, target_type, target_id, before, after)
        VALUES (%s, %s, %s, 'quote', %s, %s, %s)
        """,
        (tenant_id, actor, action, target_id,
         json.dumps(before), json.dumps(after)),
    )


async def approve_quote(settings: Settings, *, quote_id: str, actor: str) -> dict | None:
    """STATE MACHINE: pending_owner (or draft) → approved. Sets decided_at +
    client_visible_at and snapshots before/after to owner_actions (quote_approve).
    Only an approve marks the quote client-visible (§9.1)."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            f"SELECT {_QUOTE_COLS} FROM quotes WHERE id = %s FOR UPDATE", (quote_id,)
        )
        rec = await row.fetchone()
        if rec is None:
            return None
        before = _row_to_quote(rec)
        if before["status"] == "approved":
            return before  # idempotent; already approved
        now = datetime.now(timezone.utc)
        await conn.execute(
            """
            UPDATE quotes
               SET status = 'approved', decided_at = now(), client_visible_at = now()
             WHERE id = %s
            """,
            (quote_id,),
        )
        # after is built locally — a separate-connection read inside this
        # transaction cannot observe the uncommitted UPDATE.
        after = dict(before)
        after["status"] = "approved"
        after["decided_at"] = now.isoformat()
        after["client_visible_at"] = now.isoformat()
        await _write_owner_action(
            conn, tenant_id=before["tenant_id"], actor=actor, action="quote_approve",
            target_id=quote_id, before=_snapshot(before), after=_snapshot(after),
        )
    return after


async def reject_quote(
    settings: Settings, *, quote_id: str, actor: str, reason: str
) -> dict | None:
    """STATE MACHINE: pending_owner → rejected. Reason is required (CHECK
    constraint); decided_at set; never client-visible. Logs quote_reject."""
    reason = (reason or "").strip()
    async with await _connect(settings) as conn:
        row = await conn.execute(
            f"SELECT {_QUOTE_COLS} FROM quotes WHERE id = %s FOR UPDATE", (quote_id,)
        )
        rec = await row.fetchone()
        if rec is None:
            return None
        before = _row_to_quote(rec)
        if before["status"] == "rejected":
            return before  # idempotent; already rejected
        now = datetime.now(timezone.utc)
        await conn.execute(
            """
            UPDATE quotes
               SET status = 'rejected', decided_at = now(), reject_reason = %s,
                   client_visible_at = NULL
             WHERE id = %s
            """,
            (reason, quote_id),
        )
        after = dict(before)
        after["status"] = "rejected"
        after["decided_at"] = now.isoformat()
        after["reject_reason"] = reason
        after["client_visible_at"] = None
        await _write_owner_action(
            conn, tenant_id=before["tenant_id"], actor=actor, action="quote_reject",
            target_id=quote_id, before=_snapshot(before), after=_snapshot(after),
        )
    return after


async def edit_quote(
    settings: Settings,
    *,
    quote_id: str,
    actor: str,
    amount_usd: float | None,
    body: dict | None = None,
) -> dict | None:
    """STATE MACHINE: approved | rejected | pending_owner → pending_owner.
    Owner edits amount and/or body; the edited quote returns to the queue for
    final approval (§9.1: edit returns to pending). Logs quote_edit."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            f"SELECT {_QUOTE_COLS} FROM quotes WHERE id = %s FOR UPDATE", (quote_id,)
        )
        rec = await row.fetchone()
        if rec is None:
            return None
        before = _row_to_quote(rec)
        new_amount = before["amount_usd"] if amount_usd is None else float(amount_usd)
        new_body = before["body"] if body is None else dict(body)
        # Keep the body's total consistent with the edited amount.
        if body is None or "total_usd" not in new_body:
            nb = dict(new_body)
            nb["total_usd"] = new_amount
            new_body = nb
        await conn.execute(
            """
            UPDATE quotes
               SET amount_usd = %s, body = %s, status = 'pending_owner',
                   decided_at = NULL, reject_reason = NULL, client_visible_at = NULL
             WHERE id = %s
            """,
            (new_amount, json.dumps(new_body), quote_id),
        )
        after = dict(before)
        after["amount_usd"] = new_amount
        after["body"] = new_body
        after["status"] = "pending_owner"
        after["decided_at"] = None
        after["reject_reason"] = None
        after["client_visible_at"] = None
        await _write_owner_action(
            conn, tenant_id=before["tenant_id"], actor=actor, action="quote_edit",
            target_id=quote_id, before=_snapshot(before), after=_snapshot(after),
        )
    return after


async def is_re_audit(
    settings: Settings, *, tenant_id: str, run_id: str, eligible_days: int = 0
) -> bool:
    """§9.1 re-audit discount eligibility: the tenant already has a *completed*
    audit run (other than this one) completed within the last ``eligible_days``.
    Deterministic; used by the Quote Agent to decide the re-audit discount."""
    if eligible_days <= 0:
        return False
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            SELECT 1
              FROM audit_runs
             WHERE tenant_id = %s AND id <> %s AND status = 'completed'
               AND completed_at >= now() - make_interval(days => %s)
             LIMIT 1
            """,
            (tenant_id, run_id, eligible_days),
        )
        return (await row.fetchone()) is not None


async def list_pending(settings: Settings) -> list[dict]:
    """Owner review queue (§9.2): every pending_owner quote, oldest first, with
    an audit summary (standard, run status, findings severity/status mix) so the
    owner can decide without opening the full report."""
    async with await _connect(settings) as conn:
        rows = await conn.execute(
            """
            SELECT q.id, q.tenant_id, q.audit_run_id, q.status, q.amount_usd,
                   q.currency, q.body, q.requested_at, q.drafted_at,
                   r.standard, r.status AS run_status,
                   COALESCE(f.failed, 0) AS failed,
                   COALESCE(f.high, 0)   AS high,
                   COALESCE(f.total, 0)  AS total
              FROM quotes q
              JOIN audit_runs r ON r.id = q.audit_run_id
              LEFT JOIN LATERAL (
                   SELECT count(*) FILTER (WHERE status = 'failed') AS failed,
                          count(*) FILTER (WHERE severity = 'high') AS high,
                          count(*)                                  AS total
                     FROM findings WHERE run_id = q.audit_run_id
              ) f ON true
             WHERE q.status = 'pending_owner'
             ORDER BY q.created_at
            """,
        )
        recs = await rows.fetchall()
    out = []
    for r in recs:
        out.append({
            "quote_id": str(r[0]),
            "tenant_id": str(r[1]),
            "audit_run_id": str(r[2]),
            "status": r[3],
            "amount_usd": float(r[4]) if r[4] is not None else None,
            "currency": r[5],
            "body": _as_json(r[6]),
            "requested_at": r[7].isoformat() if r[7] else None,
            "drafted_at": r[8].isoformat() if r[8] else None,
            "audit_summary": {
                "standard": r[9],
                "run_status": r[10],
                "failed_findings": r[11],
                "high_severity_findings": r[12],
                "total_findings": r[13],
            },
        })
    return out


async def list_owner_actions(settings: Settings, *, quote_id: str) -> list[dict]:
    """Audit log for a quote (who, when, action, before, after) — §9.2."""
    async with await _connect(settings) as conn:
        rows = await conn.execute(
            """
            SELECT actor, action, before, after, created_at
              FROM owner_actions
             WHERE target_type = 'quote' AND target_id = %s
             ORDER BY created_at
            """,
            (quote_id,),
        )
        recs = await rows.fetchall()
    return [
        {
            "actor": r[0], "action": r[1],
            "before": _as_json(r[2]), "after": _as_json(r[3]),
            "created_at": r[4].isoformat() if r[4] else None,
        }
        for r in recs
    ]
