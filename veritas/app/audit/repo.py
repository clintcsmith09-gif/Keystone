"""Database operations for the audit pipeline (architecture §7.4).

Async psycopg helpers mirroring the uploads/service.py pattern. Covers:
audit_runs lifecycle, the audit_jobs queue (claim/complete/fail), audit_steps
traceability, findings persistence, and the (stub) quotes row. No secrets here;
connection info always comes from Settings.
"""
from __future__ import annotations
import json
from datetime import timedelta, datetime, timezone

import psycopg

from ..config import Settings

STAGES = ("normalize", "match", "report", "quote")
STAGE_TO_RUN_STATUS = {
    "normalize": "normalizing",
    "match": "matching",
    "report": "reporting",
    "quote": "reporting",  # quote is optional; keep run in reporting housekeeping
}


async def _connect(settings: Settings):
    return await psycopg.AsyncConnection.connect(settings.database_url)


def _as_json(value):
    """psycopg3 returns JSONB columns as Python dict/list already; coerce any
    str/bytes variant to a container without double-decoding."""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return json.loads(value.decode("utf-8"))
    return json.loads(value)


# --- audit_runs ---------------------------------------------------------------
async def get_upload(settings: Settings, upload_id: str) -> dict | None:
    """Fetch upload metadata needed to normalize (storage_key, filename, type)."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            "SELECT storage_key, filename, content_type, status FROM uploads WHERE id = %s",
            (upload_id,),
        )
        rec = await row.fetchone()
    if rec is None:
        return None
    return {
        "storage_key": rec[0],
        "filename": rec[1],
        "content_type": rec[2],
        "status": rec[3],
    }


async def get_upload_storage_key(settings: Settings, upload_id: str) -> str | None:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            "SELECT storage_key, status FROM uploads WHERE id = %s", (upload_id,)
        )
        rec = await row.fetchone()
    if rec is None or rec[1] != "stored":
        return None
    return rec[0]


async def create_run(
    settings: Settings,
    *,
    tenant_id: str,
    upload_id: str,
    standard: str,
    rule_set_version: int,
) -> str | None:
    """Create an audit run + its 4 job rows. Returns run_id, or None if the
    upload is not runnable (missing/still validating) or a run already exists
    (UNIQUE(upload_id) — §5 idempotent start)."""
    async with await _connect(settings) as conn:
        up = await conn.execute(
            "SELECT status FROM uploads WHERE id = %s FOR UPDATE", (upload_id,)
        )
        uprec = await up.fetchone()
        if uprec is None or uprec[0] != "stored":
            return None
        # run row
        run = await conn.execute(
            """
            INSERT INTO audit_runs (tenant_id, upload_id, standard, rule_set_version, status)
            VALUES (%s, %s, %s, %s, 'queued')
            ON CONFLICT (upload_id) DO NOTHING
            RETURNING id
            """,
            (tenant_id, upload_id, standard, rule_set_version),
        )
        rec = await run.fetchone()
        if rec is None:
            return None  # run already exists for this upload
        run_id = str(rec[0])
        for stage in STAGES:
            await conn.execute(
                "INSERT INTO audit_jobs (tenant_id, run_id, stage) VALUES (%s, %s, %s)",
                (tenant_id, run_id, stage),
            )
    return run_id


async def get_run(settings: Settings, run_id: str) -> dict | None:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            SELECT id, tenant_id, upload_id, standard, rule_set_version, status,
                   cost_estimate_usd, actual_tokens_in, actual_tokens_out,
                   started_at, completed_at, created_at
            FROM audit_runs WHERE id = %s
            """,
            (run_id,),
        )
        rec = await row.fetchone()
    if rec is None:
        return None
    return {
        "run_id": str(rec[0]),
        "tenant_id": str(rec[1]),
        "upload_id": str(rec[2]),
        "standard": rec[3],
        "rule_set_version": rec[4],
        "status": rec[5],
        "cost_estimate_usd": float(rec[6]) if rec[6] is not None else None,
        "actual_tokens_in": rec[7],
        "actual_tokens_out": rec[8],
        "started_at": rec[9].isoformat() if rec[9] else None,
        "completed_at": rec[10].isoformat() if rec[10] else None,
        "created_at": rec[11].isoformat() if rec[11] else None,
    }


async def set_run_status(
    settings: Settings, run_id: str, status: str, *, tokens_in: int = 0, tokens_out: int = 0
) -> None:
    async with await _connect(settings) as conn:
        await conn.execute(
            "UPDATE audit_runs SET status = %s, actual_tokens_in = %s, actual_tokens_out = %s "
            "WHERE id = %s",
            (status, tokens_in, tokens_out, run_id),
        )


async def mark_run_started(settings: Settings, run_id: str) -> None:
    async with await _connect(settings) as conn:
        await conn.execute(
            "UPDATE audit_runs SET started_at = COALESCE(started_at, now()) WHERE id = %s",
            (run_id,),
        )


