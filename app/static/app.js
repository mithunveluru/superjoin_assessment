/* Fact Knowledge Layer — static UI. Vanilla fetch, no build step (DECISIONS D3). */
"use strict";

// ---- tiny helpers --------------------------------------------------------
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}
const $view = () => document.getElementById("view");
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function fmt(v) {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toPrecision(6).replace(/\.?0+$/, "");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const msg = body && body.error ? `${body.error.code}: ${body.error.message}` : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return body;
}

// ---- vocabulary ----------------------------------------------------------
// Raw API enums stay the source of truth; the UI shows a readable label plus a
// shape glyph so state never depends on colour alone.
const LIFECYCLE = {
  ELIGIBLE_FOR_REASONING: ["Eligible", "g", "✓"], NORMALIZED: ["Normalized", "b", "•"],
  GROUNDED: ["Grounded", "b", "•"], CANDIDATE: ["Candidate", "y", "•"],
  RAW: ["Raw", "n", "•"], QUARANTINED: ["Quarantined", "r", "✕"],
};
const EVIDENCE = {
  VERIFIED: ["Verified", "g", "✓"], PARTIAL: ["Partial", "y", "~"],
  UNVERIFIED: ["Unverified", "r", "✕"],
};
const CATEGORY = {
  CORROBORATES: ["Corroborates", "g", "✓"], CONTRADICTS: ["Contradicts", "r", "✕"],
  DIFFERENT_CONTEXT: ["Different context", "b", "≠"],
  TEMPORAL_EVOLUTION: ["Temporal evolution", "b", "→"], UNCERTAIN: ["Uncertain", "y", "?"],
};
const DOC_STATUS = {
  done: ["Done", "g", "✓"], ingested: ["Ingested", "b", "•"],
  processing: ["Processing", "y", "◍"], uploaded: ["Uploaded", "n", "•"],
  failed: ["Failed", "r", "✕"],
};

function badge(text, kind, glyph, title) {
  return el("span", { class: "badge " + (kind || "n"), title: title || null },
    glyph ? el("span", { class: "g0", "aria-hidden": "true" }, glyph) : null, text);
}
/** Badge from one of the vocabularies above; unknown values degrade to neutral. */
function tag(value, map) {
  if (!value) return null;
  const [label, kind, glyph] = map[value] || [value, "n", "•"];
  return badge(label, kind, glyph, value);
}

