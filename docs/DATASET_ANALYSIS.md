# Dataset Analysis

**Status:** design/test observations only. Nothing here becomes application logic, a
filename check, an entity map, or an expected-answer table. The pipeline must
rediscover all of this from any corpus.

Page numbers below are **PDF sequence pages** (1-based position in the file). The
printed page label inside each document differs and jumps between retained sections
(see dataset READMEs). Capturing both numbers is a Phase 2 requirement.

---

## 1. Dataset structure

Two independent corpora under `starter-datasets/`. They do not share entities and
are never reasoned across each other.

| Corpus | Docs | Domain | Why interesting |
|---|---|---|---|
| `delhivery/` | 3 | One company, 3 disclosure formats, 3 dates (2022, Aug 2024, May 2024) | Same facts restated across prospectus / annual report / earnings deck; units and scope differ |
| `india-macroeconomy/` | 3 | Indian macro economy, 3 publishers, overlapping vintages (Jan 2025, May 2025, Nov 2025) | Same indicators from different institutions and data vintages; fiscal-year notation varies |

## 2. Document types & provenance

| File | Type | Publisher | Date | Producer (metadata) | Layout notes |
|---|---|---|---|---|---|
| delhivery/01-…prospectus-2022 | IPO prospectus (SEBI) | Delhivery Ltd | 2022-05-14 | MS Word → 3-Heights | A4, dense legal prose + restated financials in ₹ million |
| delhivery/02-…annual-report-fy24 | Annual report FY24 | Delhivery Ltd | 2024-08-08 | Adobe InDesign | **A3 landscape**, multi-column; MD&A + full financial statements |
| delhivery/03-…q4-fy24-earnings | Earnings presentation | Delhivery Ltd | 2024-05-17 | PowerPoint → Acrobat | Slides; heavy tables & charts; values in ₹ Cr |
| india-macro/01-…economic-survey-2024-25 | Govt flagship report | Ministry of Finance (GoI) | 2025-01-30 | InDesign | 2-col, charts, footnotes; "advance estimate" language |
| india-macro/02-…rbi-annual-report-2024-25 | Central-bank annual report | RBI | 2025-05-25 | InDesign | prose + large statistical appendix tables (year columns, no inline labels) |
| india-macro/03-…imf-india-2025-article-iv | IMF Article IV staff report | IMF | 2025-11-22 (tagged PDF) | Adobe Acrobat | Letter; staff report + statistical tables + annexes; FY labelled `FY2024/25` |

Provenance signals the system can extract generically: publisher name on cover /
running header, document title, "as of" / "for the year ended" dates, disclosure
type wording ("Prospectus", "Annual Report", "Staff Report").

## 3. Important entities

**Delhivery corpus** — mostly one primary entity plus people and subsidiaries:

- Delhivery (the company) — see aliases §7
- Subsidiaries / associates: SpotOn Logistics Pvt Ltd, Falcon Autotech Pvt Ltd, Vinculum, Boxseat Ventures, Delhivery Robotics, aramex/FedEx (partners, not group)
- People (directors / KMP): Sahil Barua (MD & CEO), Sandeep Kumar Barasia, Kapil Bharati, Suraj Saharan, Mohit Tandon, Suvir Suren Sujan, Aruna Sundararajan, Anindya Ghose, Madhulika Rawat (Company Secretary), Sunil Kumar Bansal (former CS)
- Business segments: Express Parcel, PTL / Part Truckload, TL / Truckload, Supply Chain Services (SCS), Cross Border
- Places: Registered Office (IGI Airport, New Delhi 110037), Corporate Office (Plot 5, Sector 44, Gurugram 122002)

**Macro corpus** — abstract entities:

- "Indian economy" / "India" (the subject of most indicators)
- Institutions as both publishers and actors: RBI, IMF, Ministry of Finance, WTO, India Post
- Indicators as entities: real GDP growth, CPI / headline inflation, current account deficit (CAD), fiscal deficit, global growth

## 4. Examples of numerical facts