async def mark_run_completed(settings: Settings, run_id: str, *, status: str = "completed") -> None:
    async with await _connect(settings) as conn:
        await conn.execute(
            "UPDATE audit_runs SET status = %s, completed_at = now() WHERE id = %s",
            (status, run_id),
        )


# --- audit_jobs queue (Postgres-backed; §7.2) -----------------------------------
async def get_job(settings: Settings, run_id: str, stage: str) -> dict | None:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            "SELECT id, run_id, stage, status, attempts, leased_at, error "
            "FROM audit_jobs WHERE run_id = %s AND stage = %s",
            (run_id, stage),
        )
        rec = await row.fetchone()
    if rec is None:
        return None
    return {
        "job_id": str(rec[0]),
        "run_id": str(rec[1]),
        "stage": rec[2],
        "status": rec[3],
        "attempts": rec[4],
        "leased_at": rec[5].isoformat() if rec[5] else None,
        "error": rec[6],
    }


async def claim_job(settings: Settings, run_id: str, stage: str, worker_id: str) -> dict | None:
    """Claim ONE specific (run_id, stage) job with FOR UPDATE SKIP LOCKED.
    Used by the orchestrator to preserve per-run stage ordering (a worker never
    starts a downstream stage before its upstream job has succeeded). Returns
    None when the job is already leased by another worker or not yet retryable."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            SELECT id, stage, attempts
            FROM audit_jobs
            WHERE run_id = %s AND stage = %s
              AND status IN ('pending','running')
              AND (leased_at IS NULL OR leased_at < now() - make_interval(secs => %s))
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (run_id, stage, settings.job_lease_timeout_seconds),
        )
        rec = await row.fetchone()
        if rec is None:
            return None
        job_id, jstage, attempts = rec[0], rec[1], rec[2]
        await conn.execute(
            """
            UPDATE audit_jobs
            SET status = 'running', attempts = attempts + 1,
                lease_token = %s, leased_at = now(),
                started_at = COALESCE(started_at, now()), error = NULL
            WHERE id = %s
            """,
            (worker_id, job_id),
        )
    return {"job_id": str(job_id), "run_id": run_id, "stage": jstage,
            "attempts": attempts + 1, "status": "running"}


async def claim_next(
    settings: Settings, worker_id: str, *, run_id: str | None = None
) -> dict | None:
    """Claim the next ready job with FOR UPDATE SKIP LOCKED (crash-safe). A job
    is claimable when status IN ('pending','running') AND its lease is NULL or
    expired (past its timeout) — so a worker that died mid-job is reclaimed."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            SELECT id, run_id, stage, attempts
            FROM audit_jobs
            WHERE (run_id = %s::uuid OR %s::uuid IS NULL)
              AND status IN ('pending','running')
              AND (leased_at IS NULL OR leased_at < now() - make_interval(secs => %s))
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (run_id, run_id, settings.job_lease_timeout_seconds),
        )
        rec = await row.fetchone()
        if rec is None:
            return None
        job_id, jrun, stage, attempts = rec[0], rec[1], rec[2], rec[3]
        await conn.execute(
            """
            UPDATE audit_jobs
            SET status = 'running', attempts = attempts + 1,
                lease_token = %s, leased_at = now(),
                started_at = COALESCE(started_at, now()), error = NULL
            WHERE id = %s
            """,
            (worker_id, job_id),
        )
    return {
        "job_id": str(job_id),
        "run_id": str(jrun),
        "stage": stage,
        "attempts": attempts + 1,
        "status": "running",
    }


async def complete_job(settings: Settings, job_id: str) -> None:
    async with await _connect(settings) as conn:
        await conn.execute(
            "UPDATE audit_jobs SET status = 'succeeded', finished_at = now(), "
            "lease_token = NULL, leased_at = NULL WHERE id = %s",
            (job_id,),
        )


async def fail_job(
    settings: Settings, job_id: str, *, attempts: int, error: str
) -> dict:
    """Register a failed attempt. If attempts >= max, the job is failed for good;
    otherwise it is requeued (status 'pending') with an exponential backoff
    lease so it cannot be retried immediately."""
    async with await _connect(settings) as conn:
        if attempts >= settings.job_max_attempts:
            await conn.execute(
                "UPDATE audit_jobs SET status = 'failed', finished_at = now(), "
                "error = %s, lease_token = NULL, leased_at = NULL WHERE id = %s",
                (error[:2000], job_id),
            )
            final = "failed"
        else:
            delay = settings.job_retry_base_seconds * (2 ** (attempts - 1))
            await conn.execute(
                "UPDATE audit_jobs SET status = 'pending', error = %s, "
                "lease_token = NULL, leased_at = now() + make_interval(secs => %s) "
                "WHERE id = %s",
                (error[:2000], delay, job_id),
            )
            final = "pending"
    return {"job_id": str(job_id), "status": final}


