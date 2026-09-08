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

function badge(text, kind) { return el("span", { class: "badge " + (kind || "n") }, text); }
const LIFECYCLE_KIND = {
  ELIGIBLE_FOR_REASONING: "g", NORMALIZED: "b", GROUNDED: "b",
  CANDIDATE: "y", RAW: "n", QUARANTINED: "r",
};
const EVIDENCE_KIND = { VERIFIED: "g", PARTIAL: "y", UNVERIFIED: "r" };
const CATEGORY_KIND = {
  CORROBORATES: "g", CONTRADICTS: "r", DIFFERENT_CONTEXT: "b",
  TEMPORAL_EVOLUTION: "b", UNCERTAIN: "y",
};
const STATUS_KIND = { done: "g", ingested: "b", processing: "y", uploaded: "n", failed: "r" };

function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of pairs) {
    if (v == null || v === "" || (Array.isArray(v) && !v.length)) continue;
    dl.append(el("dt", {}, k), el("dd", {}, v.nodeType ? v : fmt(v)));
  }
  return dl;
}
function errBox(e) { return el("div", { class: "err" }, "Error: " + e.message); }
function factLine(f) {
  return `${f.subject_raw} · ${f.predicate} · ${f.object_raw}`;
}

// ---- router ------------------------------------------------------------
const VIEWS = {
  documents: viewDocuments, facts: viewFacts, relationships: viewRelationships,
  failures: viewFailures, entities: viewEntities,
};
function currentRoute() {
  const h = (location.hash || "#/documents").slice(2).split("?")[0];
  return VIEWS[h] ? h : "documents";
}
function renderNav() {
  const nav = document.getElementById("nav");
  clear(nav);
  const active = currentRoute();
  for (const name of Object.keys(VIEWS)) {
    nav.append(el("a", {
      href: "#/" + name, class: active === name ? "active" : "",
    }, name[0].toUpperCase() + name.slice(1)));
  }
}
async function route() {
  renderNav();
  const v = $view();
  clear(v); v.append(el("p", { class: "muted" }, "loading…"));
  try {
    const node = await VIEWS[currentRoute()]();
    clear(v); v.append(node);
  } catch (e) {
    clear(v); v.append(errBox(e));
  }
}
window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

// ---- Documents -------------------------------------------------------
async function viewDocuments() {
  const wrap = el("div");
  wrap.append(el("h2", {}, "Documents"));

  const fileInput = el("input", { type: "file", accept: "application/pdf,.pdf" });
  const upBtn = el("button", { class: "primary" }, "Upload PDF");
  const upMsg = el("span", { class: "muted" });
  upBtn.addEventListener("click", async () => {
    if (!fileInput.files.length) { upMsg.textContent = "choose a file first"; return; }
    upBtn.disabled = true; upMsg.textContent = "uploading…";
    try {
      const fd = new FormData();
      fd.append("file", fileInput.files[0]);
      const doc = await api("/documents", { method: "POST", body: fd });
      upMsg.textContent = doc.duplicate ? `already ingested as #${doc.id}` : `ingested as #${doc.id}`;
      fileInput.value = "";
      await refresh();
    } catch (e) { upMsg.textContent = e.message; }
    upBtn.disabled = false;
  });
  wrap.append(el("form", { class: "filters", onsubmit: (e) => e.preventDefault() },
    el("label", { class: "field" }, "PDF file", fileInput), upBtn, upMsg));

  const table = el("table");
  wrap.append(table);
  const timers = new Map();

  async function refresh() {
    const page = await api("/documents?limit=200");
    clear(table);
    table.append(el("tr", {},
      el("th", {}, "#"), el("th", {}, "Title / file"), el("th", {}, "Status"),
      el("th", {}, "Pages"), el("th", {}, "Facts"), el("th", {}, "Rel."),
      el("th", {}, "Fail."), el("th", {}, "")));
    if (!page.items.length) {
      table.append(el("tr", {}, el("td", { colspan: "8", class: "empty" }, "No documents yet.")));
      return;
    }
    for (const d of page.items) table.append(docRow(d));
  }

  function docRow(d) {
    const c = d.counts || {};
    const statusCell = el("td", {}, badge(d.status, STATUS_KIND[d.status] || "n"));
    const btn = el("button", {}, d.status === "processing" ? "processing…" : "Process");
    btn.disabled = d.status === "processing";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api(`/documents/${d.id}/process`, { method: "POST" });
        poll(d.id, statusCell, btn, tr);
      } catch (e) { btn.textContent = e.message; }
    });
    const tr = el("tr", {},
      el("td", {}, "#" + d.id),
      el("td", {}, el("div", {}, d.title || d.original_filename || "—"),
        el("div", { class: "muted mono" }, d.sha256.slice(0, 12))),
      statusCell,
      el("td", {}, fmt(c.pages)), el("td", {}, fmt(c.facts)),
      el("td", {}, fmt(c.relationships)), el("td", {}, fmt(c.failures)),
      el("td", {}, btn));
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
      statusCell.append(badge(s.status, STATUS_KIND[s.status] || "n"));
      if (s.run && s.run.stage) statusCell.append(el("div", { class: "muted mono" }, s.run.stage));
      if (s.status === "done" || s.status === "failed") {
        clearInterval(t); timers.delete(id);
        if (s.run && s.run.error) statusCell.append(el("div", { class: "muted" }, s.run.error));
        btn.disabled = false; btn.textContent = "Process";
        await refresh();
      }
    }, 2000);
    timers.set(id, t);
  }

  await refresh();
  return wrap;
}

