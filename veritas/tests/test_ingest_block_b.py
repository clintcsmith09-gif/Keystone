"""Block B part 1 — ingestion robustness tests.

Covers, in order:
  1. format expansion (CSV dialects/encodings/BOM/header variation, XLSX sheets,
     JSON shapes incl. NDJSON) against committed synthetic fixtures;
  2. structured, human-readable errors (file/sheet/row/column/value/rule);
  3. per-rule-set ingest profiles (ISO 27001 vs PCI-DSS) and alias mapping;
  4. the preview (read-only) and accept (persisted) API paths.

Sections 1–3 need no database. Section 4 is DB-gated exactly like
``test_uploads.py`` (skipped unless VERITAS_TEST_DATABASE_URL is set).
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from app.audit import normalize as n
from app.audit import profiles as p
from app.uploads.errors import make_error, summarise

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DB_URL = os.environ.get("VERITAS_TEST_DATABASE_URL", "")


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _view(name: str, standard: str = "ISO-27001", options=None) -> dict:
    kind = n.kind_from_path(name)
    profile = p.load_profile(standard)
    opts = options if options is not None else (profile.options if profile else None)
    return n.normalize(_load(name), kind=kind, source=name, options=opts)


# --- 1. format expansion ------------------------------------------------------
def test_csv_semicolon_latin1_detected_and_aliased():
    profile = p.load_profile("ISO-27001")
    view = _view("ledger_semicolon_latin1.csv")
    assert view["encoding"] == "latin-1"
    assert view["delimiter"] == ";"
    assert view["row_count"] == 2
    # "User ID"/"User Name"/"Event Time"/"Source IP" map onto canonical fields.
    view, report = p.apply_profile(view, profile, file="ledger_semicolon_latin1.csv")
    assert view["columns"] == ["user_id", "username", "role", "timestamp", "source_ip"]
    assert report["renamed"]["User ID"] == "user_id"
    assert "André" in view["rows"][0]["username"]  # latin-1 decoded, not mangled
    assert any(i["code"] == "encoding_fallback" for i in view["issues"])


def test_csv_bom_quoted_duplicate_and_blank_headers():
    view = _view("ledger_bom_quoted.csv")
    assert view["encoding"] == "utf-8-sig"
    assert view["columns"] == ["user_id", "username", "username_2", "column_4", "amount"]
    codes = [i["code"] for i in view["issues"]]
    assert "duplicate_header" in codes and "blank_header" in codes
    # A quoted field containing the delimiter stays one value.
    assert view["rows"][0]["username"] == "Smith, Jane"
    dup = next(i for i in view["issues"] if i["code"] == "duplicate_header")
    assert dup["row"] == 1 and dup["column"] == "username" and dup["file"]


def test_csv_ragged_rows_are_flagged_not_silently_reshaped():
    view = _view("ledger_ragged.csv")
    ragged = [i for i in view["issues"] if i["code"] == "ragged_row"]
    assert len(ragged) == 2
    assert {i["row"] for i in ragged} == {3, 4}
    assert view["rows"][1]["role"] is None       # short row padded with empty
    assert view["rows"][2]["role"] == "analyst"  # extra value ignored, flagged


def test_json_nested_and_array_and_ndjson_shapes():
    nested = _view("ledger_nested.json")
    assert nested["row_count"] == 2
    assert nested["columns"] == ["userId", "user name", "role", "amount", "txn_id"]
    array = _view("ledger_array.json")
    assert array["row_count"] == 2 and array["columns"][0] == "user_id"
    nd = _view("ledger.ndjson")
    assert nd["row_count"] == 2 and nd["columns"] == ["user_id", "username", "amount"]
    assert n.kind_from_path("ledger.ndjson") == "json"


def test_xlsx_picks_tabular_sheet_and_skips_blank_rows():
    view = _view("ledger_multi_sheet.xlsx")
    assert view["sheet"] == "Access Ledger"
    assert view["header_row"] == 3
    assert view["row_count"] == 2
    assert view["columns"] == ["User ID", "User Name", "Role", "Event Time"]
    profile = p.load_profile("ISO-27001")
    view, report = p.apply_profile(view, profile, file="ledger_multi_sheet.xlsx")
    assert report["renamed"]["User ID"] == "user_id"


def test_xlsx_explicit_sheet_and_unknown_sheet_error():
    view = _view("ledger_multi_sheet.xlsx", options={"sheet": "Notes"})
    assert view["sheet"] == "Notes" and view["columns"] == ["note"]
    with pytest.raises(n.NormalizeError) as exc:
        _view("ledger_multi_sheet.xlsx", options={"sheet": "Missing"})
    err = exc.value.errors[0]
    assert err["code"] == "no_tabular_data"
    assert err["sheet"] == "Missing" and "Cover" in (err["hint"] or "")


def test_unsupported_extension_raises_structured_error():
    with pytest.raises(n.NormalizeError) as exc:
        n.kind_from_path("ledger.xls")
    assert exc.value.errors[0]["code"] == "unsupported_type"
    assert ".xls" in exc.value.errors[0]["message"]


# --- 2. human-readable structured errors --------------------------------------
def test_error_object_shape_is_stable():
    err = make_error("missing_required_field", "Row 3: user_id is empty",
                     file="ledger.csv", sheet="Sheet1", row=3, column="user_id",
                     value="", rule="ISO-27001.ingest.required_fields", hint="Fill it")
    assert set(err) == {"code", "message", "file", "sheet", "row", "column",
                        "value", "rule", "hint"}
    assert summarise([err]).startswith("ledger.csv, sheet 'Sheet1', row 3, column 'user_id':")


def test_malformed_json_error_names_file_and_position():
    with pytest.raises(n.NormalizeError) as exc:
        n.normalize(b'{"records": [{"a": 1},]}', kind="json", source="broken.json")
    err = exc.value.errors[0]
    assert err["file"] == "broken.json"
    assert err["code"] == "parse_error"
    assert "valid JSON" in err["message"]
    assert "Traceback" not in err["message"]


def test_validate_view_reports_row_column_value_and_rule():
    profile = p.load_profile("ISO-27001")
    view = {"columns": ["user_id", "username"], "header_row": 1,
            "rows": [{"user_id": "u-1", "username": "a"},
                     {"user_id": "  ", "username": "b"}]}
    errors = p.validate_view(view, profile, file="ledger.csv")
    assert len(errors) == 1
    err = errors[0]
    assert (err["code"], err["row"], err["column"], err["rule"]) == (
        "missing_required_field", 3, "user_id", "ISO-27001.ingest.required_fields")
    assert err["value"] == "  " and err["file"] == "ledger.csv"


# --- 3. normalization profiles ------------------------------------------------
def test_profiles_exist_per_rule_set_and_differ():
    iso = p.load_profile("ISO-27001")
    pci = p.load_profile("PCI-DSS")
    assert iso is not None and pci is not None
    assert iso.standard == "ISO-27001" and pci.standard == "PCI-DSS"
    assert iso.required == ("user_id",)
    assert pci.required == ("transaction_id",)
    assert "card_number" in pci.canonical and "card_number" not in iso.canonical
    assert pci.version >= 1 and iso.options.get("header_case_insensitive") is True
    assert p.profile_from_dict("X", None) is None  # absent block = old behaviour


def test_alias_mapping_is_case_insensitive_and_never_overwrites_canonical():
    profile = p.load_profile("ISO-27001")
    view = {"columns": ["userId", "user_id", "User Name"],
            "rows": [{"userId": "u-1", "user_id": "u-2", "User Name": "jsmith"}]}
    view, report = p.apply_profile(view, profile, file="x.csv")
    assert view["columns"] == ["userId", "user_id", "username"]
    assert view["rows"][0]["username"] == "jsmith"
    assert any(i["code"] == "ambiguous_alias" for i in report["issues"])
    assert "user_id" in report["fields_matched"]


def test_missing_required_field_is_reported_with_rule_and_hint():
    profile = p.load_profile("PCI-DSS")
    view = {"columns": ["amount", "currency"], "rows": [{"amount": 1, "currency": "ZAR"}]}
    view, report = p.apply_profile(view, profile, file="pci.csv")
    err = next(i for i in report["issues"] if i["code"] == "missing_required_field")
    assert err["column"] == "transaction_id"
    assert err["rule"] == "PCI-DSS.ingest.required_fields"
    assert err["hint"]


# --- 4. preview / accept API (DB-gated) ---------------------------------------
pytestmark_db = pytest.mark.skipif(not DB_URL, reason="VERITAS_TEST_DATABASE_URL not set")
TENANT = "7f9d1b3e-0000-4000-8000-00000000000b"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _preview(client, name="ledger_semicolon_latin1.csv", data=None,
             content_type="text/csv", standard="ISO-27001"):
    return client.post(
        "/api/v1/uploads/preview",
        data={"standard": standard},
        files={"file": (name, data if data is not None else _load(name), content_type)},
    )


@pytestmark_db
def test_preview_parses_without_persisting(client):
    from app.config import get_settings
    settings = get_settings()
    import psycopg
    with psycopg.connect(settings.database_url) as conn:
        before = conn.execute("SELECT count(*) FROM uploads").fetchone()[0]
    resp = _preview(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["preview"] is True and body["persisted"] is False
    assert body["row_count"] == 2 and body["format"] == "csv"
    assert body["fields_matched"]["user_id"] == "User ID"
    assert body["encoding"] == "latin-1" and body["delimiter"] == ";"
    assert body["sample_rows"] and body["standard"] == "ISO-27001"
    with psycopg.connect(settings.database_url) as conn:
        after = conn.execute("SELECT count(*) FROM uploads").fetchone()[0]
    assert before == after


@pytestmark_db
def test_preview_reports_structured_errors_for_bad_input(client):
    resp = _preview(client, name="broken.json", data=b'{"records": [}', content_type="application/json")
    assert resp.status_code == 422
    err = resp.json()["errors"][0]
    assert err["file"] == "broken.json" and err["code"] == "parse_error"
    assert resp.json()["detail"]


@pytestmark_db
def test_preview_rejects_unsupported_type_with_errors(client):
    resp = _preview(client, name="ledger.xls", data=b"junk", content_type="application/vnd.ms-excel")
    assert resp.status_code == 415
    assert resp.json()["errors"][0]["code"] == "unsupported_type"


@pytestmark_db
def test_preview_xlsx_and_pci_profile(client):
    resp = _preview(client, name="ledger_multi_sheet.xlsx", data=_load("ledger_multi_sheet.xlsx"),
                    content_type=XLSX_TYPE, standard="PCI-DSS")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sheet"] == "Access Ledger" and body["row_count"] == 2
    assert body["standard"] == "PCI-DSS"
    assert "transaction_id" in body["fields_missing"]
    assert any(e["code"] == "missing_required_field" for e in body["errors"])


@pytestmark_db
def test_accept_runs_persisted_ingest(client):
    up = client.post(
        "/api/v1/uploads",
        data={"tenant_id": TENANT},
        files={"file": ("ledger.csv", b"user_id,username,role\nu-1,jsmith,admin\n",
                        "text/csv")},
    )
    assert up.status_code == 201, up.text
    upload_id = up.json()["upload_id"]
    resp = client.post(
        f"/api/v1/uploads/{upload_id}/accept",
        json={"tenant_id": TENANT, "standard": "ISO-27001"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["run"]["status"] == "completed"
    assert body["findings"] and body["steps"]
    # Re-accepting the same upload is still the existing 409 behaviour.
    again = client.post(
        f"/api/v1/uploads/{upload_id}/accept",
        json={"tenant_id": TENANT, "standard": "ISO-27001"},
    )
    assert again.status_code == 409
