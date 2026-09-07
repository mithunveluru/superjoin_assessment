"""Deterministic fake LLM extraction client for Phase 4 tests. No network."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.models import LLMExtraction, RawExtraction


class FakeLLM:
    """Same surface as ``app.llm.AnthropicExtractor``: ``.model``,
    ``.prompt_version``, ``.extract(chunk_text, doc_header) -> LLMExtraction``.

    ``script`` is a list consumed one item per ``extract`` call:
      * ``dict`` -> treated as the parsed JSON body ``{"facts": [...]}``
      * ``str``  -> a raw response body (e.g. malformed JSON)
      * ``LLMExtraction`` -> returned verbatim (for api_error / auth / refusal …)
      * ``Exception`` -> raised (unexpected client failure)
    When the script is exhausted, returns an empty ``{"facts": []}``.
    """

    model = "fake-sonnet"
    prompt_version = "test-v1"

    def __init__(self, script: list[Any] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def extract(self, chunk_text: str, doc_header: str) -> LLMExtraction:
        self.calls.append({"chunk_text": chunk_text, "doc_header": doc_header})
        item = self.script.pop(0) if self.script else {"facts": []}

        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMExtraction):
            return item
        raw = item if isinstance(item, str) else json.dumps(item)

        try:
            parsed = RawExtraction.model_validate_json(raw)
            return LLMExtraction(
                raw_text=raw, parsed=parsed, model=self.model,
                prompt_version=self.prompt_version, stop_reason="end_turn",
                input_tokens=10, output_tokens=20,
            )
        except ValidationError as e:
            return LLMExtraction(
                raw_text=raw, parsed=None, error_code="malformed_response",
                error_detail=str(e), model=self.model,
                prompt_version=self.prompt_version, stop_reason="end_turn",
            )


def candidate(**over: Any) -> dict:
    """A structurally valid candidate dict; override any field."""
    base = {
        "subject": "Acme Corp",
        "predicate": "reported revenue",
        "object": "42 units",
        "fact_type": "semantic",
        "quote": "Acme",
        "char_start": 0,
        "char_end": 4,
    }
    base.update(over)
    return base


def api_error(code: str = "api_error", detail: str = "boom") -> LLMExtraction:
    return LLMExtraction(
        raw_text=None, parsed=None, error_code=code, error_detail=detail,
        model=FakeLLM.model, prompt_version=FakeLLM.prompt_version,
    )