// ---- Facts ---------------------------------------------------------
const LIFECYCLE_OPTS = ["", "CANDIDATE", "GROUNDED", "NORMALIZED", "ELIGIBLE_FOR_REASONING", "QUARANTINED"];
const EVIDENCE_OPTS = ["", "VERIFIED", "PARTIAL", "UNVERIFIED"];
const MODALITY_OPTS = ["", "ASSERTED", "HISTORICAL", "ESTIMATED", "FORECAST", "TARGET", "UNCERTAIN"];
const TYPE_OPTS = ["", "numeric", "semantic"];

function select(name, opts, cur) {
  return el("select", { name }, ...opts.map((o) =>
    el("option", { value: o, selected: o === cur ? "selected" : null }, o || "(any)")));
}

async function viewFacts() {
  const state = { limit: 50, offset: 0 };
  const wrap = el("div");
  wrap.append(el("h2", {}, "Facts"));

  const inputs = {
    q: el("input", { name: "q", placeholder: "full-text…", size: "14" }),
    document_id: el("input", { name: "document_id", type: "number", size: "4", min: "1" }),
    lifecycle_state: select("lifecycle_state", LIFECYCLE_OPTS),
    evidence_status: select("evidence_status", EVIDENCE_OPTS),
    modality: select("modality", MODALITY_OPTS),
    type: select("type", TYPE_OPTS),
    reasoning_eligible: select("reasoning_eligible", ["", "true", "false"]),
  };
  const form = el("form", { class: "filters", onsubmit: (e) => { e.preventDefault(); state.offset = 0; load(); } },
    el("label", { class: "field" }, "search", inputs.q),
    el("label", { class: "field" }, "doc id", inputs.document_id),
    el("label", { class: "field" }, "lifecycle", inputs.lifecycle_state),
    el("label", { class: "field" }, "evidence", inputs.evidence_status),
    el("label", { class: "field" }, "modality", inputs.modality),
    el("label", { class: "field" }, "type", inputs.type),
    el("label", { class: "field" }, "eligible", inputs.reasoning_eligible),
    el("button", { class: "primary" }, "Filter"));
  wrap.append(form);

  const table = el("table");
  const pager = el("div", { class: "pager" });
  wrap.append(table, pager);

  function query() {
    const p = new URLSearchParams();
    for (const [k, node] of Object.entries(inputs)) if (node.value) p.set(k, node.value);
    p.set("limit", state.limit); p.set("offset", state.offset);
    return p.toString();
  }

  async function load() {
    const page = await api("/facts?" + query());
    clear(table);
    table.append(el("tr", {},
      el("th", {}, "Fact"), el("th", {}, "Doc / page"), el("th", {}, "Lifecycle"),
      el("th", {}, "Evidence"), el("th", {}, "Modality")));
    if (!page.items.length) {
      table.append(el("tr", {}, el("td", { colspan: "5", class: "empty" }, "No facts match.")));
    }
    for (const f of page.items) table.append(...factRows(f));
    clear(pager);
    pager.append(
      el("button", { onclick: () => { if (state.offset > 0) { state.offset -= state.limit; load(); } } }, "← prev"),
      el("span", { class: "muted" }, `${state.offset + 1}–${state.offset + page.items.length} of ${page.total}`),
      el("button", { onclick: () => { if (state.offset + state.limit < page.total) { state.offset += state.limit; load(); } } }, "next →"));
  }

  function factRows(f) {
    const detail = el("tr", { class: "detail", hidden: "hidden" }, el("td", { colspan: "5" }, "…"));
    const row = el("tr", { class: "row", onclick: () => toggleFact(f.id, detail) },
      el("td", {}, factLine(f)),
      el("td", {}, el("div", {}, f.document_title || "#" + f.document_id),
        el("div", { class: "muted mono" }, `p.${f.printed_label || "?"} / pdf ${f.page_index}`)),
      el("td", {}, badge(f.lifecycle_state, LIFECYCLE_KIND[f.lifecycle_state]),
        f.reasoning_eligible ? " " : "", f.reasoning_eligible ? badge("eligible", "g") : ""),
      el("td", {}, badge(f.evidence_status, EVIDENCE_KIND[f.evidence_status])),
      el("td", {}, f.modality));
    return [row, detail];
  }
  async function toggleFact(id, detailRow) {
    if (!detailRow.hidden) { detailRow.hidden = true; return; }
    detailRow.hidden = false;
    const cell = detailRow.firstChild; clear(cell); cell.append("loading…");
    try {
      const f = await api(`/facts/${id}`);
      clear(cell); cell.append(factDetail(f));
    } catch (e) { clear(cell); cell.append(errBox(e)); }
  }

  await load();
  return wrap;
}

