"""LLM clients for extraction (Phase 4), entity confirmation (Phase 7) and
relationship proposal (Phase 9). One call shape, one provider: Google Gemini.

Provider specifics live in exactly one place — ``_GeminiTransport``. It takes
(system, user, json-schema) and returns a ``_Reply``: text + usage + an error
code from a fixed, provider-neutral vocabulary. Everything above it (prompts,
schemas, parsing, validation, the deterministic pipeline) speaks only ``_Reply``
and never sees a Gemini object, so swapping or adding a provider means writing
one class, not touching the application.

The SDK is imported lazily inside the transport so ``app.extract`` and the tests
need neither it nor an API key — tests inject fakes with the same surface.

Gemini takes our hand-written JSON Schemas through ``response_schema`` after
``_gemini_schema`` rewrites them into the subset that API accepts. The raw
response text is always returned so ``facts.raw_payload`` /
``raw_extractions.raw_response`` can preserve it verbatim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import (
    EntityConfirmation,
    LLMExtraction,
    RawExtraction,
    RelationshipProposal,
)

PROMPTS_DIR = Path(__file__).with_name("prompts")


PROVIDER = "gemini"


class LLMConfigError(RuntimeError):
    """No usable API key, or an unsupported provider — raised when a client is
    constructed, never mid-run."""


@dataclass
class _Reply:
    """One provider-neutral completion. ``error_code`` is the shared vocabulary:
    auth | rate_limit | timeout | api_error | refusal."""

    text: str | None = None
    stop: str | None = None          # normalized: "max_tokens" | "refusal" | provider value
    input_tokens: int = 0
    output_tokens: int = 0
    error_code: str | None = None
    error_detail: str | None = None


def _gemini_schema(schema: Any) -> Any:
    """Rewrite our JSON Schema into the subset ``response_schema`` accepts.

    Gemini has no ``additionalProperties`` and no type unions: ``["string",
    "null"]`` becomes ``{"type": "string", "nullable": True}``, and a ``None``
    member of an enum becomes nullability rather than an enum value.
    """
    if isinstance(schema, list):
        return [_gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if len(non_null) != len(value):
                out["nullable"] = True
        elif key == "enum" and isinstance(value, list):
            members = [v for v in value if v is not None]
            out["enum"] = members
            if len(members) != len(value):
                out["nullable"] = True
        elif key in ("properties", "items"):
            out[key] = _gemini_schema(value)
        else:
            out[key] = _gemini_schema(value) if isinstance(value, dict) else value
    return out


class _GeminiTransport:
    # Finish reasons that mean "the model declined", not "the call broke".
    _REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}

    def __init__(self, settings: Settings, api_key: str):
        self.settings = settings
        from google import genai  # imported here so the module loads without the SDK
        from google.genai import errors as genai_errors

        self._errors = genai_errors
        self._client = genai.Client(
            api_key=api_key,
            http_options={
                "timeout": int(settings.llm_timeout_seconds * 1000),  # ms
                # free-tier keys get 429s and 503s in bursts; the SDK's own
                # backoff keeps whole chunks from being lost to a transient spike
                "retry_options": {
                    "attempts": settings.llm_retry_attempts,
                    "initial_delay": 2.0,
                    "max_delay": 60.0,
                },
            },
        )

    def complete(self, *, system: str, user: str, schema: dict, max_tokens: int) -> _Reply:
        try:
            resp = self._client.models.generate_content(
                model=self.settings.llm_model,
                contents=user,
                config={
                    "system_instruction": system,
                    "temperature": self.settings.llm_temperature,
                    "max_output_tokens": max_tokens,
                    "response_mime_type": "application/json",
                    "response_schema": _gemini_schema(schema),
                    # we pass no tools; disabling AFC also silences the SDK's
                    # per-call "direct use of AFC" warning in the server log
                    "automatic_function_calling": {"disable": True},
                },
            )
        except self._errors.APIError as e:
            code = getattr(e, "code", None)
            kind = {401: "auth", 403: "auth", 429: "rate_limit",
                    408: "timeout", 504: "timeout"}.get(code, "api_error")
            return _Reply(error_code=kind, error_detail=f"status {code}: {e}")
        except httpx.TimeoutException as e:
            return _Reply(error_code="timeout", error_detail=f"transport timeout: {e}")
        except httpx.HTTPError as e:
            # DNS/TCP/TLS failures never reach the API, so the SDK does not wrap
            # them: without this they surface as an opaque llm_client_exception
            # once per chunk instead of a typed, retryable api_error.
            return _Reply(error_code="api_error",
                          error_detail=f"transport: {type(e).__name__}: {e}")

        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        candidates = getattr(resp, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        finish = getattr(finish, "name", finish)  # enum -> str

        if finish in self._REFUSALS:
            return _Reply(stop="refusal", input_tokens=in_tok, output_tokens=out_tok,
                          error_code="refusal", error_detail=f"model refused ({finish})")
        stop = "max_tokens" if finish == "MAX_TOKENS" else finish
        return _Reply(text=resp.text, stop=stop, input_tokens=in_tok, output_tokens=out_tok)


def _new_transport(settings: Settings) -> _GeminiTransport:
    """Settings -> one authenticated Gemini transport.

    The key and the provider are checked *before* the SDK import, so a missing
    key fails once, here, naming the variable to set — instead of the SDK
    raising an auth error once per chunk, which the per-chunk handler records as
    an opaque ``llm_client_exception``.
    """
    if settings.llm_provider != PROVIDER:
        raise LLMConfigError(
            f"unsupported FKL_LLM_PROVIDER {settings.llm_provider!r} — "
            f"this build supports {PROVIDER!r} only"
        )
    key = settings.llm_api_key()
    if not key:
        raise LLMConfigError(
            f"{settings.llm_api_key_env} is not set — extraction and reasoning need a "
            f"{PROVIDER} API key. Add it to .env (or export it) and restart the server."
        )
    return _GeminiTransport(settings, key)

# The JSON schema we ask the model to fill. Hand-written (not generated) so the
# wire contract is explicit: additionalProperties:false everywhere, enums as
# hints, required minimal. Deterministic code in app/extract.py is the real gate.
_CANDIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subject", "predicate", "object", "fact_type", "quote",
                 "char_start", "char_end"],
    "properties": {
        "subject": {"type": "string"},
        "predicate": {"type": "string"},
        "object": {"type": "string"},
        "fact_type": {"type": "string",
                      "enum": ["numeric", "semantic", "temporal", "categorical"]},
        "raw_value_text": {"type": ["string", "null"]},
        "parsed_value": {"type": ["number", "null"]},
        "unit": {"type": ["string", "null"]},
        "magnitude": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "percentage": {"type": "boolean"},
        "ratio": {"type": ["number", "null"]},
        "reporting_period": {"type": ["string", "null"]},
        "period_type": {"type": ["string", "null"],
                        "enum": ["instant", "quarter", "half_year", "fiscal_year",
                                 "calendar_year", "range", "unknown", None]},
        "scope": {"type": ["string", "null"]},
        "qualifiers": {"type": ["array", "null"], "items": {"type": "string"}},
        "modality": {"type": ["string", "null"],
                     "enum": ["asserted", "historical", "estimated", "forecast",
                              "projected", "target", "uncertain", None]},
        "quote": {"type": "string"},
        "char_start": {"type": "integer"},
        "char_end": {"type": "integer"},
    },
}
EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {"facts": {"type": "array", "items": _CANDIDATE_SCHEMA}},
}


def load_prompt(version: str, kind: str = "extraction") -> str:
    return (PROMPTS_DIR / f"{kind}_{version}.md").read_text(encoding="utf-8")


def _parse_response_text(text: str) -> tuple[RawExtraction | None, str | None]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"json_decode_error: {e}"
    try:
        return RawExtraction.model_validate(data), None
    except ValidationError as e:
        return None, f"schema_validation_error: {e}"


class Extractor:
    """Real extraction client for the configured provider. Constructed lazily by
    app.extract when no client is injected; never used from tests."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.model = self.settings.llm_model
        self.prompt_version = self.settings.prompt_version
        self._system = load_prompt(self.prompt_version)
        self._transport = _new_transport(self.settings)

    def extract(self, chunk_text: str, doc_header: str) -> LLMExtraction:
        user = (
            f"{doc_header}\n\n"
            "Passage (offsets are 0-based into this exact text):\n"
            "<<<PASSAGE\n"
            f"{chunk_text}\n"
            "PASSAGE"
        )
        r = self._transport.complete(
            system=self._system, user=user, schema=EXTRACTION_JSON_SCHEMA,
            max_tokens=self.settings.llm_max_tokens,
        )
        if r.error_code:
            return self._err(r.error_code, r.error_detail or "", stop=r.stop,
                             in_tok=r.input_tokens, out_tok=r.output_tokens)
        if r.stop == "max_tokens":
            return LLMExtraction(
                raw_text=r.text, parsed=None, error_code="truncated_response",
                error_detail="response hit max_tokens", model=self.model,
                prompt_version=self.prompt_version, stop_reason=r.stop,
                input_tokens=r.input_tokens, output_tokens=r.output_tokens,
            )
        if r.text is None:
            return self._err("malformed_response", "no text block in response",
                             stop=r.stop, in_tok=r.input_tokens, out_tok=r.output_tokens)

        parsed, parse_err = _parse_response_text(r.text)
        return LLMExtraction(
            raw_text=r.text, parsed=parsed,
            error_code=None if parsed is not None else "malformed_response",
            error_detail=parse_err, model=self.model,
            prompt_version=self.prompt_version, stop_reason=r.stop,
            input_tokens=r.input_tokens, output_tokens=r.output_tokens,
        )

    def _err(self, code: str, detail: str, *, stop: str | None = None,
             in_tok: int = 0, out_tok: int = 0) -> LLMExtraction:
        return LLMExtraction(
            raw_text=None, parsed=None, error_code=code, error_detail=detail,
            model=self.model, prompt_version=self.prompt_version, stop_reason=stop,
            input_tokens=in_tok, output_tokens=out_tok,
        )