# --- audit_steps (§7.4 traceability) -------------------------------------------
async def step_begin(
    settings: Settings,
    *,
    run_id: str,
    tenant_id: str,
    stage: str,
    agent: str,
    model_id: str,
    model_version: str,
    prompt_template_id: str,
    input_artifact_ref: str | None = None,
) -> str:
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            INSERT INTO audit_steps
                (tenant_id, run_id, stage, agent, model_id, model_version,
                 prompt_template_id, input_artifact_ref, status, started_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running', now())
            RETURNING id
            """,
            (tenant_id, run_id, stage, agent, model_id, model_version,
             prompt_template_id, input_artifact_ref),
        )
        rec = await row.fetchone()
    return str(rec[0])


async def step_end(
    settings: Settings,
    step_id: str,
    *,
    status: str,
    output_artifact_ref: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    error: str | None = None,
) -> None:
    async with await _connect(settings) as conn:
        await conn.execute(
            """
            UPDATE audit_steps SET status = %s, finished_at = now(),
                output_artifact_ref = %s, tokens_in = %s, tokens_out = %s, error = %s,
                duration_ms = EXTRACT(EPOCH FROM (now() - started_at))::int * 1000
            WHERE id = %s
            """,
            (status, output_artifact_ref, tokens_in, tokens_out, error, step_id),
        )


async def get_steps(settings: Settings, run_id: str) -> list[dict]:
    async with await _connect(settings) as conn:
        rows = await conn.execute(
            "SELECT stage, agent, model_id, model_version, prompt_template_id, "
            "tokens_in, tokens_out, duration_ms, status, error "
            "FROM audit_steps WHERE run_id = %s ORDER BY created_at",
            (run_id,),
        )
        recs = await rows.fetchall()
    return [
        {
            "stage": r[0], "agent": r[1], "model_id": r[2], "model_version": r[3],
            "prompt_template_id": r[4], "tokens_in": r[5], "tokens_out": r[6],
            "duration_ms": r[7], "status": r[8], "error": r[9],
        }
        for r in recs
    ]


# --- findings (§8) -------------------------------------------------------------
async def replace_findings(
    settings: Settings,
    *,
    run_id: str,
    tenant_id: str,
    standard: str,
    standard_version: int,
    results: list[dict],
) -> int:
    """Idempotently persist match results (clears then re-inserts for re-runs)."""
    async with await _connect(settings) as conn:
        await conn.execute("DELETE FROM findings WHERE run_id = %s", (run_id,))
        for r in results:
            evidence = dict(r.get("evidence", {}))
            # category lives on the row's evidence because the findings table
            # (§8) has no category column — keep it for grouping/citation.
            if "category" in r and not evidence.get("category"):
                evidence["category"] = r["category"]
            await conn.execute(
                """
                INSERT INTO findings
                    (tenant_id, run_id, rule_id, standard, standard_version,
                     severity, status, evidence, llm_judgment, recommendation)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id, run_id, r["rule_id"], standard, standard_version,
                    r["severity"], r["status"],
                    json.dumps(evidence),
                    json.dumps(r.get("llm_judgment")) if r.get("llm_judgment") else None,
                    r.get("recommendation"),
                ),
            )
    return len(results)


async def get_findings(settings: Settings, run_id: str) -> list[dict]:
    async with await _connect(settings) as conn:
        rows = await conn.execute(
            "SELECT rule_id, standard, standard_version, severity, status, evidence, "
            "llm_judgment, recommendation FROM findings WHERE run_id = %s ORDER BY created_at",
            (run_id,),
        )
        recs = await rows.fetchall()
    out = []
    for r in recs:
        evidence = _as_json(r[5])
        out.append({
            "rule_id": r[0], "standard": r[1], "standard_version": r[2],
            "severity": r[3], "status": r[4],
            "category": (evidence or {}).get("category", "uncategorized"),
            "evidence": evidence,
            "llm_judgment": _as_json(r[6]),
            "recommendation": r[7],
        })
    return out


# --- quotes (stub, §9) ---------------------------------------------------------
async def insert_quote_stub(settings: Settings, *, run_id: str, tenant_id: str, payload: dict) -> str | None:
    """Create a draft quote for the run; idempotent (no duplicate per run)."""
    async with await _connect(settings) as conn:
        existing = await conn.execute(
            "SELECT id FROM quotes WHERE audit_run_id = %s", (run_id,)
        )
        if await existing.fetchone() is not None:
            return None
        row = await conn.execute(
            "INSERT INTO quotes (tenant_id, audit_run_id, status, amount_usd, currency, body, requested_at) "
            "VALUES (%s, %s, 'draft', %s, %s, %s, now()) RETURNING id",
            (
                tenant_id, run_id,
                payload.get("amount_usd"),
                payload.get("currency", "USD"),
                json.dumps(payload.get("body", {})),
            ),
        )
        rec = await row.fetchone()
    return str(rec[0])
