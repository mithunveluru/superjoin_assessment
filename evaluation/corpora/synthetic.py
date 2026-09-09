"""Synthetic offline evaluation corpus — deterministic, no LLM.

Seeds a database with facts + evidence + one resolved entity, engineered so that
after ``retrieve_candidates`` + ``reason_document`` the four required properties
and the global invariants hold. Nothing here is corpus-specific: the entity is
``Acme`` and the predicates are generic financial-statement wording.

Two honesty knobs, each of which makes the corroboration property FAIL (proving
it is not satisfiable by unsupported facts):

* ``break_grounding``        — the corroboration pair is left ineligible
* ``fabricate_corroboration`` — the deck value is far from the report value, so
                                the pair classifies as CONTRADICTS, not CORROBORATES
"""

from __future__ import annotations

from app import db
from app.config import Settings
from app.facts import attach_evidence, insert_fact
from app.models import EvidenceIn, FactIn
from app.reason import reason_document

_NOW = "2026-01-01T00:00:00Z"
_FY23 = ("2022-04-01", "2023-04-01")
_FY24 = ("2023-04-01", "2024-04-01")


def _document(conn, tag: str, *, publication_date: str, title: str, filename: str) -> int:
    # title/filename are fixture labels only — "(synthetic)" keeps the fixture
    # distinguishable from corpus-derived documents in the UI and the API.
    sha = (tag + "0" * 64)[:64]
    return conn.execute(
        "INSERT INTO documents (sha256, stored_path, original_filename, title, page_count, "
        "status, uploaded_at, publication_date) VALUES (?, ?, ?, ?, 0, 'ingested', ?, ?)",
        (sha, f"uploads/{sha}.pdf", filename, title, _NOW, publication_date),
    ).lastrowid


def _entity(conn, label: str) -> int:
    return conn.execute(
        "INSERT INTO entities (canonical_label, entity_type, normalization_key, "
        "resolution_method, created_at) VALUES (?, 'org', ?, 'deterministic', ?)",
        (label, label.lower(), _NOW),
    ).lastrowid


class _Seeder:
    def __init__(self, conn):
        self.conn = conn
        self._page = {}

    def fact(self, doc: int, *, entity: int, predicate: str, obj: str, quote: str,
             base_value: float | None = None, currency: str = "INR",
             period: tuple[str, str] | None = None, period_raw: str | None = None,
             scope: dict | None = None, modality: str = "HISTORICAL",
             eligible: bool = True) -> int:
        idx = self._page.get(doc, 0)
        self._page[doc] = idx + 1
        text = f"{quote}  (synthetic evaluation page {idx})"
        self.conn.execute(
            "INSERT INTO pages (document_id, page_index, text, char_count, extraction_status) "
            "VALUES (?, ?, ?, ?, 'TEXT_EXTRACTED')", (doc, idx, text, len(text)),
        )
        chunk_id = self.conn.execute(
            "INSERT INTO chunks (document_id, page_index, seq, char_offset, char_end, text) "
            "VALUES (?, ?, 0, 0, ?, ?)", (doc, idx, len(text), text),
        ).lastrowid
        fid = insert_fact(self.conn, FactIn(
            document_id=doc, page_index=idx, subject_raw="Acme", predicate=predicate,
            predicate_norm=predicate.lower(), object_raw=obj, fact_type="numeric",
            value_raw=obj, numeric_value=base_value, base_value=base_value,
            currency=currency, subject_entity_id=entity, modality=modality, scope=scope,
            reporting_period_raw=period_raw,
            reporting_period_start=period[0] if period else None,
            reporting_period_end=period[1] if period else None,
            reporting_period_type="fiscal_year" if period else None,
            lifecycle_state="ELIGIBLE_FOR_REASONING" if eligible else "NORMALIZED",
            evidence_status="VERIFIED" if eligible else "UNVERIFIED",
            reasoning_eligible=eligible,
        ))
        attach_evidence(self.conn, fid, EvidenceIn(
            document_id=doc, page_index=idx, chunk_id=chunk_id, quote=quote,
            char_start=0, char_end=len(quote), verification_method="exact",
            evidence_status="VERIFIED" if eligible else "UNVERIFIED",
        ))
        return fid

    def quarantined(self, doc: int, *, predicate: str, obj: str, reason: str) -> int:
        idx = self._page.get(doc, 0)
        self._page[doc] = idx + 1
        self.conn.execute(
            "INSERT INTO pages (document_id, page_index, text, char_count, extraction_status) "
            "VALUES (?, ?, 'an unrelated synthetic page', 26, 'TEXT_EXTRACTED')", (doc, idx),
        )
        fid = insert_fact(self.conn, FactIn(
            document_id=doc, page_index=idx, subject_raw="Acme", predicate=predicate,
            predicate_norm=predicate.lower(), object_raw=obj, fact_type="numeric",
            value_raw=obj, lifecycle_state="QUARANTINED", evidence_status="UNVERIFIED",
            reasoning_eligible=False, quarantine_reason=reason,
        ))
        self.conn.execute(
            "INSERT INTO failures (document_id, failure_type, ref_table, ref_id, reason, detail, "
            "created_at) VALUES (?, 'grounding_failed', 'facts', ?, ?, '{}', ?)",
            (doc, fid, reason, _NOW),
        )
        return fid


