"""End-to-end API tests for the upload drop zone (architecture §6).

Covers the happy path for every allowed format, each rejection gate (wrong
type, spoofed magic bytes, oversized, malformed), the status machine via GET,
and encryption-at-rest round-tripping through the API.

DB-gated: skipped unless VERITAS_TEST_DATABASE_URL is set (a scratch database;
the fixture applies migrations and wipes the uploads table + storage objects).
"""
from __future__ import annotations

import hashlib
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

DB_URL = os.environ.get("VERITAS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DB_URL, reason="VERITAS_TEST_DATABASE_URL not set")

TENANT = "7f9d1b3e-0000-4000-8000-000000000001"
CSV_BYTES = b"date,amount\n2026-01-01,42\n2026-01-02,7\n"
JSON_BYTES = b'{"ledger": [{"date": "2026-01-01", "amount": 42}]}'


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["date", "amount"])
    ws.append(["2026-01-01", 42])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parquet_bytes() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"date": ["2026-01-01"], "amount": [42]})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


@pytest.fixture(scope="module", autouse=True)
def _schema_and_storage():
    """Apply migrations on the scratch DB and clear the shared storage root."""
    from scripts.migrate import run

    assert run(DB_URL) == 0, "migrations failed"
    root = Path(os.environ.get("VERITAS_STORAGE_ROOT", "/tmp/veritas-test-storage"))
    root.mkdir(parents=True, exist_ok=True)
    for f in root.iterdir():
        if f.is_file():
            f.unlink()
    yield


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def created_upload_ids():
    """Collect upload ids created by a test; teardown removes rows + objects."""
    ids: list[str] = []
    yield ids
    from app.storage import get_storage

    storage = get_storage()
    with psycopg.connect(DB_URL) as conn:
        for uid in ids:
            row = conn.execute(
                "SELECT storage_key FROM uploads WHERE id = %s", (uid,)
            ).fetchone()
            if row:
                storage.delete(row[0])
            conn.execute("DELETE FROM uploads WHERE id = %s", (uid,))


def _upload(client, *, filename="ledger.csv", content_type="text/csv",
            data=CSV_BYTES, tenant=TENANT, extra_files=None):
    files = {"file": (filename, data, content_type)}
    if extra_files:
        files.update(extra_files)
    return client.post(
        "/api/v1/uploads", data={"tenant_id": tenant}, files=files
    )


def _row(uid: str):
    with psycopg.connect(DB_URL) as conn:
        rec = conn.execute(
            "SELECT tenant_id, filename, size_bytes, sha256, content_type, "
            "storage_key, status, retention_until, reject_reason "
            "FROM uploads WHERE id = %s",
            (uid,),
        ).fetchone()
    return rec


# --- happy paths --------------------------------------------------------------

