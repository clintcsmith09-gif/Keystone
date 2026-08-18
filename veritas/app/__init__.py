"""Veritas AI Audit Engine — MVP service (Phase 0 foundation).

Per the ratified Sprint 2 architecture (/home/team/shared/sprint2-architecture.md):
ONE FastAPI service, Postgres job queue (no Redis), local encrypted object storage
behind a StorageBackend interface. No LLM calls, no third-party API spend, no heavy
deps at this stage.
"""