| Fact (subject · predicate · value) | Doc / PDF page | Verbatim evidence (excerpt) |
|---|---|---|
| Delhivery · revenue from services FY24 · ₹8,142 Cr | delhivery/03 p5 | "₹8,142 Cr … FY24 revenue from services … YoY: 12.7%" |
| Delhivery · revenue from operations (consolidated) FY24 · ₹81,415.38 mn | delhivery/02 p52 | "revenue from operations on consolidated basis for FY24 stood at ₹ 81,415.38 million as against ₹72,253.01 [million for FY23]" |
| Delhivery · revenue from operations (standalone) FY24 · ₹74,540.82 mn | delhivery/02 p52 | "revenue from operations on standalone basis for FY24 … stood at ₹ 74,540.82 million as against ₹66,586.61 million for FY23" |
| Delhivery · EBITDA FY24 · ₹127 Cr | delhivery/03 p5,16 | "EBITDA / EBITDA margin … ₹127Cr / 1.6% … FY23: ₹(452) Cr / (6.3%)" |
| Delhivery · PIN codes served · 17,488 (9M ended 31 Dec 2021) | delhivery/01 p47 | "servicing 17,488 PIN codes during the nine months period ended December 31, 2021, or 90.61% of the 19,300 PIN codes in India" |
| Delhivery · pin-code reach · 18,793 (Q4 FY24) | delhivery/03 p7 | "Pin-code reach 18,074 / 18,540 / 18,675 / **18,793**" (Q4FY22…Q4FY24 columns) |
| Delhivery · permanent employees on rolls · 23,381 (31 Mar 2024) | delhivery/02 p34 | "Permanent employees on the rolls of the Company were 23,381 as on March 31, 2024." |
| Delhivery · team size · 63,713 (Q4 FY24) | delhivery/03 p7 | "Team size … 63,713" (footnote: permanent employees + contractual workers, excl. partner agents) |
| India · real GDP growth FY25 · ~6.4% (first advance estimate) | india-macro/01 | "real GDP is estimated to [grow] … 6.4 per cent" |
| India · real GDP growth FY2025/26 · 6.6% (projected), 6.2% thereafter | india-macro/03 | "GDP is projected to grow at 6.6 percent in FY2025/26 before moderating to 6.2 percent" |
| India · headline inflation FY26 · 4.2% (RBI expectation) | india-macro/01 | "the RBI expects headline inflation to be 4.2 per cent in FY26. IMF has projected [the same]" |
| India · CAD · 1.2% of GDP in Q2 FY25 | india-macro/01 | "India's current account deficit (CAD) moderated slightly to 1.2 per cent of GDP in Q2 [FY25]" |
| India · CAD · 0.2% of GDP (recent quarter) | india-macro/03 | "CAD at 0.2 percent of GDP. Since July, the rupee has depreciated…" |
| Global growth 2025 · 3.3% (IMF) | india-macro/02 | "International Monetary Fund (IMF), global growth at 3.3 per cent" |

## 5. Examples of semantic facts

| Fact | Doc / PDF page | Evidence |
|---|---|---|
| Delhivery former name · "SSN Logistics Private Limited" (2011-06-22) | delhivery/01 p30 | "Our Company was incorporated as 'SSN Logistics Private Limited' … certificate of incorporation issued by the RoC on June 22, 2011" |
| Delhivery renamed → "Delhivery Private Limited" (2015-12-08), then "Delhivery Limited" (2021-10-12) | delhivery/01 p30 | "fresh certificate of incorporation … name … 'Delhivery Limited' … dated October 12, 2021" |
| Sandeep Kumar Barasia · board role · Executive Director & Chief Business Officer (during FY24) | delhivery/02 p~90 | "Mr. Sandeep Kumar Barasia (Executive Director & Chief Business Officer)" |
| Sandeep Kumar Barasia · resigned from directorship · effective 2024-07-01 (post FY24) | delhivery/02 p41 | "Mr. Sandeep Kumar Barasia resigned from the Directorship with effect from July 01, 2024." |
| Suvir Suren Sujan · resigned · effective 2023-08-24 | delhivery/02 p41 | "Suvir Suren Sujan, Non-Executive Director, resigned from the Board with effect from August 24, 2023." |
| Delhivery · described as · "India's largest integrated logistics platform" | delhivery/03 p5 | "India's largest integrated logistics platform (1) … As per RedSeer report basis FY21 revenue" |
| Delhivery · FY24 was · "first full year of EBITDA profitability" | delhivery/02 | "FY24 was our first full year of EBITDA profitability." |
| India · inflation dynamics · "benign" / "declined to 1.5 percent in September" | india-macro/03 | "Inflation dynamics are benign. Headline inflation has declined to 1.5 percent in September" |

