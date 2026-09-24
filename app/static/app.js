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
const $ = (id) => document.getElementById(id);
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function fmt(v) {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toPrecision(6).replace(/\.?0+$/, "");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
function count(n) { return n == null ? "—" : Number(n).toLocaleString("en-US"); }
function plural(n, one, many) { return `${count(n)} ${n === 1 ? one : many || one + "s"}`; }
function bytes(n) {
  return n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`;
}
function hashParams() { return new URLSearchParams(location.hash.split("?")[1] || ""); }

async function api(path, opts) {
  let res;
  try { res = await fetch(path, opts); }
  catch { throw Object.assign(new Error("Can't reach the server. Check that uvicorn is still running."), { code: "network" }); }
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const e = new Error(body && body.error ? body.error.message : `The server answered HTTP ${res.status}.`);
    e.code = body && body.error ? body.error.code : "http_" + res.status;
    throw e;
  }
  return body;
}

// ---- vocabulary ----------------------------------------------------------
// Raw API enums stay the source of truth; the UI shows a readable label plus a
// shape glyph so state never depends on colour alone.
const LIFECYCLE = {
  ELIGIBLE_FOR_REASONING: ["Eligible", "ok", "✓"], NORMALIZED: ["Normalized", "info", "•"],
  GROUNDED: ["Grounded", "info", "•"], CANDIDATE: ["Candidate", "warn", "•"],
  RAW: ["Raw", "neutral", "•"], QUARANTINED: ["Quarantined", "bad", "✕"],
};
const EVIDENCE = {
  VERIFIED: ["Verified", "ok", "✓"], PARTIAL: ["Partial", "warn", "~"],
  UNVERIFIED: ["Unverified", "bad", "✕"],
};
const CATEGORY = {
  CORROBORATES: ["Corroborates", "ok", "✓", "s-ok"],
  CONTRADICTS: ["Contradicts", "bad", "✕", "s-bad"],
  DIFFERENT_CONTEXT: ["Different context", "info", "≠", "s-info"],
  TEMPORAL_EVOLUTION: ["Temporal evolution", "info", "→", "s-info-2"],
  UNCERTAIN: ["Uncertain", "warn", "?", "s-warn"],
};
const DOC_STATUS = {
  done: ["Processed", "ok", "✓"], ingested: ["Ingested", "neutral", "•"],
  processing: ["Processing", "warn", null], uploaded: ["Uploaded", "neutral", "•"],
  failed: ["Failed", "bad", "✕"],
};

function pill(text, tone, glyph, title) {
  return el("span", { class: "pill " + (tone || "neutral"), title: title || null },
    glyph ? el("span", { class: "g0", "aria-hidden": "true" }, glyph) : null, text);
}
/** Pill from one of the vocabularies above; unknown values degrade to neutral. */
function tag(value, map) {
  if (!value) return null;
  const [label, tone, glyph] = map[value] || [value, "neutral", "•"];
  return pill(label, tone, glyph, value);
}

// One icon family: 16px grid, 1.5 stroke, round caps.
const ICONS = {
  overview: '<rect x="2.25" y="2.25" width="4.75" height="4.75" rx="1"/><rect x="9" y="2.25" width="4.75" height="4.75" rx="1"/><rect x="2.25" y="9" width="4.75" height="4.75" rx="1"/><rect x="9" y="9" width="4.75" height="4.75" rx="1"/>',
  documents: '<path d="M4 1.75h4.75L12.25 5.25v9H4z"/><path d="M8.5 1.75v3.75h3.75"/>',
  facts: '<path d="M6 4h7.5M6 8h7.5M6 12h7.5"/><path d="M2.75 4h.01M2.75 8h.01M2.75 12h.01"/>',
  relationships: '<circle cx="4" cy="4.25" r="2"/><circle cx="12" cy="11.75" r="2"/><path d="M5.6 5.8l4.8 4.4"/>',
  entities: '<circle cx="8" cy="5.5" r="2.5"/><path d="M3 14c0-2.8 2.2-4.5 5-4.5s5 1.7 5 4.5"/>',
  failures: '<path d="M8 2.25l6 11H2z"/><path d="M8 6.5v3M8 11.5v.01"/>',
  upload: '<path d="M8 10.5V2.5M5 5.25 8 2.25l3 3"/><path d="M2.75 10v2.75c0 .55.45 1 1 1h8.5c.55 0 1-.45 1-1V10"/>',
  search: '<circle cx="7" cy="7" r="4.25"/><path d="m10.25 10.25 3.25 3.25"/>',
  chevron: '<path d="M4.5 6.5 8 10l3.5-3.5"/>',
  chevronRight: '<path d="M6.5 4.5 10 8l-3.5 3.5"/>',
  arrowRight: '<path d="M3 8h10M9 4l4 4-4 4"/>',
  check: '<path d="m3.5 8.5 3 3 6-7"/>',
  x: '<path d="m4.5 4.5 7 7M11.5 4.5l-7 7"/>',
  alert: '<circle cx="8" cy="8" r="6"/><path d="M8 5v3.5M8 11v.01"/>',
  info: '<circle cx="8" cy="8" r="6"/><path d="M8 7.25V11M8 5v.01"/>',
  refresh: '<path d="M13 8a5 5 0 1 1-1.46-3.54"/><path d="M13 2.5V5h-2.5"/>',
  menu: '<path d="M2.75 4.5h10.5M2.75 8h10.5M2.75 11.5h10.5"/>',
  book: '<path d="M2.75 3.25h4a1.25 1.25 0 0 1 1.25 1.25v9a1 1 0 0 0-1-1h-4.25zM13.25 3.25h-4A1.25 1.25 0 0 0 8 4.5v9a1 1 0 0 1 1-1h4.25z"/>',
};
function icon(name) {
  return el("span", {
    class: "i", "aria-hidden": "true",
    html: `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`,
  });
}

// ---- building blocks -----------------------------------------------------
function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v, mono] of pairs) {
    if (v == null || v === "" || (Array.isArray(v) && !v.length)) continue;
    dl.append(el("dt", {}, k), el("dd", { class: mono ? "mono" : null }, v.nodeType ? v : fmt(v)));
  }
  return dl;
}
function emptyState(title, body, action, iconName) {
  return el("div", { class: "empty" },
    el("div", { class: "dz-icon" }, icon(iconName || "info")),
    el("strong", {}, title), body ? el("p", {}, body) : null, action || null);
}
function errorState(e, retry) {
  return el("div", { class: "callout bad", role: "alert" }, icon("alert"),
    el("div", {},
      el("strong", {}, "This view couldn't load. "), e.message,
      e.code ? el("div", { class: "code" }, e.code) : null,
      retry ? el("div", {}, el("button", { class: "btn sm", type: "button", onclick: retry },
        icon("refresh"), "Try again")) : null));
}
function skeleton(rows) {
  return el("div", { class: "panel skeleton-block", "aria-busy": "true", "aria-label": "Loading" },
    Array.from({ length: rows || 4 }, (_, i) =>
      el("div", { class: "skeleton", style: `width:${[58, 92, 76, 84, 68][i % 5]}%` })));
}
function sectionHead(title, aside) {
  return el("div", { class: "section-head" }, el("h2", {}, title), aside || null);
}
function panelTable(head, tbody, foot) {
  const table = el("table", {}, head, tbody);
  return [el("div", { class: "panel" }, el("div", { class: "table-wrap" }, table), foot || null), table];
}
/** Header cells: a string, or [label, className]. */
function headRow(...cols) {
  return el("thead", {}, el("tr", {}, ...cols.map((c) =>
    Array.isArray(c) ? el("th", { class: c[1], scope: "col" }, c[0]) : el("th", { scope: "col" }, c))));
}
function emptyRow(colspan, ...args) {
  return el("tr", {}, el("td", { colspan: String(colspan) }, emptyState(...args)));
}
function linkBtn(href, label, iconName, cls) {
  return el("a", { class: "btn " + (cls || ""), href }, iconName ? icon(iconName) : null, label);
}
function disclose(title, ...kids) {
  return el("details", { class: "disclose" }, el("summary", {}, icon("chevronRight"), title), ...kids);
}
/** Keyboard + pointer toggle for an expandable table row. */
function expandableRow(cells, onToggle) {
  const row = el("tr", { class: "row", tabindex: "0", "aria-expanded": "false" }, ...cells);
  const go = () => onToggle(row);
  row.addEventListener("click", (e) => { if (!e.target.closest("a, button")) go(); });
  row.addEventListener("keydown", (e) => {
    if (e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); go(); }
  });
  return row;
}

function toast(message, tone) {
  const t = el("div", { class: "toast " + (tone || "") },
    icon(tone === "bad" ? "alert" : tone === "ok" ? "check" : "info"), el("span", {}, message));
  $("toasts").append(t);
  setTimeout(() => { t.classList.add("leaving"); setTimeout(() => t.remove(), 220); }, 4500);
}

/** Claim rendered value-first: what the fact says, then who it is about. */
function claim(f) {
  return el("div", { class: "claim" },
    el("div", { class: "value" }, f.object_raw),
    el("div", { class: "subject" }, el("b", {}, f.subject_raw), " · ", f.predicate));
}
function titleCase(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase().replace(/_/g, " ") : s;
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
/** Printed page label when the PDF has one, else the 1-based PDF page. */
function pageRef(f) {
  return f.printed_label ? `p. ${f.printed_label}` : `page ${f.page_index + 1}`;
}
function source(f) {
  return el("div", { class: "src" },
    el("div", {}, factDocLabel(f)), el("div", { class: "loc" }, pageRef(f)));
}
function periodLabel(p) { return p && (p.raw || p.start) ? p.raw || p.start : null; }
function periodStr(p) {
  if (!p || (!p.raw && !p.start)) return null;
  return `${p.raw || ""} ${p.start ? `[${p.start} → ${p.end})` : ""} ${p.type ? "· " + p.type : ""}`.trim();
}
function scopeStr(scope) {
  if (!scope || typeof scope !== "object") return scope;
  const parts = Object.entries(scope).map(([k, v]) => `${k}: ${fmt(v)}`);
  return parts.length ? parts.join(" · ") : null;
}
/** Proportional bar + legend for a distribution; each part may link somewhere. */
function meter(parts) {
  const total = parts.reduce((s, p) => s + p.n, 0) || 1;
  const bar = el("div", { class: "meter", role: "img",
    "aria-label": parts.map((p) => `${p.label} ${p.n}`).join(", ") });
  const legend = el("ul", { class: "legend" });
  for (const p of parts) {
    if (p.n) bar.append(el("span", { class: p.series, style: `width:${(p.n / total) * 100}%` }));
    const inner = [el("span", { class: "swatch " + p.series }), p.label, el("b", {}, count(p.n))];
    legend.append(el("li", {}, p.href
      ? el("a", { href: p.href }, ...inner)
      : el("span", { class: "row-inner" }, ...inner)));
  }
  return el("div", {}, bar, legend);
}
function metric(label, value, note, opts) {
  const o = opts || {};
  return el("div", { class: "metric" + (o.alert ? " alert" : "") },
    el("dt", {}, label), el("dd", {}, count(value)),
    note ? (o.href ? el("a", { class: "note", href: o.href }, note) : el("div", { class: "note" }, note)) : null);
}

// ---- router --------------------------------------------------------------
const VIEWS = {
  overview: { render: viewOverview, title: "Overview", group: "Workspace",
    sub: "What has been ingested, how the extracted facts relate, and how much of it is trustworthy." },
  documents: { render: viewDocuments, title: "Documents", group: "Workspace",
    sub: "Upload a PDF, then process it: extract → verify → normalize → resolve → reason." },
  facts: { render: viewFacts, title: "Facts", group: "Knowledge",
    sub: "Every fact is pinned to a verbatim quote on its source page. Open a row to see the evidence." },
  relationships: { render: viewRelationships, title: "Relationships", group: "Knowledge",
    sub: "How facts from different documents relate — and the signals behind each call." },
  entities: { render: viewEntities, title: "Entities", group: "Knowledge",
    sub: "Subject names resolved to one canonical entity, with the aliases that were merged." },
  failures: { render: viewFailures, title: "Failures", group: "Quality",
    sub: "Quarantined facts and pipeline errors. Nothing is dropped silently — it all lands here." },
};
let routeToken = 0;
let leaveHooks = [];
function onLeave(fn) { leaveHooks.push(fn); }

function currentRoute() {
  const h = (location.hash || "#/overview").slice(2).split("?")[0];
  return VIEWS[h] ? h : "overview";
}
function renderNav() {
  const nav = $("nav");
  clear(nav);
  const active = currentRoute();
  const groups = {};
  for (const [name, v] of Object.entries(VIEWS)) (groups[v.group] ||= []).push([name, v]);
  for (const [group, items] of Object.entries(groups)) {
    nav.append(el("div", { class: "nav-group" },
      el("div", { class: "nav-label" }, group),
      ...items.map(([name, v]) => el("a", {
        href: "#/" + name, "aria-current": active === name ? "page" : null,
      }, icon(name), v.title))));
  }
}
function setActions(...nodes) { const a = $("page-actions"); clear(a); a.append(...nodes); }
function setMenu(open) {
  $("sidebar").classList.toggle("open", open);
  const b = $("menu-btn");
  b.setAttribute("aria-expanded", String(open));
  b.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
  clear(b); b.append(icon(open ? "x" : "menu"));
}

async function route(fromNavigation) {
  const token = ++routeToken;
  leaveHooks.forEach((fn) => fn()); leaveHooks = [];
  setMenu(false);
  renderNav();
  const v = VIEWS[currentRoute()];
  document.title = `${v.title} · Fact Knowledge Layer`;
  $("page-title").textContent = v.title;
  $("page-sub").textContent = v.sub;
  setActions();
  const view = $("view");
  clear(view); view.append(skeleton(4));
  if (fromNavigation) $("page-title").focus({ preventScroll: true });
  let node;
  try { node = await v.render(); }
  catch (e) { node = errorState(e, () => route()); }
  if (token !== routeToken) return;  // the user navigated on while this loaded
  clear(view);
  view.style.animation = "none"; void view.offsetWidth; view.style.animation = "";
  view.append(node);
}

let health = null;
async function renderHealth() {
  const box = $("health");
  try { health = await api("/health"); }
  catch { health = null; }
  clear(box);
  if (!health) {
    box.append(el("div", { class: "health-row" }, el("span", { class: "dot bad" }), el("span", {}, "API unreachable")));
    return;
  }
  const key = health.llm.api_key_present;
  box.append(
    el("div", { class: "health-row", title: `database: ${health.database.path}` },
      el("span", { class: "dot " + (health.status === "ok" ? "ok" : "bad") }),
      el("span", {}, health.status === "ok" ? "API connected" : "API degraded")),
    el("div", { class: "health-row", title: key ? `${health.llm.provider} · ${health.llm.model}` : `Set ${health.llm.api_key_env} to enable extraction` },
      el("span", { class: "dot " + (key ? "ok" : "warn") }),
      el("span", {}, key ? health.llm.model : "No LLM key — extraction off")),
    el("div", { class: "health-row" },
      el("a", { href: "/docs" }, "API reference"), el("span", { class: "muted" }, `· v${health.version}`)));
}

window.addEventListener("hashchange", () => route(true));
window.addEventListener("DOMContentLoaded", () => {
  $("menu-btn").addEventListener("click", () => setMenu(!$("sidebar").classList.contains("open")));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("sidebar").classList.contains("open")) { setMenu(false); $("menu-btn").focus(); }
  });
  setMenu(false);
  route(false);
  renderHealth();
});

// ---- Overview ------------------------------------------------------------
const STAGES = [
  ["Ingest", "PDF to page text with exact character offsets."],
  ["Extract", "Candidate facts from each chunk, as structured output.", true],
  ["Verify", "The quote must be found on its page, or the fact is quarantined."],
  ["Normalize", "Numbers, units, currencies and periods made comparable."],
  ["Resolve", "Subject names merged into canonical entities.", true],
  ["Retrieve", "Bounded candidate pairs with deterministic signals."],
  ["Reason", "Rules decide the relationship; a model may only propose.", true],
];
function pipelineSteps() {
  return el("div", {},
    el("ol", { class: "steps" }, STAGES.map(([name, text, model], i) => el("li", {},
      el("span", { class: "n" }, String(i + 1).padStart(2, "0")),
      el("div", {}, el("b", {}, name, model ? el("span", { class: "model", title: "Model-assisted" }) : null)),
      el("p", {}, text)))),
    el("div", { class: "steps-key" }, el("span", { class: "model" }),
      "Model-assisted stage. Every other stage is deterministic, and no model output is trusted without a check."));
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
  const byCat = {
    CORROBORATES: corroborates.total, CONTRADICTS: contradicts.total,
    DIFFERENT_CONTEXT: differentContext.total, TEMPORAL_EVOLUTION: temporal.total,
    UNCERTAIN: uncertain.total,
  };
  const rels = Object.values(byCat).reduce((a, b) => a + b, 0);
  setActions(linkBtn("#/documents", "Upload a PDF", "upload", "primary"));

  const wrap = el("div", { class: "stack" });

  if (!docs.total) {
    wrap.append(el("section", { class: "panel panel-pad" }, el("div", { class: "onboard" },
      el("div", {},
        el("h2", {}, "Turn a stack of PDFs into facts you can check."),
        el("p", {}, "Each fact keeps the exact sentence it came from. Facts from different documents are then compared, and a difference in scope, period or units is never mistaken for a contradiction."),
        linkBtn("#/documents", "Upload your first PDF", "upload", "primary")),
      el("ol", {},
        el("li", {}, el("div", {}, el("b", {}, "Upload"), "An annual report, an earnings deck, a research note — any text-based PDF.")),
        el("li", {}, el("div", {}, el("b", {}, "Process"), "Facts are extracted, and each one must be found verbatim on its page to be kept.")),
        el("li", {}, el("div", {}, el("b", {}, "Compare"), "Add a second document to see what corroborates, contradicts or only differs in context."))))));
    wrap.append(el("section", {}, sectionHead("How a document is processed"), pipelineSteps()));
    return wrap;
  }

  wrap.append(el("dl", { class: "metrics", style: "margin:0" },
    metric("Documents", docs.total, `${count(processed.total)} processed`, { href: "#/documents" }),
    metric("Facts", facts.total, `${count(eligible.total)} eligible for comparison`, { href: "#/facts" }),
    metric("Relationships", rels, plural(contradicts.total, "contradiction"),
      { href: "#/relationships?category=CONTRADICTS" }),
    metric("Entities", entities.total, "canonical subjects", { href: "#/entities" }),
    metric("Quarantined", quarantined.total,
      quarantined.total ? "held out of reasoning" : "every fact grounded",
      { alert: quarantined.total > 0, href: "#/failures" })));

  const mix = el("section", { class: "panel panel-pad" },
    sectionHead("How facts relate", el("a", { href: "#/relationships" }, "Open", icon("arrowRight"))),
    rels
      ? meter(Object.entries(byCat).map(([c, n]) => ({
        label: CATEGORY[c][0], n, series: CATEGORY[c][3], href: "#/relationships?category=" + c })))
      : emptyState("Nothing to compare yet", "Relationships appear once two processed documents share a subject.", null, "relationships"),
    el("p", { class: "caption" },
      "A gap explained by scope, period or modality is filed as different context — never as a contradiction."));

  const other = Math.max(0, facts.total - eligible.total - quarantined.total);
  const integrity = el("section", { class: "panel panel-pad" },
    sectionHead("Evidence integrity", el("a", { href: "#/failures" }, plural(failures.total, "failure"), icon("arrowRight"))),
    facts.total
      ? meter([
        { label: "Eligible for comparison", n: eligible.total, series: "s-ok", href: "#/facts?reasoning_eligible=true" },
        { label: "Quarantined", n: quarantined.total, series: "s-bad", href: "#/facts?lifecycle_state=QUARANTINED" },
        { label: "Unverified evidence", n: unverified.total, series: "s-warn", href: "#/facts?evidence_status=UNVERIFIED" },
        { label: "Still in the pipeline", n: other, series: "s-neutral" },
      ])
      : emptyState("No facts yet", "Process a document to extract its facts.", null, "facts"),
    el("p", { class: "caption" },
      "Only facts whose quote was re-found on the source page are eligible for comparison."));
  wrap.append(el("div", { class: "split" }, mix, integrity));

  const [recent] = panelTable(
    headRow("Document", ["Status", "tight"], ["Pages", "r hide-sm"], ["Facts", "r"], ["Relationships", "r hide-sm"]),
    el("tbody", {}, docs.items.map((d) => el("tr", {},
      el("td", {}, el("div", {}, docLabel(d)), el("div", { class: "sub mono" }, "#" + d.id)),
      el("td", { class: "tight" }, tag(d.status, DOC_STATUS)),
      el("td", { class: "r hide-sm" }, count((d.counts || {}).pages)),
      el("td", { class: "r" }, el("a", { class: "num-link", href: "#/facts?document_id=" + d.id }, count((d.counts || {}).facts))),
      el("td", { class: "r hide-sm" }, count((d.counts || {}).relationships))))));
  wrap.append(el("section", {},
    sectionHead("Recent documents", el("a", { href: "#/documents" }, "All documents", icon("arrowRight"))), recent));

  wrap.append(el("section", {}, sectionHead("How a document is processed"), pipelineSteps()));
  return wrap;
}

// ---- Documents -----------------------------------------------------------
async function viewDocuments() {
  const wrap = el("div", { class: "stack" });
  // extract + reason call the LLM; without a key Process will stop at extract and
  // mark the document failed. Say so before the click, not after.
  const h = await api("/health").catch(() => null);
  if (h && !h.llm.api_key_present) {
    wrap.append(el("div", { class: "callout warn" }, icon("alert"),
      el("div", {},
        el("strong", {}, `Extraction is off: no ${h.llm.provider} API key. `),
        "You can still upload, but processing will stop at the extract stage. Add ",
        el("code", {}, h.llm.api_key_env), " to ", el("code", {}, ".env"),
        ` and restart the server (model ${h.llm.model}).`)));
  }

  // -- upload ---------------------------------------------------------------
  const fileInput = el("input", { type: "file", accept: "application/pdf,.pdf", id: "pdf-file",
    "aria-describedby": "pdf-hint" });
  const zone = el("label", { class: "dropzone", for: "pdf-file" },
    el("div", { class: "dz-icon" }, icon("upload")),
    el("div", { class: "dz-text" },
      el("strong", {}, "Drop a PDF here, or ", el("u", {}, "browse your files")),
      el("span", { id: "pdf-hint" }, "Text-based PDFs work best — scanned pages are flagged, not OCR'd. Re-uploading the same file is detected.")),
    fileInput);
  const chip = el("div", { class: "file-chip", hidden: "hidden" });
  const upBtn = el("button", { class: "btn primary", type: "submit", disabled: "disabled" }, icon("upload"), "Upload");
  const msg = el("span", { class: "form-msg", role: "status" });
  let file = null;

  function setMsg(text, tone) {
    clear(msg); msg.className = "form-msg " + (tone || "");
    if (text && tone) msg.append(icon(tone === "bad" ? "alert" : "check"));
    if (text) msg.append(text);
  }
  function choose(f) {
    file = null; zone.classList.remove("invalid"); setMsg("");
    clear(chip); chip.hidden = true; upBtn.disabled = true;
    if (!f) return;
    if (!/\.pdf$/i.test(f.name) && f.type !== "application/pdf") {
      zone.classList.add("invalid");
      setMsg(`“${f.name}” isn't a PDF. Only .pdf files can be ingested.`, "bad");
      return;
    }
    file = f;
    chip.append(icon("documents"), el("span", { class: "name", title: f.name }, f.name),
      el("span", { class: "size" }, bytes(f.size)),
      el("button", { class: "btn ghost icon-only", type: "button", "aria-label": "Remove file",
        onclick: () => { fileInput.value = ""; choose(null); } }, icon("x")));
    chip.hidden = false; upBtn.disabled = false;
  }
  fileInput.addEventListener("change", () => choose(fileInput.files[0]));
  for (const ev of ["dragenter", "dragover"]) {
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); });
  }
  zone.addEventListener("drop", (e) => choose(e.dataTransfer.files[0]));

  const form = el("form", { class: "panel panel-pad", "aria-label": "Upload a PDF" },
    zone, el("div", { class: "upload-row" }, chip, upBtn, msg));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!file) { setMsg("Choose a PDF first.", "bad"); return; }
    upBtn.disabled = true; clear(upBtn); upBtn.append(el("span", { class: "spinner" }), "Uploading…");
    setMsg("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const doc = await api("/documents", { method: "POST", body: fd });
      const text = doc.duplicate
        ? `Already in the library as #${doc.id} — nothing new to ingest.`
        : `Ingested as #${doc.id} · ${plural((doc.counts || {}).pages, "page")}. Process it below.`;
      setMsg(text, "ok");
      toast(doc.duplicate ? `“${docLabel(doc)}” was already uploaded` : `“${docLabel(doc)}” uploaded`, "ok");
      fileInput.value = ""; choose(null); setMsg(text, "ok");
      await refresh();
    } catch (err) {
      setMsg(err.message, "bad");
      upBtn.disabled = false;
    }
    clear(upBtn); upBtn.append(icon("upload"), "Upload");
    upBtn.disabled = !file;
  });
  wrap.append(el("section", {}, sectionHead("Add a document"), form));

  // -- library --------------------------------------------------------------
  const tbody = el("tbody", {});
  const [tablePanel, libTable] = panelTable(
    headRow("Document", "Status", ["Pages", "r hide-sm"], ["Facts", "r"], ["Eligible", "r hide-sm"],
      ["Relationships", "r hide-sm"], ["Failures", "r hide-sm"], ["", "tight"]),
    tbody);
  libTable.classList.add("card-rows");
  const countNote = el("p", {});
  wrap.append(el("section", {}, sectionHead("Library", countNote), tablePanel));
  const timers = new Map();
  onLeave(() => { timers.forEach(clearInterval); timers.clear(); });
  setActions(el("button", { class: "btn", type: "button", onclick: () => refresh().catch((e) => toast(e.message, "bad")) },
    icon("refresh"), "Refresh"));

  async function refresh() {
    const page = await api("/documents?limit=200");
    clear(tbody);
    countNote.textContent = page.total ? plural(page.total, "document") : "";
    if (!page.items.length) {
      tbody.append(emptyRow(8, "No documents yet",
        "Upload a PDF above. It is split into pages and chunks straight away; processing is a separate step.",
        null, "documents"));
      return;
    }
    for (const d of page.items) tbody.append(docRow(d));
  }

  function statusCell(status, detail, stage) {
    const pillNode = status === "processing"
      ? el("span", { class: "pill warn" }, el("span", { class: "spinner", "aria-hidden": "true" }),
        stage ? `Processing · ${stage}` : "Processing")
      : tag(status, DOC_STATUS);
    return detail ? [pillNode, el("div", { class: "detail-text", title: detail }, detail)] : [pillNode];
  }

  function docRow(d) {
    const c = d.counts || {};
    const cell = el("td", { class: "c-status" }, el("div", { class: "status-cell" },
      // a bare "Failed" says nothing — show which stage gave up and why
      ...statusCell(d.status, d.status === "failed" ? d.status_detail : null)));
    // a document that already holds facts cannot be re-processed (the API
    // returns 409): re-extracting would duplicate candidates, and a failure
    // would flip a good document to "failed" for nothing.
    const processed = (c.facts || 0) > 0;
    const btn = el("button", { class: "btn sm" + (processed ? " ghost" : d.status === "failed" ? "" : " primary"),
      type: "button", "aria-label": `Process document ${d.id}` });
    function label(state) {
      clear(btn);
      if (state === "running") btn.append(el("span", { class: "spinner" }), "Processing…");
      else if (processed) btn.append(icon("check"), "Processed");
      else btn.append(d.status === "failed" ? "Retry" : "Process");
    }
    label(d.status === "processing" ? "running" : null);
    btn.disabled = d.status === "processing" || processed;
    if (processed) btn.title = `Already holds ${plural(c.facts, "fact")} — re-processing would duplicate them.`;
    btn.addEventListener("click", async () => {
      btn.disabled = true; label("running");
      try {
        await api(`/documents/${d.id}/process`, { method: "POST" });
        const sc = cell.firstChild; clear(sc); sc.append(...statusCell("processing", null, "queued"));
        poll(d, cell, btn);
      } catch (e) {
        btn.disabled = false; label(null);
        toast(e.message, "bad");
      }
    });
    const tr = el("tr", {},
      el("td", { class: "c-main" }, el("div", {}, docLabel(d)),
        el("div", { class: "sub mono" }, "#" + d.id + " · " +
          (d.title && d.original_filename ? d.original_filename : d.sha256.slice(0, 12)))),
      cell,
      el("td", { class: "r hide-sm" }, count(c.pages)),
      el("td", { class: "r c-meta", "data-label": "Facts" }, c.facts
        ? el("a", { class: "num-link", href: "#/facts?document_id=" + d.id }, count(c.facts)) : count(c.facts)),
      el("td", { class: "r hide-sm" }, count(c.eligible_facts)),
      el("td", { class: "r hide-sm" }, count(c.relationships)),
      el("td", { class: "r hide-sm" }, count(c.failures)),
      // "Processed" already shows in the status pill; only explain the odd case
      // of a document that holds facts without a completed run (seeded data)
      el("td", { class: "tight c-action" }, processed && d.status === "done" ? null : btn));
    if (d.status === "processing") poll(d, cell, btn);
    return tr;
  }

  function poll(d, cell, btn) {
    if (timers.has(d.id)) return;
    const t = setInterval(async () => {
      let s;
      try { s = await api(`/documents/${d.id}/status`); }
      catch { clearInterval(t); timers.delete(d.id); return; }
      const sc = cell.firstChild;
      clear(sc);
      sc.append(...statusCell(s.status, s.run && s.run.error, s.run && s.run.stage));
      if (s.status === "done" || s.status === "failed") {
        clearInterval(t); timers.delete(d.id);
        const st = (s.run && s.run.stats) || {};
        if (s.status === "done") {
          toast(`${docLabel(d)}: ${plural(st.facts_grounded || 0, "fact")} grounded, ${plural(st.relationships_produced || 0, "relationship")}`, "ok");
        } else {
          toast(`${docLabel(d)} failed at ${(s.run && s.run.stage) || "a stage"}`, "bad");
        }
        await refresh();
      }
    }, 2000);
    timers.set(d.id, t);
  }

  await refresh();
  return wrap;
}

