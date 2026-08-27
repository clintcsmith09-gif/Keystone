"""Phase 0.4 — Veritas Compliance Report artifact (architecture §8).
Covers: §8 JSON schema + summary-count correctness, one-source-of-truth
Markdown rendering, idempotent regeneration, encrypted on-completion storage,
free re-download, and tenant-isolated download endpoints (client A cannot fetch
client B's report; owner sees all).
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
db_gated = pytest.mark.skipif(not DB_URL, reason="VERITAS_TEST_DATABASE_URL not set")

from app.audit import report as report_mod  # noqa: E402
from app.audit.report import build_report, to_markdown  # noqa: E402

TENANT_A = "7f9d1b3e-0000-4000-8000-0000000000aa"
TENANT_B = "7f9d1b3e-0000-4000-8000-0000000000bb"
CSV = (
    b"user_id,username,role,amount,currency,card_number,expiry,status,timestamp\n"
    b"1,alice,admin,42,USD,4111111111111111,12/26,active,2026-01-01T10:00:00\n"
    b"2,bob,analyst,-5,USD,,,active,2026-01-01T11:00:00\n"
)
STORAGE_ROOT = "/tmp/veritas-report-test-storage"


def _sample_findings() -> list[dict]:
    return [
        {"rule_id": "ISO-1", "severity": "high", "status": "failed",
         "evidence": {"missing_columns": ["tax_id"]}, "llm_judgment": None,
         "recommendation": "Required columns missing"},
        {"rule_id": "ISO-2", "severity": "medium", "status": "passed",
         "evidence": {"present_columns": ["user_id"], "checked": 10},
         "llm_judgment": None, "recommendation": None},
        {"rule_id": "ISO-3", "severity": "low", "status": "needs_review",
         "evidence": {"judgment": True},
         "llm_judgment": {"model_id": "noop-llm", "model_version": "0.1.0",
                          "tokens_in": 5, "tokens_out": 2},
         "recommendation": "Requires review"},
    ]


def _sample_report() -> dict:
    return build_report(
        run_id="00000000-0000-4000-8000-000000000001",
        standard="ISO-27001",
        rule_set_version=1,
        generated_at="2026-08-17T00:00:00+00:00",
        model_versions={"report_synthesizer": {"model_id": "noop-llm",
                                               "model_version": "0.1.0"}},
        results=_sample_findings(),
    )


# --- §8 JSON schema + summary correctness (pure, no DB) -----------------------
def test_report_schema_fields_and_types():
    r = _sample_report()
    assert r["report_version"] == "v0.1"
    assert r["schema_version"] == 1
    # fixed top-level keys from §8
    for key in ("run_id", "standard", "rule_set_version", "generated_at",
                "model_versions", "summary", "findings",
                "data_quality_notes", "artifacts"):
        assert key in r
    assert isinstance(r["findings"], list)
    assert isinstance(r["data_quality_notes"], list)
    assert isinstance(r["artifacts"], list)
    assert isinstance(r["model_versions"], dict)
    # finding shape
    for f in r["findings"]:
        assert set(f) == {"rule_id", "severity", "status", "evidence",
                          "recommendation", "llm_judgment"}


def test_report_summary_counts_match_findings():
    r = _sample_report()
    from app.audit.report import _STATUS_KEYS
    for key in _STATUS_KEYS:
        expected = sum(1 for f in _sample_findings() if f["status"] == key)
        assert r["summary"][key] == expected, key
    assert r["summary"]["total"] == len(_sample_findings())


def test_report_evidence_is_dictionary():
    for f in _sample_report()["findings"]:
        assert isinstance(f["evidence"], dict)


def test_report_optional_llm_judgment_only_when_present():
    r = _sample_report()
    by_rule = {f["rule_id"]: f for f in r["findings"]}
    # needs_review finding had a judgment; others did not
    assert "llm_judgment" in by_rule["ISO-3"]
    assert by_rule["ISO-1"]["llm_judgment"] is None


# --- Markdown rendering (pure, no DB) ----------------------------------------
def test_markdown_renders_report_faithfully():
    r = _sample_report()
    md = to_markdown(r)
    assert md.startswith("# Veritas Compliance Report v0.1")
    assert r["run_id"] in md
    assert r["standard"] in md
    assert "rule set v1" in md
    for f in r["findings"]:
        assert f["rule_id"] in md
        assert f["severity"] in md
        assert f["status"] in md
    # summary counts appear in the table
    for k, v in r["summary"].items():
        assert f"| {k} | {v} |" in md


# --- DB-gated integration: storage, idempotency, endpoints, isolation --------
@db_gated
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


@db_gated
def _clear_tables():
    with psycopg.connect(DB_URL) as conn:
        for t in ("findings", "quotes", "audit_steps", "audit_jobs",
                  "audit_runs", "uploads"):
            conn.execute(f"DELETE FROM {t}")


@db_gated
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


@db_gated
async def _seed_upload(settings, tenant):
    from app.storage import get_storage
    from app.uploads.service import insert_upload
    storage = get_storage(settings)
    key = secrets.token_hex(16)
    storage.put(key, CSV, content_type="text/csv")
    uid = await insert_upload(
        settings, tenant_id=tenant, filename="ledger.csv", size_bytes=len(CSV),
        sha256=hashlib.sha256(CSV).hexdigest(), content_type="text/csv",
        storage_key=key, status="stored",
    )
    return uid


@db_gated
async def _run_audit(settings, tenant):
    from app.audit.repo import create_run
    from app.audit.pipeline import process_run
    uid = await _seed_upload(settings, tenant)
    run_id = await create_run(settings, tenant_id=tenant, upload_id=uid,
                              standard="ISO-27001", rule_set_version=1)
    result = await process_run(settings, run_id)
    assert result["status"] == "completed"
    return run_id


@db_gated
def test_full_run_stores_valid_encrypted_report_and_idempotent_get():
    from app.audit.pipeline import process_run
    from app.storage import get_storage
    settings = _env_settings()
    _clear_tables()
    run_id = asyncio.run(_run_audit(settings, TENANT_A))

    storage = get_storage(settings)
    raw = storage.get(report_mod.report_artifact_key(run_id))
    assert raw, "report artifact must be stored encrypted by completion"
    report = json.loads(raw.decode("utf-8"))
    assert report["run_id"] == run_id
    assert report["standard"] == "ISO-27001"
    assert report["findings"]

    # idempotent regeneration: re-running from the same stored findings yields
    # the same deterministic body (summary + findings identical).
    from app.audit import repo
    from app.audit.rules import get_ruleset
    from app.audit.llm import get_llm
    findings = asyncio.run(repo.get_findings(settings, run_id))
    rs = get_ruleset("ISO-27001")
    llm = get_llm()
    regen = asyncio.run(report_mod.synthesize(rs, findings, llm, run_id=run_id))
    assert regen["findings"] == report["findings"]
    assert regen["summary"] == report["summary"]

    # process_run is re-runnable: succeeded stages are skipped, report intact
    result2 = asyncio.run(process_run(settings, run_id))
    assert result2["status"] == "completed"
    raw2 = storage.get(report_mod.report_artifact_key(run_id))
    assert json.loads(raw2.decode("utf-8")) == report


@db_gated
def test_report_endpoint_json_and_md_and_owner():
    os.environ["VERITAS_DATABASE_URL"] = DB_URL
    os.environ["VERITAS_STORAGE_ROOT"] = STORAGE_ROOT
    from app.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    _clear_tables()
    run_id = asyncio.run(_run_audit(_env_settings(), TENANT_A))

    # JSON download
    r = client.get(f"/api/v1/audits/{run_id}/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == run_id
    assert body["report_version"] == "v0.1"
    assert body["findings"]

    # Markdown download — one source, two renderings (no drift)
    m = client.get(f"/api/v1/audits/{run_id}/report?format=md")
    assert m.status_code == 200
    assert m.headers["content-type"].startswith("text/markdown")
    assert run_id in m.text
    assert "# Veritas Compliance Report" in m.text
    assert str(body["summary"]["total"]) in m.text

    # unknown format rejected
    bad = client.get(f"/api/v1/audits/{run_id}/report?format=xml")
    assert bad.status_code == 422

    # free re-download returns identical artifact
    r2 = client.get(f"/api/v1/audits/{run_id}/report")
    assert r2.json() == body


@db_gated
def test_tenant_isolation_report_and_trail():
    os.environ["VERITAS_DATABASE_URL"] = DB_URL
    os.environ["VERITAS_STORAGE_ROOT"] = STORAGE_ROOT
    from app.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    _clear_tables()
    run_a = asyncio.run(_run_audit(_env_settings(), TENANT_A))
    run_b = asyncio.run(_run_audit(_env_settings(), TENANT_B))
    assert run_a != run_b

    # owner (no tenant header) sees both
    assert client.get(f"/api/v1/audits/{run_a}/report").status_code == 200
    assert client.get(f"/api/v1/audits/{run_b}/report").status_code == 200

    # each client sees only its own report
    ok = client.get(f"/api/v1/audits/{run_a}/report",
                    headers={"X-Tenant-Id": TENANT_A})
    assert ok.status_code == 200
    assert ok.json()["run_id"] == run_a

    # client B cannot fetch client A's report (nor its trail)
    denied = client.get(f"/api/v1/audits/{run_a}/report",
                        headers={"X-Tenant-Id": TENANT_B})
    assert denied.status_code == 404
    trail_denied = client.get(f"/api/v1/audits/{run_a}/trail",
                              headers={"X-Tenant-Id": TENANT_B})
    assert trail_denied.status_code == 404

    # own tenant can read trail; trail surface is consistent with run
    trail = client.get(f"/api/v1/audits/{run_b}/trail",
                       headers={"X-Tenant-Id": TENANT_B})
    assert trail.status_code == 200
    tb = trail.json()
    assert tb["run"]["run_id"] == run_b
    assert len(tb["steps"]) == 4
    assert tb["findings"]

    # owner reads all trails
    assert client.get(f"/api/v1/audits/{run_a}/trail").status_code == 200
    assert client.get(f"/api/v1/audits/{run_b}/trail").status_code == 200
