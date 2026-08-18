"""Integration tests for migrations against a fresh database.

Skipped unless VERITAS_TEST_DATABASE_URL is set. The URL must point at a SCRATCH
database — the fixture drops and recreates the schema tables.

Run:
    VERITAS_TEST_DATABASE_URL=postgresql://localhost/veritas_test pytest
"""
from __future__ import annotations

import os

import psycopg
import pytest
from scripts.migrate import MIGRATIONS_DIR, run

DB_URL = os.environ.get("VERITAS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="VERITAS_TEST_DATABASE_URL not set")

BUSINESS_TABLES = [
    "uploads",
    "audit_runs",
    "audit_jobs",
    "audit_steps",
    "findings",
    "quotes",
    "owner_actions",
]


def _fresh_db() -> str:
    """Drop the app tables and re-run migrations, returning the connection URL."""
    with psycopg.connect(DB_URL) as conn:
        conn.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
        for t in reversed(BUSINESS_TABLES):
            conn.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    rc = run(DB_URL)
    assert rc == 0, "migrate.py failed"
    return DB_URL


def test_migrations_apply_to_fresh_db():
    url = _fresh_db()
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        ).fetchall()
        assert [r[0] for r in rows] == [p.name for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]


def test_migrations_are_idempotent():
    url = _fresh_db()
    rc = run(url)  # second run must skip everything
    assert rc == 0
    with psycopg.connect(url) as conn:
        count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        assert count == len(list(MIGRATIONS_DIR.glob("*.sql")))


def test_all_business_tables_have_tenant_id():
    url = _fresh_db()
    with psycopg.connect(url) as conn:
        for table in BUSINESS_TABLES:
            col = conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'tenant_id'
                """,
                (table,),
            ).fetchone()
            assert col, f"{table} is missing tenant_id"


def test_audit_runs_state_machine_check():
    """§4.2: audit_runs.status must allow the full documented machine."""
    url = _fresh_db()
    with psycopg.connect(url) as conn:
        check = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'audit_runs_status_check'
            """
        ).fetchone()
        assert check, "audit_runs status CHECK missing"
        for state in [
            "uploaded", "validating", "storing", "queued", "normalizing",
            "matching", "reporting", "completed", "failed", "cost_gate_halted",
            "rejected_upload",
        ]:
            assert f"'{state}'" in check[0], f"state {state} not in audit_runs CHECK"


def test_quotes_state_machine_check():
    """§9: quotes.status CHECK covers draft/pending_owner/approved/rejected."""
    url = _fresh_db()
    with psycopg.connect(url) as conn:
        check = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'quotes_status_check'
            """
        ).fetchone()
        assert check
        for state in ["draft", "pending_owner", "approved", "rejected"]:
            assert f"'{state}'" in check[0]


def test_audit_jobs_stage_and_status_checks():
    """§7.2: stages normalize/match/report/quote; status pending/running/succeeded/failed."""
    url = _fresh_db()
    with psycopg.connect(url) as conn:
        stage = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'audit_jobs_stage_check'"
        ).fetchone()
        status = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'audit_jobs_status_check'"
        ).fetchone()
        assert stage and status
        for s in ["normalize", "match", "report", "quote"]:
            assert f"'{s}'" in stage[0]
        for s in ["pending", "running", "succeeded", "failed"]:
            assert f"'{s}'" in status[0]


def test_reject_reason_required_on_reject():
    """§9.1: rejecting a quote without a reason must fail at the DB layer."""
    url = _fresh_db()
    with psycopg.connect(url) as conn:
        tenant = conn.execute("SELECT gen_random_uuid()").fetchone()[0]
        upload = conn.execute(
            "INSERT INTO uploads (tenant_id, filename, size_bytes, sha256, content_type, storage_key, status) "
            "VALUES (%s, 'ledger.csv', 10, 'ab' || repeat('0',62), 'text/csv', 'k-1', 'stored') RETURNING id",
            (tenant,),
        ).fetchone()[0]
        run_row = conn.execute(
            "INSERT INTO audit_runs (tenant_id, upload_id, standard) "
            "VALUES (%s, %s, 'ISO-27001') RETURNING id",
            (tenant, upload),
        ).fetchone()[0]
        quote = conn.execute(
            "INSERT INTO quotes (tenant_id, audit_run_id, status) "
            "VALUES (%s, %s, 'pending_owner') RETURNING id",
            (tenant, run_row),
        ).fetchone()[0]
        # reject without reason -> constraint violation (savepoint keeps txn usable)
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "UPDATE quotes SET status = 'rejected', decided_at = now() WHERE id = %s",
                    (quote,),
                )
        # reject with reason -> OK
        conn.execute(
            "UPDATE quotes SET status = 'rejected', decided_at = now(), reject_reason = 'scope unclear' WHERE id = %s",
            (quote,),
        )
        assert conn.execute("SELECT status FROM quotes WHERE id = %s", (quote,)).fetchone()[0] == "rejected"
