"""Smoke tests: the app imports and /health responds."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_responds():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "veritas"
    assert body["version"]


def test_unknown_route_404():
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
