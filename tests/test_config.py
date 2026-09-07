"""Configuration loads sane defaults, honours env overrides, and resolves the
LLM API key from the named external variable (never a FKL_ one)."""

from __future__ import annotations

from app.config import Settings, get_settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("FKL_LLM_MODEL", raising=False)
    monkeypatch.delenv("FKL_RETRIEVAL_TOP_K", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.llm_provider == "anthropic"
    assert s.llm_model == "claude-sonnet-5"
    assert s.llm_temperature == 0.0
    assert s.retrieval_top_k == 15
    assert s.fy_convention_default == "apr-mar"
    # retrieval weights are a starting point, documented to sum to 1.0
    assert abs(
        s.retrieval_weight_embedding
        + s.retrieval_weight_predicate
        + s.retrieval_weight_bm25
        - 1.0
    ) < 1e-9


def test_env_override(monkeypatch):
    monkeypatch.setenv("FKL_RETRIEVAL_TOP_K", "3")
    monkeypatch.setenv("FKL_LLM_MODEL", "claude-opus-5")
    get_settings.cache_clear()
    s = get_settings()
    assert s.retrieval_top_k == 3
    assert s.llm_model == "claude-opus-5"
    get_settings.cache_clear()


def test_llm_api_key_resolution(monkeypatch):
    s = Settings(llm_api_key_env="ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert s.llm_api_key() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    assert s.llm_api_key() == "sk-test-123"


def test_api_key_var_is_configurable(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "abc")
    s = Settings(llm_api_key_env="MY_CUSTOM_KEY")
    assert s.llm_api_key() == "abc"
