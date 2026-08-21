"""Upload validation gates 1 & 2 (architecture §6.2).

Gate 1 — type allowlist: filename extension + declared Content-Type must both be
on the allowlist and agree with each other. Everything else (macros, executables,
archives) is rejected before any further processing.

Gate 2 — content sniffing: the actual bytes must match the declared type. CSV and
JSON are text formats without magic numbers, so their sniff is structural (valid
UTF-8, no NUL bytes; JSON must start with ``{``/``[``). XLSX and Parquet have
real magic bytes. A mismatch means the client lied about the type → reject.

Both gates run on bounded input only (filename/headers, and the first 8 KiB of
the file) — never the whole file in RAM.
"""
from __future__ import annotations

import re
from pathlib import Path

# --- gate 1: allowlists -------------------------------------------------------

# Canonical Content-Type per allowed extension. A declared type must match the
# extension's expected type; parquet additionally accepts application/octet-stream
# because that is what many parquet-producing clients send (the magic bytes in
# gate 2 then confirm it really is parquet).
ALLOWED_TYPES: dict[str, tuple[str, ...]] = {
    ".csv": ("text/csv",),
    ".json": ("application/json",),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
    ".parquet": ("application/vnd.apache.parquet", "application/octet-stream"),
}

_SAFE_FILENAME_RE = re.compile(r"^[^/\x00\\]+$")


class ValidationError(Exception):
    """Rejection with a client-safe reason. ``http_status`` is the suggested code."""

    def __init__(self, reason: str, http_status: int = 415) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


def check_type_allowlist(filename: str, content_type: str | None) -> str:
    """Gate 1. Returns the canonical extension (e.g. ``.csv``) or raises."""
    if not _SAFE_FILENAME_RE.match(filename):
        raise ValidationError("filename contains path separators or control characters")
    ext = Path(filename).suffix.lower()
    allowed = ALLOWED_TYPES.get(ext)
    if allowed is None:
        raise ValidationError(
            f"unsupported file type {ext or '(no extension)'}: only CSV, XLSX, JSON "
            "and Parquet are accepted (macros, executables and archives are rejected)"
        )
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared not in allowed:
        raise ValidationError(
            f"content-type {declared or '(missing)'} does not match a {ext} file"
        )
    return ext


# --- gate 2: content sniffing -------------------------------------------------

_SNIFF_BYTES = 8 * 1024
_ZIP_MAGIC = b"PK\x03\x04"
_PARQUET_MAGIC = b"PAR1"


def sniff_content(path: Path, ext: str) -> None:
    """Gate 2. Raise ValidationError if the file's bytes contradict ``ext``."""
    with path.open("rb") as fh:
        head = fh.read(_SNIFF_BYTES)

    if ext == ".csv":
        _sniff_text_csv(head)
    elif ext == ".json":
        _sniff_json(head)
    elif ext == ".xlsx":
        if not head.startswith(_ZIP_MAGIC):
            raise ValidationError(
                "file content is not an XLSX workbook (missing zip/xlsx signature)"
            )
    elif ext == ".parquet":
        if not head.startswith(_PARQUET_MAGIC):
            raise ValidationError(
                "file content is not a Parquet file (missing PAR1 signature)"
            )
    else:  # pragma: no cover - gate 1 prevents this
        raise ValidationError("unrecognized extension")


def _sniff_text_csv(head: bytes) -> None:
    # CSV is plain text: must decode as UTF-8 and contain no NUL bytes (binary
    # formats such as PNG/GIF/JPEG/ZIP all carry NULs in their first bytes).
    if b"\x00" in head:
        raise ValidationError("file content is not text (NUL bytes found); expected a CSV")
    if head.startswith(_ZIP_MAGIC) or head.startswith(_PARQUET_MAGIC):
        raise ValidationError("file content is not a CSV (binary container signature found)")
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("file content is not valid UTF-8 text; expected a CSV") from exc


def _sniff_json(head: bytes) -> None:
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")  # tolerate a UTF-8 BOM
    if not stripped or stripped[:1] not in (b"{", b"["):
        raise ValidationError("file content does not look like JSON (must start with { or [)")
    if b"\x00" in head:
        raise ValidationError("file content is not text (NUL bytes found); expected JSON")
