# Demo video script

Read this straight through while you screen-record. The navigation is written
into the narration — say the words, do the thing you're saying. Square brackets
are silent cues, don't read them.

**435 spoken words ≈ 2 min 55 s** at a normal pace. If you speak slowly and run
long, drop the *Temporal evolution* beat at 1:58 — it's the one section the demo
survives without.

**Setup before recording**

```bash
FKL_DATABASE_PATH=data/demo.db python scripts/seed_demo.py --force
FKL_DATABASE_PATH=data/demo.db uvicorn app.main:app
```

Open `http://localhost:8000/` on the **Overview** view. The five tiles should
read **2 · 7 · 15 · 1 · 1**. Browser zoom 100 %, no other tabs open.

---

**[0:00 — Overview on screen, don't click yet]**

"Hi — this is the Fact Knowledge Layer. It takes PDFs, pulls out facts, ties
every fact to the exact sentence it came from, then works out how facts across
different documents relate. The hard part isn't extraction — it's that two
documents can state the same thing with different numbers and both be right.

Down here is the pipeline it ran through. The model only interprets meaning —
verifying evidence, normalizing the numbers, and making the final call are all
deterministic code.

**[0:22 — click Facts, set Lifecycle to ELIGIBLE_FOR_REASONING, click Apply]**

Let me start with one fact. I'll filter to the ones eligible for reasoning —

**[click the `8,142 Cr` row]**

— and open this one. Acme, revenue from services, eight thousand one hundred
forty-two crore.

It opens to the verbatim quote, the document, the page, and confirmation the
quote was re-derived from that page exactly. FY24 is resolved into real dates,
the value normalized into a comparable base. Nothing becomes eligible until that
chain resolves — if the quote isn't found, the fact is quarantined instead.

**[0:50 — click Relationships, then the Corroborates chip, open the card]**

Now the interesting part. Relationships — starting with corroboration.

This pair is from two different documents. One says eighty-one thousand four
hundred fifteen million, the other eight thousand one hundred forty-two crore.
Different wording, different scale —

**[expand Deterministic signals]**

— but expand the signals, and after normalization they're the same number, to
six thousandths of a percent.

**[1:15 — click the Contradicts chip, open the red-edged card]**

Contradictions next. Profit after tax, same company, same period, same scope,
but five thousand million against eight thousand — thirty-seven percent apart,
past the threshold. A real disagreement.

**[1:33 — click Different context, open the card showing 81,415.38 and 74,540.82]**

And here's the case the whole thing exists for. These two differ by eight percent
— same company, same period. A naive system shouts contradiction. But expand the
reasoning: one is consolidated, the other standalone. It picks up that scope
conflict, names the dimension, and calls it a context difference, not a
contradiction.

**[1:58 — click Temporal evolution, open the FY24-vs-FY23 card]**

Same idea across time. Same measure, adjacent years, the value moved twenty-six
percent. A number that changed, not two sources disagreeing.

**[2:12 — click Uncertain, open a card]**

And it knows when to stop. Revenue against profit after tax — same company, same
period, but not the same measure, so it abstains. Eight of the fifteen pairs
here are uncertain by design.

**[2:27 — click Failures in the sidebar]**

Same on the failure side. This fact's quote couldn't be verified against its
page, so it's quarantined — kept and inspectable, but it can never enter a
relationship.

**[2:40 — click back to Overview]**

So: deterministic core, the model only for genuine ambiguity, and deterministic
code gets the last word. Four hundred forty-seven tests, no network needed. This
corpus is a labelled synthetic fixture — the one that exercises all five
categories. Separately, a real twenty-seven page earnings PDF ran end to end on
live Gemini: fifty-three facts, fifty-one grounded. Thanks for watching."

---

## Notes

- If you have 20 seconds spare, add an upload beat after the fact walkthrough:
  Documents → upload a PDF → **Process** → the row polls
  `processing → done | failed`, and a failure names its own reason inline.
  Needs `GEMINI_API_KEY` and remaining daily quota.
- Say the synthetic-fixture line out loud. It lands better than being asked.
- Don't show Uncertain counts on the real Delhivery corpus — extraction quality
  on boilerplate pages inflates them. The fix is in `extraction_v2.md` but hasn't
  been re-measured against live Gemini yet.
