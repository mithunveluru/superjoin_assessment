# Recording walkthrough — 3 minutes

Read the **SAY** lines aloud, word for word. Do the **DO** lines on screen while
you say them. Timings are cumulative. The narration is 382 words — about
2 min 45 s spoken, leaving room for click pauses inside three minutes.

## Before you hit record

```bash
FKL_DATABASE_PATH=data/demo.db python scripts/seed_demo.py --force
FKL_DATABASE_PATH=data/demo.db uvicorn app.main:app
```

- Browser at **http://localhost:8000/**, on the **Overview** view.
- Close other tabs; hide bookmarks bar. Browser zoom **100 %**, window ~1440 wide.
- Check the five tiles read **2 · 7 · 15 · 1 · 1** (documents, facts,
  relationships, entities, quarantined). If not, re-run the seed line.
- Have the **Relationships** view loaded once already so it is warm.

---

### 0:00 — 0:18 · Opening

**DO** — Sit on **Overview**. Don't click anything yet.

**SAY**
> "This is a fact knowledge layer. It reads PDFs, extracts facts, ties every one
> to the exact sentence it came from, and works out how facts across documents
> relate. The hard part isn't extraction — it's that two documents can state the
> same thing with different numbers and both be correct."

---

### 0:18 — 0:30 · The pipeline

**DO** — Scroll to the **Pipeline** strip at the bottom of Overview.

**SAY**
> "The pipeline is ingest, extract, verify, normalize, resolve, retrieve,
> reason. The language model only interprets meaning. Deterministic code
> verifies the evidence, normalizes the numbers, and makes the final call."

---

### 0:30 — 1:00 · Every fact is grounded

**DO** — Click **Facts** → set **Lifecycle** to `ELIGIBLE_FOR_REASONING` →
**Apply** → click the row **`8,142 Cr`**.

**SAY**
> "Acme, revenue from services, eight thousand one hundred forty-two crore.
> Opening it gives the verbatim quote, the document, the page, and confirmation
> the quote was re-derived from that page exactly. FY24 is resolved to real
> dates and the value normalized to a comparable base. Nothing becomes eligible
> for reasoning until that chain resolves."

---

### 1:00 — 1:22 · Corroboration

**DO** — Click **Relationships** → **Corroborates** chip → click the card header
→ expand **Deterministic signals**.

**SAY**
> "This pair comes from two different documents. One says eighty-one thousand
> four hundred fifteen million, the other eight thousand one hundred forty-two
> crore. Different words, different scale — after normalization, the same
> number, to six thousandths of a percent. Corroborated."

---

### 1:22 — 1:44 · A real contradiction

**DO** — Click the **Contradicts** chip → open the card with the red left edge.

**SAY**
> "A genuine conflict. Profit after tax, same company, same period, same scope —
> but five thousand million against eight thousand. Thirty-seven percent apart,
> past the contradiction threshold. This one it will call a real disagreement."

---

### 1:44 — 2:12 · Context, not conflict  ← the key moment

**DO** — Click **Different context** → open the card whose two values are
**81,415.38** and **74,540.82** → expand **Why this call**.

**SAY**
> "And this is what the whole design exists for. These two differ by eight
> percent, same company, same period. A naive system reports a contradiction.
> But one figure is consolidated and the other is standalone. The system detects
> that scope conflict, names the dimension, and classifies it as a context
> difference — not a contradiction."

---

### 2:12 — 2:26 · Time

**DO** — Click **Temporal evolution** → open the FY24-versus-FY23 card.

**SAY**
> "Same idea across time. Same measure, adjacent years, the value moved
> twenty-six percent. That's a number that changed, not a disagreement."

---

### 2:26 — 2:44 · Knowing when to stop

**DO** — Click **Uncertain** → open one card. Then click **Failures** in the
sidebar.

**SAY**
> "It also refuses to guess. Revenue versus profit after tax — same company,
> same period, but not the same measure, so it abstains. And here in Failures, a
> fact whose quote could not be verified against the page is quarantined. It's
> kept and inspectable, but it can never enter a relationship."

---

### 2:44 — 3:00 · Close

**DO** — Click back to **Overview**.

**SAY**
> "So: deterministic core, the model only for genuine ambiguity, and the
> deterministic layer always has the last word. Four hundred forty-seven tests,
> no network needed. And beyond this fixture, a real twenty-seven page earnings
> PDF ran end to end on live Gemini — fifty-three facts, fifty-one grounded."

---

## Notes for the recording

- The demo corpus is a **labelled synthetic fixture** — it's the only corpus that
  contains all five relationship categories, and saying so out loud is stronger
  than implying it's real data.
- If asked "is this real?": the real Delhivery PDF run is in the README under
  *Validation status* — 27 pages, 53 facts, 51 grounded.
- Optional 20-second add-on if you have room: **Documents** → upload a PDF →
  **Process** → the row polls `processing → done | failed` and a failure names
  its own reason. Needs `GEMINI_API_KEY` plus remaining daily quota.
- Don't demo Uncertain counts on the real corpus — extraction quality on
  boilerplate pages inflates them; the fix is in `extraction_v2.md` but hasn't
  been re-measured yet.
