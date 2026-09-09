"""Deterministic fake LLM extraction client for Phase 4 tests. No network."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.models import (
    EntityConfirmation,
    LLMExtraction,
    RawExtraction,
    RelationshipProposal,
)


class FakeLLM:
    """Same surface as ``app.llm.Extractor``: ``.model``,
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


class FakeEntityConfirmer:
    """Same surface as ``app.llm.EntityConfirmer``:
    ``.confirm_entities(surfaces, context) -> EntityConfirmation``.

    ``script`` is consumed one item per call:
      * ``EntityConfirmation`` -> returned verbatim
      * ``dict``               -> kwargs for ``EntityConfirmation``
      * ``Exception``          -> raised
    Exhausted script -> a conservative ``same=False`` (no merge).
    """

    model = "fake-sonnet"
    prompt_version = "test-v1"

    def __init__(self, script: list[Any] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def confirm_entities(self, surfaces: list[str], context: str = "") -> EntityConfirmation:
        self.calls.append({"surfaces": list(surfaces), "context": context})
        item = self.script.pop(0) if self.script else EntityConfirmation(same=False)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, EntityConfirmation):
            return item
        return EntityConfirmation(**item)


def api_error(code: str = "api_error", detail: str = "boom") -> LLMExtraction:
    return LLMExtraction(
        raw_text=None, parsed=None, error_code=code, error_detail=detail,
        model=FakeLLM.model, prompt_version=FakeLLM.prompt_version,
    )


class FakeRelationshipConfirmer:
    """Same surface as ``app.llm.RelationshipConfirmer``:
    ``.classify_relationship(packet) -> RelationshipProposal``.

    ``script`` is consumed one item per call:
      * ``RelationshipProposal`` -> returned verbatim
      * ``dict``                 -> kwargs for ``RelationshipProposal``
      * ``Exception``            -> raised
    Exhausted script -> a conservative UNCERTAIN proposal.
    """

    model = "fake-sonnet"
    prompt_version = "test-v1"

    def __init__(self, script: list[Any] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def classify_relationship(self, packet: dict) -> RelationshipProposal:
        self.calls.append({"packet": packet})
        item = self.script.pop(0) if self.script else RelationshipProposal(
            relationship="UNCERTAIN", confidence=0.2, reason="fake default")
        if isinstance(item, Exception):
            raise item
        if isinstance(item, RelationshipProposal):
            return item
        return RelationshipProposal(**item)
