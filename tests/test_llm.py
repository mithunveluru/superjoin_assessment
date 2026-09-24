"""The Gemini provider adapter.

Offline and deterministic: no network, no real key, no SDK calls. Covers the
three pieces of ``app/llm.py`` that are pure functions of configuration and of
one provider reply — which transport gets built, the JSON-Schema dialect Gemini
is handed, and the conversion of a Gemini response into the project's internal
schemas (including every malformed / failed shape).
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.llm import (
    EXTRACTION_JSON_SCHEMA,
    EntityConfirmer,
    Extractor,
    LLMConfigError,
    RelationshipConfirmer,
    _gemini_schema,
    _new_transport,
    _Reply,
)

FAKE_KEY_VAR = "FKL_TEST_KEY"


def _settings(**kw) -> Settings:
    kw.setdefault("llm_api_key_env", FAKE_KEY_VAR)
    return Settings(_env_file=None, **kw)


@pytest.fixture
def fake_key(monkeypatch):
    """A syntactically fine, non-functional key. Never a real one."""
    monkeypatch.setenv(FAKE_KEY_VAR, "not-a-real-key")
    return FAKE_KEY_VAR


# --------------------------------------------------------------------------- #
# provider selection                                                         #
# --------------------------------------------------------------------------- #
def test_missing_key_names_the_variable(monkeypatch):
    monkeypatch.delenv(FAKE_KEY_VAR, raising=False)
    with pytest.raises(LLMConfigError) as ei:
        _new_transport(_settings())
    assert FAKE_KEY_VAR in str(ei.value)
    assert "gemini" in str(ei.value)


def test_non_gemini_provider_is_rejected(fake_key):
    with pytest.raises(LLMConfigError) as ei:
        _new_transport(_settings(llm_provider="anthropic"))
    assert "anthropic" in str(ei.value) and "gemini" in str(ei.value)


def test_gemini_transport_builds_with_a_fake_key(fake_key):
    t = _new_transport(_settings())
    assert type(t).__name__ == "_GeminiTransport"
    assert hasattr(t, "complete")


@pytest.mark.parametrize("client_cls", [Extractor, EntityConfirmer, RelationshipConfirmer])
def test_every_client_goes_through_the_gemini_transport(fake_key, client_cls):
    """Extractor / entity confirmer / relationship confirmer share one adapter."""
    c = client_cls(_settings())
    assert type(c._transport).__name__ == "_GeminiTransport"
    assert c.model == "gemini-3.6-flash"


# --------------------------------------------------------------------------- #
# gemini schema dialect                                                      #
# --------------------------------------------------------------------------- #
def test_nullable_union_becomes_nullable_flag():
    assert _gemini_schema({"type": ["string", "null"]}) == {"type": "string", "nullable": True}


def test_null_enum_member_becomes_nullable_not_a_value():
    out = _gemini_schema({"type": ["string", "null"], "enum": ["a", "b", None]})
    assert out["enum"] == ["a", "b"]
    assert out["nullable"] is True


def test_additional_properties_is_dropped_everywhere():
    out = _gemini_schema(EXTRACTION_JSON_SCHEMA)
    dumped = repr(out)
    assert "additionalProperties" not in dumped
    assert '"null"' not in dumped and "'null'" not in dumped
    assert out["required"] == ["facts"]
    item = out["properties"]["facts"]["items"]
    assert item["properties"]["char_start"] == {"type": "integer"}
    assert item["properties"]["raw_value_text"] == {"type": "string", "nullable": True}


# --------------------------------------------------------------------------- #
# reply -> internal schema (the adapter boundary)                            #
# --------------------------------------------------------------------------- #
class StubTransport:
    """Stands in for _GeminiTransport: returns a scripted _Reply."""

    def __init__(self, reply: _Reply):
        self.reply = reply
        self.calls = 0

    def complete(self, **kw):
        self.calls += 1
        return self.reply


def _client(cls, reply, fake_key):
    c = cls(_settings())
    c._transport = StubTransport(reply)
    return c


GOOD_EXTRACTION = (
    '{"facts": [{"subject": "Acme", "predicate": "revenue", "object": "1 crore",'
    ' "fact_type": "numeric", "quote": "revenue was 1 crore",'
    ' "char_start": 0, "char_end": 19}]}'
)


def test_extractor_converts_a_gemini_reply_into_LLMExtraction(fake_key):
    e = _client(Extractor, _Reply(text=GOOD_EXTRACTION, stop="STOP",
                                  input_tokens=11, output_tokens=7), fake_key)
    out = e.extract("revenue was 1 crore", "Doc: test")
    assert out.error_code is None
    assert out.parsed is not None and len(out.parsed.facts) == 1
    assert out.parsed.facts[0].subject == "Acme"
    assert out.raw_text == GOOD_EXTRACTION          # raw response preserved verbatim
    assert (out.input_tokens, out.output_tokens) == (11, 7)
    assert out.model == "gemini-3.6-flash"


@pytest.mark.parametrize(
    ("reply", "expected_code"),
    [
        (_Reply(text="not json at all"), "malformed_response"),
        (_Reply(text='{"facts": [{"subject": "only"}]}'), "malformed_response"),
        (_Reply(text=None), "malformed_response"),
        (_Reply(text="{}", stop="max_tokens"), "truncated_response"),
        (_Reply(error_code="auth", error_detail="401"), "auth"),
        (_Reply(error_code="rate_limit", error_detail="429"), "rate_limit"),
        (_Reply(error_code="timeout", error_detail="deadline"), "timeout"),
        (_Reply(error_code="api_error", error_detail="500"), "api_error"),
        (_Reply(error_code="refusal", error_detail="blocked"), "refusal"),
    ],
)
def test_extractor_handles_every_bad_reply_without_raising(fake_key, reply, expected_code):
    e = _client(Extractor, reply, fake_key)
    out = e.extract("chunk", "Doc: test")
    assert out.error_code == expected_code
    assert out.parsed is None                       # never a half-parsed fact


def test_entity_confirmer_converts_a_gemini_reply(fake_key):
    c = _client(EntityConfirmer, _Reply(text='{"same": true, "confidence": 0.9,'
                                             ' "canonical_label": "Acme Ltd"}'), fake_key)
    out = c.confirm_entities(["Acme", "Acme Limited"])
    assert out.same is True and out.confidence == 0.9
    assert out.canonical_label == "Acme Ltd"
    assert out.error_code is None


@pytest.mark.parametrize("reply", [
    _Reply(text="}{ broken"),
    _Reply(text='{"confidence": 0.9}'),               # missing required "same"
    _Reply(text=None),
    _Reply(error_code="rate_limit", error_detail="429"),
])
def test_entity_confirmer_degrades_to_not_same_on_bad_replies(fake_key, reply):
    c = _client(EntityConfirmer, reply, fake_key)
    out = c.confirm_entities(["A", "B"])
    assert out.same is False                          # never a fabricated merge
    assert out.error_code in {"malformed_response", "api_error"}


def test_relationship_confirmer_converts_a_gemini_reply(fake_key):
    c = _client(RelationshipConfirmer, _Reply(text='{"relationship": "CORROBORATES",'
                                                   ' "confidence": 0.8, "reason": "same measure"}'),
                fake_key)
    out = c.classify_relationship({"a": 1})
    assert out.relationship == "CORROBORATES"
    assert out.confidence == 0.8 and out.reason == "same measure"
    assert out.error_code is None


@pytest.mark.parametrize("reply", [
    _Reply(text="nope"),
    _Reply(text='{"confidence": 0.8}'),               # missing "relationship"
    _Reply(text=None),
    _Reply(error_code="api_error", error_detail="503"),
])
def test_relationship_confirmer_degrades_on_bad_replies(fake_key, reply):
    c = _client(RelationshipConfirmer, reply, fake_key)
    out = c.classify_relationship({"a": 1})
    assert out.error_code in {"malformed_response", "api_error"}
    # no fabricated category: app.reason._validate_proposal rejects anything
    # outside the five, so an errored proposal can never become a relationship
    assert out.relationship not in {
        "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION"}


# --------------------------------------------------------------------------- #
# transport-level failures (never reach the API, so the SDK does not wrap them)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raised", "expected_code"),
    [
        (httpx.ConnectError("[Errno 113] No route to host"), "api_error"),
        (httpx.ReadTimeout("timed out"), "timeout"),
        (httpx.ConnectTimeout("connect timed out"), "timeout"),
        (httpx.RemoteProtocolError("server disconnected"), "api_error"),
    ],
)
def test_network_failures_become_typed_replies_not_exceptions(fake_key, monkeypatch,
                                                              raised, expected_code):
    """A DNS/TCP/TLS failure must degrade to a typed _Reply, exactly like an
    API error — never escape as an opaque client exception."""
    t = _new_transport(_settings())

    class Boom:
        def generate_content(self, **kw):
            raise raised

    monkeypatch.setattr(t, "_client", type("C", (), {"models": Boom()})())
    reply = t.complete(system="s", user="u", schema={"type": "object"}, max_tokens=16)
    assert reply.error_code == expected_code
    assert reply.text is None


def test_transport_caps_thinking_so_the_answer_fits(fake_key, monkeypatch):
    """Thinking tokens share max_output_tokens: uncapped, a thinking model spent
    ~7.9k of 8192 on a one-page chunk and the JSON answer came back truncated."""
    t = _new_transport(_settings())
    sent = {}

    class Capture:
        def generate_content(self, **kw):
            sent.update(kw["config"])
            raise httpx.ConnectError("stop here")

    monkeypatch.setattr(t, "_client", type("C", (), {"models": Capture()})())
    t.complete(system="s", user="u", schema={"type": "object"}, max_tokens=16)
    assert sent["thinking_config"] == {"thinking_level": "LOW"}


# --------------------------------------------------------------------------- #
# prompt versions                                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_every_shipped_extraction_prompt_loads(version):
    from app.llm import load_prompt

    text = load_prompt(version)
    assert "facts" in text and "char_start" in text


def test_default_prompt_version_exists_and_pins_the_subject_rule():
    """v2 is the default; the subject rule is what it exists to fix."""
    from app.llm import load_prompt

    s = Settings(_env_file=None)
    assert s.prompt_version == "v2"
    text = load_prompt(s.prompt_version)
    assert "grammatical subject" in text          # subject != sentence subject
    assert "document artifact" in text            # boilerplate subjects excluded


# --------------------------------------------------------------------------- #
# Groq provider (OpenAI-compatible HTTP; httpx.MockTransport, no network)     #
# --------------------------------------------------------------------------- #
def _groq(monkeypatch, handler, **kw):
    """A Groq transport whose HTTP layer is ``handler`` (request -> Response)."""
    from app.llm import _GroqTransport

    monkeypatch.setenv(FAKE_KEY_VAR, "not-a-real-key")
    monkeypatch.setattr("app.llm.time.sleep", lambda s: None)
    t = _new_transport(_settings(llm_provider="groq", **kw))
    assert isinstance(t, _GroqTransport)
    t._client = httpx.Client(transport=httpx.MockTransport(handler), headers=t._client.headers)
    return t


def _ok(content, finish="stop"):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    })


def test_groq_provider_defaults_model_and_key_variable():
    s = Settings(_env_file=None, llm_provider="groq")
    assert s.llm_model == "openai/gpt-oss-120b"
    assert s.llm_api_key_env == "GROQ_API_KEY"
    # an explicit choice always wins over the provider default
    assert Settings(_env_file=None, llm_provider="groq", llm_model="qwen/x").llm_model == "qwen/x"


def test_groq_request_shape_and_reply_conversion(monkeypatch):
    import json as _json

    sent = {}

    def handler(req):
        sent.update(_json.loads(req.content))
        sent["auth"] = req.headers["authorization"]
        return _ok('{"facts": []}')

    t = _groq(monkeypatch, handler)
    r = t.complete(system="sys", user="usr", schema={"type": "object"}, max_tokens=64)
    assert (r.text, r.stop, r.input_tokens, r.output_tokens, r.error_code) == \
        ('{"facts": []}', "stop", 11, 7, None)
    assert sent["auth"] == "Bearer not-a-real-key"
    assert sent["model"] == "openai/gpt-oss-120b"
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["max_completion_tokens"] == 64
    assert sent["reasoning_effort"] == "low"


def test_groq_retries_a_short_rate_limit_then_succeeds(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"},
                              json={"error": {"message": "slow down"}}) if len(calls) == 1 \
            else _ok('{"facts": []}')

    r = _groq(monkeypatch, handler).complete(system="s", user="u", schema={}, max_tokens=8)
    assert len(calls) == 2 and r.error_code is None


def test_groq_daily_quota_fails_fast_as_rate_limit(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "7200"}, json={
            "error": {"message": "Rate limit reached for requests per day"}})

    r = _groq(monkeypatch, handler).complete(system="s", user="u", schema={}, max_tokens=8)
    assert len(calls) == 1, "a day-long wait must not be retried"
    assert r.error_code == "rate_limit" and "per day" in r.error_detail


@pytest.mark.parametrize(("status", "code"), [(401, "auth"), (400, "api_error"), (504, "timeout")])
def test_groq_http_errors_become_typed_replies(monkeypatch, status, code):
    t = _groq(monkeypatch, lambda req: httpx.Response(status, json={"error": {"message": "nope"}}),
              llm_retry_attempts=1)
    r = t.complete(system="s", user="u", schema={}, max_tokens=8)
    assert r.error_code == code and r.text is None and "nope" in r.error_detail


def test_groq_length_finish_is_a_truncated_extraction(monkeypatch):
    t = _groq(monkeypatch, lambda req: _ok('{"facts": [', finish="length"))
    ex = Extractor(_settings(llm_provider="groq"))
    ex._transport = t
    out = ex.extract("Acme revenue was 5.", "doc")
    assert out.error_code == "truncated_response"
