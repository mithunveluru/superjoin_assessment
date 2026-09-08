"""Phase 8 (Stage A) — bounded candidate-pair retrieval over eligible facts.

High recall, deterministic, no LLM, **no embeddings** — the planned embedding
rung is deferred (no ``fastembed`` in this environment; ``facts.embedding`` stays
NULL and lexical retrieval is the baseline, see DECISIONS D22). Read-only: Phase
8 never changes a fact's lifecycle, evidence, or truth status, and never writes a
relationship. Its output is a *ranked candidate set* for Phase 9 to consume.

Pipeline:
  1. load every ``ELIGIBLE_FOR_REASONING`` fact (cross-document);
  2. block on shared structure via an inverted index —
       entity          : identical resolved ``subject_entity_id``
       predicate_exact : identical normalized predicate
       lexical         : >= ``FKL_RETRIEVAL_MIN_SHARED_TOKENS`` shared content
                         tokens (a token covering > ``FKL_RETRIEVAL_BUCKET_MAX``
                         facts is dropped as non-discriminative);
  3. score each surviving pair with an explicit weighted formula (config weights);
  4. keep entity / exact-predicate pairs regardless of score (strong structural
     candidates); keep the rest at score >= ``FKL_RETRIEVAL_CANDIDATE_THRESHOLD``;
  5. keep the top ``FKL_RETRIEVAL_TOP_K`` per fact (union), canonicalize to
     ``(min, max)`` fact id, dedupe, sort  ->  <= top_k * n_eligible pairs;
  6. attach the full deterministic ``SignalSet`` (``app.signals.compute``).

Context differences (period, scope, currency, modality) are recorded as signals
in step 6 — they never exclude a pair here.
"""

from __future__ import annotations

import logging
import sqlite3
from itertools import combinations
from typing import Any

from rapidfuzz import fuzz

from app import signals
from app.config import Settings, get_settings
from app.models import CandidatePair
from app.signals import content_tokens

log = logging.getLogger("fkl.retrieve")

_ELIGIBLE = "ELIGIBLE_FOR_REASONING"


def _score(w_e: float, w_p: float, w_l: float, *, same_entity: bool,
           predicate_sim: float, shared_tokens: int) -> float:
    """retrieval_score in [0, 1]: how useful this fact is as a candidate for
    downstream comparison. Not a truth score, not a contradiction score."""
    total = w_e + w_p + w_l
    if total <= 0:
        return 0.0
    lexical = min(1.0, shared_tokens / 3.0)
    raw = w_e * (1.0 if same_entity else 0.0) + w_p * predicate_sim + w_l * lexical
    return round(raw / total, 6)