## 6. Temporal information

- **Indian fiscal year = 1 Apr → 31 Mar.** Notations seen: `FY24`, `FY 2023-24`, `Fiscal 2021`, `FY2024/25` (IMF), `2024-25`, `Q4 FY24`, `Q2 FY25`, `nine months period ended December 31, 2021`, `as on March 31, 2024`, `Mar '24`.
- Report vintages matter: the same indicator is reported by ES (Jan 2025, advance estimate), RBI (May 2025), IMF (Nov 2025) — later = more revised.
- "as of" dates for counts (pin codes, employees, fleet) vs "for the period" dates for flows (revenue, shipments).
- Forward-looking vs actual: "projected", "expected to", "estimated", "first advance estimate", "staff baseline scenario".

## 7. Scope information

Scope qualifiers that must **not** be normalized away:

- Consolidated vs standalone (Delhivery revenue differs by ~₹6,900 mn on this alone)
- Reported vs adjusted vs pro forma vs restated (EBITDA vs Adjusted EBITDA vs Service EBITDA)
- Segment scope: total vs Express Parcel / PTL / TL / SCS / Cross Border
- Counting basis: "permanent employees on rolls" vs "team size (incl. contractual)" vs "incl. partner agents" (23,381 / 63,713 / 98,135 for the same company, same date)
- Geographic scope: "India" vs "global"; "17,488 PIN codes" vs "19,300 PIN codes in India (per India Post)" (numerator vs denominator)
- Period type: quarterly vs full-year vs YTD; point-in-time reading vs annual average (inflation 1.5% Sept vs 4.2% FY avg)

## 8. Units

- Currency magnitudes: **₹ crore (Cr) = 10 million**; **₹ lakh = 0.1 million**; ₹ million; "₹ in million". Prospectus & AR financial statements use ₹ million; earnings deck uses ₹ Cr; narrative sometimes writes "₹1,266 million" which is easily misread as ₹1,266 crore.
- Percent vs percentage points vs basis points (bps): "EBITDA margin 1.6%", "781 Bps" improvement, "149 Bps".
- Counts: shipments in Mn / Bn, tonnage in "Tons", "'000 Tons", "Mn Tons"; area in "million sq ft".
- Negative values shown as parentheses: `(452)`, `₹(404) Cr`, `(6.3%)`.
- Ratios: "Debt/Equity 0.01x", "NWC 31 days".

## 9. Currencies

Almost entirely INR (₹, Rs, INR). USD appears in the macro/industry context:
"US$216 billion in Fiscal 2020" (logistics spend), IMF figures quoted in USD bn for
trade/reserves. Forex earnings quoted in ₹ million. No FX conversion should be
attempted — currency is a scope dimension, not something to normalize.

## 10. Terminology variations (same measure, different words)

| Concept | Variants seen |
|---|---|
| Top-line | "revenue from services", "revenue from operations", "revenue from customers", "total revenue from customers", "revenue from services (excl. traded goods)" |
| Profitability | "EBITDA", "Reported EBITDA", "Adjusted EBITDA", "Service EBITDA", "PAT", "Profit/(Loss) after tax", "loss for the year" |
| Headcount | "permanent employees on the rolls", "team size", "headcount", "total headcount", "on-roll & permanent workforce" |
| Network reach | "PIN codes", "pin-code reach", "pin codes covered", "servicing … PIN codes" |
| Inflation | "CPI inflation", "headline inflation", "retail inflation" |
| Growth | "real GDP growth", "real GDP is estimated to", "GDP is projected to grow", "growth of X per cent" |
| Deficit | "current account deficit", "CAD", "current account balance" (sign flips) |

## 11. Entity aliases (must be discovered, not hard-coded)

