"""Cost-gate + telemetry database operations (architecture §11.3, §13 Q2).

Persistence behind the cost gate:
  * cost_gate_events       — append-only audit trail of every gate decision.
  * owner_notifications    — in-app owner alerts (halt, monthly warn/cap, grant).
  * monthly spend query    — aggregate token telemetry across audits this month.
  * owner approval actions — granting a cap-queued run (awaiting_owner_approval)
                             to proceed, logged to owner_actions (same hard-gate
                             discipline as the quote queue, §9.2).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import psycopg

from ..config import Settings
from .repo import _connect, _as_json


# --- owner_notifications -------------------------------------------------------
async def add_notification(
    settings: Settings,
    *,
    tenant_id: str,
    kind: str,
    message: str,
    run_id: str | None = None,
    payload: dict | None = None,
) -> str:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            INSERT INTO owner_notifications (tenant_id, kind, message, run_id, payload)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (tenant_id, kind, message, run_id,
             json.dumps(payload) if payload is not None else None),
        )
        rec = await row.fetchone()
    return str(rec[0])


async def list_notifications(settings: Settings, *, unacked_only: bool = True) -> list[dict]:
    async with await _connect(settings) as conn:
        where = "WHERE acknowledged_at IS NULL" if unacked_only else ""
        rows = await conn.execute(
            f"""
            SELECT id, tenant_id, kind, message, run_id, payload, created_at,
                   acknowledged_at
            FROM owner_notifications {where} ORDER BY created_at DESC
            """
        )
        recs = await rows.fetchall()
    return [
        {
            "id": str(r[0]), "tenant_id": str(r[1]), "kind": r[2],
            "message": r[3], "run_id": str(r[4]) if r[4] else None,
            "payload": _as_json(r[5]),
            "created_at": r[6].isoformat() if r[6] else None,
            "acknowledged_at": r[7].isoformat() if r[7] else None,
        }
        for r in recs
    ]


async def acknowledge_notification(settings: Settings, notification_id: str) -> bool:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            "UPDATE owner_notifications SET acknowledged_at = now() "
            "WHERE id = %s AND acknowledged_at IS NULL",
            (notification_id,),
        )
        return row.rowcount > 0


# --- cost_gate_events (audit trail) --------------------------------------------
async def record_gate_event(
    settings: Settings,
    *,
    tenant_id: str,
    kind: str,
    decision: str,
    run_id: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    budget_in: int = 0,
    budget_out: int = 0,
    detail: str = "",
) -> str:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            INSERT INTO cost_gate_events
                (tenant_id, run_id, kind, decision, tokens_in, tokens_out,
                 budget_in, budget_out, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (tenant_id, run_id, kind, decision, tokens_in, tokens_out,
             budget_in, budget_out, detail),
        )
        rec = await row.fetchone()
    return str(rec[0])


# --- monthly spend telemetry ---------------------------------------------------
async def monthly_spend(
    settings: Settings,
    *,
    tenant_id: str,
    reference_date: datetime | None = None,
) -> dict:
    """Aggregate measured LLM token spend across ALL audit steps for the tenant
    in the reference month (§11.3 monthly aggregate view). Uses audit_steps, the
    per-stage token telemetry, so it reflects actual measured usage."""
    ref = reference_date or datetime.now(timezone.utc)
    start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    async with await _connect(settings) as conn:
        if tenant_id:
            sql = """
                SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), COUNT(*)
                FROM audit_steps
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
                  AND status = 'succeeded'
            """
            params = (tenant_id, start, nxt)
        else:
            sql = """
                SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), COUNT(*)
                FROM audit_steps
                WHERE created_at >= %s AND created_at < %s AND status = 'succeeded'
            """
            params = (start, nxt)
        row = await conn.execute(sql, params)
        rec = await row.fetchone()
    return {
        "month": f"{start.year:04d}-{start.month:02d}",
        "tokens_in": int(rec[0]),
        "tokens_out": int(rec[1]),
        "steps": int(rec[2]),
        "started_at": start.isoformat(),
    }


async def monthly_usage_by_audit(settings: Settings, *, tenant_id: str,
                                 reference_date: datetime | None = None) -> list[dict]:
    """Per-audit breakdown for the monthly aggregate view (which runs consumed
    this month's tokens)."""
    ref = reference_date or datetime.now(timezone.utc)
    start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    async with await _connect(settings) as conn:
        if tenant_id:
            sql = """
                SELECT s.run_id, r.standard, r.status,
                       SUM(s.tokens_in), SUM(s.tokens_out), COUNT(*)
                FROM audit_steps s
                JOIN audit_runs r ON r.id = s.run_id
                WHERE s.tenant_id = %s AND s.created_at >= %s AND s.created_at < %s
                  AND s.status = 'succeeded'
                GROUP BY s.run_id, r.standard, r.status
                ORDER BY s.run_id
            """
            params = (tenant_id, start, nxt)
        else:
            sql = """
                SELECT s.run_id, r.standard, r.status,
                       SUM(s.tokens_in), SUM(s.tokens_out), COUNT(*)
                FROM audit_steps s
                JOIN audit_runs r ON r.id = s.run_id
                WHERE s.created_at >= %s AND s.created_at < %s AND s.status = 'succeeded'
                GROUP BY s.run_id, r.standard, r.status
                ORDER BY s.run_id
            """
            params = (start, nxt)
        rows = await conn.execute(sql, params)
        recs = await rows.fetchall()
    return [
        {
            "run_id": str(r[0]), "standard": r[1], "run_status": r[2],
            "tokens_in": int(r[3]), "tokens_out": int(r[4]), "steps": int(r[5]),
        }
        for r in recs
    ]


# --- owner review of cap-queued audits -----------------------------------------
async def list_pending_approvals(settings: Settings) -> list[dict]:
    """Runs waiting because the monthly cap was reached (status
    awaiting_owner_approval), with an audit summary for the owner queue."""
    async with await _connect(settings) as conn:
        rows = await conn.execute(
            """
            SELECT r.id, r.tenant_id, r.standard, r.rule_set_version,
                   r.status, r.gate_halt_reason, r.created_at,
                   u.filename, u.size_bytes
            FROM audit_runs r
            JOIN uploads u ON u.id = r.upload_id
            WHERE r.status = 'awaiting_owner_approval'
            ORDER BY r.created_at
            """
        )
        recs = await rows.fetchall()
    return [
        {
            "run_id": str(r[0]), "tenant_id": str(r[1]), "standard": r[2],
            "rule_set_version": r[3], "status": r[4], "gate_halt_reason": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "upload_filename": r[7], "upload_size_bytes": r[8],
        }
        for r in recs
    ]


async def approve_cap_queued_run(
    settings: Settings, *, run_id: str, actor: str,
) -> dict | None:
    """Owner grants a cap-queued run to proceed: status -> 'queued' and
    approved_via_gate = true so the pipeline's monthly gate lets this one run.
    Logs an owner_actions row + notification. Returns the updated run or None."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            "SELECT id, tenant_id, status FROM audit_runs WHERE id = %s FOR UPDATE",
            (run_id,),
        )
        rec = await row.fetchone()
        if rec is None:
            return None
        run_id, tenant_id, status = str(rec[0]), str(rec[1]), rec[2]
        if status == "queued" and (await conn.execute(
            "SELECT approved_via_gate FROM audit_runs WHERE id = %s", (run_id,)
        )).fetchone()[0]:
            return {"run_id": run_id, "status": "queued", "granted": True}
        before = {"status": status, "approved_via_gate": False}
        await conn.execute(
            """
            UPDATE audit_runs
               SET status = 'queued', approved_via_gate = true,
                   gate_halt_reason = 'owner approved after monthly cap'
             WHERE id = %s
            """,
            (run_id,),
        )
        await conn.execute(
            """
            INSERT INTO owner_actions (tenant_id, actor, action, target_type,
                                       target_id, before, after)
            VALUES (%s, %s, 'cost_gate_approve', 'audit_run', %s, %s, %s)
            """,
            (tenant_id, actor, run_id,
             json.dumps(before),
             json.dumps({"status": "queued", "approved_via_gate": True})),
        )
    await add_notification(
        settings, tenant_id=tenant_id, kind="approval_granted",
        message="Owner approved a cap-queued audit to run.",
        run_id=run_id,
    )
    return {"run_id": run_id, "status": "queued", "granted": True}