const ICONS = {
  overview: '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
  documents: '<path d="M4 1.8h4.6L12 5.2v9H4z"/><path d="M8.4 1.8v3.6H12"/>',
  facts: '<path d="M6 4h8M6 8h8M6 12h8"/><path d="M2.6 4h.01M2.6 8h.01M2.6 12h.01"/>',
  relationships: '<circle cx="4" cy="4.2" r="2.2"/><circle cx="12" cy="11.8" r="2.2"/><path d="M5.7 5.9l4.6 4.2"/>',
  entities: '<circle cx="8" cy="5.4" r="2.6"/><path d="M2.8 14c0-2.9 2.3-4.6 5.2-4.6s5.2 1.7 5.2 4.6"/>',
  failures: '<path d="M8 2.2l6 11.2H2z"/><path d="M8 6.4v3.3M8 11.6v.2"/>',
};
function icon(name) {
  return el("span", {
    "aria-hidden": "true",
    html: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"
      stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`,
  });
}

function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of pairs) {
    if (v == null || v === "" || (Array.isArray(v) && !v.length)) continue;
    dl.append(el("dt", {}, k), el("dd", {}, v.nodeType ? v : fmt(v)));
  }
  return dl;
}
function errBox(e) {
  return el("div", { class: "err", role: "alert" },
    el("span", { "aria-hidden": "true" }, "✕"), el("div", {}, e.message));
}
function empty(title, hint) {
  return el("div", { class: "empty" }, el("strong", {}, title), hint || "");
}
function skeleton(rows) {
  return el("div", { class: "card card-pad" },
    Array.from({ length: rows || 4 }, (_, i) =>
      el("div", { class: "skeleton", style: `width:${[60, 92, 78, 85, 70][i % 5]}%` })));
}
function section(title, ...kids) {
  return el("section", {}, el("div", { class: "section-head" }, el("h2", {}, title)), ...kids);
}
function cardTable(...rows) {
  return el("div", { class: "card" }, el("div", { class: "table-wrap" }, el("table", {}, ...rows)));
}
function headRow(...labels) {
  return el("thead", {}, el("tr", {}, ...labels.map((l) => el("th", {}, l))));
}

/** Claim rendered value-first: what the fact says, then who it is about. */
function claim(f) {
  return el("div", { class: "claim" },
    el("div", { class: "value" }, f.object_raw),
    el("div", { class: "subject" }, el("b", {}, f.subject_raw), " · ", f.predicate));
}
function titleCase(s) {
  return s ? s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ") : s;
}
function factLine(f) {
  return `${f.subject_raw} · ${f.predicate} · ${f.object_raw}`;
}
/** Never render a bare "—" for a document: fall back title → filename → id. */
function docLabel(d) {
  return d.title || d.original_filename || "Untitled document #" + d.id;
}
function factDocLabel(f) {
  return f.document_title || "Document #" + f.document_id;
}
function source(f) {
  return el("div", { class: "src" },
    el("div", { class: "doc" }, factDocLabel(f)),
    el("div", { class: "loc" }, `p.${f.printed_label || "?"} · pdf ${f.page_index}`));
}
function periodStr(p) {
  if (!p || (!p.raw && !p.start)) return null;
  return `${p.raw || ""} ${p.start ? `[${p.start} → ${p.end})` : ""} ${p.type ? "· " + p.type : ""}`.trim();
}
function scopeStr(scope) {
  if (!scope || typeof scope !== "object") return scope;
  const parts = Object.entries(scope).map(([k, v]) => `${k}: ${fmt(v)}`);
  return parts.length ? parts.join(" · ") : null;
}
function disclose(title, ...kids) {
  return el("details", { class: "disclose" }, el("summary", {}, title), ...kids);
}
/** Proportional bar + legend for a {label: count} distribution. */
function meter(parts) {
  const total = parts.reduce((s, p) => s + p.n, 0) || 1;
  const bar = el("div", { class: "meter", role: "img", "aria-label":
    parts.map((p) => `${p.label} ${p.n}`).join(", ") });
  const key = el("div", { class: "meter-key" });
  for (const p of parts) {
    if (p.n) bar.append(el("span", { class: "dot " + p.kind, style: `width:${(p.n / total) * 100}%` }));
    key.append(el("div", {}, el("span", { class: "dot " + p.kind }), p.label, " ", el("b", {}, p.n)));
  }
  return el("div", {}, bar, key);
}

// ---- router --------------------------------------------------------------
const VIEWS = {
  overview: [viewOverview, "Overview", "Facts, evidence and cross-document relationships at a glance"],
  documents: [viewDocuments, "Documents", "Upload a PDF, run the pipeline, follow each stage"],
  facts: [viewFacts, "Facts", "Every fact is pinned to a verbatim quote in its source page"],
  relationships: [viewRelationships, "Relationships", "How facts relate across documents — and the signals behind each call"],
  entities: [viewEntities, "Entities", "Subject surfaces resolved to canonical entities"],
  failures: [viewFailures, "Failures", "Quarantined facts and pipeline errors — kept, never hidden"],
};
function currentRoute() {
  const h = (location.hash || "#/overview").slice(2).split("?")[0];
  return VIEWS[h] ? h : "overview";
}
function renderNav() {
  const nav = document.getElementById("nav");
  clear(nav);
  const active = currentRoute();
  for (const [name, [, label]] of Object.entries(VIEWS)) {
    nav.append(el("a", {
      href: "#/" + name,
      class: active === name ? "active" : "",
      "aria-current": active === name ? "page" : null,
    }, icon(name), label));
  }
}
async function route() {
  renderNav();
  const [render, title, sub] = VIEWS[currentRoute()];
  document.getElementById("page-title").textContent = title;
  document.getElementById("page-sub").textContent = sub;
  const v = $view();
  clear(v); v.append(skeleton(4));
  try {
    const node = await render();
    clear(v); v.append(node);
  } catch (e) {
    clear(v); v.append(errBox(e));
  }
}
async function renderHealth() {
  const side = document.getElementById("topbar-side");
  try {
    const h = await api("/health");
    clear(side);
    side.append(
      el("span", { class: "badge n", title: `database: ${h.database.path}` },
        el("span", { class: "status-dot " + (h.status === "ok" ? "g" : "r") }),
        `API ${h.status} · v${h.version}`),
      h.llm.api_key_present
        ? badge("LLM ready", "g", "✓", h.llm.model)
        : badge("No LLM key", "y", "!", "extraction and reasoning stages need an API key"));
  } catch { clear(side); side.append(badge("API unreachable", "r", "✕")); }
}
window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", () => { route(); renderHealth(); });

// ---- Overview ------------------------------------------------------------
function tile(label, value, note, alert) {
  return el("div", { class: "tile" + (alert ? " alert" : "") },
    el("div", { class: "k" }, label),
    el("div", { class: "v" }, fmt(value)),
    note ? el("div", { class: "n" }, note) : null);
}

async function viewOverview() {
  // Counts come from the same list endpoints the other views use (limit=1 →
  // only the total is needed), so the overview can never drift from them.
  const [docs, processed, facts, eligible, quarantined, unverified, entities, failures,
    corroborates, contradicts, differentContext, temporal, uncertain] = await Promise.all([
    api("/documents?limit=5"),
    api("/documents?limit=1&status=done"),
    api("/facts?limit=1"),
    api("/facts?limit=1&reasoning_eligible=true"),
    api("/facts?limit=1&lifecycle_state=QUARANTINED"),
    api("/facts?limit=1&evidence_status=UNVERIFIED"),
    api("/entities?limit=1"),
    api("/failures?limit=1"),
    api("/relationships?limit=1&category=CORROBORATES"),
    api("/relationships?limit=1&category=CONTRADICTS"),
    api("/relationships?limit=1&category=DIFFERENT_CONTEXT"),
    api("/relationships?limit=1&category=TEMPORAL_EVOLUTION"),
    api("/relationships?limit=1&category=UNCERTAIN"),
  ]);
  const rels = corroborates.total + contradicts.total + differentContext.total
    + temporal.total + uncertain.total;

  const wrap = el("div", { class: "stack" });

  wrap.append(el("div", { class: "tiles" },
    tile("Documents", docs.total, `${processed.total} fully processed`),
    tile("Facts", facts.total, `${eligible.total} reasoning-eligible`),
    tile("Relationships", rels, `${contradicts.total} contradiction${contradicts.total === 1 ? "" : "s"}`),
    tile("Entities", entities.total, "resolved subjects"),
    tile("Quarantined", quarantined.total, "facts held out of reasoning", quarantined.total > 0)));

  wrap.append(section("Cross-document resolution",
    el("div", { class: "card card-pad" },
      rels
        ? meter([
          { label: "Corroborates", n: corroborates.total, kind: "g" },
          { label: "Contradicts", n: contradicts.total, kind: "r" },
          { label: "Different context", n: differentContext.total, kind: "b" },
          { label: "Temporal evolution", n: temporal.total, kind: "b2" },
          { label: "Uncertain", n: uncertain.total, kind: "y" },
        ])
        : empty("No relationships yet", "Process at least two documents to compare facts across them."),
      el("p", { class: "note", style: "margin:14px 0 0" },
        "A context difference — different scope, period or modality — is classified as such, never as a contradiction."))));

  wrap.append(section("Evidence integrity",
    el("div", { class: "card card-pad" },
      facts.total
        ? meter([
          { label: "Reasoning-eligible", n: eligible.total, kind: "g" },
          { label: "Quarantined", n: quarantined.total, kind: "r" },
          { label: "Unverified evidence", n: unverified.total, kind: "y" },
          { label: "Other states", n: Math.max(0, facts.total - eligible.total - quarantined.total), kind: "n" },
        ])
        : empty("No facts yet", "Upload and process a PDF to populate the knowledge layer."),
      el("p", { class: "note", style: "margin:14px 0 0" },
        `${failures.total} failure${failures.total === 1 ? "" : "s"} recorded · `,
        el("a", { href: "#/failures" }, "inspect the failure surface")))));

  const recent = cardTable(
    headRow("#", "Document", "Status", "Pages", "Facts", "Rel."),
    el("tbody", {}, docs.items.length
      ? docs.items.map((d) => el("tr", {},
        el("td", { class: "mono tight" }, "#" + d.id),
        el("td", {}, docLabel(d)),
        el("td", { class: "tight" }, tag(d.status, DOC_STATUS)),
        el("td", { class: "num" }, fmt((d.counts || {}).pages)),
        el("td", { class: "num" }, fmt((d.counts || {}).facts)),
        el("td", { class: "num" }, fmt((d.counts || {}).relationships))))
      : el("tr", {}, el("td", { colspan: "6" },
        empty("No documents yet", "Upload a PDF from the Documents view.")))));
  const head = el("div", { class: "section-head" },
    el("h2", {}, "Recent documents"), el("a", { href: "#/documents" }, "All documents →"));
  wrap.append(el("section", {}, head, recent));

  wrap.append(section("Pipeline",
    el("div", { class: "card card-pad" },
      el("div", { class: "pipeline" },
        ["PDF", "ingest", "extract", "verify", "normalize", "resolve", "retrieve", "reason", "API / UI"]
          .flatMap((s, i) => [i ? el("i", { "aria-hidden": "true" }, "→") : null, el("span", {}, s)])))));

  return wrap;
}

// ---- Documents -----------------------------------------------------------
async function viewDocuments() {
  const wrap = el("div", { class: "stack" });
  // extract + reason call the LLM; without a key Process will stop at extract and
  // mark the document failed. Say so before the click, not after.
  const health = await api("/health").catch(() => null);
  const noKey = health && !health.llm.api_key_present;
  if (noKey) {
    wrap.append(el("div", { class: "notice" },
      el("span", { class: "badge y" }, el("span", { class: "g0" }, "!"), "No LLM key"),
      el("div", {},
        el("b", {}, "Processing will stop at the extract stage. "),
        `Set GEMINI_API_KEY (provider ${health.llm.provider}, model ${health.llm.model}) `,
        "and restart the server to run extraction and reasoning. ",
        "Seeded demo documents already carry facts and relationships — re-processing them ",
        "marks them failed; restore with ", el("code", {}, "python scripts/seed_demo.py --force"), ".")));
  }

  const fileInput = el("input", { type: "file", accept: "application/pdf,.pdf", id: "pdf-file" });
  const upBtn = el("button", { class: "primary" }, "Upload PDF");
  const upMsg = el("span", { class: "hint", role: "status" });
  upBtn.addEventListener("click", async () => {
    if (!fileInput.files.length) { upMsg.textContent = "Choose a .pdf file first."; return; }
    upBtn.disabled = true; upMsg.textContent = "Uploading…";
    try {
      const fd = new FormData();
      fd.append("file", fileInput.files[0]);
      const doc = await api("/documents", { method: "POST", body: fd });
      upMsg.textContent = doc.duplicate ? `Already ingested as #${doc.id}` : `Ingested as #${doc.id}`;
      fileInput.value = "";
      await refresh();
    } catch (e) { upMsg.textContent = e.message; }
    upBtn.disabled = false;
  });
  wrap.append(el("div", { class: "card" },
    el("div", { class: "card-head" }, el("h3", {}, "Ingest a document")),
    el("div", { class: "card-body" },
      el("form", { class: "filters", onsubmit: (e) => e.preventDefault() },
        el("label", { class: "field", for: "pdf-file" }, el("span", {}, "PDF file"), fileInput),
        upBtn, upMsg),
      el("p", { class: "note", style: "margin:10px 0 0" },
        "Upload stores and paginates the PDF. Processing runs extract → verify → normalize → resolve → reason in the background."))));

  const tbody = el("tbody", {});
  wrap.append(cardTable(
    headRow("#", "Title / file", "Status", "Pages", "Facts", "Eligible", "Rel.", "Failures", ""),
    tbody));
  const timers = new Map();

  async function refresh() {
    const page = await api("/documents?limit=200");
    clear(tbody);
    if (!page.items.length) {
      tbody.append(el("tr", {}, el("td", { colspan: "9" },
        empty("No documents yet", "Upload a PDF above to start the pipeline."))));
      return;
    }
    for (const d of page.items) tbody.append(docRow(d));
  }

  function docRow(d) {
    const c = d.counts || {};
    const statusCell = el("td", { class: "tight" }, tag(d.status, DOC_STATUS),
      // a bare "Failed" badge explains nothing — say which stage gave up and why
      d.status === "failed" && d.status_detail
        ? el("div", { class: "note", style: "margin-top:4px;max-width:22rem;white-space:normal" },
          d.status_detail)
        : null);
    // a document that already holds facts cannot be re-processed (the API
    // returns 409): re-extracting would duplicate candidates, and a failure
    // would flip a good document to "failed" for nothing.
    const processed = (c.facts || 0) > 0;
    const btn = el("button", {},
      d.status === "processing" ? "Processing…" : processed ? "Processed" : "Process");
    btn.disabled = d.status === "processing" || processed;
    btn.setAttribute("aria-label", `Process document ${d.id}`);
    if (processed) {
      btn.title = `Already holds ${c.facts} fact(s) — re-processing would duplicate them.`;
    }
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api(`/documents/${d.id}/process`, { method: "POST" });
        poll(d.id, statusCell, btn, tr);
      } catch (e) { btn.textContent = e.message; }
    });
    const tr = el("tr", {},
      el("td", { class: "mono tight" }, "#" + d.id),
      el("td", {}, el("div", { class: "docname" }, docLabel(d)),
        el("div", { class: "muted mono" },
          d.title && d.original_filename ? d.original_filename : d.sha256.slice(0, 12))),
      statusCell,
      el("td", { class: "num" }, fmt(c.pages)), el("td", { class: "num" }, fmt(c.facts)),
      el("td", { class: "num" }, fmt(c.eligible_facts)),
      el("td", { class: "num" }, fmt(c.relationships)), el("td", { class: "num" }, fmt(c.failures)),
      el("td", { class: "tight" }, btn));
    if (d.status === "processing") poll(d.id, statusCell, btn, tr);
    return tr;
  }

  function poll(id, statusCell, btn, tr) {
    if (timers.has(id)) return;
    const t = setInterval(async () => {
      let s;
      try { s = await api(`/documents/${id}/status`); }
      catch (e) { clearInterval(t); timers.delete(id); return; }
      clear(statusCell);
      statusCell.append(tag(s.status, DOC_STATUS));
      if (s.run && s.run.stage) statusCell.append(el("div", { class: "muted mono" }, s.run.stage));
      if (s.status === "done" || s.status === "failed") {
        clearInterval(t); timers.delete(id);
        if (s.run && s.run.error) statusCell.append(el("div", { class: "note" }, s.run.error));
        btn.disabled = false; btn.textContent = "Process";
        await refresh();
      }
    }, 2000);
    timers.set(id, t);
  }

  await refresh();
  return wrap;
}