// ---- Facts ---------------------------------------------------------------
const LIFECYCLE_OPTS = ["", "CANDIDATE", "GROUNDED", "NORMALIZED", "ELIGIBLE_FOR_REASONING", "QUARANTINED"];
const EVIDENCE_OPTS = ["", "VERIFIED", "PARTIAL", "UNVERIFIED"];
const MODALITY_OPTS = ["", "ASSERTED", "HISTORICAL", "ESTIMATED", "FORECAST", "TARGET", "UNCERTAIN"];
const TYPE_OPTS = ["", "numeric", "semantic"];

function select(name, opts, labels) {
  return el("select", { class: "select", name, id: "f-" + name }, ...opts.map((o) =>
    el("option", { value: o }, o ? (labels && labels[o]) || titleCase(o) : "Any")));
}
function field(label, node, cls) {
  return el("label", { class: "field " + (cls || ""), for: node.id || null }, el("span", {}, label), node);
}

async function viewFacts() {
  const state = { limit: 50, offset: 0 };
  const wrap = el("div", { class: "stack-sm" });
  const initial = hashParams();

  const inputs = {
    q: el("input", { class: "input", type: "search", name: "q", id: "f-q", placeholder: "Search quotes, subjects, values…", autocomplete: "off" }),
    document_id: el("input", { class: "input", name: "document_id", id: "f-doc", type: "number", min: "1", placeholder: "Any", inputmode: "numeric" }),
    lifecycle_state: select("lifecycle_state", LIFECYCLE_OPTS, { ELIGIBLE_FOR_REASONING: "Eligible" }),
    evidence_status: select("evidence_status", EVIDENCE_OPTS),
    modality: select("modality", MODALITY_OPTS),
    type: select("type", TYPE_OPTS),
    reasoning_eligible: select("reasoning_eligible", ["", "true", "false"], { true: "Yes", false: "No" }),
  };
  for (const [k, node] of Object.entries(inputs)) if (initial.get(k)) node.value = initial.get(k);

  const resetBtn = el("button", { class: "btn ghost sm", type: "button", onclick: () => {
    for (const node of Object.values(inputs)) node.value = "";
    apply();
  } }, icon("x"), "Clear filters");
  const summary = el("span", { role: "status" });
  const form = el("form", { class: "panel panel-pad", role: "search", onsubmit: (e) => { e.preventDefault(); apply(); } },
    el("div", { class: "toolbar" },
      el("label", { class: "field grow", for: "f-q" }, el("span", {}, "Search"),
        el("div", { class: "input-icon" }, icon("search"), inputs.q)),
      field("Lifecycle", inputs.lifecycle_state),
      field("Evidence", inputs.evidence_status),
      field("Modality", inputs.modality),
      field("Type", inputs.type),
      field("Eligible", inputs.reasoning_eligible),
      field("Document #", inputs.document_id)),
    el("div", { class: "toolbar-foot" }, summary, resetBtn));
  for (const node of Object.values(inputs)) {
    if (node.tagName === "SELECT") node.addEventListener("change", apply);
  }
  let debounce;
  inputs.q.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(apply, 350); });
  inputs.document_id.addEventListener("change", apply);
  onLeave(() => clearTimeout(debounce));
  wrap.append(form);

  const tbody = el("tbody", {});
  const pager = el("div", { class: "panel-foot" });
  const [tablePanel, table] = panelTable(
    headRow("Fact", ["Source", "hide-sm"], ["Status", "tight"], ["Modality", "tight hide-sm"], ["", "tight"]),
    tbody, pager);
  wrap.append(tablePanel);

  function active() { return Object.values(inputs).some((n) => n.value); }
  function query() {
    const p = new URLSearchParams();
    for (const [k, node] of Object.entries(inputs)) if (node.value) p.set(k, node.value);
    p.set("limit", state.limit); p.set("offset", state.offset);
    return p.toString();
  }
  function apply() { state.offset = 0; load().catch(showError); }
  function showError(e) { clear(tbody); tbody.append(el("tr", {}, el("td", { colspan: "5" }, errorState(e, apply)))); }

  let seq = 0;
  async function load() {
    const mine = ++seq;
    table.setAttribute("aria-busy", "true");
    const page = await api("/facts?" + query()).finally(() => table.removeAttribute("aria-busy"));
    if (mine !== seq) return;  // a newer query already answered
    resetBtn.hidden = !active();
    summary.textContent = active()
      ? `${plural(page.total, "fact")} ${page.total === 1 ? "matches" : "match"} these filters`
      : `${plural(page.total, "fact")} across all documents`;
    clear(tbody);
    if (!page.items.length) {
      tbody.append(active()
        ? emptyRow(5, "No facts match", "Loosen a filter or clear them all.",
          el("button", { class: "btn sm", type: "button", onclick: () => resetBtn.click() }, "Clear filters"), "search")
        : emptyRow(5, "No facts yet", "Facts appear here once a document has been processed.",
          linkBtn("#/documents", "Go to documents", null, "sm"), "facts"));
    }
    for (const f of page.items) tbody.append(...factRows(f));
    clear(pager);
    const shown = page.items.length ? `${state.offset + 1}–${state.offset + page.items.length}` : "0";
    pager.append(
      el("span", { class: "num" }, `${shown} of ${count(page.total)}`),
      el("span", { style: "flex:1" }),
      el("button", { class: "btn sm", type: "button", disabled: state.offset === 0 || null,
        onclick: () => { state.offset = Math.max(0, state.offset - state.limit); load().catch(showError); } }, "Previous"),
      el("button", { class: "btn sm", type: "button", disabled: state.offset + state.limit >= page.total || null,
        onclick: () => { state.offset += state.limit; load().catch(showError); } }, "Next"));
  }

  function factRows(f) {
    const detail = el("tr", { class: "detail", hidden: "hidden" }, el("td", { colspan: "5" }));
    const row = expandableRow([
      el("td", {}, claim(f),
        periodLabel(f.reporting_period)
          ? el("div", { class: "pills", style: "margin-top:6px" }, pill(periodLabel(f.reporting_period), "quiet"))
          : null),
      el("td", { class: "hide-sm" }, source(f)),
      el("td", { class: "tight" }, el("div", { class: "pills", style: "flex-direction:column;align-items:flex-start;gap:4px" },
        tag(f.lifecycle_state, LIFECYCLE), tag(f.evidence_status, EVIDENCE))),
      el("td", { class: "tight hide-sm muted" }, titleCase(f.modality) || "—"),
      el("td", { class: "tight" }, el("span", { class: "chev" }, icon("chevron"))),
    ], (r) => toggleDetail(r, detail, () => api(`/facts/${f.id}`).then(factDetail)));
    row.setAttribute("aria-label", `${factLine(f)} — show evidence`);
    return [row, detail];
  }

  await load();
  return wrap;
}

