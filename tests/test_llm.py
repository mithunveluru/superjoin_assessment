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
    assert c.model == "gemini-2.5-flash"


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
    assert out.model == "gemini-2.5-flash"


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