// ---- Facts ---------------------------------------------------------------
const LIFECYCLE_OPTS = ["", "CANDIDATE", "GROUNDED", "NORMALIZED", "ELIGIBLE_FOR_REASONING", "QUARANTINED"];
const EVIDENCE_OPTS = ["", "VERIFIED", "PARTIAL", "UNVERIFIED"];
const MODALITY_OPTS = ["", "ASSERTED", "HISTORICAL", "ESTIMATED", "FORECAST", "TARGET", "UNCERTAIN"];
const TYPE_OPTS = ["", "numeric", "semantic"];

function select(name, opts, cur) {
  return el("select", { name, id: "f-" + name }, ...opts.map((o) =>
    el("option", { value: o, selected: o === cur ? "selected" : null }, o || "Any")));
}
function field(label, node) {
  return el("label", { class: "field", for: node.id || null }, el("span", {}, label), node);
}

async function viewFacts() {
  const state = { limit: 50, offset: 0 };
  const wrap = el("div", { class: "stack" });

  const inputs = {
    q: el("input", { name: "q", id: "f-q", placeholder: "Full-text…", size: "16" }),
    document_id: el("input", { name: "document_id", id: "f-doc", type: "number", size: "4", min: "1", placeholder: "#" }),
    lifecycle_state: select("lifecycle_state", LIFECYCLE_OPTS),
    evidence_status: select("evidence_status", EVIDENCE_OPTS),
    modality: select("modality", MODALITY_OPTS),
    type: select("type", TYPE_OPTS),
    reasoning_eligible: select("reasoning_eligible", ["", "true", "false"]),
  };
  const form = el("form", { class: "filters", onsubmit: (e) => { e.preventDefault(); state.offset = 0; load(); } },
    field("Search", inputs.q),
    field("Doc id", inputs.document_id),
    field("Lifecycle", inputs.lifecycle_state),
    field("Evidence", inputs.evidence_status),
    field("Modality", inputs.modality),
    field("Type", inputs.type),
    field("Eligible", inputs.reasoning_eligible),
    el("button", { class: "primary", type: "submit" }, "Apply"));
  wrap.append(el("div", { class: "card card-pad" }, form));

  const tbody = el("tbody", {});
  const pager = el("div", { class: "pager" });
  const table = el("div", { class: "card" },
    el("div", { class: "table-wrap" },
      el("table", {}, headRow("Fact", "Source", "Lifecycle", "Evidence", "Modality"), tbody)),
    pager);
  wrap.append(table);

  function query() {
    const p = new URLSearchParams();
    for (const [k, node] of Object.entries(inputs)) if (node.value) p.set(k, node.value);
    p.set("limit", state.limit); p.set("offset", state.offset);
    return p.toString();
  }

  async function load() {
    const page = await api("/facts?" + query());
    clear(tbody);
    if (!page.items.length) {
      tbody.append(el("tr", {}, el("td", { colspan: "5" },
        empty("No facts match these filters", "Clear a filter, or process a document first."))));
    }
    for (const f of page.items) tbody.append(...factRows(f));
    clear(pager);
    const shown = page.items.length ? `${state.offset + 1}–${state.offset + page.items.length}` : "0";
    pager.append(
      el("button", {
        disabled: state.offset === 0 || null,
        onclick: () => { if (state.offset > 0) { state.offset -= state.limit; load(); } },
      }, "← Previous"),
      el("button", {
        disabled: state.offset + state.limit >= page.total || null,
        onclick: () => { if (state.offset + state.limit < page.total) { state.offset += state.limit; load(); } },
      }, "Next →"),
      el("span", { class: "count" }, `${shown} of ${page.total}`));
  }

  function factRows(f) {
    const detail = el("tr", { class: "detail", hidden: "hidden" }, el("td", { colspan: "5" }));
    const row = el("tr", {
      class: "row", tabindex: "0", role: "button", "aria-expanded": "false",
      onclick: () => toggleFact(f.id, detail, row),
      onkeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleFact(f.id, detail, row); }
      },
    },
      el("td", {}, claim(f),
        periodStr(f.reporting_period)
          ? el("div", { class: "badge-row", style: "margin-top:6px" },
            badge(f.reporting_period.raw || f.reporting_period.start, "n"))
          : null),
      el("td", {}, source(f)),
      el("td", { class: "tight" }, tag(f.lifecycle_state, LIFECYCLE)),
      el("td", { class: "tight" }, tag(f.evidence_status, EVIDENCE)),
      el("td", { class: "tight" }, el("span", { class: "note" }, titleCase(f.modality))));
    return [row, detail];
  }
  async function toggleFact(id, detailRow, row) {
    const open = detailRow.hidden;
    detailRow.hidden = !open;
    row.setAttribute("aria-expanded", String(open));
    if (!open) return;
    const cell = detailRow.firstChild;
    clear(cell); cell.append(el("div", { class: "skeleton", style: "width:70%;margin-top:14px" }));
    try {
      const f = await api(`/facts/${id}`);
      clear(cell); cell.append(factDetail(f));
    } catch (e) { clear(cell); cell.append(errBox(e)); }
  }

  await load();
  return wrap;
}

