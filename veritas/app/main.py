"""FastAPI application entry point (Phase 0 foundation).

Run (dev):
    cd veritas
    uvicorn app.main:app --reload

/health is a liveness + lightweight readiness check: it always returns 200 with
service metadata, and reports database reachability when configured (24/7 uptime
monitoring requirement, architecture §13 Q2 — the monitoring probe can consume this).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI

from .config import get_settings
from .storage import get_storage

VERSION = "0.1.0"
SERVICE = "veritas"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Storage root must exist before the pipeline writes anything; the encrypted
    # backend creates it on construction (fails fast if master key is missing).
    get_storage(settings)
    yield


app = FastAPI(
    title="Veritas AI Audit Engine",
    description="Automated, auditable compliance audits (MVP service).",
    version=VERSION,
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    body: dict = {
        "status": "ok",
        "service": SERVICE,
        "version": VERSION,
        "environment": settings.environment,
    }
    try:
        async with await psycopg.AsyncConnection.connect(
            settings.database_url, connect_timeout=2
        ) as conn:
            await conn.execute("SELECT 1")
        body["database"] = "ok"
    except Exception:
        # Liveness stays 200; readiness detail reports the outage. The monitor
        # (architecture §13 Q2) alerts on database != ok.
        body["database"] = "unavailable"
    return body
