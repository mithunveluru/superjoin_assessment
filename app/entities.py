"""Phase 7 — entity resolution.

Turns the free-text ``facts.subject_raw`` surfaces of one document into links to
rows in the global ``entities`` table, **without any dataset-specific alias
list**. The only lists consulted are language-generic and live in
``Settings`` (legal-form suffixes, honorifics, anaphora words, rename
predicates).

Pipeline per document:

  1. deterministic normalization  -> ``normalization_key`` (blocking key)
  2. exact-key match to an existing entity            -> link (``deterministic``)
  3. fuzzy block (rapidfuzz token_set_ratio >= cfg)   -> borderline
       - token_sort_ratio >= cfg  -> auto-merge (``similarity``)
       - else, if an LLM confirmer is supplied        -> confirm / split (``llm``)
       - else                                         -> ``entity_ambiguous`` (no merge)
  4. no match at all                                  -> new entity (``deterministic``)
  5. rename facts ("formerly known as" ...) -> alias edges (``derived_fact``)
  6. anaphora surfaces ("the Company") -> the document's dominant entity
  7. backfill ``facts.subject_entity_id``
  8. promote NORMALIZED facts with verified evidence -> ELIGIBLE_FOR_REASONING

No embeddings here — name+context cosine blocking is deferred to Phase 8, which
needs ``facts.embedding`` for retrieval anyway. token_set_ratio is the
high-recall block gate; token_sort_ratio (order- and length-sensitive, so a bare
name and "<name> Robotics" score low) is the auto-merge gate.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from rapidfuzz import fuzz

from app import db
from app.config import Settings, get_settings
from app.facts import FactError, mark_reasoning_eligible
from app.models import DocResolutionSummary, EntityLink

log = logging.getLogger("fkl.entities")

_PUNCT_RE = re.compile(r"[^\w\s&-]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


class EntityError(RuntimeError):
    def __init__(self, code: str, *, detail: Any = None, run_id: int | None = None):
        self.code = code
        self.detail = detail
        self.run_id = run_id
        super().__init__(code if detail is None else f"{code}: {detail}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# deterministic normalization                                                #
# --------------------------------------------------------------------------- #
def normalize_name(raw: str, settings: Settings) -> str:
    """Generic blocking key: lowercase, drop punctuation, strip trailing
    legal-form suffixes and leading honorifics. No dataset-specific rules."""
    s = _PUNCT_RE.sub(" ", (raw or "").lower())
    toks = _WS_RE.sub(" ", s).strip().split(" ")
    toks = [t for t in toks if t]
    while toks and toks[-1] in settings.entity_legal_suffixes:
        toks.pop()
    while toks and toks[0] in settings.entity_person_honorifics:
        toks.pop(0)
    return " ".join(toks)


def guess_type(raw: str, settings: Settings) -> str:
    """Cheap: an honorific prefix -> person, otherwise org.
    ponytail: first surface for a key wins; upgrade to a name-shape classifier
    (or the LLM) if entity_type precision matters downstream."""
    low = (raw or "").strip().lower()
    first = re.split(r"[.\s]+", low, maxsplit=1)[0]
    if first in settings.entity_person_honorifics:
        return "person"
    return "org"


def _stripped(raw: str) -> str:
    return _WS_RE.sub(" ", (raw or "").lower()).strip().rstrip(".")


# --------------------------------------------------------------------------- #
# entity / alias writes                                                      #
# --------------------------------------------------------------------------- #
def _add_alias(conn, entity_id, surface, normalized, method, score, source_fact_id) -> bool:
    cur = conn.execute(
        "INSERT INTO entity_aliases "
        "(entity_id, surface, normalized, match_method, score, source_fact_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entity_id, surface) DO NOTHING",
        (entity_id, surface.strip(), normalized or None, method, score, source_fact_id, _now()),
    )
    return cur.rowcount > 0


def _new_entity(conn, surface, key, etype, method, score, run_id) -> int:
    return conn.execute(
        "INSERT INTO entities "
        "(canonical_label, entity_type, normalization_key, resolution_method, "
        " resolution_score, created_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (surface.strip(), etype, key, method, score, run_id, _now()),
    ).lastrowid


def _merge_entities(conn, keep_id: int, drop_id: int) -> None:
    if keep_id == drop_id:
        return
    conn.execute("UPDATE OR IGNORE entity_aliases SET entity_id = ? WHERE entity_id = ?",
                 (keep_id, drop_id))
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (drop_id,))
    conn.execute("UPDATE facts SET subject_entity_id = ? WHERE subject_entity_id = ?",
                 (keep_id, drop_id))
    conn.execute("DELETE FROM entities WHERE id = ?", (drop_id,))


# --------------------------------------------------------------------------- #
# resolve one named surface                                                  #
# --------------------------------------------------------------------------- #
def _resolve_named(conn, surface: str, settings: Settings, run_id: int | None,
                   llm) -> tuple[EntityLink, str | None]:
    """Returns (link, ambiguous_reason). link.entity_id is None when the surface
    is left unresolved (a borderline merge nobody could confirm)."""
    key = normalize_name(surface, settings)
    if not key:
        return EntityLink(surface=surface), "empty_key"
    etype = guess_type(surface, settings)

    exact = conn.execute(
        "SELECT id, canonical_label FROM entities WHERE normalization_key = ? ORDER BY id LIMIT 1",
        (key,),
    ).fetchone()
    if exact:
        _add_alias(conn, exact["id"], surface, key, "deterministic", None, None)
        return EntityLink(surface=surface, entity_id=exact["id"],
                          canonical_label=exact["canonical_label"],
                          match_method="deterministic"), None

    cands = conn.execute(
        "SELECT id, canonical_label, normalization_key FROM entities "
        "WHERE normalization_key IS NOT NULL AND normalization_key <> ''"
    ).fetchall()
    blocked = [
        (r["id"], r["canonical_label"], r["normalization_key"],
         fuzz.token_set_ratio(key, r["normalization_key"]))
        for r in cands
    ]
    blocked = [b for b in blocked if b[3] >= settings.entity_block_fuzzy_threshold]
    if blocked:
        blocked.sort(key=lambda b: -b[3])
        eid, label, ckey, _ = blocked[0]
        sort_score = fuzz.token_sort_ratio(key, ckey)
        if sort_score >= settings.entity_merge_fuzzy_threshold:
            _add_alias(conn, eid, surface, key, "similarity", sort_score / 100, None)
            conn.execute(
                "UPDATE entities SET resolution_method = 'similarity', resolution_score = ? "
                "WHERE id = ? AND resolution_method = 'deterministic'",
                (sort_score / 100, eid),
            )
            return EntityLink(surface=surface, entity_id=eid, canonical_label=label,
                              match_method="similarity", score=sort_score / 100), None
        if llm is not None:
            conf = llm.confirm_entities([surface, label])
            if conf.error_code is None and conf.same and conf.confidence >= 0.5:
                _add_alias(conn, eid, surface, key, "llm", conf.confidence, None)
                conn.execute("UPDATE entities SET llm_confirmed = 1, llm_confidence = ?, "
                             "resolution_method = 'llm_confirmed' WHERE id = ?",
                             (conf.confidence, eid))
                return EntityLink(surface=surface, entity_id=eid,
                                  canonical_label=conf.canonical_label or label,
                                  match_method="llm", score=conf.confidence), None
            return EntityLink(surface=surface), "llm_not_same" if conf.error_code is None \
                else f"llm_{conf.error_code}"
        return EntityLink(surface=surface), "similar_unconfirmed"

    eid = _new_entity(conn, surface, key, etype, "deterministic", None, run_id)
    _add_alias(conn, eid, surface, key, "deterministic", None, None)
    return EntityLink(surface=surface, entity_id=eid, canonical_label=surface.strip(),
                      match_method="deterministic", created=True), None


# --------------------------------------------------------------------------- #
# rename facts -> derived-fact aliases                                       #
# --------------------------------------------------------------------------- #
def _rename_pairs(conn, document_id: int, settings: Settings):
    rows = conn.execute(
        "SELECT id, subject_raw, object_raw, predicate, predicate_norm FROM facts "
        "WHERE document_id = ? AND lifecycle_state IN "
        "('NORMALIZED','ELIGIBLE_FOR_REASONING') AND object_raw <> ''",
        (document_id,),
    ).fetchall()
    for r in rows:
        hay = f"{(r['predicate'] or '').lower()} {(r['predicate_norm'] or '').lower()}"
        if any(pat in hay for pat in settings.entity_rename_predicates):
            yield r["subject_raw"], r["object_raw"], r["id"]


# --------------------------------------------------------------------------- #
# batch                                                                      #
# --------------------------------------------------------------------------- #
def resolve_document(document_id: int, *, database_path: str | None = None,
                     settings: Settings | None = None, llm=None) -> DocResolutionSummary:
    settings = settings or get_settings()
    conn = db.connect(database_path)
    run_id: int | None = None
    try:
        if conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
            raise EntityError("unknown_document", detail=document_id)

        with db.transaction(conn):
            run_id = conn.execute(
                "INSERT INTO runs (document_id, run_type, status, stage, started_at) "
                "VALUES (?, 'resolve', 'running', 'resolve', ?)",
                (document_id, _now()),
            ).lastrowid

        facts = conn.execute(
            "SELECT id, subject_raw, lifecycle_state FROM facts WHERE document_id = ? "
            "AND lifecycle_state IN ('NORMALIZED','ELIGIBLE_FOR_REASONING') ORDER BY id",
            (document_id,),
        ).fetchall()
        surface_facts: dict[str, list[int]] = {}
        for f in facts:
            surface_facts.setdefault(f["subject_raw"], []).append(f["id"])

        c = dict.fromkeys(
            ("entities_created", "entities_linked", "aliases_added", "derived_aliases",
             "anaphora_resolved", "ambiguous", "promoted_eligible", "errors"), 0)
        by_method: dict[str, int] = {}
        resolved: dict[str, int] = {}
        anaphora: list[str] = []

        for surface in surface_facts:
            if _stripped(surface) in settings.entity_anaphora:
                anaphora.append(surface)
                continue
            try:
                with db.transaction(conn):
                    link, reason = _resolve_named(conn, surface, settings, run_id, llm)
                    if link.entity_id is None:
                        c["ambiguous"] += 1
                        _record_failure(conn, run_id, document_id, "entity_ambiguous",
                                        reason or "unresolved", surface)
                    else:
                        resolved[surface] = link.entity_id
                        c["entities_created" if link.created else "entities_linked"] += 1
                        if link.match_method:
                            by_method[link.match_method] = by_method.get(link.match_method, 0) + 1
                        c["aliases_added"] += 1
            except Exception as e:  # noqa: BLE001 — isolate a failing surface
                c["errors"] += 1
                with db.transaction(conn):
                    _record_failure(conn, run_id, document_id, "run_error",
                                    f"resolve_exception: {e!r}"[:300], surface)

        for subj, former, fid in _rename_pairs(conn, document_id, settings):
            try:
                with db.transaction(conn):
                    eid = resolved.get(subj)
                    if eid is None:
                        link, _ = _resolve_named(conn, subj, settings, run_id, None)
                        if link.entity_id is None:
                            continue
                        eid = resolved[subj] = link.entity_id
                    former_key = normalize_name(former, settings)
                    other = conn.execute(
                        "SELECT id FROM entities WHERE normalization_key = ? AND id <> ?",
                        (former_key, eid),
                    ).fetchone() if former_key else None
                    if other:
                        _merge_entities(conn, eid, other["id"])
                        resolved.update({s: eid for s, v in resolved.items() if v == other["id"]})
                    if _add_alias(conn, eid, former, former_key or None, "derived_fact", None, fid):
                        c["derived_aliases"] += 1
                        by_method["derived_fact"] = by_method.get("derived_fact", 0) + 1
            except Exception as e:  # noqa: BLE001
                c["errors"] += 1
                with db.transaction(conn):
                    _record_failure(conn, run_id, document_id, "run_error",
                                    f"rename_exception: {e!r}"[:300], subj)

        if anaphora:
            dominant = _dominant_entity(resolved, surface_facts)
            for surface in anaphora:
                if dominant is None:
                    c["ambiguous"] += 1
                    with db.transaction(conn):
                        _record_failure(conn, run_id, document_id, "entity_ambiguous",
                                        "anaphora_no_dominant_entity", surface)
                else:
                    resolved[surface] = dominant
                    c["anaphora_resolved"] += 1
                    by_method["anaphora"] = by_method.get("anaphora", 0) + 1
                    with db.transaction(conn):
                        _add_alias(conn, dominant, surface, None, "similarity", None, None)

        with db.transaction(conn):
            for surface, eid in resolved.items():
                conn.execute(
                    "UPDATE facts SET subject_entity_id = ? WHERE document_id = ? "
                    "AND subject_raw = ?",
                    (eid, document_id, surface),
                )

        for f in facts:
            if f["lifecycle_state"] != "NORMALIZED":
                continue
            try:
                with db.transaction(conn):
                    mark_reasoning_eligible(conn, f["id"])
                c["promoted_eligible"] += 1
            except FactError:
                pass  # evidence not eligible -> fact stays NORMALIZED (by design)

        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'done', stage = 'complete', finished_at = ? WHERE id = ?",
                (_now(), run_id),
            )
        log.info("resolved document_id=%s surfaces=%s created=%s linked=%s derived=%s "
                 "anaphora=%s ambiguous=%s promoted=%s", document_id, len(surface_facts),
                 c["entities_created"], c["entities_linked"], c["derived_aliases"],
                 c["anaphora_resolved"], c["ambiguous"], c["promoted_eligible"])
        return DocResolutionSummary(
            run_id=run_id, document_id=document_id, status="done",
            surfaces=len(surface_facts), by_method=by_method, **c,
        )
    except EntityError as e:
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, e.code)
            e.run_id = run_id
        raise
    except Exception as e:  # noqa: BLE001
        if run_id is not None:
            _mark_run_failed(conn, run_id, document_id, f"unexpected: {e!r}")
        raise EntityError("resolution_failed", detail=repr(e)[:200], run_id=run_id) from e
    finally:
        conn.close()


def _dominant_entity(resolved: dict[str, int], surface_facts: dict[str, list[int]]) -> int | None:
    tally: Counter[int] = Counter()
    for surface, eid in resolved.items():
        tally[eid] += len(surface_facts[surface])
    top = tally.most_common(2)
    if not top or (len(top) > 1 and top[0][1] == top[1][1]):
        return None
    return top[0][0]


def _record_failure(conn, run_id, document_id, failure_type, reason, surface) -> None:
    conn.execute(
        "INSERT INTO failures "
        "(run_id, document_id, failure_type, ref_table, ref_id, reason, detail, created_at) "
        "VALUES (?, ?, ?, 'entities', NULL, ?, ?, ?)",
        (run_id, document_id, failure_type, reason,
         json.dumps({"surface": surface}, ensure_ascii=False), _now()),
    )


def _mark_run_failed(conn, run_id: int, document_id: int, reason: str) -> None:
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (reason[:500], _now(), run_id),
            )
            _record_failure(conn, run_id, document_id, "run_error", reason[:500], None)
    except Exception:  # noqa: BLE001
        log.exception("failed to record resolve-run failure run_id=%s", run_id)


# --------------------------------------------------------------------------- #
# observability                                                              #
# --------------------------------------------------------------------------- #
def resolution_summary(
    conn: sqlite3.Connection, *, document_id: int | None = None
) -> dict[str, Any]:
    where = " WHERE document_id = ?" if document_id is not None else ""
    params: list[Any] = [document_id] if document_id is not None else []

    entities_total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    by_res_method = {
        r["resolution_method"]: r["n"]
        for r in conn.execute(
            "SELECT resolution_method, COUNT(*) AS n FROM entities GROUP BY resolution_method"
        )
    }
    by_alias_method = {
        r["match_method"]: r["n"]
        for r in conn.execute(
            "SELECT match_method, COUNT(*) AS n FROM entity_aliases GROUP BY match_method"
        )
    }
    facts_linked = conn.execute(
        f"SELECT COUNT(*) FROM facts{where}"
        f"{' AND' if where else ' WHERE'} subject_entity_id IS NOT NULL", params,
    ).fetchone()[0]
    facts_total = conn.execute(f"SELECT COUNT(*) FROM facts{where}", params).fetchone()[0]
    eligible = conn.execute(
        f"SELECT COUNT(*) FROM facts{where}"
        f"{' AND' if where else ' WHERE'} lifecycle_state = 'ELIGIBLE_FOR_REASONING'", params,
    ).fetchone()[0]
    ambiguous = conn.execute(
        f"SELECT COUNT(*) FROM failures{where}"
        f"{' AND' if where else ' WHERE'} failure_type = 'entity_ambiguous'", params,
    ).fetchone()[0]
    return {
        "entities_total": entities_total,
        "entities_by_resolution_method": by_res_method,
        "aliases_by_match_method": by_alias_method,
        "facts_linked": facts_linked,
        "facts_total": facts_total,
        "facts_eligible_for_reasoning": eligible,
        "entity_ambiguous_failures": ambiguous,
    }