/** Shared expand/collapse for detail rows: lazy-loads the content once per open. */
async function toggleDetail(row, detailRow, loadContent) {
  const open = detailRow.hidden;
  detailRow.hidden = !open;
  row.setAttribute("aria-expanded", String(open));
  if (!open) return;
  const cell = detailRow.firstChild;
  clear(cell); cell.append(el("div", { class: "inset" }, el("div", {}, el("div", { class: "skeleton", style: "width:70%" }))));
  try { const node = await loadContent(); clear(cell); cell.append(node); }
  catch (e) { clear(cell); cell.append(errorState(e)); }
}

function factDetail(f) {
  const left = el("div", {});
  if (f.evidence && f.evidence.quote) {
    left.append(el("div", {}, el("span", { class: "label" }, "Verbatim evidence"),
      el("blockquote", {}, f.evidence.quote),
      el("p", { class: "meta" },
        `${factDocLabel(f)} · ${pageRef(f.evidence)} · matched ${f.evidence.verification_method || "—"}`)));
  } else {
    left.append(el("div", { class: "callout warn" }, icon("alert"),
      el("div", {}, el("strong", {}, "No verified quote. "), "This fact's quote could not be found on its page, so it is kept out of every comparison.")));
  }
  if (f.context_window) left.append(disclose("Surrounding text on the page", el("div", { class: "ctxwin" }, f.context_window)));
  if (f.relationships && f.relationships.length) {
    const links = el("div", { class: "pills" });
    for (const r of f.relationships) {
      const [label, tone, glyph] = CATEGORY[r.category] || [r.category, "neutral", "•"];
      const text = `${label}${r.context_dimension ? " · " + r.context_dimension : ""} · fact #${r.other_fact_id}`;
      links.append(el("a", { href: "#/relationships?open=" + r.id }, pill(text, tone, glyph, r.category)));
    }
    left.append(el("div", {}, el("span", { class: "label" }, `Related facts (${f.relationships.length})`), links));
  }

  const right = el("div", {},
    el("div", {}, el("span", { class: "label" }, "Claim & context"), kv([
      ["Subject", f.subject_raw],
      ["Entity", f.entity ? el("a", { href: "#/entities" }, `${f.entity.canonical_label} (#${f.entity.id})`) : null],
      ["Attribute", f.predicate],
      ["Value", f.object_raw],
      ["Value text", f.value_text],
      ["Reporting period", periodStr(f.reporting_period)],
      ["Scope", scopeStr(f.scope)],
      ["Qualifiers", f.qualifiers && f.qualifiers.length ? f.qualifiers.join(", ") : null],
      ["Modality", titleCase(f.modality)],
      ["Context complete", f.context_complete ? "Yes" : "No"],
      ["Publisher", f.publisher],
      ["Publication date", f.publication_date],
      ["Data vintage", f.data_vintage],
    ])));
  if (f.numeric) {
    right.append(disclose("Normalized value", kv([
      ["numeric_value", f.numeric.numeric_value, true],
      ["magnitude", f.numeric.magnitude, true],
      ["base_value", f.numeric.base_value, true],
      ["currency", f.numeric.currency, true],
      ["is_percentage", f.numeric.is_percentage, true],
      ["percentage_ratio", f.numeric.percentage_ratio, true],
      ["unit_norm", f.numeric.unit_norm, true],
    ])));
  }
  if (f.evidence) {
    right.append(disclose("Verification", kv([
      ["Method", f.evidence.verification_method],
      ["Numeric re-derivation", f.evidence.numeric_rederivation],
      ["Fuzzy score", f.evidence.fuzzy_score],
      ["Char range", f.evidence.char_start != null ? `${f.evidence.char_start}–${f.evidence.char_end}` : null, true],
      ["Notes", f.evidence.notes],
    ])));
  }
  if (f.repro && f.repro.extraction_model) {
    right.append(disclose("Reproducibility", kv([
      ["Extraction model", f.repro.extraction_model, true],
      ["Prompt version", f.repro.prompt_version, true],
      ["Temperature", f.repro.extraction_temperature, true],
    ])));
  }
  return el("div", { class: "inset" }, left, right);
}

