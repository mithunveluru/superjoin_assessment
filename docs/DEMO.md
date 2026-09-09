# Demo script (3 minutes)

Every number below is what the app actually shows with the demo database seeded.
Times are cumulative. Words in *italics* are what to say; **bold** is what to click.

The relationship examples come from **`scripts/seed_demo.py`**, a deterministic
**synthetic** two-document fixture (entity `Acme`) — a labelled *validation
fixture*, not corpus-derived data. It is used for the demo because it exercises
all five relationship categories in seven facts. A live Gemini extraction run on
one real Delhivery PDF **was** performed (27 pages → 53 facts, 51 grounded); it
is quoted at 2:35 rather than clicked. No cross-document relationship between two
*real* documents is claimed. See README → *Validation status*.

## Setup (once, off camera)

```bash
pip install -r requirements.txt
FKL_DATABASE_PATH=data/demo.db python scripts/seed_demo.py --force
FKL_DATABASE_PATH=data/demo.db uvicorn app.main:app        # http://localhost:8000/
```

No API key and no network needed for the walkthrough itself.

The dashboard should read **2 documents · 7 facts · 15 relationships · 1 entity
· 9 failures**. If it doesn't, re-run the seed command.

---

## 0:00–0:20 — What this is

Open **Overview**.

> *"Facts that matter are scattered across documents and stated differently. Two
> reports can give different numbers for the same thing because they use a
> different basis, a different period, or different units — and a naive
> comparison calls every one of those a contradiction. This system grounds each
> fact in its source evidence first, then compares facts through explicit
> signals, so a contextual difference is never reported as a contradiction."*

Point at the **pipeline strip** at the bottom:
`PDF → ingest → extract → verify → normalize → resolve → retrieve → reason → API/UI`.

> *"LLMs interpret meaning. Deterministic code verifies evidence, normalizes the
> numbers, and makes the final call."*

## 0:20–0:50 — A fact is only as good as its evidence

**Facts** → set **Lifecycle** to `ELIGIBLE_FOR_REASONING` → **Apply** → click the
row **`8,142 Cr · Acme · revenue from services`**.

Point at, in order:

- the **verbatim quote** — *"the exact sentence from the page"*
- the source line — *"document, printed page, PDF page, and `verified exact`"*
- **Reporting period** `FY24 [2023-04-01 → 2024-04-01) · fiscal_year`
- expand **Normalized representation** → `base_value`, `currency INR`

> *"Nothing becomes eligible for reasoning until its quote has been re-derived
> from the source page — character offsets and all. The chain is fact → evidence
> → chunk → page → document, and it either resolves or the fact is
> quarantined."*

## 0:50–1:15 — Corroboration across documents

**Relationships** → **Corroborates** chip → open the card.

- Fact A `81,415.38 million` — Annual Report
- Fact B `8,142 Cr` — Earnings Deck

Expand → **Deterministic signals**.

> *"Different documents, different wording, different scale words — crore versus
> million. After normalization they are the same number: the delta is
> 0.006 percent and `unit_equivalent` is true. Same proposition, both grounded."*

## 1:15–1:40 — A real contradiction

**Contradicts** chip (the card carries a red edge) → open it.

- `profit after tax · 5,000.00 million` vs `8,000.00 million`, same entity, same FY24

> *"Same entity, same period, no scope difference, values 37.5 percent apart —
> past the contradiction threshold. This one the system is willing to call a
> genuine disagreement."*

## 1:40–2:10 — The interesting case: context, not conflict

**Different context** chip → open the card whose two facts are
`81,415.38 million` and `74,540.82 million`, **both FY24**.

Expand → **Why this call**.

> *"These differ by 8.4 percent and share an entity and a period — a naive
> system reports a contradiction. But one is consolidated and the other
> standalone. `scope_conflict` is non-empty, so it is classified as different
> context, with the dimension named: `scope:basis`. This is the case the whole
> design exists for."*

Then **Temporal evolution** → the FY24 vs FY23 pair.

> *"Same measure, adjacent periods, value moved 26.3 percent. That is a number
> that changed over time, not a disagreement."*

## 2:10–2:35 — Abstention and failure are first-class

**Uncertain** chip → open one.

> *"Same entity and period, but `revenue from operations` versus `profit after
> tax` — predicate similarity 0.27. Not the same measure, so the system abstains
> instead of guessing. Eight of fifteen pairs here are uncertain by design."*

**Failures** view.

> *"And a fabricated quote — `permanent employees · 99,999` — failed
> verification with `quote_not_found in source text`. It is quarantined: kept,
> inspectable, promoted nowhere, and it can never enter a relationship."*

## 2:35–3:00 — Close

> *"The split is a deterministic core with the LLM used only for genuine
> semantic ambiguity, and the deterministic layer always has the last word — an
> LLM `CONTRADICTS` on numbers that are equal after normalization is overridden
> to `CORROBORATES`."*
>
> *"447 tests, ruff clean, no network needed. Beyond this fixture, a real
> Delhivery PDF was extracted end-to-end with live Gemini — 27 pages, 53 facts,
> 51 grounded against their source quotes. Limitations, including the free-tier
> quota and extraction quality on boilerplate pages, are in `docs/RISKS.md` and
> the README."*

---

## If you have 30 extra seconds

**Documents** → upload any PDF → **Process** → the row polls
`processing → done | failed`, and a failed stage names its own reason inline.
Needs `GEMINI_API_KEY` and remaining daily quota; without either, the row fails
at `extract` within seconds and says exactly why.

## Where each quoted number comes from

| Claim | Source |
|---|---|
| 2 docs · 7 facts · 15 relationships · 1 entity · 9 failures | `scripts/seed_demo.py`, deterministic |
| corroboration delta 0.006 %, `unit_equivalent` | relationship signals, synthetic fixture |
| contradiction 37.5 % vs the 15 % threshold | `FKL_NUMERIC_CONTRADICTION_THRESHOLD` |
| different context 8.4 %, `scope:basis` | relationship signals, synthetic fixture |
| temporal evolution 26.3 %, adjacent periods | relationship signals, synthetic fixture |
| uncertain predicate similarity 0.27 | relationship signals, synthetic fixture |
| 447 tests, ruff clean | `pytest`, `ruff check .` |
| 27 pages, 53 facts, 51 grounded | live Gemini run, README → *Validation status* |
