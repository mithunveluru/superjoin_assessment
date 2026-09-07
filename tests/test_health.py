"""The health endpoint boots the app (lifespan runs init_db) and reports a
working database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.test_db import EXPECTED_TABLES


def test_health_ok(db_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(app) as client:  # context manager -> lifespan -> init_db()
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["database"]["ok"] is True
    assert body["database"]["tables"] == len(EXPECTED_TABLES)
    assert body["database"]["path"].endswith("knowledge.db")
    assert body["llm"]["model"] == "claude-sonnet-5"
    assert body["llm"]["api_key_present"] is False


def test_health_reports_api_key_when_set(db_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-xyz")
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["llm"]["api_key_present"] is True
