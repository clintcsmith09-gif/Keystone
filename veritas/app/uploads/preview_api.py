"""Upload preview / accept API (Block B part 1).

Two additive endpoints that let a client show the operator what ingestion WILL
do before anything is persisted:

  POST /api/v1/uploads/preview
      Parses the uploaded bytes read-only, applies the rule set's ingest profile
      in memory and returns what normalization would do: the format/encoding
      detected, fields matched to canonical names, row and column counts, issues
      and structured, human-readable validation errors. NOTHING is written —
      no uploads row, no storage object, no audit run.

  POST /api/v1/uploads/{upload_id}/accept
      The confirm step. Runs the EXISTING persisted ingest path (the same
      handler as POST /api/v1/uploads/{upload_id}/audit) on an already-stored
      upload, so behaviour is identical; the response body additionally carries
      ``"accepted": true``.

Both reuse the existing validation gates and normalizer — this module adds no
new parsing rules of its own.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..audit import normalize as normalize_mod
from ..audit import profiles as profiles_mod
from ..audit.api import start_audit
from ..config import get_settings
from . import multipart, validation
from .errors import make_error, summarise

router = APIRouter(prefix="/uploads", tags=["ingest"])

SAMPLE_ROWS = 5


def _bad_request(errors: list[dict], status_code: int) -> JSONResponse:
    return JSONResponse(
        {"detail": summarise(errors), "errors": errors}, status_code=status_code
    )


def _profile_or_error(standard: str):
    profile = profiles_mod.load_profile(standard)
    if profile is None:
        return None, make_error(
            "unsupported_type",
            f"No ingest profile is defined for standard '{standard}'.",
            rule="ingest.profile",
            hint="Use a standard that has a rule set, e.g. ISO-27001 or PCI-DSS.",
        )
    return profile, None


@router.post("/preview", status_code=200)
async def preview_upload(request: Request) -> JSONResponse:
    settings = get_settings()

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > settings.upload_max_bytes:
        return _bad_request([make_error(
            "oversized_upload",
            f"The upload is larger than the {settings.upload_max_bytes} byte limit.",
            rule="ingest.size",
            hint="Split the export into smaller files.",
        )], 413)

    try:
        boundary = multipart.parse_boundary(request.headers.get("content-type"))
        parts = await multipart.parse_parts(
            request.stream(), boundary, max_file_bytes=settings.upload_max_bytes
        )
    except multipart.OversizedUpload as exc:
        return _bad_request([make_error(
            "oversized_upload", str(exc), rule="ingest.size",
        )], 413)
    except multipart.MalformedMultipart as exc:
        return _bad_request([make_error(
            "malformed_multipart", str(exc), rule="ingest.transport",
        )], 400)

    fields = {p.name: (p.value or b"").decode("utf-8", "replace")
              for p in parts if p.value is not None}
    file_parts = [p for p in parts if p.temp_path is not None]
    if not file_parts:
        return _bad_request([make_error(
            "malformed_multipart", "No file part in the upload.",
            rule="ingest.transport", hint="Send the file as a part named 'file'.",
        )], 400)
    part = file_parts[0]
    if part.name != "file":
        return _bad_request([make_error(
            "malformed_multipart", "The file part must be named 'file'.",
            rule="ingest.transport",
        )], 400)
    if not part.filename:
        return _bad_request([make_error(
            "malformed_multipart", "The file part is missing a filename.",
            rule="ingest.transport",
        )], 400)

    tmp = Path(part.temp_path)
    try:
        try:
            ext = validation.check_type_allowlist(part.filename, part.content_type)
        except validation.ValidationError as exc:
            return _bad_request([make_error(
                "unsupported_type" if exc.http_status == 415 else "type_mismatch",
                exc.reason, file=part.filename, rule="ingest.format",
            )], exc.http_status)

        try:
            validation.sniff_content(tmp, ext)
        except validation.ValidationError as exc:
            return _bad_request([make_error(
                "not_text" if "text" in exc.reason else "type_mismatch",
                exc.reason, file=part.filename, rule="ingest.format",
            )], 422)

        standard = (fields.get("standard") or "ISO-27001").strip()
        profile, profile_error = _profile_or_error(standard)
        if profile_error is not None:
            return _bad_request([profile_error], 422)

        raw = await run_in_threadpool(tmp.read_bytes)
        kind = ext.lstrip(".")
        try:
            view = normalize_mod.normalize(
                raw, kind=kind, source=part.filename, options=profile.options
            )
        except normalize_mod.NormalizeError as exc:
            return _bad_request(exc.errors, 422)

        view, report = profiles_mod.apply_profile(
            view, profile, file=part.filename
        )
        errors = profiles_mod.validate_view(view, profile, file=part.filename)
        rows = view.get("rows") or []
        body = {
            "preview": True,
            "persisted": False,
            "filename": part.filename,
            "format": kind,
            "standard": profile.standard,
            "profile_version": profile.version,
            "row_count": len(rows),
            "column_count": len(view.get("columns") or []),
            "columns": view.get("columns") or [],
            "fields_matched": report["fields_matched"],
            "fields_missing": report["missing_required"],
            "unmatched_columns": report["unmatched_source_columns"],
            "renamed_columns": report["renamed"],
            "encoding": view.get("encoding"),
            "delimiter": view.get("delimiter"),
            "sheet": view.get("sheet"),
            "issues": view.get("issues") or [],
            "errors": errors,
            "sample_rows": rows[:SAMPLE_ROWS],
        }
        return JSONResponse(body, status_code=200)
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/{upload_id}/accept", status_code=201)
async def accept_upload(
    upload_id: uuid.UUID,
    payload: dict = Body(default={"standard": "ISO-27001"}),
) -> JSONResponse:
    """Confirm a previewed upload by running the existing persisted audit path."""
    response = await start_audit(upload_id, payload)
    if response.status_code != 201:
        return response
    try:
        body = json.loads(response.body)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return response
    body["accepted"] = True
    return JSONResponse(body, status_code=response.status_code)