function factDetail(f) {
  const box = el("div", { style: "padding-top:12px" });
  if (f.evidence && f.evidence.quote) {
    box.append(el("h4", { class: "label", style: "margin-top:0" }, "Verbatim evidence"),
      el("blockquote", {}, f.evidence.quote),
      el("p", { class: "note", style: "margin:8px 0 0" },
        `${factDocLabel(f)} · printed page ${f.evidence.printed_label || "?"}`
        + ` · pdf page ${f.evidence.page_index} · verified ${f.evidence.verification_method || "—"}`));
  }
  box.append(el("h4", { class: "label" }, "Claim & context"));
  box.append(kv([
    ["Subject", f.subject_raw],
    ["Entity", f.entity ? `${f.entity.canonical_label} (#${f.entity.id})` : null],
    ["Attribute", f.predicate],
    ["Value", f.object_raw],
    ["Value text", f.value_text],
    ["Reporting period", periodStr(f.reporting_period)],
    ["Scope", scopeStr(f.scope)],
    ["Qualifiers", f.qualifiers && f.qualifiers.length ? f.qualifiers.join(", ") : null],
    ["Modality", f.modality],
    ["Context complete", f.context_complete ? "yes" : "no"],
    ["Publisher", f.publisher],
    ["Publication date", f.publication_date],
    ["Data vintage", f.data_vintage],
  ]));

  if (f.numeric) {
    box.append(disclose("Normalized representation", kv([
      ["numeric_value", f.numeric.numeric_value],
      ["magnitude", f.numeric.magnitude],
      ["base_value", f.numeric.base_value],
      ["currency", f.numeric.currency],
      ["is_percentage", f.numeric.is_percentage],
      ["percentage_ratio", f.numeric.percentage_ratio],
      ["unit_norm", f.numeric.unit_norm],
    ])));
  }
  if (f.evidence) {
    box.append(disclose("Verification detail", kv([
      ["Method", f.evidence.verification_method],
      ["Numeric re-derivation", f.evidence.numeric_rederivation],
      ["Fuzzy score", f.evidence.fuzzy_score],
      ["Char range", f.evidence.char_start != null ? `${f.evidence.char_start}–${f.evidence.char_end}` : null],
      ["Notes", f.evidence.notes],
    ])));
  }
  if (f.context_window) {
    box.append(disclose("Source context window", el("div", { class: "ctxwin" }, f.context_window)));
  }
  if (f.repro && f.repro.extraction_model) {
    box.append(disclose("Reproducibility", kv([
      ["Extraction model", f.repro.extraction_model],
      ["Prompt version", f.repro.prompt_version],
      ["Temperature", f.repro.extraction_temperature],
    ])));
  }
  if (f.relationships && f.relationships.length) {
    const ul = el("div", { class: "badge-row", style: "margin-top:8px" });
    for (const r of f.relationships) {
      const [label, kind, glyph] = CATEGORY[r.category] || [r.category, "n", "•"];
      const text = `${label}${r.context_dimension ? " · " + r.context_dimension : ""}`
        + ` → fact #${r.other_fact_id}`;
      ul.append(el("a", { href: "#/relationships?open=" + r.id }, badge(text, kind, glyph, r.category)));
    }
    box.append(el("h4", { class: "label" }, "Relationships"), ul);
  }
  return box;
}