// ---- Relationships -------------------------------------------------------
const CATEGORIES = ["", "CORROBORATES", "CONTRADICTS", "DIFFERENT_CONTEXT", "TEMPORAL_EVOLUTION", "UNCERTAIN"];
const EMPTY_COPY = {
  "": ["No relationships yet", "They appear once two processed documents contribute facts about the same subject."],
  CORROBORATES: ["Nothing corroborated yet", "Two documents stating the same value for the same measure and period would show up here."],
  CONTRADICTS: ["No contradictions", "Facts that still disagree after units, periods and scope are normalized would show up here."],
  DIFFERENT_CONTEXT: ["No context differences", "Pairs that differ only in scope, currency, unit or modality are filed here."],
  TEMPORAL_EVOLUTION: ["No changes over time", "The same measure reported for different periods would show up here."],
  UNCERTAIN: ["Nothing uncertain", "Pairs the rules can't settle — and won't guess about — are filed here."],
};

async function viewRelationships() {
  const params = hashParams();
  const openId = Number(params.get("open")) || null;
  const state = { category: CATEGORIES.includes(params.get("category")) ? params.get("category") : "" };
  const wrap = el("div", {});

  const counts = await Promise.all(CATEGORIES.map((c) =>
    api("/relationships?limit=1" + (c ? "&category=" + c : "")).then((p) => p.total)));

  const tabs = el("div", { class: "tabs", role: "tablist", "aria-label": "Relationship category" });
  const buttons = CATEGORIES.map((c, i) => el("button", {
    class: "tab", type: "button", role: "tab", id: "tab-" + (c || "ALL"), "aria-controls": "rel-panel",
    onclick: () => pick(c),
  }, c ? CATEGORY[c][0] : "All", el("span", { class: "count" }, count(counts[i]))));
  tabs.append(...buttons);
  tabs.addEventListener("keydown", (e) => {
    const i = CATEGORIES.indexOf(state.category);
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null;
    if (next == null) return;
    e.preventDefault();
    const j = (next + CATEGORIES.length) % CATEGORIES.length;
    pick(CATEGORIES[j]); buttons[j].focus();
  });
  function renderTabs() {
    buttons.forEach((b, i) => {
      const on = CATEGORIES[i] === state.category;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
    });
  }
  function pick(c) {
    state.category = c; renderTabs();
    // keep the URL shareable without re-running the router
    history.replaceState(null, "", "#/relationships" + (c ? "?category=" + c : ""));
    load().catch((e) => { clear(list); list.append(errorState(e, () => pick(c))); });
  }
  renderTabs();
  wrap.append(tabs);

  const list = el("div", { class: "rel-list", id: "rel-panel", role: "tabpanel" });
  wrap.append(list);

  async function load() {
    const p = new URLSearchParams({ limit: "200", sort: "confidence" });
    if (state.category) p.set("category", state.category);
    list.setAttribute("aria-labelledby", "tab-" + (state.category || "ALL"));
    list.setAttribute("aria-busy", "true");
    const page = await api("/relationships?" + p.toString()).finally(() => list.removeAttribute("aria-busy"));
    clear(list);
    if (!page.items.length) {
      const [t, b] = EMPTY_COPY[state.category];
      list.append(el("div", { class: "panel" }, emptyState(t, b, null, "relationships")));
      return;
    }
    for (const r of page.items) list.append(relCard(r, r.id === openId));
    const target = openId && list.querySelector(`[data-id="${openId}"]`);
    if (target) target.scrollIntoView({ block: "center" });
  }

  await load();
  return wrap;
}