@pytest.mark.parametrize(
    "filename,content_type,payload",
    [
        ("ledger.csv", "text/csv", CSV_BYTES),
        ("ledger.json", "application/json", JSON_BYTES),
        ("ledger.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", None),
        ("ledger.parquet", "application/vnd.apache.parquet", None),
    ],
)
def test_happy_path_stored(client, created_upload_ids, filename, content_type, payload):
    if filename.endswith(".xlsx"):
        payload = _xlsx_bytes()
    elif filename.endswith(".parquet"):
        payload = _parquet_bytes()

    resp = _upload(client, filename=filename, content_type=content_type, data=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "stored", f"unexpected status: {body!r}"
    uid = body["upload_id"]
    created_upload_ids.append(uid)
    assert body["size_bytes"] == len(payload)
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()

    # GET reflects the terminal state with full metadata.
    got = client.get(f"/api/v1/uploads/{uid}").json()
    assert got["status"] == "stored"
    assert got["filename"] == filename
    assert got["tenant_id"] == TENANT
    assert got["size_bytes"] == len(payload)
    assert got["reject_reason"] is None

    # DB row: opaque storage key, retention ~30 days out (§10.4).
    rec = _row(uid)
    assert str(rec[0]) == TENANT  # psycopg returns uuid.UUID for uuid columns
    assert rec[1] == filename
    assert rec[3] == hashlib.sha256(payload).hexdigest()
    assert rec[5] != filename  # storage_key is opaque, never the filename
    assert rec[6] == "stored"
    retention = rec[7]
    assert retention.tzinfo is not None
    delta = retention - datetime.now(timezone.utc)
    assert timedelta(days=29) < delta < timedelta(days=31)


def test_encryption_at_rest_round_trip(client, created_upload_ids):
    resp = _upload(client)
    assert resp.status_code == 201
    uid = resp.json()["upload_id"]
    created_upload_ids.append(uid)
    rec = _row(uid)
    storage_key = rec[5]

    from app.storage import get_storage

    storage = get_storage()
    # Round trip: what we stored is exactly what we uploaded.
    assert storage.get(storage_key) == CSV_BYTES
    # At rest: ciphertext only — VRT1 envelope header, no plaintext anywhere.
    raw = (storage.root / storage_key).read_bytes()
    assert raw.startswith(b"VRT1")
    assert CSV_BYTES not in raw


def test_rejected_upload_has_no_object(client, created_upload_ids):
    """Quarantined files must never be written to storage (§6.2)."""
    from app.storage import get_storage

    storage = get_storage()
    resp = _upload(
        client, filename="ledger.csv", content_type="text/csv",
        data=b"\x89PNG\r\n\x1a\n\x00\x00\x00 not a csv",
    )
    assert resp.status_code == 201
    uid = resp.json()["upload_id"]
    created_upload_ids.append(uid)
    rec = _row(uid)
    assert rec[6] == "rejected_upload"
    assert not storage.exists(rec[5]), "rejected upload must not be stored"


# --- gate 1: type allowlist ---------------------------------------------------

@pytest.mark.parametrize(
    "filename,content_type",
    [
        ("evil.exe", "application/x-msdownload"),
        ("macro.xlsm", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("archive.zip", "application/zip"),
        ("noextension", "text/csv"),
        ("script.py", "text/x-python"),
    ],
)
def test_gate1_wrong_type_rejected(client, filename, content_type):
    resp = _upload(client, filename=filename, content_type=content_type, data=b"junk")
    assert resp.status_code == 415, resp.text
    assert "only CSV, XLSX, JSON" in resp.json()["detail"]


def test_gate1_content_type_mismatch_rejected(client):
    """Declared Content-Type must match the extension's allowlisted type."""
    resp = _upload(client, filename="ledger.csv", content_type="application/json", data=CSV_BYTES)
    assert resp.status_code == 415, resp.text
    assert "does not match" in resp.json()["detail"]


def test_gate1_missing_content_type_rejected(client):
    """A file part without a Content-Type header is strictly rejected."""
    boundary = "XbndY01z"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="tenant_id"\r\n\r\n'
        f"{TENANT}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="ledger.csv"\r\n'
        f"\r\n"  # no Content-Type header on this part
    ).encode() + CSV_BYTES + f"\r\n--{boundary}--\r\n".encode()
    resp = client.post(
        "/api/v1/uploads",
        content=body,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert resp.status_code == 415


# --- gate 2: magic bytes / content sniffing -----------------------------------

def test_gate2_spoofed_csv_rejected(client, created_upload_ids):
    """A .csv that is actually a PNG must be quarantined."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"fake image payload"
    resp = _upload(client, filename="ledger.csv", content_type="text/csv", data=png)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "rejected_upload"
    assert "NUL" in body["reject_reason"]
    created_upload_ids.append(body["upload_id"])
    got = client.get(f"/api/v1/uploads/{body['upload_id']}").json()
    assert got["status"] == "rejected_upload"
    assert got["reject_reason"] == body["reject_reason"]


def test_gate2_spoofed_xlsx_rejected(client, created_upload_ids):
    """A .xlsx that is plain text (not a zip) must be quarantined."""
    resp = _upload(
        client,
        filename="ledger.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=b"this is definitely not a zip archive",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "rejected_upload"
    assert "not an XLSX" in body["reject_reason"]
    created_upload_ids.append(body["upload_id"])


def test_gate2_spoofed_parquet_rejected(client, created_upload_ids):
    resp = _upload(
        client,
        filename="ledger.parquet",
        content_type="application/vnd.apache.parquet",
        data=b'{"not": "parquet"}',
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "rejected_upload"
    assert "PAR1" in body["reject_reason"]
    created_upload_ids.append(body["upload_id"])


# --- gate 3: parse safety -----------------------------------------------------

def test_gate3_malformed_json_rejected(client, created_upload_ids):
    resp = _upload(
        client, filename="data.json", content_type="application/json",
        data=b'{"broken": ',
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "rejected_upload"
    assert "safety parse failed" in body["reject_reason"]
    created_upload_ids.append(body["upload_id"])


def test_gate3_malformed_csv_rejected(client, created_upload_ids):
    """CSV valid in the sniff window but corrupt later must fail the parse gate."""
    payload = b"date,amount\n" + b"2026-01-01,42\n" * 1000 + b"\xff\xfe not utf-8\n"
    resp = _upload(client, filename="broken.csv", content_type="text/csv", data=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "rejected_upload"
    assert "safety parse failed" in body["reject_reason"]
    created_upload_ids.append(body["upload_id"])


# --- size cap -----------------------------------------------------------------

def test_oversized_rejected(client):
    from app.config import get_settings

    settings = get_settings()
    original = settings.upload_max_bytes
    settings.upload_max_bytes = 1024
    try:
        resp = _upload(client, data=b"x" * 2048)
        assert resp.status_code == 413, resp.text
        assert "limit" in resp.json()["detail"]
    finally:
        settings.upload_max_bytes = original


def test_oversized_rejected_via_content_length(client):
    """A declared Content-Length over the cap is rejected before any read."""
    from app.config import get_settings

    settings = get_settings()
    original = settings.upload_max_bytes
    settings.upload_max_bytes = 1024
    try:
        resp = client.post(
            "/api/v1/uploads",
            data={"tenant_id": TENANT},
            files={"file": ("big.csv", b"y" * 4096, "text/csv")},
        )
        assert resp.status_code == 413
    finally:
        settings.upload_max_bytes = original


# --- request-level rejects ----------------------------------------------------

def test_missing_tenant_id_422(client):
    resp = client.post(
        "/api/v1/uploads",
        files={"file": ("ledger.csv", CSV_BYTES, "text/csv")},
    )
    assert resp.status_code == 422


def test_invalid_tenant_id_422(client):
    resp = _upload(client, tenant="not-a-uuid")
    assert resp.status_code == 422


def test_not_multipart_400(client):
    resp = client.post(
        "/api/v1/uploads",
        content="application/json",
        json={"tenant_id": TENANT},
    )
    assert resp.status_code == 400


def test_no_file_part_400(client):
    resp = client.post(
        "/api/v1/uploads",
        data={"tenant_id": TENANT},
        files={"not_the_file": ("ledger.csv", CSV_BYTES, "text/csv")},
    )
    assert resp.status_code == 400


def test_two_file_parts_400(client):
    resp = client.post(
        "/api/v1/uploads",
        data={"tenant_id": TENANT},
        files=[
            ("file", ("a.csv", CSV_BYTES, "text/csv")),
            ("file", ("b.csv", b"x,y\n", "text/csv")),
        ],
    )
    assert resp.status_code == 400


# --- GET ----------------------------------------------------------------------

def test_get_unknown_upload_404(client):
    resp = client.get("/api/v1/uploads/00000000-0000-4000-8000-000000000000")
    assert resp.status_code == 404


def test_get_malformed_uuid_422(client):
    resp = client.get("/api/v1/uploads/not-a-uuid")
    assert resp.status_code == 422
