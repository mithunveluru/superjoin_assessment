"""Configuration loads sane defaults, honours env overrides, and resolves the
LLM API key from the named external variable (never a FKL_ one)."""

from __future__ import annotations

from app import config
from app.config import Settings, get_settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("FKL_LLM_MODEL", raising=False)
    monkeypatch.delenv("FKL_RETRIEVAL_TOP_K", raising=False)
    get_settings.cache_clear()
    # _env_file=None: assert the declared defaults, not whatever the developer's
    # local .env happens to select (a real .env may switch provider/model).
    s = Settings(_env_file=None)
    assert s.llm_provider == "gemini"
    assert s.llm_model == "gemini-2.5-flash"
    assert s.llm_api_key_env == "GEMINI_API_KEY"
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
    # a name neither the environment nor the project .env defines
    s = Settings(llm_api_key_env="FKL_TEST_ABSENT_KEY")
    monkeypatch.delenv("FKL_TEST_ABSENT_KEY", raising=False)
    assert s.llm_api_key() is None
    monkeypatch.setenv("FKL_TEST_ABSENT_KEY", "sk-test-123")
    assert s.llm_api_key() == "sk-test-123"


def test_llm_api_key_falls_back_to_dotenv_and_env_wins(monkeypatch):
    """.env is a fallback source for the key; a real env var always beats it."""
    monkeypatch.setattr(config, "_dotenv_values", lambda: {"SOME_KEY": "from-dotenv"})
    s = Settings(llm_api_key_env="SOME_KEY")
    monkeypatch.delenv("SOME_KEY", raising=False)
    assert s.llm_api_key() == "from-dotenv"
    monkeypatch.setenv("SOME_KEY", "from-env")
    assert s.llm_api_key() == "from-env"


def test_api_key_var_is_configurable(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "abc")
    s = Settings(llm_api_key_env="MY_CUSTOM_KEY")
    assert s.llm_api_key() == "abc"
