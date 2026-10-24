"""Regenerate the Block B ingest fixtures (small, synthetic, no real data).

Run from ``veritas/``:  python tests/fixtures/make_fixtures.py
The generated files are committed; this script only exists so they can be
rebuilt deterministically.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 1) Semicolon-delimited, latin-1 encoded, aliased headers ("User ID", "User Name").
SEMI_LATIN1 = (
    "User ID;User Name;Role;Event Time;Source IP\n"
    "u-001;André du Plessis;admin;2026-01-01T08:00:00Z;10.0.0.5\n"
    "u-002;Thandi Mokoena;analyst;2026-01-01T09:30:00Z;10.0.0.6\n"
)

# 2) UTF-8 BOM, quoted field containing the delimiter, duplicate + blank headers.
BOM_QUOTED = (
    "\ufeffuser_id,username,username,,amount\n"
    'u-001,"Smith, Jane",jsmith,,120.50\n'
    'u-002,"Nkosi, Sipho",snkosi,,80.00\n'
)

# 3) Ragged CSV: one short row, one row with an extra value.
RAGGED = (
    "user_id,username,role\n"
    "u-001,jsmith,admin\n"
    "u-002,snkosi\n"
    "u-003,tmokoena,analyst,EXTRA\n"
)

# 4) Nested JSON export: records one level below a wrapper object.
NESTED_JSON = {
    "export": {"generated_at": "2026-01-02T00:00:00Z", "system": "synthetic-demo"},
    "data": {
        "items": [
            {"userId": "u-001", "user name": "jsmith", "role": "admin",
             "amount": 120.5, "txn_id": "t-001"},
            {"userId": "u-002", "user name": "snkosi", "role": "analyst",
             "amount": 80.0, "txn_id": "t-002"},
        ]
    },
}

# 5) Top-level array of objects.
ARRAY_JSON = [
    {"user_id": "u-001", "username": "jsmith", "amount": 12.0},
    {"user_id": "u-002", "username": "snkosi", "amount": 7.0},
]

# 6) Newline-delimited JSON.
NDJSON = (
    '{"user_id": "u-001", "username": "jsmith", "amount": 12.0}\n'
    '{"user_id": "u-002", "username": "snkosi", "amount": 7.0}\n'
)


def _xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    empty = wb.active
    empty.title = "Cover"
    empty.append(["Synthetic demo export"])  # single non-tabular cell → not a table

    ledger = wb.create_sheet("Access Ledger")
    ledger.append([])                     # leading blank rows are skipped
    ledger.append([])
    ledger.append(["User ID", "User Name", "Role", "Event Time"])
    ledger.append(["u-001", "jsmith", "admin", "2026-01-01T08:00:00Z"])
    ledger.append(["u-002", "snkosi", "analyst", "2026-01-01T09:30:00Z"])

    extra = wb.create_sheet("Notes")
    extra.append(["note"])
    extra.append(["synthetic fixture, not client data"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    (HERE / "ledger_semicolon_latin1.csv").write_bytes(SEMI_LATIN1.encode("latin-1"))
    (HERE / "ledger_bom_quoted.csv").write_bytes(BOM_QUOTED.encode("utf-8"))
    (HERE / "ledger_ragged.csv").write_text(RAGGED, encoding="utf-8")
    (HERE / "ledger_nested.json").write_text(json.dumps(NESTED_JSON, indent=2), encoding="utf-8")
    (HERE / "ledger_array.json").write_text(json.dumps(ARRAY_JSON, indent=2), encoding="utf-8")
    (HERE / "ledger.ndjson").write_text(NDJSON, encoding="utf-8")
    (HERE / "ledger_multi_sheet.xlsx").write_bytes(_xlsx())
    print("fixtures written to", HERE)


if __name__ == "__main__":
    main()
