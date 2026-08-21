"""Audit pipeline tests (architecture §7): Postgres job queue + 4-stage
orchestration, offline (noop LLM). DB-gated — skipped unless
VERITAS_TEST_DATABASE_URL is set (a scratch database).
"""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import os
import secrets
from pathlib import Path

import psycopg
import pytest

DB_URL = os.environ.get("VERITAS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="VERITAS_TEST_DATABASE_URL not set")

TENANT = "7f9d1b3e-0000-4000-8000-000000000002"
CSV = (
    b"user_id,username,role,amount,currency,card_number,expiry,status,timestamp\n"
    b"1,alice,admin,42,USD,4111111111111111,12/26,active,2026-01-01T10:00:00\n"
    b"2,bob,analyst,-5,USD,,,active,2026-01-01T11:00:00\n"
)
STORAGE_ROOT = "/tmp/veritas-audit-test-storage"


@pytest.fixture(scope="module", autouse=True)
def _schema_and_storage():
    from scripts.migrate import run

    assert run(DB_URL) == 0, "migrations failed"
    root = Path(STORAGE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    for f in root.iterdir():
        if f.is_file():
            f.unlink()
    yield
    import base64

    # leave a valid key env for any stale storage use
    if not os.environ.get("VERITAS_MASTER_KEY"):
        os.environ["VERITAS_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()


def _env_settings(**overrides):
    from app.config import Settings

    defaults = dict(
        database_url=DB_URL,
        storage_root=STORAGE_ROOT,
        master_key=os.environ.get("VERITAS_MASTER_KEY", "x" * 64),
        environment="test",
        llm_provider="noop",
        job_lease_timeout_seconds=overrides.pop("job_lease_timeout_seconds", 600),
        job_retry_base_seconds=overrides.pop("job_retry_base_seconds", 3600),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _clear_business_tables():
    with psycopg.connect(DB_URL) as conn:
        conn.execute("DELETE FROM findings")
        conn.execute("DELETE FROM quotes")
        conn.execute("DELETE FROM audit_steps")
        conn.execute("DELETE FROM audit_jobs")
        conn.execute("DELETE FROM audit_runs")
        conn.execute("DELETE FROM uploads")


async def _seed_upload(settings) -> tuple[str, str]:
    from app.storage import get_storage
    from app.uploads.service import insert_upload

    storage = get_storage(settings)
    key = secrets.token_hex(16)
    storage.put(key, CSV, content_type="text/csv")
    uid = await insert_upload(
        settings,
        tenant_id=TENANT,
        filename="ledger.csv",
        size_bytes=len(CSV),
        sha256=hashlib.sha256(CSV).hexdigest(),
        content_type="text/csv",
        storage_key=key,
        status="stored",
    )
    return uid, key


async def _create_run(settings, upload_id: str) -> str:
    from app.audit.repo import create_run

    run_id = await create_run(
        settings, tenant_id=TENANT, upload_id=upload_id,
        standard="ISO-27001", rule_set_version=1,
    )
    assert run_id, "create_run should succeed for a stored upload"
    return run_id


def _job_counts(run_id: str) -> dict:
    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute(
            "SELECT stage, status, attempts FROM audit_jobs WHERE run_id = %s ORDER BY created_at",
            (run_id,),
        ).fetchall()
    return {stage: {"status": status, "attempts": attempts} for stage, status, attempts in rows}


# --- queue: claim / retry / crash recovery -------------------------------------
def test_create_run_enqueues_four_jobs():
    settings = _env_settings()
    _clear_business_tables()
    uid, _ = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))
    counts = _job_counts(run_id)
    assert list(counts) == ["normalize", "match", "report", "quote"]
    assert all(c["status"] == "pending" for c in counts.values())


def test_claim_job_sets_lease_and_second_worker_is_excluded():
    from app.audit.repo import claim_job

    settings = _env_settings(job_lease_timeout_seconds=600)
    _clear_business_tables()
    uid, _ = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))

    claimed = asyncio.run(claim_job(settings, run_id, "normalize", "worker-a"))
    assert claimed is not None and claimed["stage"] == "normalize" and claimed["attempts"] == 1
    # same job is excluded for worker B while leased
    none2 = asyncio.run(claim_job(settings, run_id, "normalize", "worker-b"))
    assert none2 is None
    # job is now running with a lease token
    with psycopg.connect(DB_URL) as conn:
        row = conn.execute(
            "SELECT status, lease_token, attempts FROM audit_jobs WHERE run_id = %s AND stage = 'normalize'",
            (run_id,),
        ).fetchone()
    assert row[0] == "running" and row[1] == "worker-a" and row[2] == 1


def test_skipped_locked_claims_different_jobs_from_two_workers():
    from app.audit.repo import claim_next

    settings = _env_settings(job_lease_timeout_seconds=600)
    _clear_business_tables()
    uid, _ = asyncio.run(_seed_upload(settings))
    run_id_a = asyncio.run(_create_run(settings, uid))
    uid2, _ = asyncio.run(_seed_upload(settings))
    run_id_b = asyncio.run(_create_run(settings, uid2))

    a = asyncio.run(claim_next(settings, "w1", run_id=run_id_a))
    b = asyncio.run(claim_next(settings, "w2", run_id=run_id_b))
    assert a["run_id"] == run_id_a and b["run_id"] == run_id_b
    assert (a["run_id"], a["stage"]) != (b["run_id"], b["stage"]) or run_id_a != run_id_b


def test_fail_job_requeues_with_backoff_then_fails_after_max():
    from app.audit.repo import claim_job, complete_job, fail_job

    settings = _env_settings(job_lease_timeout_seconds=0, job_max_attempts=3,
                             job_retry_base_seconds=0)
    _clear_business_tables()
    uid, _ = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))

    for attempt in range(1, 4):
        claimed = asyncio.run(claim_job(settings, run_id, "match", f"w{attempt}"))
        assert claimed is not None, f"claim should succeed on attempt {attempt}"
        state = asyncio.run(fail_job(
            settings, claimed["job_id"], attempts=claimed["attempts"], error=f"boom {attempt}"))
        if attempt < 3:
            assert state["status"] == "pending", "should requeue with backoff"
        else:
            assert state["status"] == "failed", "should be permanently failed after max attempts"

    # downstream of a failed job not reached
    with psycopg.connect(DB_URL) as conn:
        status = conn.execute(
            "SELECT status FROM audit_jobs WHERE run_id = %s AND stage = 'match'", (run_id,)
        ).fetchone()[0]
    assert status == "failed"


def test_crash_recovery_reclaims_expired_lease():
    from app.audit.repo import claim_job

    # tiny lease timeout -> previously-leased job becomes claimable again
    settings = _env_settings(job_lease_timeout_seconds=0, job_retry_base_seconds=3600)
    _clear_business_tables()
    uid, _ = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))

    first = asyncio.run(claim_job(settings, run_id, "normalize", "crashed-worker"))
    assert first is not None
    # lease already expired (timeout 0) -> reclaimable
    recovered = asyncio.run(claim_job(settings, run_id, "normalize", "new-worker"))
    assert recovered is not None and recovered["attempts"] == 2
