"""Thin Anthropic wrapper for Phase 4 candidate extraction. One provider, one
call shape. The SDK is imported lazily so ``app.extract`` and the tests never
need it or an API key — tests inject a fake with the same ``extract`` surface.

Sonnet 5 rejects sampling params (temperature/top_p/top_k) and assistant
prefill; this client sends neither. Structured output is requested via
``output_config.format`` (json_schema) and the raw text is always returned so
``facts.raw_payload`` / ``raw_extractions.raw_response`` can preserve it.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import EntityConfirmation, LLMExtraction, RawExtraction

PROMPTS_DIR = Path(__file__).with_name("prompts")

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


class AnthropicExtractor:
    """Real extraction client. Constructed lazily by app.extract when no client
    is injected; never used from tests."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.model = self.settings.llm_model
        self.prompt_version = self.settings.prompt_version
        self._system = load_prompt(self.prompt_version)
        # imported here so the module loads without the SDK installed
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=self.settings.llm_timeout_seconds)

    def extract(self, chunk_text: str, doc_header: str) -> LLMExtraction:
        a = self._anthropic
        user = (
            f"{doc_header}\n\n"
            "Passage (offsets are 0-based into this exact text):\n"
            "<<<PASSAGE\n"
            f"{chunk_text}\n"
            "PASSAGE"
        )
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.settings.llm_max_tokens,
                system=self._system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self.settings.llm_effort,
                    "format": {"type": "json_schema", "schema": EXTRACTION_JSON_SCHEMA},
                },
            )
        except a.AuthenticationError as e:
            return self._err("auth", str(e))
        except a.RateLimitError as e:  # SDK already retried; treat as chunk-level
            return self._err("rate_limit", str(e))
        except a.APITimeoutError as e:
            return self._err("timeout", str(e))
        except a.APIConnectionError as e:
            return self._err("api_error", f"connection: {e}")
        except a.APIStatusError as e:
            return self._err("api_error", f"status {e.status_code}: {e.message}")

        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        stop = getattr(resp, "stop_reason", None)

        if stop == "refusal":
            return self._err("refusal", "model refused", stop=stop,
                             in_tok=in_tok, out_tok=out_tok)

        text = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), None
        )
        if stop == "max_tokens":
            return LLMExtraction(
                raw_text=text, parsed=None, error_code="truncated_response",
                error_detail="response hit max_tokens", model=self.model,
                prompt_version=self.prompt_version, stop_reason=stop,
                input_tokens=in_tok, output_tokens=out_tok,
            )
        if text is None:
            return self._err("malformed_response", "no text block in response",
                             stop=stop, in_tok=in_tok, out_tok=out_tok)

        parsed, parse_err = _parse_response_text(text)
        return LLMExtraction(
            raw_text=text, parsed=parsed,
            error_code=None if parsed is not None else "malformed_response",
            error_detail=parse_err, model=self.model,
            prompt_version=self.prompt_version, stop_reason=stop,
            input_tokens=in_tok, output_tokens=out_tok,
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


class AnthropicEntityConfirmer:
    """Confirms/splits ONE borderline entity cluster. Same lazy-import, no-sampling
    shape as ``AnthropicExtractor``; never used from tests (they inject a fake)."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.model = self.settings.llm_model
        self.prompt_version = self.settings.entity_prompt_version
        self._system = load_prompt(self.prompt_version, kind="entity_confirm")
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=self.settings.llm_timeout_seconds)

    def confirm_entities(self, surfaces: list[str], context: str = "") -> EntityConfirmation:
        a = self._anthropic
        user = (
            "Surfaces (decide if these are one entity):\n"
            + "\n".join(f"- {s}" for s in surfaces)
            + (f"\n\nContext:\n{context}" if context else "")
        )
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=self._system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": self.settings.llm_effort,
                    "format": {"type": "json_schema", "schema": _ENTITY_CONFIRM_SCHEMA},
                },
            )
        except (a.AuthenticationError, a.RateLimitError, a.APITimeoutError,
                a.APIConnectionError, a.APIStatusError) as e:
            return EntityConfirmation(same=False, error_code="api_error", error_detail=str(e))

        text = next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), None
        )
        if not text:
            return EntityConfirmation(same=False, error_code="malformed_response",
                                     error_detail="no text block")
        try:
            data = json.loads(text)
            return EntityConfirmation(
                same=bool(data["same"]),
                confidence=float(data.get("confidence") or 0.0),
                canonical_label=data.get("canonical_label"),
                groups=data.get("groups"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return EntityConfirmation(same=False, error_code="malformed_response",
                                     error_detail=str(e))