function factDetail(f) {
  const box = el("div");
  if (f.evidence && f.evidence.quote) box.append(el("blockquote", {}, f.evidence.quote));
  const pairs = [
    ["subject", f.subject_raw],
    ["entity", f.entity ? `${f.entity.canonical_label} (#${f.entity.id})` : null],
    ["predicate", f.predicate],
    ["object", f.object_raw],
    ["value_text", f.value_text],
    ["reporting period", periodStr(f.reporting_period)],
    ["scope", f.scope],
    ["qualifiers", f.qualifiers && f.qualifiers.length ? f.qualifiers.join(", ") : null],
    ["modality", f.modality],
    ["context complete", f.context_complete],
    ["publisher", f.publisher],
    ["publication date", f.publication_date],
    ["data vintage", f.data_vintage],
  ];
  if (f.numeric) {
    pairs.push(["numeric_value", f.numeric.numeric_value],
      ["magnitude", f.numeric.magnitude],
      ["base_value", f.numeric.base_value],
      ["currency", f.numeric.currency],
      ["is_percentage", f.numeric.is_percentage],
      ["percentage_ratio", f.numeric.percentage_ratio],
      ["unit_norm", f.numeric.unit_norm]);
  }
  box.append(kv(pairs));
  if (f.evidence) {
    box.append(el("h4", { class: "muted" }, "verification"));
    box.append(kv([
      ["method", f.evidence.verification_method],
      ["numeric re-derivation", f.evidence.numeric_rederivation],
      ["fuzzy score", f.evidence.fuzzy_score],
      ["char range", f.evidence.char_start != null ? `${f.evidence.char_start}–${f.evidence.char_end}` : null],
      ["notes", f.evidence.notes],
    ]));
  }
  if (f.context_window) {
    box.append(el("h4", { class: "muted" }, "context window"),
      el("div", { class: "ctxwin" }, f.context_window));
  }
  if (f.repro && f.repro.extraction_model) {
    box.append(kv([["extraction model", f.repro.extraction_model],
      ["prompt version", f.repro.prompt_version]]));
  }
  if (f.relationships && f.relationships.length) {
    const ul = el("ul");
    for (const r of f.relationships) {
      ul.append(el("li", {}, el("a", { href: "#/relationships?open=" + r.id },
        `${r.category}${r.context_dimension ? " (" + r.context_dimension + ")" : ""} → fact #${r.other_fact_id}`)));
    }
    box.append(el("h4", { class: "muted" }, "relationships"), ul);
  }
  return box;
}
function periodStr(p) {
  if (!p || (!p.raw && !p.start)) return null;
  return `${p.raw || ""} ${p.start ? `[${p.start} → ${p.end})` : ""} ${p.type ? "· " + p.type : ""}`.trim();
}

