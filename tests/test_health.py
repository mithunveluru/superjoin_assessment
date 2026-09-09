"""The health endpoint boots the app (lifespan runs init_db) and reports a
working database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from tests.test_db import EXPECTED_TABLES


def test_health_ok(db_path, monkeypatch):
    # pin model/key-name so the assertions do not depend on the local .env
    monkeypatch.setenv("FKL_LLM_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("FKL_LLM_API_KEY_ENV", "FKL_TEST_ABSENT_KEY")
    monkeypatch.delenv("FKL_TEST_ABSENT_KEY", raising=False)
    get_settings.cache_clear()
    with TestClient(app) as client:  # context manager -> lifespan -> init_db()
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["database"]["ok"] is True
    assert body["database"]["tables"] == len(EXPECTED_TABLES)
    assert body["database"]["path"].endswith("knowledge.db")
    assert body["llm"]["provider"] == "gemini"
    assert body["llm"]["model"] == "gemini-2.5-flash"
    assert body["llm"]["api_key_present"] is False


def test_health_reports_api_key_when_set(db_path, monkeypatch):
    monkeypatch.setenv("FKL_LLM_API_KEY_ENV", "FKL_TEST_KEY")
    monkeypatch.setenv("FKL_TEST_KEY", "not-a-real-key")
    get_settings.cache_clear()
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["llm"]["api_key_present"] is True