// ---- Relationships -------------------------------------------------------
const CATEGORIES = ["", "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN"];

async function viewRelationships() {
  const params = new URLSearchParams((location.hash.split("?")[1]) || "");
  const openId = params.get("open");
  const state = { category: "" };
  const wrap = el("div", { class: "stack" });

  const tabs = el("div", { class: "chips", role: "tablist", "aria-label": "Relationship category" });
  for (const c of CATEGORIES) {
    tabs.append(el("button", {
      type: "button", role: "tab",
      onclick: () => { state.category = c; renderTabs(); load(); },
    }, c ? CATEGORY[c][0] : "All"));
  }
  function renderTabs() {
    [...tabs.children].forEach((b, i) => {
      const on = CATEGORIES[i] === state.category;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", String(on));
    });
  }
  renderTabs();
  wrap.append(tabs);

  const list = el("div");
  wrap.append(list);

  async function load() {
    const p = new URLSearchParams({ limit: "200", sort: "confidence" });
    if (state.category) p.set("category", state.category);
    const page = await api("/relationships?" + p.toString());
    clear(list);
    if (!page.items.length) {
      list.append(el("div", { class: "card" },
        empty("No relationships in this category",
          "Relationships appear once two documents contribute comparable facts.")));
      return;
    }
    for (const r of page.items) list.append(relCard(r, r.id === Number(openId)));
  }

  await load();
  return wrap;
}