# --- Phase 7: entity-cluster confirmation ---------------------------------------
_ENTITY_CONFIRM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["same", "confidence"],
    "properties": {
        "same": {"type": "boolean"},
        "confidence": {"type": "number"},
        "canonical_label": {"type": ["string", "null"]},
        "groups": {
            "type": ["array", "null"],
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "reasoning": {"type": ["string", "null"]},
    },
}


class EntityConfirmer:
    """Confirms/splits ONE borderline entity cluster. Same lazy-transport,
    no-sampling shape as ``Extractor``; never used from tests (they inject a fake)."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.model = self.settings.llm_model
        self.prompt_version = self.settings.entity_prompt_version
        self._system = load_prompt(self.prompt_version, kind="entity_confirm")
        self._transport = _new_transport(self.settings)

    def confirm_entities(self, surfaces: list[str], context: str = "") -> EntityConfirmation:
        user = (
            "Surfaces (decide if these are one entity):\n"
            + "\n".join(f"- {s}" for s in surfaces)
            + (f"\n\nContext:\n{context}" if context else "")
        )
        r = self._transport.complete(system=self._system, user=user,
                                     schema=_ENTITY_CONFIRM_SCHEMA, max_tokens=1024)
        if r.error_code:
            return EntityConfirmation(same=False, error_code="api_error",
                                      error_detail=r.error_detail)
        if not r.text:
            return EntityConfirmation(same=False, error_code="malformed_response",
                                     error_detail="no text block")
        try:
            data = json.loads(r.text)
            return EntityConfirmation(
                same=bool(data["same"]),
                confidence=float(data.get("confidence") or 0.0),
                canonical_label=data.get("canonical_label"),
                groups=data.get("groups"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return EntityConfirmation(same=False, error_code="malformed_response",
                                     error_detail=str(e))


# --- Phase 9: relationship confirmation ---------------------------------------
_RELATIONSHIP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relationship", "confidence", "reason"],
    "properties": {
        "relationship": {"type": "string", "enum": [
            "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT",
            "TEMPORAL_EVOLUTION", "UNCERTAIN",
        ]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "context_differences": {"type": ["array", "null"], "items": {"type": "string"}},
        "uncertainties": {"type": ["array", "null"], "items": {"type": "string"}},
    },
}


class RelationshipConfirmer:
    """Proposes ONE relationship category + reasoning for a candidate pair from a
    structured packet. Deterministic code in ``app.reason`` validates and may
    override the proposal (it never has the last word). Same lazy-transport,
    no-sampling shape as the other clients; never used from tests."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.model = self.settings.llm_model
        self.prompt_version = self.settings.relationship_prompt_version
        self._system = load_prompt(self.prompt_version, kind="relationship")
        self._transport = _new_transport(self.settings)

    def classify_relationship(self, packet: dict) -> RelationshipProposal:
        user = (
            "Two comparable facts and the deterministic signals about how they "
            "relate:\n\n" + json.dumps(packet, indent=2, ensure_ascii=False, default=str)
        )
        r = self._transport.complete(system=self._system, user=user,
                                     schema=_RELATIONSHIP_SCHEMA, max_tokens=1024)
        if r.error_code:
            return RelationshipProposal(error_code="api_error", error_detail=r.error_detail)
        if not r.text:
            return RelationshipProposal(error_code="malformed_response",
                                       error_detail="no text block")
        try:
            data = json.loads(r.text)
            return RelationshipProposal(
                relationship=str(data["relationship"]),
                confidence=float(data.get("confidence") or 0.0),
                reason=str(data.get("reason") or ""),
                context_differences=list(data.get("context_differences") or []),
                uncertainties=list(data.get("uncertainties") or []),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return RelationshipProposal(error_code="malformed_response", error_detail=str(e))