def seed_corpus(database_path: str, *, settings: Settings | None = None,
                break_grounding: bool = False,
                fabricate_corroboration: bool = False) -> dict:
    """Build the corpus into ``database_path`` and run retrieval + reasoning."""
    db.init_db(database_path)
    conn = db.connect(database_path)
    try:
        doc_a = _document(conn, "syntheticA", publication_date="2025-01-30",
                          title="Acme Annual Report FY24 (synthetic)",
                          filename="acme-annual-report-fy24.pdf")
        doc_b = _document(conn, "syntheticB", publication_date="2025-05-25",
                          title="Acme Q4 FY24 Earnings Deck (synthetic)",
                          filename="acme-q4-fy24-earnings-deck.pdf")
        acme = _entity(conn, "Acme")
        s = _Seeder(conn)
        elig = not break_grounding

        f1 = s.fact(doc_a, entity=acme, predicate="revenue from operations",
                    obj="81,415.38 million",
                    quote="revenue from operations on a consolidated basis for FY24 stood at "
                          "INR 81,415.38 million",
                    base_value=8.141538e10, period=_FY24, period_raw="FY24",
                    scope={"basis": "consolidated"}, eligible=elig)
        f2 = s.fact(doc_a, entity=acme, predicate="revenue from operations",
                    obj="74,540.82 million",
                    quote="revenue from operations on a standalone basis for FY24 stood at "
                          "INR 74,540.82 million",
                    base_value=7.454082e10, period=_FY24, period_raw="FY24",
                    scope={"basis": "standalone"})
        f3 = s.fact(doc_a, entity=acme, predicate="revenue from operations",
                    obj="60,000.00 million",
                    quote="revenue from operations on a consolidated basis for FY23 stood at "
                          "INR 60,000.00 million",
                    base_value=6.0e10, period=_FY23, period_raw="FY23",
                    scope={"basis": "consolidated"})
        f4 = s.fact(doc_a, entity=acme, predicate="profit after tax", obj="5,000.00 million",
                    quote="profit after tax for FY24 was INR 5,000.00 million",
                    base_value=5.0e9, period=_FY24, period_raw="FY24")
        f5 = s.fact(doc_a, entity=acme, predicate="profit after tax", obj="8,000.00 million",
                    quote="profit after tax for FY24 was INR 8,000.00 million",
                    base_value=8.0e9, period=_FY24, period_raw="FY24")
        f6 = s.quarantined(doc_a, predicate="permanent employees", obj="99,999",
                           reason="quote_not_found in source text")

        deck_value = 5.0e10 if fabricate_corroboration else 8.142e10
        f7 = s.fact(doc_b, entity=acme, predicate="revenue from services", obj="8,142 Cr",
                    quote="FY24 revenue from services was INR 8,142 Cr", base_value=deck_value,
                    period=_FY24, period_raw="FY24", scope={"basis": "consolidated"},
                    eligible=elig)
        conn.commit()
    finally:
        conn.close()

    for doc in (doc_a, doc_b):
        reason_document(doc, database_path=database_path, settings=settings)

    return {
        "doc_a": doc_a, "doc_b": doc_b, "entity": acme,
        "facts": {"f1": f1, "f2": f2, "f3": f3, "f4": f4, "f5": f5, "f6": f6, "f7": f7},
    }