| Canonical (discovered) | Surface forms in corpus | Discovery signal |
|---|---|---|
| Delhivery Limited | "Delhivery Limited", "Delhivery", "the Company", "our Company", "SSN Logistics Private Limited", "Delhivery Private Limited" | legal-suffix stripping + fuzzy match; **the rename is an explicit extracted fact** (§5) that adds the historical aliases |
| CIN of Delhivery | `U63090DL2011PLC221234` (prospectus) vs `L63090DL2011PLC221234` (AR) | same 21-char CIN except first letter; "U"→"L" = unlisted→listed after IPO — a reconcilable difference, not two entities |
| Registered Office | "N24-N34, S24-S34, Air Cargo Logistics Centre-II, Opposite Gate 6 Cargo Terminal, Indira Gandhi International Airport, New Delhi 110037" vs "…IGI Airport, New Delhi 110037" | address normalization / high token overlap + shared PIN 110037 |
| SpotOn | "SpotOn Logistics Private Limited", "Spoton", "SpotOn" | case + suffix |
| RBI | "Reserve Bank of India", "RBI", "the Reserve Bank", "the central bank" | abbreviation expansion appears in-text |
| Sandeep Kumar Barasia | "Sandeep Kumar Barasia", "Mr. Sandeep Kumar Barasia", "S. K. Barasia" | honorific / initial stripping |

## 12. Candidate relationships

Format per the assignment: Fact A, Fact B, why related, context to consider.
**Category is the system's job, not pre-labelled here.** The "design expectation"
line is a hypothesis for the Phase‑10 evaluation harness to check as a
*property* — it never becomes a rule, an alias, or an answer key in `app/`.
Taxonomy: `CORROBORATES | CONTRADICTS | DIFFERENT_CONTEXT | TEMPORAL_EVOLUTION |
UNCERTAIN` (DECISIONS D12). "Different dates" alone imply nothing — a value that
genuinely moved over time is `TEMPORAL_EVOLUTION`, not a contradiction and not
something to reconcile.

### C1 — Corroboration across formats/units (Delhivery revenue FY24)
- **A:** Delhivery · revenue from operations (consolidated) FY24 = **₹81,415.38 million** — delhivery/02, PDF p52 — *"revenue from operations on consolidated basis for FY24 stood at ₹ 81,415.38 million"*
- **B:** Delhivery · revenue from services FY24 = **₹8,142 Cr** — delhivery/03, PDF p5 — *"₹8,142 Cr — FY24 revenue from services"*
- **Why related:** same entity, same period (FY24), same concept (top-line); values equal after Cr→million (8,142 Cr = 81,420 mn ≈ 81,415 mn, rounding).
- **Context to consider:** unit conversion (Cr vs million); "operations" vs "services (excl. traded goods)" — traded-goods revenue is ~0 in FY24 so they coincide; consolidated basis on both.
- **Design expectation:** CORROBORATES.

### C2 — Apparent contradiction reconciled by scope (Delhivery revenue FY24, consolidated vs standalone)
- **A:** revenue from operations FY24 = **₹74,540.82 million** — delhivery/02, PDF p52 — *"on standalone basis for FY24 … ₹ 74,540.82 million"*
- **B:** revenue from operations FY24 = **₹81,415.38 million** — delhivery/02, PDF p52 — *"on consolidated basis for FY24 … ₹ 81,415.38 million"*
- **Why related:** same entity, same period, same predicate, values differ ~9%.
- **Context to consider:** `basis = standalone` vs `basis = consolidated` — an explicit scope qualifier present in both sentences.
- **Design expectation:** DIFFERENT_CONTEXT, `context_dimension=scope:basis` (standalone vs consolidated).

### C3 — Apparent contradiction reconciled by counting basis (Delhivery headcount)
- **A:** permanent employees on rolls = **23,381** as on 2024-03-31 — delhivery/02, PDF p34 — *"Permanent employees on the rolls of the Company were 23,381 as on March 31, 2024."*
- **B:** team size = **63,713** (Q4 FY24) — delhivery/03, PDF p7 — *"Team size … 63,713"* (footnote: *"Includes permanent employees and contractual workers (excluding partner agents…)"*); AR page-1 highlight adds **98,135** *(incl. partner agents)*.
- **Why related:** same entity, same date, "how many people work here".
- **Context to consider:** `counting_basis` — permanent-on-roll ⊂ team size ⊂ incl. partner agents. Footnotes define the scope.
- **Design expectation:** DIFFERENT_CONTEXT, `context_dimension=scope:counting_basis`.