function relCard(r, openNow) {
  const body = el("div", { class: "card-body", hidden: openNow ? null : "hidden" });
  const [label, kind, glyph] = CATEGORY[r.category] || [r.category, "n", "•"];
  const head = el("button", {
    class: "rel-head", type: "button", "aria-expanded": String(!!openNow),
    onclick: () => toggleRel(r.id, body, head),
  },
    badge(r.category_label || label, kind, glyph, r.category),
    r.context_dimension ? badge(r.context_dimension, "n") : null,
    el("span", { class: "note" }, "confidence ", el("b", {}, fmt(r.confidence))),
    el("span", { class: "spacer" }),
    r.llm_used ? badge("LLM-assisted", "b") : badge("Deterministic", "n"),
    el("span", { class: "caret", "aria-hidden": "true" }, openNow ? "▲" : "▼"));
  const card = el("div", { class: "card" + (r.category === "CONTRADICTS" ? " contradiction" : "") }, head,
    el("div", { class: "card-body" },
      el("div", { class: "pair" }, relSide("Fact A", r.fact_a), relSide("Fact B", r.fact_b))),
    body);
  if (openNow) fillRel(r.id, body);
  return card;
}
function relSide(title, f) {
  return el("div", { class: "pane" }, el("h4", {}, title),
    claim(f),
    el("div", { class: "badge-row", style: "margin:8px 0" },
      badge(factDocLabel(f), "n"),
      badge("p." + (f.printed_label || "?"), "n"),
      periodStr(f.reporting_period) ? badge(f.reporting_period.raw || f.reporting_period.start, "n") : null,
      tag(f.evidence_status, EVIDENCE)),
    f.evidence && f.evidence.quote ? el("blockquote", {}, f.evidence.quote) : null);
}
async function toggleRel(id, body, head) {
  const open = body.hidden;
  body.hidden = !open;
  head.setAttribute("aria-expanded", String(open));
  head.lastChild.textContent = open ? "▲" : "▼";
  if (open) await fillRel(id, body);
}
async function fillRel(id, body) {
  clear(body); body.append(el("div", { class: "skeleton", style: "width:60%" }));
  try {
    const r = await api(`/relationships/${id}`);
    clear(body);
    const sig = el("table", { class: "signals" }, el("tbody", {}));
    for (const [k, v] of Object.entries(r.deterministic_signals || {})) {
      sig.lastChild.append(el("tr", {}, el("td", {}, k), el("td", { class: "mono" }, fmt(v))));
    }
    body.append(
      el("h4", { class: "label", style: "margin-top:0" }, "Why this call"),
      el("div", { class: "reasoning" }, r.reasoning || "—"),
      kv([
        ["LLM proposed", r.llm_proposed_category],
        ["Final category", r.category],
        ["Validation action", r.validation_action],
        ["Validation notes", r.validation_notes],
      ]),
      disclose("Deterministic signals", el("div", { class: "table-wrap" }, sig)),
      disclose("Source context",
        el("div", { class: "pair" },
          evidenceBlock("Fact A", r.fact_a), evidenceBlock("Fact B", r.fact_b))));
  } catch (e) { clear(body); body.append(errBox(e)); }
}
function evidenceBlock(title, f) {
  return el("div", { class: "pane" }, el("h4", {}, title),
    f.evidence && f.evidence.quote
      ? el("blockquote", {}, f.evidence.quote)
      : el("div", { class: "note" }, "No quote recorded."),
    f.context_window ? el("div", { class: "ctxwin", style: "margin-top:8px" }, f.context_window) : null);
}