// ---- Relationships -------------------------------------------------
const CATEGORIES = ["", "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN"];

async function viewRelationships() {
  const params = new URLSearchParams((location.hash.split("?")[1]) || "");
  const openId = params.get("open");
  const state = { category: "" };
  const wrap = el("div");
  wrap.append(el("h2", {}, "Relationships"));

  const tabs = el("div", { class: "tabs" });
  for (const c of CATEGORIES) {
    const b = el("button", { onclick: () => { state.category = c; renderTabs(); load(); } }, c || "All");
    tabs.append(b);
  }
  function renderTabs() {
    [...tabs.children].forEach((b, i) => b.classList.toggle("active", CATEGORIES[i] === state.category));
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
    if (!page.items.length) { list.append(el("p", { class: "empty" }, "No relationships in this category.")); return; }
    for (const r of page.items) list.append(relCard(r, r.id === Number(openId)));
  }

  await load();
  return wrap;
}

function relCard(r, openNow) {
  const body = el("div", { hidden: openNow ? null : "hidden" });
  const head = el("div", { class: "head", onclick: () => toggleRel(r.id, body) },
    badge(r.category_label || r.category, CATEGORY_KIND[r.category]),
    r.context_dimension ? badge(r.context_dimension, "n") : "",
    el("span", { class: "muted" }, "confidence " + fmt(r.confidence)),
    r.llm_used ? badge("LLM", "b") : badge("deterministic", "n"));
  const card = el("div", { class: "card" }, head,
    el("div", { class: "cols" },
      relSide("Fact A", r.fact_a), relSide("Fact B", r.fact_b)),
    body);
  if (openNow) fillRel(r.id, body);
  return card;
}
function relSide(title, f) {
  return el("div", { class: "side" }, el("h4", {}, title),
    el("div", {}, factLine(f)),
    el("div", { class: "muted mono" }, (f.document_title || "#" + f.document_id) + " p." + (f.printed_label || "?")),
    f.evidence && f.evidence.quote ? el("blockquote", {}, f.evidence.quote) : "");
}
async function toggleRel(id, body) {
  if (!body.hidden) { body.hidden = true; return; }
  body.hidden = false;
  await fillRel(id, body);
}
async function fillRel(id, body) {
  clear(body); body.append("loading…");
  try {
    const r = await api(`/relationships/${id}`);
    clear(body);
    const sig = el("table", { class: "signals" });
    for (const [k, v] of Object.entries(r.deterministic_signals || {})) {
      sig.append(el("tr", {}, el("td", {}, k), el("td", { class: "mono" }, fmt(v))));
    }
    body.append(
      el("h4", { class: "muted" }, "deterministic signals"), sig,
      kv([
        ["LLM proposed", r.llm_proposed_category],
        ["final category", r.category],
        ["validation action", r.validation_action],
        ["validation notes", r.validation_notes],
      ]),
      el("h4", { class: "muted" }, "reasoning"),
      el("div", {}, r.reasoning || "—"),
      el("h4", { class: "muted" }, "evidence"),
      el("div", { class: "cols" },
        evidenceBlock("Fact A", r.fact_a), evidenceBlock("Fact B", r.fact_b)));
  } catch (e) { clear(body); body.append(errBox(e)); }
}
function evidenceBlock(title, f) {
  return el("div", { class: "side" }, el("h4", {}, title),
    f.evidence && f.evidence.quote ? el("blockquote", {}, f.evidence.quote) : el("div", { class: "muted" }, "no quote"),
    f.context_window ? el("div", { class: "ctxwin" }, f.context_window) : "");
}