### C4 — Apparent contradiction reconciled by time (Delhivery PIN-code reach)
- **A:** serves **17,488** PIN codes, 9M ended 2021-12-31 — delhivery/01, PDF p47 — *"servicing 17,488 PIN codes during the nine months period ended December 31, 2021"*
- **B:** pin-code reach **18,793** as of Q4 FY24 — delhivery/03, PDF p7; AR narrative *"reach more than 18,700 out of the 19,300 pin codes in India"*
- **Why related:** same entity, same predicate, values differ and rise monotonically over time.
- **Context to consider:** `as_of` date 2021-12 vs 2024-03; network expansion. Also note 19,300 is the India-Post denominator, not a Delhivery figure — guard against comparing 17,488 to 19,300 as if the same predicate.
- **Design expectation:** TEMPORAL_EVOLUTION (same predicate, disjoint as‑of dates, both HISTORICAL, value rises).

### C5 — Time-scoped director status (NOT pre-classified — the system must decide)
- **A:** Sandeep Kumar Barasia · board role · "Executive Director & Chief Business Officer" — delhivery/01 (2022) board list and delhivery/02 FY24 body — modality `HISTORICAL`, period ≈ up to FY24 (year ended 2024-03-31).
- **B:** Sandeep Kumar Barasia · board status · "resigned from the Directorship with effect from July 01, 2024" — delhivery/02, PDF p41 — modality `HISTORICAL`, instant 2024-07-01.
- **Why related:** same person, same entity, predicate = board membership status; values differ.
- **Context to consider:** `reporting_period` of A vs the effective date of B. If A's period ends on/before 2024-07-01 and B is a later dated event, both `HISTORICAL`, `period_relation=disjoint` → this is **TEMPORAL_EVOLUTION** (he really did hold the role, then really did resign), *not* a contradiction and *not* something to "reconcile". It becomes `CONTRADICTS` only if a document asserts him an active director for a period that *overlaps* 2024-07-01 or later.
- **Design expectation (not asserted, not hard-coded):** `TEMPORAL_EVOLUTION` on the starter data; the classifier reaches it from dates + modality via `signals.validate()`, with no rule that mentions this person.

### C6 — Entity identity across a rename (Delhivery)
- **A:** "Our Company was incorporated as 'SSN Logistics Private Limited' … on June 22, 2011" — delhivery/01, PDF p30.
- **B:** "Delhivery Limited … CIN L63090DL2011PLC221234" — delhivery/02, PDF p30.
- **Why related:** the CIN embeds `2011` and `DL` and `221234`; the prospectus CIN `U63090DL2011PLC221234` differs only U→L; the rename chain is stated explicitly.
- **Context to consider:** company-law status change (private→public, unlisted→listed). Same legal entity.
- **Design expectation:** feeds entity resolution (a `derived_fact` alias edge); not a fact‑vs‑fact relationship.

### C7 — Corroboration across publishers (India headline-inflation projection)
- **A:** "the RBI expects headline inflation to be **4.2 per cent** in FY26" — india-macro/01 (Economic Survey).
- **B:** "IMF has projected [headline inflation]" at the same figure — india-macro/01, same sentence; cross-check against india-macro/03 (IMF report) medium-term projection.
- **Why related:** same indicator, same target period (FY26), two institutions.
- **Context to consider:** projection vs actual (`modality = projected`); which vintage; whether "FY26" (RBI) and IMF's "FY2025/26" denote the same 12 months.
- **Design expectation:** CORROBORATES.

### C8 — Apparent contradiction reconciled by data vintage & FY labelling (India real GDP growth)
- **A:** real GDP growth FY25 ≈ **6.4%** ("first advance estimate") — india-macro/01 (Jan 2025).
- **B:** real GDP growth **6.6%** for "FY2025/26", moderating to 6.2% — india-macro/03 (IMF, Nov 2025); RBI (india-macro/02, May 2025) gives its own figure in Table II.2.1.
- **Why related:** same indicator, same country, overlapping-looking periods.
- **Context to consider:** (i) FY label mismatch — ES "FY25" = year ended Mar 2025; IMF "FY2025/26" = year ending Mar 2026; not the same year. (ii) vintage — advance estimate vs later projection vs revised actual. Values are not directly comparable until both are pinned to a period + vintage.
- **Design expectation:** TEMPORAL_EVOLUTION or DIFFERENT_CONTEXT (`context_dimension=vintage`) once each FY label is resolved via its document's `fy_convention`; UNCERTAIN if a period can't be pinned.