// ---- Failures ------------------------------------------------------------
async function viewFailures() {
  const wrap = el("div", { class: "stack" });
  const page = await api("/failures?limit=200");

  const byType = Object.entries(page.counts_by_type);
  wrap.append(el("div", { class: "tiles" },
    tile("Failures", page.total, "recorded across all runs", page.total > 0),
    ...byType.slice(0, 4).map(([k, v]) => tile(titleCase(k), v, ""))));

  const tbody = el("tbody", {});
  if (!page.items.length) {
    tbody.append(el("tr", {}, el("td", { colspan: "5" },
      empty("No failures", "Every fact and pair processed cleanly."))));
  }
  for (const it of page.items) {
    let linked = el("span", { class: "note" }, "—");
    if (it.fact) {
      linked = el("div", { class: "claim" },
        el("div", { class: "value" }, it.fact.object_raw),
        el("div", { class: "subject" }, it.fact.subject_raw, " · ", it.fact.predicate));
    } else if (it.relationship) {
      linked = el("a", { href: "#/relationships?open=" + it.relationship.id },
        (CATEGORY[it.relationship.category] || [it.relationship.category])[0] + " #" + it.relationship.id);
    }
    // an unresolved pair is an honest "don't know", not an error — colour it as such
    const soft = it.failure_type.includes("uncertain");
    tbody.append(el("tr", {},
      el("td", { class: "tight" },
        badge(titleCase(it.failure_type), soft ? "y" : "r", soft ? "?" : "✕", it.failure_type)),
      el("td", {}, it.reason),
      el("td", { class: "mono tight" }, `${it.ref_table || ""}${it.ref_id ? "#" + it.ref_id : ""}`),
      el("td", { class: "mono tight" }, it.document_id ? "#" + it.document_id : "—"),
      el("td", {}, linked)));
  }
  wrap.append(cardTable(headRow("Type", "Reason", "Ref", "Doc", "Linked record"), tbody));
  return wrap;
}