def retrieve_candidates(conn: sqlite3.Connection, *, document_id: int | None = None,
                        settings: Settings | None = None) -> list[CandidatePair]:
    """Ranked candidate pairs among reasoning-eligible facts. ``document_id``
    restricts the result to pairs touching that document (counterparties are
    still drawn from every document)."""
    settings = settings or get_settings()
    w_e = settings.retrieval_weight_entity
    w_p = settings.retrieval_weight_predicate
    w_l = settings.retrieval_weight_bm25

    facts = conn.execute(
        "SELECT id, document_id, subject_entity_id, subject_raw, predicate, predicate_norm, "
        "object_raw, fact_type FROM facts WHERE lifecycle_state = ? ORDER BY id",
        (_ELIGIBLE,),
    ).fetchall()
    n = len(facts)
    if n < 2:
        return []

    norm_pred: list[str] = []
    tok_sets: list[set[str]] = []
    for f in facts:
        norm_pred.append(
            ((f["predicate_norm"] or "").strip() or (f["predicate"] or "").strip()).lower()
        )
        tok_sets.append(content_tokens(
            " ".join(filter(None, (f["subject_raw"], f["predicate"], f["object_raw"])))
        ))

    ent_buckets: dict[int, list[int]] = {}
    pred_buckets: dict[str, list[int]] = {}
    tok_buckets: dict[str, list[int]] = {}
    for pos, f in enumerate(facts):
        if f["subject_entity_id"] is not None:
            ent_buckets.setdefault(f["subject_entity_id"], []).append(pos)
        if norm_pred[pos]:
            pred_buckets.setdefault(norm_pred[pos], []).append(pos)
        for t in tok_sets[pos]:
            tok_buckets.setdefault(t, []).append(pos)

    # ponytail: O(bucket^2) per bucket; entity/predicate buckets are small and
    # oversized token buckets are dropped. Swap in an ANN index if the eligible
    # set ever outgrows ~1e4 facts.
    pair_methods: dict[tuple[int, int], set[str]] = {}

    def _add(a: int, b: int, method: str) -> None:
        i, j = (a, b) if a < b else (b, a)
        pair_methods.setdefault((i, j), set()).add(method)

    for idxs in ent_buckets.values():
        for i, j in combinations(idxs, 2):
            _add(i, j, "entity")
    for idxs in pred_buckets.values():
        for i, j in combinations(idxs, 2):
            _add(i, j, "predicate_exact")
    dropped_token_blocks = 0
    for idxs in tok_buckets.values():
        if len(idxs) > settings.retrieval_bucket_max:
            dropped_token_blocks += 1
            continue
        for i, j in combinations(idxs, 2):
            _add(i, j, "lexical")

    results: list[tuple[int, int, float, list[str]]] = []
    for (i, j), ms in pair_methods.items():
        shared = len(tok_sets[i] & tok_sets[j])
        if ms == {"lexical"} and shared < settings.retrieval_min_shared_tokens:
            continue
        same_entity = "entity" in ms
        predicate_sim = (
            fuzz.token_set_ratio(norm_pred[i], norm_pred[j]) / 100.0
            if norm_pred[i] and norm_pred[j] else 0.0
        )
        method_list: list[str] = []
        if same_entity:
            method_list.append("entity")
        if "predicate_exact" in ms:
            method_list.append("predicate_exact")
        elif predicate_sim >= settings.predicate_similarity_threshold:
            method_list.append("predicate_similar")
        if "lexical" in ms:
            method_list.append("lexical")

        score = _score(w_e, w_p, w_l, same_entity=same_entity,
                       predicate_sim=predicate_sim, shared_tokens=shared)
        structural = same_entity or "predicate_exact" in ms
        if not structural and score < settings.retrieval_candidate_threshold:
            continue
        results.append((i, j, score, method_list))

    # top-k per fact (union of both sides)
    by_pos: dict[int, list[int]] = {}
    for ri, (i, j, _, _) in enumerate(results):
        by_pos.setdefault(i, []).append(ri)
        by_pos.setdefault(j, []).append(ri)
    keep: set[int] = set()
    for ris in by_pos.values():
        ris.sort(key=lambda ri: (-results[ri][2], results[ri][0], results[ri][1]))
        keep.update(ris[: settings.retrieval_top_k])

    pairs: list[CandidatePair] = []
    for ri in keep:
        i, j, score, method_list = results[ri]
        fa_id, fb_id = facts[i]["id"], facts[j]["id"]  # facts sorted by id -> fa_id < fb_id
        if document_id is not None and document_id not in (
            facts[i]["document_id"], facts[j]["document_id"]
        ):
            continue
        pairs.append(CandidatePair(
            fact_a_id=fa_id, fact_b_id=fb_id, retrieval_score=score,
            retrieval_methods=method_list, signals=signals.compute(conn, fa_id, fb_id),
        ))
    pairs.sort(key=lambda p: (p.fact_a_id, p.fact_b_id))
    log.info(
        "retrieval eligible=%s candidate_pairs=%s cross_document=%s dropped_token_blocks=%s",
        n, len(pairs), sum(1 for p in pairs if not p.signals.same_document), dropped_token_blocks,
    )
    return pairs


def retrieval_summary(conn: sqlite3.Connection,
                      pairs: list[CandidatePair]) -> dict[str, Any]:
    """Deterministic observability for one retrieval run."""
    settings = get_settings()
    eligible = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE lifecycle_state = ?", (_ELIGIBLE,)
    ).fetchone()[0]
    by_method: dict[str, int] = {}
    by_period: dict[str, int] = {}
    by_entity: dict[str, int] = {}
    for p in pairs:
        for m in p.retrieval_methods:
            by_method[m] = by_method.get(m, 0) + 1
        by_period[p.signals.period_relation] = by_period.get(p.signals.period_relation, 0) + 1
        by_entity[p.signals.entity_relation] = by_entity.get(p.signals.entity_relation, 0) + 1
    return {
        "facts_eligible": eligible,
        "candidate_pairs": len(pairs),
        "max_possible": settings.retrieval_top_k * eligible,
        "cross_document_pairs": sum(1 for p in pairs if not p.signals.same_document),
        "numeric_comparable_pairs": sum(1 for p in pairs if p.signals.numeric_comparable),
        "pairs_with_scope_conflict": sum(1 for p in pairs if p.signals.scope_conflict),
        "pairs_by_retrieval_method": by_method,
        "pairs_by_period_relation": by_period,
        "pairs_by_entity_relation": by_entity,
        "config": {
            "top_k": settings.retrieval_top_k,
            "candidate_threshold": settings.retrieval_candidate_threshold,
            "predicate_similarity_threshold": settings.predicate_similarity_threshold,
            "weight_entity": settings.retrieval_weight_entity,
            "weight_predicate": settings.retrieval_weight_predicate,
            "weight_bm25": settings.retrieval_weight_bm25,
            "min_shared_tokens": settings.retrieval_min_shared_tokens,
            "bucket_max": settings.retrieval_bucket_max,
        },
    }
