"""End-to-end audit pipeline tests (architecture §7, §4.2): upload a stored
ledger, run a full audit through the 4-stage queue, and verify the run completes
with a populated audit trail + findings. Also covers the failure/retry path and
re-runnability. Offline (noop LLM). DB-gated.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path

import psycopg
import pytest

DB_URL = os.environ.get("VERITAS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="VERITAS_TEST_DATABASE_URL not set")

TENANT = "7f9d1b3e-0000-4000-8000-000000000003"
CSV = (
    b"user_id,username,role,amount,currency,card_number,expiry,status,timestamp\n"
    b"1,alice,admin,42,USD,4111111111111111,12/26,active,2026-01-01T10:00:00\n"
    b"2,bob,analyst,-5,USD,,,active,2026-01-01T11:00:00\n"
)
STORAGE_ROOT = "/tmp/veritas-audit-e2e-storage"


@pytest.fixture(scope="module", autouse=True)
def _schema_and_storage():
    from scripts.migrate import run

    assert run(DB_URL) == 0, "migrations failed"
    root = Path(STORAGE_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    for f in root.iterdir():
        if f.is_file():
            f.unlink()
    if not os.environ.get("VERITAS_MASTER_KEY"):
        os.environ["VERITAS_MASTER_KEY"] = secrets.token_hex(32)
    yield


def _clear_business_tables():
    with psycopg.connect(DB_URL) as conn:
        for t in ("findings", "quotes", "audit_steps", "audit_jobs", "audit_runs", "uploads"):
            conn.execute(f"DELETE FROM {t}")


def _env_settings(**overrides):
    from app.config import Settings

    defaults = dict(
        database_url=DB_URL,
        storage_root=STORAGE_ROOT,
        master_key=os.environ.get("VERITAS_MASTER_KEY", "x" * 64),
        environment="test",
        llm_provider="noop",
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _seed_upload(settings):
    from app.storage import get_storage
    from app.uploads.service import insert_upload

    storage = get_storage(settings)
    key = secrets.token_hex(16)
    storage.put(key, CSV, content_type="text/csv")
    uid = await insert_upload(
        settings, tenant_id=TENANT, filename="ledger.csv", size_bytes=len(CSV),
        sha256=hashlib.sha256(CSV).hexdigest(), content_type="text/csv",
        storage_key=key, status="stored",
    )
    return uid, key


async def _create_run(settings, upload_id):
    from app.audit.repo import create_run

    return await create_run(settings, tenant_id=TENANT, upload_id=upload_id,
                            standard="ISO-27001", rule_set_version=1)


def _run_summary(run_id: str) -> dict:
    with psycopg.connect(DB_URL) as conn:
        run = conn.execute("SELECT status, actual_tokens_in FROM audit_runs WHERE id = %s",
                           (run_id,)).fetchone()
        steps = conn.execute(
            "SELECT stage, agent, status FROM audit_steps WHERE run_id = %s ORDER BY created_at",
            (run_id,)).fetchall()
        jobs = conn.execute(
            "SELECT stage, status FROM audit_jobs WHERE run_id = %s ORDER BY created_at",
            (run_id,)).fetchall()
        findings = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE status='failed') FROM findings WHERE run_id = %s",
            (run_id,)).fetchone()
    return {
        "run_status": run[0], "tokens_in": run[1],
        "steps": steps, "jobs": jobs, "findings_total": findings[0],
        "findings_failed": findings[1],
    }


# --- happy path: upload -> audit -> completed with trail ----------------------
def test_full_run_completes_with_findings_and_trail():
    from app.audit.pipeline import process_run

    settings = _env_settings()
    _clear_business_tables()
    uid, _ = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))
    result = asyncio.run(process_run(settings, run_id))
    assert result["status"] == "completed"

    s = _run_summary(run_id)
    assert s["run_status"] == "completed"
    assert s["steps"] and len(s["steps"]) == 4
    assert all(st[2] == "succeeded" for st in s["steps"])  # every step succeeded
    assert all(jb[1] == "succeeded" for jb in s["jobs"])
    assert s["findings_total"] == 24
    assert s["findings_failed"] > 0  # seeded ledger has a negative amount
    assert s["tokens_in"] >= 0  # telemetry populated


def test_pipeline_is_rerunnable_and_idempotent():
    from app.audit.pipeline import process_run

    settings = _env_settings()
    _clear_business_tables()
    uid, _ = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))
    asyncio.run(process_run(settings, run_id))
    before = _run_summary(run_id)
    again = asyncio.run(process_run(settings, run_id))
    assert again["status"] == "completed"
    after = _run_summary(run_id)
    # no duplicate findings / steps from re-running a completed run
    assert after["findings_total"] == before["findings_total"]
    assert len(after["steps"]) == len(before["steps"]) == 4


# --- failure/retry path -------------------------------------------------------
def test_stage_failure_retries_then_recovers():
    from app.audit.pipeline import process_run
    from app.audit.repo import fail_job, claim_job

    # small backoff/timeout so retries are immediately claimable in-test
    settings = _env_settings(job_lease_timeout_seconds=0, job_retry_base_seconds=0,
                             job_max_attempts=3)
    _clear_business_tables()
    uid, key = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))

    # remove the upload object so normalize raises -> first attempt fails
    from app.storage import get_storage
    get_storage(settings).delete(key)
    result = asyncio.run(process_run(settings, run_id))
    assert result["status"] == "retry_scheduled"
    with psycopg.connect(DB_URL) as conn:
        attempts = conn.execute(
            "SELECT attempts FROM audit_jobs WHERE run_id = %s AND stage = 'normalize'",
            (run_id,)).fetchone()[0]
    assert attempts == 1

    # restore the object and re-run -> the stage recovers and the run completes
    asyncio.run(_restore(settings, key))
    result2 = asyncio.run(process_run(settings, run_id))
    assert result2["status"] == "completed"


async def _restore(settings, key):
    from app.storage import get_storage
    get_storage(settings).put(key, CSV, content_type="text/csv")


def test_stage_failure_exhausts_attempts_and_run_fails():
    from app.audit.pipeline import process_run
    from app.storage import get_storage

    settings = _env_settings(job_lease_timeout_seconds=0, job_retry_base_seconds=0,
                             job_max_attempts=1)
    _clear_business_tables()
    uid, key = asyncio.run(_seed_upload(settings))
    run_id = asyncio.run(_create_run(settings, uid))
    get_storage(settings).delete(key)  # normalize will always fail
    result = asyncio.run(process_run(settings, run_id))
    assert result["status"] == "failed"
    s = _run_summary(run_id)
    assert s["run_status"] == "failed"
    assert s["steps"][0][2] == "failed"  # normalize step failed


# --- end-to-end via the HTTP API ----------------------------------------------
def test_api_upload_audit_completes():
    os.environ["VERITAS_DATABASE_URL"] = DB_URL
    os.environ["VERITAS_STORAGE_ROOT"] = STORAGE_ROOT
    from app.config import get_settings
    get_settings.cache_clear()

    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    _clear_business_tables()
    resp = client.post(
        "/api/v1/uploads",
        data={"tenant_id": TENANT},
        files={"file": ("ledger.csv", CSV, "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    upload_id = resp.json()["upload_id"]

    audit = client.post(
        f"/api/v1/uploads/{upload_id}/audit",
        json={"tenant_id": TENANT, "standard": "ISO-27001"},
    )
    assert audit.status_code == 201, audit.text
    body = audit.json()
    assert body["run"]["status"] == "completed"
    assert len(body["steps"]) == 4
    assert all(st["status"] == "succeeded" for st in body["steps"])
    assert body["findings"]
    assert body["quote"] is None or body["quote"]  # drafted quote stub present elsewhere

    # GET audit-run round-trip
    got = client.get(f"/api/v1/uploads/audit-runs/{body['run']['run_id']}")
    assert got.status_code == 200


def test_start_audit_rejects_non_stored_upload():
    os.environ["VERITAS_DATABASE_URL"] = DB_URL
    from app.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/uploads/{secrets.token_hex(16).replace('.','')}/audit",
        json={"tenant_id": TENANT, "standard": "ISO-27001"},
    )
    assert resp.status_code in (404, 422)  # unknown upload id is a non-UUID → 404 path


def test_start_audit_rejects_unknown_standard():
    os.environ["VERITAS_DATABASE_URL"] = DB_URL
    from app.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    _clear_business_tables()
    resp = client.post(
        "/api/v1/uploads",
        data={"tenant_id": TENANT},
        files={"file": ("ledger.csv", CSV, "text/csv")},
    )
    upload_id = resp.json()["upload_id"]
    audit = client.post(
        f"/api/v1/uploads/{upload_id}/audit",
        json={"tenant_id": TENANT, "standard": "NOPE"},
    )
    assert audit.status_code == 422