// ---- Failures ----------------------------------------------------
async function viewFailures() {
  const wrap = el("div");
  wrap.append(el("h2", {}, "Failures"));
  const page = await api("/failures?limit=200");
  wrap.append(el("p", { class: "muted" },
    "by type: " + (Object.entries(page.counts_by_type).map(([k, v]) => `${k} ${v}`).join(" · ") || "none")));
  const table = el("table");
  table.append(el("tr", {}, el("th", {}, "Type"), el("th", {}, "Reason"),
    el("th", {}, "Ref"), el("th", {}, "Doc"), el("th", {}, "Linked")));
  if (!page.items.length) table.append(el("tr", {}, el("td", { colspan: "5", class: "empty" }, "No failures.")));
  for (const it of page.items) {
    let linked = "—";
    if (it.fact) linked = el("span", {}, factLine(it.fact), " ", badge(it.fact.lifecycle_state, LIFECYCLE_KIND[it.fact.lifecycle_state]));
    else if (it.relationship) linked = el("a", { href: "#/relationships?open=" + it.relationship.id },
      it.relationship.category + " #" + it.relationship.id);
    table.append(el("tr", {},
      el("td", {}, badge(it.failure_type, "r")),
      el("td", {}, it.reason),
      el("td", { class: "mono" }, `${it.ref_table || ""}${it.ref_id ? "#" + it.ref_id : ""}`),
      el("td", {}, it.document_id ? "#" + it.document_id : "—"),
      el("td", {}, linked)));
  }
  wrap.append(table);
  return wrap;
}

// ---- Entities --------------------------------------------------
async function viewEntities() {
  const wrap = el("div");
  wrap.append(el("h2", {}, "Entities"));
  const page = await api("/entities?limit=200");
  const table = el("table");
  table.append(el("tr", {}, el("th", {}, "#"), el("th", {}, "Canonical"), el("th", {}, "Type"),
    el("th", {}, "Aliases"), el("th", {}, "Facts"), el("th", {}, "Method")));
  if (!page.items.length) table.append(el("tr", {}, el("td", { colspan: "6", class: "empty" }, "No entities.")));
  for (const e of page.items) {
    const detail = el("tr", { class: "detail", hidden: "hidden" }, el("td", { colspan: "6" }, "…"));
    const row = el("tr", { class: "row", onclick: () => toggleEntity(e.id, detail) },
      el("td", {}, "#" + e.id), el("td", {}, e.canonical_label), el("td", {}, e.entity_type || "—"),
      el("td", {}, fmt(e.alias_count)), el("td", {}, fmt(e.fact_count)),
      el("td", {}, e.resolution_method || "—", e.llm_confirmed ? " " : "", e.llm_confirmed ? badge("LLM", "b") : ""));
    table.append(row, detail);
  }
  wrap.append(table);
  return wrap;
}
async function toggleEntity(id, detailRow) {
  if (!detailRow.hidden) { detailRow.hidden = true; return; }
  detailRow.hidden = false;
  const cell = detailRow.firstChild; clear(cell); cell.append("loading…");
  try {
    const e = await api(`/entities/${id}`);
    clear(cell);
    const aliases = el("ul");
    for (const a of e.aliases || []) {
      aliases.append(el("li", {}, `${a.surface} `, badge(a.match_method || "?", "n"),
        a.source_fact_id ? ` (from fact #${a.source_fact_id})` : ""));
    }
    const facts = el("ul");
    for (const f of e.sample_facts || []) facts.append(el("li", {}, factLine(f)));
    cell.append(el("h4", { class: "muted" }, "aliases"), aliases,
      el("h4", { class: "muted" }, "sample facts"), facts);
  } catch (err) { clear(cell); cell.append(errBox(err)); }
}