function relCard(r, openNow) {
  const [label, tone, glyph] = CATEGORY[r.category] || [r.category, "neutral", "•"];
  const body = el("div", { class: "rel-body", hidden: openNow ? null : "hidden", id: "rel-body-" + r.id });
  const toggle = el("button", {
    class: "btn ghost sm", type: "button", "aria-expanded": String(!!openNow), "aria-controls": "rel-body-" + r.id,
  });
  function setToggle(open) {
    const chev = icon("chevron");
    if (open) chev.style.transform = "rotate(180deg)";
    clear(toggle); toggle.append(open ? "Hide reasoning" : "Why this call", chev);
    toggle.setAttribute("aria-expanded", String(open));
  }
  setToggle(!!openNow);
  toggle.addEventListener("click", async () => {
    const open = body.hidden;
    body.hidden = !open; setToggle(open);
    if (open) await fillRel(r.id, body);
  });
  const conf = typeof r.confidence === "number" ? r.confidence : null;
  const card = el("article", {
    class: "panel rel" + (r.category === "CONTRADICTS" ? " contradiction" : ""), "data-id": String(r.id),
    "aria-label": `${label} relationship ${r.id}`,
  },
    el("div", { class: "rel-head" },
      pill(label, tone, glyph, r.category_label || r.category),
      r.context_dimension ? pill(r.context_dimension, "quiet") : null,
      conf != null ? el("span", { class: "conf", title: "Heuristic confidence, not a calibrated probability" },
        el("span", { class: "conf-bar" }, el("span", { style: `width:${Math.round(conf * 100)}%` })),
        "confidence " + fmt(conf)) : null,
      el("span", { class: "spacer" }),
      el("span", { class: "method" }, r.llm_used ? "Model-assisted" : "Rule-based"),
      toggle),
    el("div", { class: "pair" },
      relSide("Fact A", r.fact_a),
      el("div", { class: "link-glyph " + tone, "aria-hidden": "true" }, glyph),
      relSide("Fact B", r.fact_b)),
    body);
  if (openNow) fillRel(r.id, body);
  return card;
}
function relSide(title, f) {
  return el("div", { class: "side" },
    el("div", { class: "who" }, title),
    claim(f),
    el("div", { class: "pills" },
      pill(factDocLabel(f), "neutral"),
      pill(pageRef(f), "quiet"),
      periodLabel(f.reporting_period) ? pill(periodLabel(f.reporting_period), "quiet") : null,
      tag(f.evidence_status, EVIDENCE)),
    f.evidence && f.evidence.quote ? el("blockquote", {}, f.evidence.quote) : null);
}
async function fillRel(id, body) {
  if (body.dataset.loaded) return;
  clear(body); body.append(el("div", { class: "skeleton", style: "width:60%" }));
  try {
    const r = await api(`/relationships/${id}`);
    clear(body);
    const sig = el("tbody", {});
    for (const [k, v] of Object.entries(r.deterministic_signals || {})) {
      sig.append(el("tr", {}, el("td", {}, k.replace(/_/g, " ")), el("td", {}, fmt(v))));
    }
    const changed = r.llm_proposed_category && r.llm_proposed_category !== r.category;
    body.append(
      el("div", {}, el("span", { class: "label" }, "Reasoning"), el("p", { class: "reasoning" }, r.reasoning || "No reasoning recorded.")),
      kv([
        ["Final category", (CATEGORY[r.category] || [r.category])[0]],
        ["Model proposed", r.llm_proposed_category
          ? (CATEGORY[r.llm_proposed_category] || [r.llm_proposed_category])[0] + (changed ? " — overridden by the rules" : "")
          : "Not consulted"],
        ["Validation", titleCase(r.validation_action)],
        ["Validation notes", r.validation_notes],
      ]),
      disclose(`Deterministic signals (${sig.children.length})`, el("div", { class: "table-wrap" }, el("table", { class: "signals" }, sig))),
      disclose("Source context for both facts",
        el("div", { class: "split" }, evidenceBlock("Fact A", r.fact_a), evidenceBlock("Fact B", r.fact_b))));
    body.dataset.loaded = "1";
  } catch (e) { clear(body); body.append(errorState(e, () => fillRel(id, body))); }
}
function evidenceBlock(title, f) {
  return el("div", { class: "side" }, el("div", { class: "who" }, `${title} · ${factDocLabel(f)} · ${pageRef(f)}`),
    f.context_window ? el("div", { class: "ctxwin" }, f.context_window)
      : f.evidence && f.evidence.quote ? el("blockquote", {}, f.evidence.quote)
        : el("p", { class: "muted" }, "No source text recorded."));
}

