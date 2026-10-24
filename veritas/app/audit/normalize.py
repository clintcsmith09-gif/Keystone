"""Stage 1 — Ingest/Normalizer (architecture §7.1).

Deterministic data work: reads the stored raw upload bytes, parses the supported
formats (CSV / JSON / XLSX / Parquet) into a single consistent tabular view, and
writes it as a versioned JSON artifact. This is the ONLY stage that touches the
raw payload. No LLM is involved (llm_assist=False); the output is fully
reproducible so downstream stages can be re-run without re-ingesting.

Block B additions (all additive — existing keys/behaviour are preserved):
  * tolerant text decoding: utf-8-sig (BOM) → utf-16 (BOM) → latin-1 fallback,
    with the encoding actually used recorded on the view;
  * tolerant CSV dialects: delimiter/quotechar can be supplied (ingest profile)
    or are sniffed from the sample; blank leading lines, BOMs, whitespace,
    duplicate and blank headers are handled deterministically;
  * XLSX: first non-empty sheet by default, explicit sheet selection, leading
    blank rows skipped, sheet name + header row recorded;
  * JSON: array-of-objects, object-wrapped arrays (top-level or one level of
    nesting), and newline-delimited JSON (NDJSON) exports;
  * structured, human-readable issues (``app.uploads.errors``) for anything the
    parser had to work around, instead of silently mangling data.

Documented in ``veritas/docs/ingestion.md``.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ..uploads.errors import from_exception, make_error

NORMALIZE_TEMPLATE_ID = "ingest-normalize-v1"
AGENT = "ingest_normalizer"

# Top-level keys treated as an envelope around a list of records.
WRAPPER_KEYS = (
    "records", "rows", "ledger", "data", "items", "results", "entries",
    "transactions", "events", "log",
)
CSV_DELIMITERS = ",;\t|"
_SNIFF_CHARS = 8192
_WS_RE = re.compile(r"\s+")


class NormalizeError(Exception):
    """Raised when the payload cannot be normalized into a tabular view.

    Carries structured, client-safe ``errors`` (see ``app.uploads.errors``) so
    callers never have to parse exception prose. ``message`` keeps the legacy
    string form used by existing callers/tests.
    """

    def __init__(self, message: str, *, errors: list[dict] | None = None,
                 code: str = "parse_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.errors = errors or [make_error(code, message)]


# --- text decoding ------------------------------------------------------------
def decode_text(content: bytes) -> tuple[str, str]:
    """Decode bytes tolerantly; returns ``(text, encoding)``.

    Order: utf-8 (BOM-aware) → utf-16 when a UTF-16 BOM is present → latin-1,
    which cannot fail. The caller records the encoding so a fallback is visible
    rather than silent.
    """
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return content.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:  # pragma: no cover - truncated BOM
            pass
    try:
        return content.decode("utf-8-sig"), "utf-8-sig" if content[:3] == b"\xef\xbb\xbf" else "utf-8"
    except UnicodeDecodeError:
        return content.decode("latin-1"), "latin-1"


# --- header / row helpers -----------------------------------------------------
def normalize_headers(header, *, file: str | None = None,
                      sheet: str | None = None) -> tuple[list[str], list[dict]]:
    """Clean header cells; returns ``(columns, issues)``.

    * BOMs, surrounding whitespace and repeated inner whitespace are stripped;
    * blank/None headers become ``column_<n>`` (1-based);
    * duplicate headers get a deterministic ``_2``, ``_3`` … suffix instead of
      silently overwriting the earlier column (the old DictReader behaviour).
    """
    columns: list[str] = []
    issues: list[dict] = []
    seen: dict[str, int] = {}
    for i, cell in enumerate(header):
        name = "" if cell is None else str(cell).strip().lstrip("\ufeff")
        name = _WS_RE.sub(" ", name)
        if not name:
            name = f"column_{i + 1}"
            issues.append(make_error(
                "blank_header",
                f"The header row has an empty column name at position {i + 1}; "
                f"it was named '{name}' so its values stay addressable.",
                file=file, sheet=sheet, row=1, column=name,
                rule="ingest.header",
                hint="Give the column a name in the export, or map it in the ingest profile.",
            ))
        base = name
        count = seen.get(base, 0) + 1
        seen[base] = count
        if count > 1:
            name = f"{base}_{count}"
            issues.append(make_error(
                "duplicate_header",
                f"The header row repeats the column name '{base}'; the "
                f"duplicate was renamed '{name}' so both columns are kept.",
                file=file, sheet=sheet, row=1, column=base, value=name,
                rule="ingest.header",
                hint="Give each column a unique name to avoid the rename.",
            ))
        columns.append(name)
    return columns, issues


def _is_blank_row(row) -> bool:
    return all(cell is None or (isinstance(cell, str) and not cell.strip()) for cell in row)


def _rows_from_matrix(matrix, columns: list[str], *, file: str | None,
                      sheet: str | None, header_row: int) -> tuple[list[dict], list[dict]]:
    """Turn a row matrix (header already removed) into dicts + issues."""
    rows: list[dict] = []
    issues: list[dict] = []
    for offset, raw in enumerate(matrix):
        row_number = header_row + offset + 1  # 1-based, header row counted
        if _is_blank_row(raw):
            continue
        cells = list(raw)
        if len(cells) > len(columns):
            issues.append(make_error(
                "ragged_row",
                f"Row {row_number} has {len(cells)} values but the header has "
                f"{len(columns)} columns; the extra values were ignored.",
                file=file, sheet=sheet, row=row_number, column=columns[-1] if columns else None,
                value=cells[len(columns)], rule="ingest.shape",
                hint="Check for unquoted delimiters inside a field.",
            ))
        elif 0 < len(cells) < len(columns):
            issues.append(make_error(
                "ragged_row",
                f"Row {row_number} has {len(cells)} values but the header has "
                f"{len(columns)} columns; missing cells were treated as empty.",
                file=file, sheet=sheet, row=row_number,
                column=columns[len(cells)] if len(cells) < len(columns) else None,
                rule="ingest.shape",
                hint="Check for a truncated export or a missing trailing delimiter.",
            ))
        row = {}
        for i, col in enumerate(columns):
            row[col] = cells[i] if i < len(cells) else None
        rows.append(row)
    return rows, issues


# --- CSV ----------------------------------------------------------------------
def detect_dialect(text: str, *, delimiter: str | None = None,
                   quotechar: str | None = None) -> tuple[str, str, bool]:
    """Return ``(delimiter, quotechar, sniffed)`` for a text payload.

    Explicit values (from an ingest profile) always win; otherwise a bounded
    sample is sniffed, falling back to comma/quote.
    """
    if delimiter:
        return delimiter, quotechar or '"', False
    sample = text[:_SNIFF_CHARS]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=CSV_DELIMITERS)
        return dialect.delimiter, quotechar or dialect.quotechar or '"', True
    except csv.Error:
        return ",", quotechar or '"', False


def normalize_csv(content: bytes, *, file: str | None = None,
                  delimiter: str | None = None,
                  quotechar: str | None = None) -> dict:
    """CSV/TSV → rows of dicts (first non-blank row = header)."""
    text, encoding = decode_text(content)
    delim, quote, sniffed = detect_dialect(text, delimiter=delimiter, quotechar=quotechar)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim,
                        quotechar=quote, strict=True)
    matrix = [r for r in reader]
    issues: list[dict] = []
    if encoding == "latin-1":
        issues.append(make_error(
            "encoding_fallback",
            "The file is not valid UTF-8; it was read as latin-1 so accented "
            "characters may be approximate.",
            file=file, rule="ingest.encoding",
            hint="Re-export the file as UTF-8 to preserve characters exactly.",
        ))
    # Drop leading blank lines so a preamble of empty rows does not become the header.
    header_row = 0
    while header_row < len(matrix) and _is_blank_row(matrix[header_row]):
        header_row += 1
    if header_row >= len(matrix):
        raise NormalizeError(
            "the CSV file has no header row", code="no_tabular_data",
            errors=[make_error(
                "no_tabular_data",
                "The file contains no header row or data, so there is nothing to audit.",
                file=file, rule="ingest.shape",
            )],
        )
    columns, header_issues = normalize_headers(matrix[header_row], file=file)
    issues.extend(header_issues)
    rows, row_issues = _rows_from_matrix(
        matrix[header_row + 1:], columns, file=file, sheet=None,
        header_row=header_row + 1,
    )
    issues.extend(row_issues)
    return {
        "columns": columns,
        "rows": rows,
        "meta": {
            "encoding": encoding,
            "delimiter": delim,
            "quotechar": quote,
            "dialect_sniffed": sniffed,
            "header_row": header_row + 1,
            "sheet": None,
        },
        "issues": issues,
    }


# --- JSON ---------------------------------------------------------------------
def _records_from(obj, depth: int = 0) -> list[dict] | None:
    """Find the record list inside a JSON document; None if there is none."""
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if not isinstance(obj, dict):
        return None
    for key in WRAPPER_KEYS:
        value = obj.get(key)
        if isinstance(value, list) and value:
            return [r for r in value if isinstance(r, dict)]
    if depth == 0:
        # One level of nesting: {"data": {"items": [...]}} / {"payload": {"records": [...]}}
        for value in obj.values():
            if isinstance(value, (dict, list)):
                found = _records_from(value, depth + 1)
                if found:
                    return found
    return None


def _parse_ndjson(text: str) -> list[dict] | None:
    """Newline-delimited JSON (one object per line) — common export format."""
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        rows.append(obj)
    return rows or None


def normalize_json(content: bytes, *, file: str | None = None) -> dict:
    text, encoding = decode_text(content)
    issues: list[dict] = []
    container = "array"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        nd = _parse_ndjson(text)
        if nd is None:
            raise NormalizeError(
                f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})",
                code="parse_error",
                errors=[from_exception(exc, file=file)],
            ) from None
        data = nd
        container = "ndjson"
    rows = _records_from(data)
    if rows is None:
        if isinstance(data, dict):
            rows = [data]
            container = "object"
        else:
            raise NormalizeError(
                "JSON root must be an object or an array of objects",
                code="no_tabular_data",
                errors=[make_error(
                    "no_tabular_data",
                    "The JSON document does not contain an array of objects, so "
                    "it cannot be audited as a table.",
                    file=file, rule="ingest.shape",
                    hint="Export records as [{...}, {...}] or wrap them under "
                         "'records', 'rows', 'ledger', 'data' or 'items'.",
                )],
            )
    elif container == "array" and isinstance(data, dict):
        container = "wrapped"
    if encoding == "latin-1":
        issues.append(make_error(
            "encoding_fallback",
            "The file is not valid UTF-8; it was read as latin-1.",
            file=file, rule="ingest.encoding",
        ))
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return {
        "columns": columns,
        "rows": rows,
        "meta": {
            "encoding": encoding,
            "container": container,
            "sheet": None,
            "header_row": None,
            "delimiter": None,
        },
        "issues": issues,
    }


# --- XLSX ---------------------------------------------------------------------
def normalize_xlsx(content: bytes, *, file: str | None = None,
                   sheet: str | None = None) -> dict:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    issues: list[dict] = []
    try:
        if sheet:
            if sheet not in wb.sheetnames:
                raise NormalizeError(
                    f"sheet {sheet!r} not found (available: {wb.sheetnames})",
                    code="no_tabular_data",
                    errors=[make_error(
                        "no_tabular_data",
                        f"The workbook has no sheet named '{sheet}'.",
                        file=file, sheet=sheet, rule="ingest.sheet",
                        hint=f"Available sheets: {', '.join(wb.sheetnames)}.",
                    )],
                )
            candidates = [wb[sheet]]
        else:
            candidates = list(wb.worksheets)
        ws = None
        matrix: list[tuple] = []
        fallback: tuple | None = None
        for candidate in candidates:
            matrix = list(candidate.iter_rows(values_only=True))
            first = next((i for i, r in enumerate(matrix) if not _is_blank_row(r)), None)
            if first is None:
                continue  # empty sheet — never a table
            header_cells = [c for c in matrix[first]
                            if c is not None and str(c).strip()]
            has_data = any(not _is_blank_row(r) for r in matrix[first + 1:])
            if len(header_cells) >= 2 and has_data:
                ws = candidate
                break
            if fallback is None:
                fallback = (candidate, matrix)
        if ws is None and fallback is not None:
            # No clearly tabular sheet: use the first non-empty one as a table.
            ws, matrix = fallback
        if ws is None:
            raise NormalizeError(
                "the workbook contains no data", code="no_tabular_data",
                errors=[make_error(
                    "no_tabular_data",
                    "Every sheet in the workbook is empty, so there is nothing to audit.",
                    file=file, rule="ingest.shape",
                )],
            )
        sheet_name = ws.title
        header_row = 0
        while header_row < len(matrix) and _is_blank_row(matrix[header_row]):
            header_row += 1
        columns, header_issues = normalize_headers(
            matrix[header_row], file=file, sheet=sheet_name
        )
        issues.extend(header_issues)
        rows, row_issues = _rows_from_matrix(
            matrix[header_row + 1:], columns, file=file, sheet=sheet_name,
            header_row=header_row + 1,
        )
        issues.extend(row_issues)
        return {
            "columns": columns,
            "rows": rows,
            "meta": {
                "encoding": None,
                "delimiter": None,
                "sheet": sheet_name,
                "sheet_names": list(wb.sheetnames),
                "header_row": header_row + 1,
            },
            "issues": issues,
        }
    finally:
        wb.close()


# --- Parquet ------------------------------------------------------------------
def normalize_parquet(content: bytes, *, file: str | None = None) -> dict:
    import pyarrow.parquet as pq

    table = pq.read_table(io.BytesIO(content))
    columns = [c for c in table.column_names]
    rows = [dict(zip(columns, r)) for r in table.to_pylist()]
    return {
        "columns": columns,
        "rows": rows,
        "meta": {"encoding": None, "delimiter": None, "sheet": None,
                 "header_row": None},
        "issues": [],
    }


_FORMAT_PARSERS = {
    "csv": normalize_csv,
    "json": normalize_json,
    "xlsx": normalize_xlsx,
    "parquet": normalize_parquet,
}

# Human-readable format names for messages.
FORMAT_LABELS = {"csv": "CSV", "json": "JSON", "xlsx": "XLSX", "parquet": "Parquet"}


def parse(content: bytes, *, kind: str, file: str | None = None,
          options: dict | None = None) -> dict:
    """Parse a payload into ``{columns, rows, meta, issues}`` without wrapping.

    ``options`` are the CSV/XLSX knobs declared by an ingest profile
    (``delimiter``, ``quotechar``, ``sheet``); unknown keys are ignored so a
    profile can carry future options safely.
    """
    parser = _FORMAT_PARSERS.get(kind)
    if parser is None:
        raise NormalizeError(
            f"unsupported format: {kind!r}", code="unsupported_type",
            errors=[make_error(
                "unsupported_type",
                f"Files of type '{kind}' cannot be read. Supported formats are "
                "CSV, JSON, XLSX and Parquet.",
                file=file, rule="ingest.format",
            )],
        )
    opts = options or {}
    kwargs: dict = {"file": file}
    if kind in ("csv", "json", "xlsx"):
        for key in ("delimiter", "quotechar", "sheet"):
            if opts.get(key):
                kwargs[key] = opts[key]
    try:
        return parser(content, **kwargs)
    except NormalizeError:
        raise
    except Exception as exc:  # noqa: BLE001 - every parse failure becomes structured
        raise NormalizeError(
            f"could not read the {FORMAT_LABELS.get(kind, kind)} file",
            code="parse_error",
            errors=[from_exception(exc, file=file)],
        ) from None


def normalize(content: bytes, *, kind: str, source: str | None = None,
              options: dict | None = None) -> dict:
    """Produce the normalized single-table view for a raw payload.

    Additive keys (Block B): ``encoding``, ``delimiter``, ``sheet``,
    ``header_row``, ``issues``. Existing keys are unchanged.
    """
    parsed = parse(content, kind=kind, file=source, options=options)
    rows = parsed["rows"]
    meta = parsed.get("meta") or {}
    result = {
        "table": "ledger",
        "columns": parsed["columns"],
        "rows": rows,
        "row_count": len(rows),
        "source": source or "upload",
        "format": kind,
        "schema_version": 1,
        # --- Block B additions -------------------------------------------------
        "encoding": meta.get("encoding"),
        "delimiter": meta.get("delimiter"),
        "sheet": meta.get("sheet"),
        "header_row": meta.get("header_row"),
        "issues": parsed.get("issues") or [],
    }
    return result


# Newline-delimited JSON is parsed by the JSON parser (it detects NDJSON itself).
_EXT_ALIASES = {"ndjson": "json"}


def kind_from_path(filename: str) -> str:
    """Map an upload filename to the normalize kind (same allowlist as gate 1)."""
    suffix = Path(filename).suffix.lower().lstrip(".")
    suffix = _EXT_ALIASES.get(suffix, suffix)
    if suffix not in _FORMAT_PARSERS:
        raise NormalizeError(
            f"unsupported upload type: {suffix!r}", code="unsupported_type",
            errors=[make_error(
                "unsupported_type",
                f"Files ending in '.{suffix}' cannot be read. Supported formats "
                "are .csv, .json, .xlsx and .parquet.",
                file=filename, rule="ingest.format",
            )],
        )
    return suffix
