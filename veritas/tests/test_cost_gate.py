"""Phase 0.6 cost gate tests (architecture §11.3).

Two layers:
  * pure, DB-free tests of the deterministic gate math (estimate, pre-run,
    mid-flight, monthly) in tests/test_cost_gate_math.py.
  * DB-gated integration tests here proving the gate actually halts / blocks /
    queues runs through the pipeline orchestration, plus the monthly token
    telemetry aggregation.

Skipped unless VERITAS_TEST_DATABASE_URL is set (a scratch database).
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
STORAGE_ROOT = "/tmp/veritas-costgate-test-storage"


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


def _env_settings(**overrides):
    from app.config import Settings

    defaults = dict(
        database_url=DB_URL,
        storage_root=STORAGE_ROOT,
        master_key=os.environ.get("VERITAS_MASTER_KEY", "x" * 64),
        environment="test",
        llm_provider="noop",
        job_lease_timeout_seconds=600,
        job_retry_base_seconds=3600,
        cost_gate_enabled=True,
        cost_gate_max_tokens_in=250_000,
        cost_gate_max_tokens_out=75_000,
        # Non-zero estimate knobs: per-audit pre-run estimate scales with file
        # size + rule count. Only the mid-flight test zeros these (so its pre-run
        # estimate passes while the measured match tokens trigger the abort).
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _clear():
    with psycopg.connect(DB_URL) as conn:
        for t in ("cost_gate_events", "owner_notifications", "findings", "quotes",
                  "owner_actions", "audit_steps", "audit_jobs",
                  "audit_runs", "uploads"):
            conn.execute(f"DELETE FROM {t}")


def _seed_upload(settings, *, size_bytes: int | None = None) -> tuple[str, str]:
    from app.storage import get_storage
    from app.uploads.service import insert_upload

    storage = get_storage(settings)
    key = secrets.token_hex(16)
    storage.put(key, CSV, content_type="text/csv")
    uid = asyncio.run(insert_upload(
        settings,
        tenant_id=TENANT,
        filename="ledger.csv",
        size_bytes=size_bytes if size_bytes is not None else len(CSV),
        sha256=hashlib.sha256(CSV).hexdigest(),
        content_type="text/csv",
        storage_key=key,
        status="stored",
    ))
    return uid, key


def _create_run(settings, upload_id: str) -> str:
    from app.audit.repo import create_run

    run_id = asyncio.run(create_run(
        settings, tenant_id=TENANT, upload_id=upload_id,
        standard="ISO-27001", rule_set_version=1,
    ))
    assert run_id, "create_run should succeed"
    return run_id


def _run_json(settings, run_id: str) -> dict:
    from app.audit.repo import get_run

    return asyncio.run(get_run(settings, run_id))


def _job_statuses(run_id: str) -> dict:
    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute(
            "SELECT stage, status FROM audit_jobs WHERE run_id = %s ORDER BY created_at",
            (run_id,),
        ).fetchall()
    return {s: st for s, st in rows}


def _notify_kinds() -> list[str]:
    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute("SELECT kind FROM owner_notifications").fetchall()
    return [r[0] for r in rows]


def _events(run_id: str) -> list[dict]:
    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute(
            "SELECT kind, decision, tokens_in, budget_in "
            "FROM cost_gate_events WHERE run_id = %s ORDER BY created_at",
            (run_id,),
        ).fetchall()
    return [{"kind": r[0], "decision": r[1], "tokens_in": r[2], "budget_in": r[3]} for r in rows]


# --- §11.3a pre-run estimate gate ----------------------------------------------
def test_pre_run_halt_over_budget_run_does_not_start():
    from app.audit.pipeline import process_run

    # Restore non-zero estimate knobs so the 5 MB file produces an over-budget
    # deterministic upper bound (the module defaults zero these for other tests).
    settings = _env_settings(
        cost_gate_max_tokens_in=250_000,
        cost_gate_estimate_tokens_per_byte=0.5,
        cost_gate_estimate_tokens_per_row=40,
        cost_gate_estimate_tokens_per_judgment_rule=300,
        cost_gate_estimate_tokens_out_per_judgment_rule=100,
    )
    _clear()
    uid, _ = _seed_upload(settings, size_bytes=5_000_000)  # 5 MB => huge estimate
    run_id = _create_run(settings, uid)

    result = asyncio.run(process_run(settings, run_id))
    assert result["status"] == "cost_gate_halted"
    # Run must NOT have started: no stage claimed, jobs all still pending.
    assert _job_statuses(run_id) == {s: "pending" for s in
                                     ("normalize", "match", "report", "quote")}
    ev = _events(run_id)
    assert any(e["kind"] == "pre_run" and e["decision"] == "halt" for e in ev)
    assert _notify_kinds() == ["cost_gate_halted"]


def test_pre_run_passes_within_budget_and_completes():
    from app.audit.pipeline import process_run

    settings = _env_settings(cost_gate_max_tokens_in=250_000)
    _clear()
    uid, _ = _seed_upload(settings)
    run_id = _create_run(settings, uid)
    result = asyncio.run(process_run(settings, run_id))
    assert result["status"] == "completed"
    assert _job_statuses(run_id) == {s: "succeeded" for s in
                                     ("normalize", "match", "report", "quote")}


# --- §11.3b mid-flight abort ---------------------------------------------------
def test_mid_flight_abort_halts_at_stage_boundary_and_preserves_artifacts():
    from app.audit.pipeline import process_run

    # Per-audit in-budget small enough that the match stage (68 tokens) exceeds
    # it, but the deterministic estimate (zeroed knobs) passes pre-run.
    settings = _env_settings(
        cost_gate_max_tokens_in=50,
        cost_gate_estimate_tokens_per_byte=0,
        cost_gate_estimate_tokens_per_row=0,
        cost_gate_estimate_tokens_per_judgment_rule=0,
        cost_gate_estimate_tokens_out_per_judgment_rule=0,
    )
    _clear()
    uid, key = _seed_upload(settings)
    run_id = _create_run(settings, uid)

    result = asyncio.run(process_run(settings, run_id))
    assert result["status"] == "cost_gate_halted"
    assert result["actual_tokens_in"] > 50
    # Completed stages succeeded (artifacts preserved); later stages never run.
    statuses = _job_statuses(run_id)
    assert statuses["normalize"] == "succeeded"
    assert statuses["match"] == "succeeded"
    assert statuses["report"] == "pending"
    assert statuses["quote"] == "pending"
    ev = _events(run_id)
    assert any(e["kind"] == "mid_flight" and e["decision"] == "halt" for e in ev)


# --- §11.3c monthly aggregate cap ----------------------------------------------
def test_monthly_cap_queues_new_audit_and_owner_approval_grants_run():
    from app.audit.pipeline import process_run
    from app.audit.cost_gate_repo import approve_cap_queued_run, monthly_spend

    # Tiny monthly in-cap: the first completed run (68 tokens) blows past it.
    settings = _env_settings(cost_gate_max_tokens_in=1_000,
                             cost_gate_monthly_tokens_in=30)
    _clear()

    # Run 1 completes and consumes the monthly budget.
    uid1, _ = _seed_upload(settings)
    run1 = _create_run(settings, uid1)
    r1 = asyncio.run(process_run(settings, run1))
    assert r1["status"] == "completed"
    usage = asyncio.run(monthly_spend(settings, tenant_id=TENANT))
    assert usage["tokens_in"] >= 68

    # Run 2 (new audit) is queued for owner approval at the cap.
    uid2, _ = _seed_upload(settings)
    run2 = _create_run(settings, uid2)
    r2 = asyncio.run(process_run(settings, run2))
    assert r2["status"] == "awaiting_owner_approval"
    assert "monthly_cap" in _notify_kinds()
    assert _job_statuses(run2)["normalize"] == "pending"  # never started

    # Owner approves -> run proceeds (grant consumed) and completes.
    ack = asyncio.run(approve_cap_queued_run(
        settings, run_id=run2, actor="owner"))
    assert ack["status"] == "queued"
    r2b = asyncio.run(process_run(settings, run2))
    assert r2b["status"] == "completed"


# --- token telemetry aggregation (§7.4/§11.3) ----------------------------------
def test_monthly_spend_aggregates_per_stage_tokens():
    from app.audit.cost_gate_repo import monthly_spend, monthly_usage_by_audit
    from app.audit.repo import get_steps
    from app.audit.pipeline import process_run

    settings = _env_settings(cost_gate_max_tokens_in=1_000)
    _clear()
    uid, _ = _seed_upload(settings)
    run_id = _create_run(settings, uid)
    asyncio.run(process_run(settings, run_id))

    # Per-stage token measurement is surfaced on the run's steps.
    steps = asyncio.run(get_steps(settings, run_id))
    by_stage = {s["stage"]: s for s in steps}
    assert set(by_stage) == {"normalize", "match", "report", "quote"}
    assert by_stage["match"]["tokens_in"] > 0

    # Monthly aggregate view reflects the measured usage across audits.
    usage = asyncio.run(monthly_spend(settings, tenant_id=TENANT))
    assert usage["tokens_in"] == sum(s["tokens_in"] for s in steps)
    assert usage["steps"] == len([s for s in steps if s["status"] == "succeeded"])
    per = asyncio.run(monthly_usage_by_audit(settings, tenant_id=TENANT))
    assert any(p["run_id"] == run_id and p["tokens_in"] == usage["tokens_in"] for p in per)