// ---- Entities ------------------------------------------------------------
async function viewEntities() {
  const page = await api("/entities?limit=200");
  const tbody = el("tbody", {});
  if (!page.items.length) {
    tbody.append(emptyRow(6, "No entities yet",
      "Entities are resolved from fact subjects when a document is processed.", null, "entities"));
  }
  for (const e of page.items) {
    const detail = el("tr", { class: "detail", hidden: "hidden" }, el("td", { colspan: "6" }));
    const row = expandableRow([
      el("td", {}, el("div", { style: "font-weight:500" }, e.canonical_label),
        el("div", { class: "sub mono" }, "#" + e.id)),
      el("td", { class: "tight hide-sm" }, e.entity_type ? pill(e.entity_type, "quiet") : "—"),
      el("td", { class: "r" }, count(e.alias_count)),
      el("td", { class: "r" }, count(e.fact_count)),
      el("td", { class: "tight hide-sm" }, el("div", { class: "pills" },
        el("span", { class: "muted" }, titleCase(e.resolution_method) || "—"),
        e.llm_confirmed ? pill("Model-confirmed", "info") : null)),
      el("td", { class: "tight" }, el("span", { class: "chev" }, icon("chevron"))),
    ], (r) => toggleDetail(r, detail, () => api(`/entities/${e.id}`).then(entityDetail)));
    row.setAttribute("aria-label", `${e.canonical_label} — show aliases and facts`);
    tbody.append(row, detail);
  }
  const [panel] = panelTable(
    headRow("Canonical entity", ["Type", "tight hide-sm"], ["Aliases", "r"], ["Facts", "r"],
      ["Resolution", "tight hide-sm"], ["", "tight"]), tbody);
  return el("div", {}, sectionHead("All entities", el("p", {}, plural(page.total, "entity", "entities"))), panel);
}
function entityDetail(e) {
  const aliases = el("div", { class: "pills" });
  for (const a of e.aliases || []) {
    aliases.append(pill(a.surface + (a.match_method ? " · " + a.match_method : ""), "neutral", null,
      a.source_fact_id ? "from fact #" + a.source_fact_id : null));
  }
  const facts = el("ul", { style: "margin:0;padding-left:18px;display:grid;gap:4px" });
  for (const f of e.sample_facts || []) facts.append(el("li", { class: "muted", style: "font-size:13px" }, factLine(f)));
  return el("div", { class: "inset" },
    el("div", {}, el("span", { class: "label" }, "Aliases merged into this entity"),
      aliases.children.length ? aliases : el("p", { class: "muted" }, "None — every fact used the canonical name.")),
    el("div", {}, el("span", { class: "label" }, "Sample facts"),
      facts.children.length ? facts : el("p", { class: "muted" }, "No facts yet."),
      el("p", { class: "meta" }, el("a", { href: "#/facts?q=" + encodeURIComponent(e.canonical_label) }, "Search all facts for this entity"))));
}