### C9 — Apparent contradiction reconciled by period/scope (India CAD)
- **A:** CAD = **1.2% of GDP** in Q2 FY25 — india-macro/01.
- **B:** CAD = **0.2% of GDP** (a more recent quarter) — india-macro/03; plus IMF projection **1.0% of GDP** for FY2025/26.
- **Why related:** same indicator, same country; quarterly point vs quarterly point vs annual projection.
- **Context to consider:** which quarter; quarterly vs full-year; actual vs projection; sign convention ("current account balance" is negative of "deficit").
- **Design expectation:** DIFFERENT_CONTEXT (`context_dimension=time`/period type) — quarterly point vs quarterly point vs annual projection; modality differs.

### C10 — Apparent contradiction reconciled by time/scope (India inflation level)
- **A:** headline inflation "declined to **1.5 percent** in September" — india-macro/03.
- **B:** headline inflation averaged **4.6 per cent** during [FY25]; expected **4.2%** FY26 — india-macro/02 / india-macro/01.
- **Why related:** same indicator, values far apart.
- **Context to consider:** single-month reading vs annual average vs forward projection; base effects. Not a contradiction.
- **Design expectation:** DIFFERENT_CONTEXT (`context_dimension=time` — single‑month reading vs annual average vs forecast).

## 13. Likely extraction challenges

- **A3 landscape multi-column annual report** — reading order and column bleed; a fact's number and its period header can land far apart in the text layer.
- **Slide tables with column headers as period labels** (`Q4 FY22 | Q4 FY23 | Q3 FY24 | Q4 FY24`) — each cell is a fact whose period is the column header; losing the header orphans the value.
- **RBI statistical appendix** — wide tables, multi-row headers, year columns, values with no inline subject; high risk of wrong period/subject attribution → should be penalised or quarantined.
- **Parenthesised negatives** `(452)` and `₹(404) Cr` must parse to negative.
- **Magnitude words** ("₹1,266 million" in prose next to "₹127 Cr" elsewhere) — must not be read as crore.
- **Footnote-defined scope** — the counting basis for "team size" or "cash & cash equivalents" lives in a numbered footnote on the same slide.
- **Charts with data labels only** (no table) — values printed on bars; text layer gives a bag of numbers with weak structure.
- **Running headers/footers** repeat the CIN, company name, page furniture on every page → dedup / down-weight.
- **Ligatures / spacing** from some producers ("PIN codes" vs "PINcodes"), digitally-signed blocks with split characters ("MADHULIK A VIPIN RAWAT").
- **Printed page label ≠ PDF page** and jumps between retained sections.
- Scanned pages are not expected here (all have a text layer), but OCR fallback still needs a trigger for unseen PDFs.

## 14. Likely reasoning challenges

- **Predicate matching under synonymy** — deciding "revenue from services" ≡ "revenue from operations" *in a given context* but "Service EBITDA" ≠ "EBITDA".
- **Period algebra** — FY vs quarter vs 9M vs calendar; `FY26` vs `FY2025/26`; overlap and containment tests; "as of" vs "for the period".
- **Scope conflict detection** — standalone/consolidated, reported/adjusted, segment/total, counting basis. Absence of a scope qualifier ≠ same scope.
- **Vintage / revision awareness** — advance estimate vs provisional vs revised vs projected; later report supersedes earlier for the same period.
- **Numerator/denominator traps** — 17,488 (served) vs 19,300 (total in India); 23,381 ⊂ 63,713.
- **Sign conventions** — deficit vs balance; YoY decline shown as `(2.2%)`.
- **Modality** — projected/estimated/expected vs asserted; a projection that "contradicts" an actual is usually just a forecast.
- **Directional/temporal status facts** — a person "active" then "resigned"; needs effective-date bounds to avoid false contradictions.
- **Corroboration despite different wording** — matching values that are expressed in different units, bases, and vocabulary is the core task.
- **Knowing when to abstain** — if entity match, period, or scope can't be established, output UNCERTAIN rather than guess.
