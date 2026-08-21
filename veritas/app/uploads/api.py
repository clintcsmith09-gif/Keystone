"""Upload drop zone API — POST /api/v1/uploads and GET /api/v1/uploads/{id}.

Implements the ratified upload/ingest path (architecture §5, §6):
  * streaming multipart ingest with a hard size cap enforced ON the stream,
  * three server-side validation gates (allowlist → magic bytes → sandboxed
    safe parse), never trusting the client,
  * envelope-encrypted storage at rest via the existing StorageBackend,
  * uploads metadata row with the §4.2 state machine
    uploaded → validating → storing → stored | rejected_upload
    (audit-run states are Phase 0.3+ and out of scope here).

Transport-level rejections (malformed multipart, wrong type, oversized) return
4xx with no DB row; quarantined files (spoofed content, failed parse) create a
rejected_upload row so GET can report why.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..storage import get_storage
from . import multipart, validation
from .service import (
    get_upload,
    insert_upload,
    new_storage_key,
    run_safe_parse,
    set_upload_status,
    stream_sha256,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])


def _rejected_body(upload_id: str, filename: str, reason: str) -> dict:
    return {
        "upload_id": upload_id,
        "status": "rejected_upload",
        "filename": filename,
        "reject_reason": reason,
    }


@router.post("", status_code=201)
async def create_upload(request: Request) -> JSONResponse:
    settings = get_settings()

    # Fast pre-check when the client declares a Content-Length (chunked uploads
    # are still enforced by the streaming parser below).
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > settings.upload_max_bytes:
        return JSONResponse(
            {"detail": f"upload exceeds the {settings.upload_max_bytes} byte limit"},
            status_code=413,
        )

    try:
        boundary = multipart.parse_boundary(request.headers.get("content-type"))
    except multipart.MalformedMultipart as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    try:
        parts = await multipart.parse_parts(
            request.stream(),
            boundary,
            max_file_bytes=settings.upload_max_bytes,
        )
    except multipart.OversizedUpload as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    except multipart.MalformedMultipart as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    fields = {
        p.name: (p.value or b"").decode("utf-8", "replace")
        for p in parts
        if p.value is not None
    }
    file_parts = [p for p in parts if p.temp_path is not None]
    if not file_parts:
        return JSONResponse({"detail": "no file part in upload"}, status_code=400)
    part = file_parts[0]
    if part.name != "file":
        return JSONResponse(
            {"detail": "file part must be named 'file'"}, status_code=400
        )
    tmp = Path(part.temp_path)

    try:
        # tenant_id is a required form field at MVP (no auth session yet).
        try:
            tenant_id = str(uuid.UUID(fields.get("tenant_id", "").strip()))
        except ValueError:
            return JSONResponse({"detail": "tenant_id must be a UUID"}, status_code=422)
        if not part.filename:
            return JSONResponse({"detail": "file part is missing a filename"}, status_code=400)

        # --- Gate 1: type allowlist (never trust the client) ------------------
        try:
            ext = validation.check_type_allowlist(part.filename, part.content_type)
        except validation.ValidationError as exc:
            return JSONResponse({"detail": exc.reason}, status_code=exc.http_status)

        # Give the spooled temp file the real extension: openpyxl (gate 3)
        # infers the workbook format from the filename suffix.
        if tmp.suffix.lower() != ext:
            renamed = tmp.with_suffix(ext)
            os.replace(tmp, renamed)
            tmp = renamed

        size_bytes = tmp.stat().st_size
        sha256 = await run_in_threadpool(stream_sha256, tmp)
        storage_key = new_storage_key()

        upload_id = await insert_upload(
            settings,
            tenant_id=tenant_id,
            filename=part.filename,
            size_bytes=size_bytes,
            sha256=sha256,
            content_type=(part.content_type or "application/octet-stream"),
            storage_key=storage_key,
            status="uploaded",
        )

        # --- Gate 2: magic bytes / content sniffing ---------------------------
        await set_upload_status(settings, upload_id, "validating")
        try:
            validation.sniff_content(tmp, ext)
        except validation.ValidationError as exc:
            await set_upload_status(settings, upload_id, "rejected_upload", exc.reason)
            return JSONResponse(
                _rejected_body(upload_id, part.filename, exc.reason), status_code=201
            )

        # --- Gate 3: safe parse in a sandboxed subprocess ---------------------
        ok, message = await run_in_threadpool(run_safe_parse, ext.lstrip("."), tmp, settings)
        if not ok:
            await set_upload_status(settings, upload_id, "rejected_upload", message)
            return JSONResponse(
                _rejected_body(upload_id, part.filename, message), status_code=201
            )

        # --- Store: encrypt at rest via the existing StorageBackend -----------
        await set_upload_status(settings, upload_id, "storing")
        storage = get_storage(settings)

        def _store() -> None:
            with tmp.open("rb") as fh:
                storage.put(storage_key, fh.read(), content_type=part.content_type or "application/octet-stream")

        await run_in_threadpool(_store)
        await set_upload_status(settings, upload_id, "stored")
        return JSONResponse(
            {
                "upload_id": upload_id,
                "status": "stored",
                "filename": part.filename,
                "size_bytes": size_bytes,
                "sha256": sha256,
            },
            status_code=201,
        )
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/{upload_id}")
async def upload_status(upload_id: uuid.UUID) -> JSONResponse:
    settings = get_settings()
    rec = await get_upload(settings, str(upload_id))
    if rec is None:
        return JSONResponse({"detail": "upload not found"}, status_code=404)
    return JSONResponse(rec, status_code=200)