// ---- Failures ------------------------------------------------------------
async function viewFailures() {
  const wrap = el("div", { class: "stack" });
  const all = await api("/failures?limit=200");
  const byType = Object.entries(all.counts_by_type).sort((a, b) => b[1] - a[1]);

  wrap.append(el("dl", { class: "metrics", style: "margin:0" },
    metric("Recorded", all.total, "across every run", { alert: all.total > 0 }),
    ...byType.slice(0, 3).map(([k, v]) => metric(titleCase(k), v, null))));

  const typeSel = el("select", { class: "select", id: "f-failure-type", style: "width:auto;min-width:200px" },
    el("option", { value: "" }, `All types (${count(all.total)})`),
    ...byType.map(([k, v]) => el("option", { value: k }, `${titleCase(k)} (${count(v)})`)));
  const tbody = el("tbody", {});
  const [panel] = panelTable(
    headRow(["Type", "tight"], "Reason", "Linked record", ["Document", "tight hide-sm"], ["Ref", "tight hide-md"]), tbody);

  function render(items) {
    // real failures first; "uncertain" pairs are honest don't-knows, not errors
    items = [...items].sort((a, b) =>
      a.failure_type.includes("uncertain") - b.failure_type.includes("uncertain"));
    clear(tbody);
    if (!items.length) {
      tbody.append(emptyRow(5, "No failures recorded",
        "Every fact was grounded and every pair was settled. Anything that goes wrong in a run will be listed here.", null, "check"));
    }
    for (const it of items) {
      let linked = el("span", { class: "muted" }, "—");
      if (it.fact) {
        linked = claim(it.fact);
      } else if (it.relationship) {
        linked = el("a", { href: "#/relationships?open=" + it.relationship.id, style: "white-space:nowrap" },
          (CATEGORY[it.relationship.category] || [it.relationship.category])[0] + " · relationship #" + it.relationship.id);
      }
      // an unresolved pair is an honest "don't know", not an error — colour it as such
      const soft = it.failure_type.includes("uncertain");
      tbody.append(el("tr", {},
        el("td", { class: "tight" }, pill(titleCase(it.failure_type), soft ? "warn" : "bad", soft ? "?" : "✕", it.failure_type)),
        // bare machine codes ("page_low_text") read as prose; sentences pass through
        el("td", { style: "max-width:28rem" }, /^[a-z_]+$/.test(it.reason) ? titleCase(it.reason) : it.reason),
        el("td", {}, linked),
        el("td", { class: "tight hide-sm" }, it.document_id
          ? el("a", { class: "num-link mono", href: "#/facts?document_id=" + it.document_id }, "#" + it.document_id) : "—"),
        el("td", { class: "tight hide-md mono muted" }, `${it.ref_table || ""}${it.ref_id ? "#" + it.ref_id : ""}`)));
    }
  }
  typeSel.addEventListener("change", async () => {
    try {
      const page = typeSel.value
        ? await api("/failures?limit=200&failure_type=" + encodeURIComponent(typeSel.value)) : all;
      render(page.items);
    } catch (e) { clear(tbody); tbody.append(el("tr", {}, el("td", { colspan: "5" }, errorState(e)))); }
  });
  render(all.items);

  wrap.append(el("section", {},
    el("div", { class: "section-head" }, el("h2", {}, "Failure log"),
      el("label", { class: "sr-only", for: "f-failure-type" }, "Filter by failure type"), typeSel),
    panel));
  return wrap;
}
