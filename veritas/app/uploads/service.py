"""Upload orchestration helpers (architecture §6): sandboxed parse gate runner
and the uploads-row database operations. The endpoint in ``api.py`` drives the
state machine; these helpers do the heavy lifting without touching HTTP.
"""
from __future__ import annotations

import hashlib
import resource
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg

from ..config import Settings

# veritas/ directory — lets the child subprocess resolve `app.safe_parse`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def new_storage_key() -> str:
    """Random opaque storage key (architecture §6.3: never the filename)."""
    return str(uuid4())


def stream_sha256(path: Path) -> str:
    """SHA-256 of a file without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_safe_parse(kind: str, path: Path, settings: Settings) -> tuple[bool, str]:
    """Run the parse-safety gate (architecture §6.2) in a sandboxed subprocess.

    The child gets hard RLIMIT_AS (memory) and RLIMIT_CPU limits plus a
    wall-clock timeout backstop, so a hostile or pathological file cannot consume
    the service's memory or CPU. Returns ``(ok, message)``; the caller quarantines
    the upload (``status=rejected_upload``) unless ``ok``.
    """
    mem_bytes = settings.parse_memory_limit_mb * 1024 * 1024
    cpu_seconds = settings.parse_cpu_seconds

    def _set_limits() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.uploads.safe_parse", kind, str(path)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_set_limits,
            timeout=cpu_seconds + 30,
        )
    except subprocess.TimeoutExpired:
        return False, "safety parse over-ran the CPU time limit"
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return False, f"safety parse failed: {detail or f'exit code {proc.returncode}'}"


# --- uploads rows -------------------------------------------------------------

async def _connect(settings: Settings):
    return await psycopg.AsyncConnection.connect(settings.database_url)


async def insert_upload(
    settings: Settings,
    *,
    tenant_id: str,
    filename: str,
    size_bytes: int,
    sha256: str,
    content_type: str,
    storage_key: str,
    status: str,
) -> str:
    """Insert an uploads row; returns the new upload id (uuid string)."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            INSERT INTO uploads
                (tenant_id, filename, size_bytes, sha256, content_type, storage_key,
                 status, retention_until)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    now() + make_interval(days => %s))
            RETURNING id
            """,
            (
                tenant_id,
                filename,
                size_bytes,
                sha256,
                content_type,
                storage_key,
                status,
                settings.retention_days,
            ),
        )
        return str((await row.fetchone())[0])


async def set_upload_status(
    settings: Settings, upload_id: str, status: str, reject_reason: str | None = None
) -> None:
    """Transition an upload's status (state machine per §6.2/§4.2)."""
    async with await _connect(settings) as conn:
        await conn.execute(
            "UPDATE uploads SET status = %s, reject_reason = %s WHERE id = %s",
            (status, reject_reason, upload_id),
        )


async def get_upload(settings: Settings, upload_id: str) -> dict | None:
    """Fetch upload metadata for GET /uploads/{id}; None if it does not exist."""
    async with await _connect(settings) as conn:
        row = await conn.execute(
            """
            SELECT id, tenant_id, filename, size_bytes, sha256, content_type,
                   status, uploaded_at, retention_until, reject_reason
            FROM uploads WHERE id = %s
            """,
            (upload_id,),
        )
        rec = await row.fetchone()
    if rec is None:
        return None
    return {
        "upload_id": str(rec[0]),
        "tenant_id": str(rec[1]),
        "filename": rec[2],
        "size_bytes": rec[3],
        "sha256": rec[4],
        "content_type": rec[5],
        "status": rec[6],
        "uploaded_at": rec[7].isoformat() if rec[7] else None,
        "retention_until": rec[8].isoformat() if rec[8] else None,
        "reject_reason": rec[9],
    }
