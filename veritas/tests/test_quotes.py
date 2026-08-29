"""Quote flow + owner review queue tests (architecture §9, §10.3).

Covers: deterministic autonomous draft (derives from scope/volume/severity),
the quotes state machine (draft → pending_owner → approved | rejected, edit →
pending), the append-only owner_actions audit log, the hard gate (a client
never sees a non-approved quote — no price leak), tenant isolation, and the
client request flow end-to-end. Offline (noop LLM), DB-gated.
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

TENANT_A = "7f9d1b3e-0000-4000-8000-0000000000aa"
TENANT_B = "7f9d1b3e-0000-4000-8000-0000000000bb"
CSV = (
    b"user_id,username,role,amount,currency,card_number,expiry,status,timestamp\n"
    b"1,alice,admin,42,USD,4111111111111111,12/26,active,2026-01-01T10:00:00\n"
    b"2,bob,analyst,-5,USD,,,active,2026-01-01T11:00:00\n"
)
STORAGE_ROOT = "/tmp/veritas-quotes-e2e-storage"


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


def _clear_tables():
    with psycopg.connect(DB_URL) as conn:
        for t in ("owner_actions", "findings", "quotes", "audit_steps",
                  "audit_jobs", "audit_runs", "uploads"):
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


async def _seed_completed_run(settings, tenant: str) -> str:
    """Upload + run a full ISO-27001 audit, returning the completed run_id."""
    from app.storage import get_storage
    from app.uploads.service import insert_upload
    from app.audit.repo import create_run
    from app.audit.pipeline import process_run

    storage = get_storage(settings)
    key = secrets.token_hex(16)
    storage.put(key, CSV, content_type="text/csv")
    uid = await insert_upload(
        settings, tenant_id=tenant, filename="ledger.csv", size_bytes=len(CSV),
        sha256=hashlib.sha256(CSV).hexdigest(), content_type="text/csv",
        storage_key=key, status="stored",
    )
    run_id = await create_run(settings, tenant_id=tenant, upload_id=uid,
                              standard="ISO-27001", rule_set_version=1)
    result = await process_run(settings, run_id)
    assert result["status"] == "completed", result
    return run_id


def _api_client():
    """TestClient bound to the app, with the DB/storage env configured."""
    os.environ["VERITAS_DATABASE_URL"] = DB_URL
    os.environ["VERITAS_STORAGE_ROOT"] = STORAGE_ROOT
    from app.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


# --- unit: deterministic pricing -------------------------------------------------
def test_itemize_is_deterministic_and_scope_driven():
    from app.audit.quote_rules import load_quote_rules, itemize, QuoteInputs

    rules = load_quote_rules()
    ins = QuoteInputs(standard="ISO-27001", rules_evaluated=24,
                      data_rows=2, data_files=1, high_findings=0, failed_findings=0)
    a = itemize(rules, ins)
    b = itemize(rules, ins)
    assert a == b  # reproducible
    # base 1200 + ISO adder 800 + volume band 0 (<=50k rows) = 2000
    assert a["total_usd"] == 2000.0
    assert a["discount_usd"] == 0.0


def test_itemize_applies_volume_and_severity_adders():
    from app.audit.quote_rules import load_quote_rules, itemize, QuoteInputs

    rules = load_quote_rules()
    ins = QuoteInputs(standard="PCI-DSS", rules_evaluated=27, data_rows=600_000,
                      data_files=1, high_findings=2, failed_findings=3)
    r = itemize(rules, ins)
    # base 1200 + PCI 900 + volume band (>500k) 900 + high 2*50 + failed 3*40
    assert r["total_usd"] == 1200.0 + 900.0 + 900.0 + 2 * 50.0 + 3 * 40.0


def test_itemize_applies_re_audit_discount():
    from app.audit.quote_rules import load_quote_rules, itemize, QuoteInputs

    rules = load_quote_rules()
    base = itemize(rules, QuoteInputs(standard="ISO-27001", data_rows=2))
    re_aud = itemize(rules, QuoteInputs(standard="ISO-27001", data_rows=2, re_audit=True))
    # 25% percent discount off the running total
    assert re_aud["discount_usd"] == round(base["total_usd"] * 0.25, 2)


def test_quote_agent_derives_inputs_from_report_findings():
    from app.audit.quote import DeterministicQuoteAgent, derive_quote_inputs
    from app.audit.quote_rules import QuoteInputs

    run = {"run_id": "r1", "standard": "ISO-27001", "rule_set_version": 1}
    report = {"standard": "ISO-27001", "findings": [
        {"severity": "high", "status": "failed", "evidence": {"row_count": 10}},
        {"severity": "low", "status": "passed", "evidence": {}},
    ]}
    ins = derive_quote_inputs(run=run, report=report)
    assert isinstance(ins, QuoteInputs)
    assert ins.standard == "ISO-27001"
    assert ins.rules_evaluated == 2
    assert ins.high_findings == 1
    assert ins.failed_findings == 1
    assert ins.data_rows == 10  # from finding evidence row_count

    agent = DeterministicQuoteAgent()
    out = asyncio.run(agent.quote(run=run, report=report))
    assert out["amount_usd"] == out["body"]["total_usd"]
    assert out["body"]["audit_scope"]["standard"] == "ISO-27001"
    assert out["body"]["severity_mix"]["high"] == 1
    assert out["body"]["severity_mix"]["failed"] == 1


# --- state machine + audit log (repo-level) --------------------------------------
def test_repo_state_machine_approve_reject_edit():
    from app.audit import quotes_repo

    settings = _env_settings()
    run_id = asyncio.run(_seed_completed_run(settings, TENANT_A))
    qr = asyncio.run(quotes_repo.request_quote(
        settings, run_id=run_id, tenant_id=TENANT_A,
        payload={"amount_usd": 2000.0, "currency": "USD", "body": {"total_usd": 2000.0}},
    ))
    assert qr["status"] == "pending_owner"

    # reject requires a reason (CHECK constraint) handled at API; repo-level:
    q_rej = asyncio.run(quotes_repo.reject_quote(
        settings, quote_id=qr["quote_id"], actor="owner-1", reason="price too high"))
    assert q_rej["status"] == "rejected"
    assert q_rej["reject_reason"] == "price too high"
    actions = asyncio.run(quotes_repo.list_owner_actions(settings, quote_id=qr["quote_id"]))
    assert actions[0]["action"] == "quote_reject"
    assert actions[0]["before"]["status"] == "pending_owner"
    assert actions[0]["after"]["status"] == "rejected"

    # edit a rejected quote -> returns to pending_owner
    q_edit = asyncio.run(quotes_repo.edit_quote(
        settings, quote_id=qr["quote_id"], actor="owner-1", amount_usd=1500.0))
    assert q_edit["status"] == "pending_owner"
    assert q_edit["amount_usd"] == 1500.0
    assert q_edit["decided_at"] is None
    actions = asyncio.run(quotes_repo.list_owner_actions(settings, quote_id=qr["quote_id"]))
    assert actions[-1]["action"] == "quote_edit"

    # approve -> client visible, decided_at set
    q_ok = asyncio.run(quotes_repo.approve_quote(
        settings, quote_id=qr["quote_id"], actor="owner-1"))
    assert q_ok["status"] == "approved"
    assert q_ok["client_visible_at"] is not None
    actions = asyncio.run(quotes_repo.list_owner_actions(settings, quote_id=qr["quote_id"]))
    assert actions[-1]["action"] == "quote_approve"


# --- hard gate + tenant isolation + client flow (end-to-end) ----------------------
def test_client_request_flow_hard_gate_and_isolation():
    settings = _env_settings()
    _clear_tables()
    run_id = asyncio.run(_seed_completed_run(settings, TENANT_A))
    client = _api_client()

    # tenant B cannot request a quote on tenant A's run (isolation)
    bad = client.post(f"/api/v1/audits/{run_id}/quote-request",
                      json={"tenant_id": TENANT_B})
    assert bad.status_code == 404

    # tenant A requests a quote -> drafted + routed to owner (pending_owner)
    r = client.post(f"/api/v1/audits/{run_id}/quote-request",
                    json={"tenant_id": TENANT_A})
    assert r.status_code == 201, r.text
    quote_id = r.json()["quote_id"]

    # HARD GATE: client sees NO amount / body before approval
    pre = client.get(f"/api/v1/quotes/{quote_id}", headers={"X-Tenant-Id": TENANT_A})
    assert pre.status_code == 200
    assert pre.json()["client_visible"] is False
    assert "amount_usd" not in pre.json()
    assert "body" not in pre.json()

    # different tenant sees 404 (no cross-tenant read)
    cross = client.get(f"/api/v1/quotes/{quote_id}", headers={"X-Tenant-Id": TENANT_B})
    assert cross.status_code == 404

    # owner queue lists the pending quote with an audit summary + amount
    queue = client.get("/api/v1/owner/queue")
    assert queue.status_code == 200
    pending = [q for q in queue.json()["pending"] if q["quote_id"] == quote_id]
    assert len(pending) == 1
    assert pending[0]["audit_summary"]["standard"] == "ISO-27001"
    assert pending[0]["amount_usd"] is not None  # owner sees the price

    # owner approve -> client now sees the approved quote (price + items)
    appr = client.post(f"/api/v1/owner/queue/{quote_id}/approve", json={"actor": "owner-1"})
    assert appr.status_code == 200
    post = client.get(f"/api/v1/quotes/{quote_id}", headers={"X-Tenant-Id": TENANT_A})
    assert post.json()["client_visible"] is True
    assert post.json()["amount_usd"] == pending[0]["amount_usd"]
    assert "itemization" in post.json()["body"]
    # owner action logged
    act = client.get(f"/api/v1/quotes/{quote_id}")  # no tenant header = owner
    assert any(a["action"] == "quote_approve" for a in act.json()["owner_actions"])


def test_owner_reject_requires_reason_and_returns_to_pending_on_edit():
    settings = _env_settings()
    _clear_tables()
    run_id = asyncio.run(_seed_completed_run(settings, TENANT_A))
    client = _api_client()
    qid = client.post(f"/api/v1/audits/{run_id}/quote-request",
                      json={"tenant_id": TENANT_A}).json()["quote_id"]

    # reject without reason -> 422
    r = client.post(f"/api/v1/owner/queue/{qid}/reject", json={"actor": "owner-1"})
    assert r.status_code == 422
    # reject with reason -> rejected
    r = client.post(f"/api/v1/owner/queue/{qid}/reject",
                    json={"actor": "owner-1", "reason": "scope too broad"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    # rejected quote never appears for the client as approved
    post = client.get(f"/api/v1/quotes/{qid}", headers={"X-Tenant-Id": TENANT_A})
    assert post.json()["client_visible"] is False

    # owner edits -> returns to pending for re-approval
    r = client.post(f"/api/v1/owner/queue/{qid}/edit",
                    json={"actor": "owner-1", "amount_usd": 1750.0})
    assert r.status_code == 200
    assert r.json()["status"] == "pending_owner"
    assert r.json()["amount_usd"] == 1750.0
