"""Structured, human-readable ingest errors (Block B).

One error object shape is used by every ingest path (upload gates, normalize,
normalization profiles and the preview endpoint) so the API can return
machine-readable errors that a client can render without parsing prose.

Shape (every key is always present; unknown values are ``None``)::

    {
      "code": "missing_required_field",   # stable, machine-readable
      "message": "Row 4: required field 'user_id' is empty",  # plain language
      "file": "ledger.csv",               # the uploaded file name
      "sheet": None,                       # XLSX sheet, else None
      "row": 4,                            # 1-based data row (header = row 1)
      "column": "user_id",                 # column name (or header cell)
      "value": "",                         # offending value, truncated
      "rule": "ISO-27001.ingest.required_fields",  # the rule/profile check
      "hint": "Map 'principal' to user_id in the ingest profile."
    }

Nothing here raises raw exceptions to a caller: parse errors are converted into
these objects with :func:`from_exception`, which keeps the original exception
text out of client responses while recording a safe, specific message.

This module is additive: the pre-existing rejection shapes (``detail`` and
``reject_reason``) are unchanged; new responses additionally carry ``errors``.
"""
from __future__ import annotations

from typing import Any

MAX_VALUE_CHARS = 120

# Stable error codes (documented in docs/ingestion.md).
CODES = (
    "unsupported_type",
    "type_mismatch",
    "malformed_multipart",
    "oversized_upload",
    "not_text",
    "parse_error",
    "empty_file",
    "no_tabular_data",
    "duplicate_header",
    "blank_header",
    "ragged_row",
    "missing_required_field",
    "unknown_field",
    "ambiguous_alias",
    "encoding_fallback",
    "empty_sheet",
)


def _short(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS] + "…"
    return text


def make_error(
    code: str,
    message: str,
    *,
    file: str | None = None,
    sheet: str | None = None,
    row: int | None = None,
    column: str | None = None,
    value: Any = None,
    rule: str | None = None,
    hint: str | None = None,
) -> dict:
    """Build one structured error object (all keys always present)."""
    return {
        "code": code,
        "message": message,
        "file": file,
        "sheet": sheet,
        "row": row,
        "column": column,
        "value": _short(value),
        "rule": rule,
        "hint": hint,
    }


def from_exception(
    exc: BaseException,
    *,
    code: str = "parse_error",
    file: str | None = None,
    sheet: str | None = None,
    row: int | None = None,
    column: str | None = None,
    rule: str | None = None,
) -> dict:
    """Convert an exception into a safe error object.

    The exception class name and message are used to build a plain-language
    message; no traceback, module path or internal object repr is exposed.
    """
    detail = str(exc).strip() or exc.__class__.__name__
    name = exc.__class__.__name__
    if name == "JSONDecodeError":
        message = f"the file is not valid JSON: {detail}"
        code = "parse_error"
    elif name == "UnicodeDecodeError":
        message = "the file could not be decoded as text (unsupported character encoding)"
        code = "not_text"
    elif name == "csv.Error":
        message = f"the file is not valid delimited text: {detail}"
        code = "parse_error"
    else:
        message = f"the file could not be read: {detail}"
    return make_error(
        code, message, file=file, sheet=sheet, row=row, column=column, rule=rule
    )


def summarise(errors: list[dict]) -> str:
    """One-line human summary of a list of error objects (for ``detail``)."""
    if not errors:
        return "no errors"
    first = errors[0]
    where = []
    if first.get("file"):
        where.append(str(first["file"]))
    if first.get("sheet"):
        where.append(f"sheet {first['sheet']!r}")
    if first.get("row") is not None:
        where.append(f"row {first['row']}")
    if first.get("column"):
        where.append(f"column {first['column']!r}")
    location = ", ".join(where)
    prefix = f"{location}: " if location else ""
    extra = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{prefix}{first['message']}{extra}"
