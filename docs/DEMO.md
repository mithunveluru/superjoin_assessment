# Demo video script — 2 minutes

Read it straight through while you record. The clicks are written into what you
say. Square brackets are silent cues — don't read them out.

**Setup before recording**

```bash
FKL_DATABASE_PATH=data/demo.db python scripts/seed_demo.py --force
FKL_DATABASE_PATH=data/demo.db uvicorn app.main:app
```

Open `http://localhost:8000/` on **Overview**. Tiles should read
**2 · 7 · 15 · 1 · 1**. Zoom 100 %, no other tabs.

~290 words, about 1 min 55 s spoken.

---

**[0:00 — Overview on screen]**

"Hey — so this is the Fact Knowledge Layer.

You give it PDFs. It pulls out facts, and it ties every single fact back to the
exact sentence it came from. Then it works out how facts from different
documents relate to each other.

The tricky part isn't pulling facts out. It's that two documents can say the
same thing with completely different numbers, and both of them be right. That's
the problem I wanted to solve.

**[0:22 — click Facts, filter Lifecycle to ELIGIBLE_FOR_REASONING, Apply, open the `8,142 Cr` row]**

Let me show you one fact. I'll filter down to the ones that passed verification,
and open this one — Acme, revenue from services, eight thousand crore.

You can see the quote it came from, the document, the page. And that quote was
checked back against the source page, character by character. If it doesn't
match, the fact gets quarantined and never used.

**[0:50 — click Relationships, Corroborates chip, open the card]**

Now the relationships. This first one's a match across two different documents.
One's written in millions, the other in crore — completely different wording.
But after normalising, it's the same number. So it marks them as corroborating.

**[1:10 — click Contradicts, open the red-edged card]**

This one's a real contradiction. Same company, same period, same basis — but the
profit figures are thirty-seven percent apart. That's a genuine conflict.

**[1:22 — click Different context, open the card with 81,415.38 and 74,540.82]**

And this is the one I care about most. These two also look like a contradiction —
eight percent apart, same company, same year. But one's consolidated and one's
standalone. It catches that, and calls it a context difference instead.

**[1:42 — click Uncertain, then Failures]**

It also knows when to stay quiet. If two facts aren't the same measure, it says
uncertain instead of guessing. And in Failures, anything that couldn't be
verified is quarantined — still visible, but never used.

**[1:55 — back to Overview]**

So: deterministic core, model only for the genuinely ambiguous bits. Thanks for
watching."

---

## Notes

- This corpus is a labelled synthetic fixture — it's the one that exercises all
  five relationship categories. If you have room, say so out loud; it lands
  better than being asked. The real run (27-page Delhivery PDF on live Gemini,
  53 facts, 51 grounded) is in the README under *Validation status*.
- Optional beat if you're under time: Documents → upload a PDF → **Process** →
  the row polls `processing → done | failed` and names its own failure reason.
  Needs `GEMINI_API_KEY` and remaining daily quota.
- Don't show Uncertain counts on the real Delhivery corpus — extraction quality
  on boilerplate pages inflates them.