// ---- Entities ------------------------------------------------------------
async function viewEntities() {
  const wrap = el("div", { class: "stack" });
  const page = await api("/entities?limit=200");
  const tbody = el("tbody", {});
  if (!page.items.length) {
    tbody.append(el("tr", {}, el("td", { colspan: "6" },
      empty("No entities", "Entities are resolved from fact subjects during processing."))));
  }
  for (const e of page.items) {
    const detail = el("tr", { class: "detail", hidden: "hidden" }, el("td", { colspan: "6" }));
    const row = el("tr", {
      class: "row", tabindex: "0", role: "button", "aria-expanded": "false",
      onclick: () => toggleEntity(e.id, detail, row),
      onkeydown: (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleEntity(e.id, detail, row); }
      },
    },
      el("td", { class: "mono tight" }, "#" + e.id),
      el("td", {}, el("b", {}, e.canonical_label)),
      el("td", { class: "tight" }, e.entity_type ? badge(e.entity_type, "n") : "—"),
      el("td", { class: "num" }, fmt(e.alias_count)), el("td", { class: "num" }, fmt(e.fact_count)),
      el("td", { class: "tight" }, el("span", { class: "note" }, e.resolution_method || "—"), " ",
        e.llm_confirmed ? badge("LLM", "b") : null));
    tbody.append(row, detail);
  }
  wrap.append(cardTable(
    headRow("#", "Canonical entity", "Type", "Aliases", "Facts", "Resolution"), tbody));
  return wrap;
}
async function toggleEntity(id, detailRow, row) {
  const open = detailRow.hidden;
  detailRow.hidden = !open;
  row.setAttribute("aria-expanded", String(open));
  if (!open) return;
  const cell = detailRow.firstChild;
  clear(cell); cell.append(el("div", { class: "skeleton", style: "width:55%;margin-top:14px" }));
  try {
    const e = await api(`/entities/${id}`);
    clear(cell);
    const aliases = el("div", { class: "badge-row" });
    for (const a of e.aliases || []) {
      aliases.append(badge(a.surface + (a.match_method ? " · " + a.match_method : ""), "n", null,
        a.source_fact_id ? "from fact #" + a.source_fact_id : null));
    }
    const facts = el("ul", { style: "margin:6px 0 0;padding-left:18px" });
    for (const f of e.sample_facts || []) facts.append(el("li", { class: "note" }, factLine(f)));
    cell.append(
      el("h4", { class: "label", style: "margin-top:12px" }, "Aliases"),
      aliases.children.length ? aliases : el("div", { class: "note" }, "None recorded."),
      el("h4", { class: "label" }, "Sample facts"),
      facts.children.length ? facts : el("div", { class: "note" }, "None."));
  } catch (err) { clear(cell); cell.append(errBox(err)); }
}
