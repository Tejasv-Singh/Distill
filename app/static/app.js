/* Distill frontend: a small dependency-free SPA. All scraped text goes in via textContent. */
"use strict";

// ------------------------------------------------------------------ utilities
const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
let timers = [];
let CONFIG = null;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.style.cssText = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const safeHref = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const fmtNum = (n) => (n ?? 0).toLocaleString();
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}
function toast(msg, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : ""), role: "status", text: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3000);
}
async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.body ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") init.body = JSON.stringify(opts.body);
  let r;
  try { r = await fetch(path, init); } catch { throw new Error("Can't reach the server. Is it running?"); }
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json().catch(() => ({})) : await r.text();
  if (!r.ok) {
    let d = body && body.detail;
    if (Array.isArray(d)) d = d.map((x) => (x.msg || "").replace(/^Value error, /, "")).join(" ");
    throw new Error(d || `Request failed (HTTP ${r.status})`);
  }
  return body;
}
function every(ms, fn) { const id = setInterval(fn, ms); timers.push(id); return id; }
function clearTimers() { timers.forEach(clearInterval); timers = []; }
function modal({ title, body, confirm = "Confirm", danger = false, onConfirm }) {
  const close = () => scrim.remove();
  const btn = h("button", { class: "btn " + (danger ? "danger" : "primary"), text: confirm });
  btn.onclick = async () => { btn.disabled = true; try { await onConfirm(); close(); } catch (e) { toast(e.message, true); btn.disabled = false; } };
  const scrim = h("div", { class: "scrim", onclick: (e) => e.target === scrim && close() },
    h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
      h("h3", { text: title }), body,
      h("div", { class: "row", style: "justify-content:flex-end;margin-top:18px" },
        h("button", { class: "btn", text: "Cancel", onclick: close }), btn)));
  document.body.append(scrim);
  setTimeout(() => (scrim.querySelector("input,textarea,select") || btn).focus(), 30);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied to clipboard"); }
  catch { toast("Copy failed. Your browser blocked clipboard access.", true); }
}
const statusPill = (s) => {
  const map = { done: ["ok", "Ready"], running: ["accent", "Running"], queued: ["", "Queued"], failed: ["bad", "Failed"], cancelled: ["warn", "Cancelled"] };
  const [cls, label] = map[s] || ["", s];
  return h("span", { class: "pill " + cls }, h("span", { class: "dot" }), label);
};

// ------------------------------------------------------------------- catalog
const FORMATS = {
  documents: [["pdf", "PDF", "Print-ready document"], ["docx", "DOCX", "Editable Word file"], ["markdown", "Markdown", "Clean .md text"],
              ["txt", "TXT", "Plain text"], ["html", "HTML", "Standalone web page"]],
  spreadsheets: [["csv", "CSV", "One table, any tool"], ["xlsx", "XLSX", "Excel, one sheet per table"]],
  structured: [["json", "JSON", "Full structured document"], ["xml", "XML", "Same data as XML"]],
  presentations: [["pptx", "PPTX", "Slides from the content"]],
};
const AI_OUT = [
  ["detailed_summary", "Detailed summary", "Section-by-section"], ["executive_summary", "Executive summary", "Bottom line first"],
  ["research_report", "Research report", "Findings, data, limits"], ["technical_documentation", "Technical docs", "Concepts, usage, params"],
  ["study_notes", "Study notes", "Concepts + self-test"], ["blog_post", "Blog post", "Readable, attributed"],
  ["newsletter", "Newsletter", "Short story items"], ["comparison_report", "Comparison report", "Side-by-side table"],
  ["key_insights", "Key insights", "Takeaways + evidence"], ["faq", "FAQ", "Questions and answers"],
  ["presentation", "AI presentation", "Slide deck written by AI"], ["custom", "Custom output", "Describe anything"],
];
const LABEL = Object.fromEntries([...Object.values(FORMATS).flat(), ...AI_OUT].map(([k, l]) => [k, l]));

// --------------------------------------------------------------------- router
const routes = [
  [/^#?\/?$/, viewHome], [/^#\/history(?:\?.*)?$/, viewHistory], [/^#\/p\/(\w+)$/, viewProject],
  [/^#\/p\/(\w+)\/new$/, viewConfigure], [/^#\/o\/(\w+)$/, viewOutput],
  [/^#\/chat$/, () => viewChat(null)], [/^#\/chat\/(\w+)$/, viewChat],
];
async function route() {
  clearTimers();
  const hash = location.hash || "#/";
  document.querySelectorAll("[data-nav]").forEach((a) =>
    a.classList.toggle("on", (a.dataset.nav === "home" && (hash === "#/" || hash === "")) || (a.dataset.nav === "history" && hash.startsWith("#/history")) || (a.dataset.nav === "chat" && hash.startsWith("#/chat"))));
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      app.replaceChildren(h("div", { class: "skeleton", style: "height:220px;margin-top:20px" }));
      try { await fn(...m.slice(1)); } catch (e) { app.replaceChildren(errorView(e)); }
      app.focus({ preventScroll: true });
      window.scrollTo(0, 0);
      return;
    }
  }
  app.replaceChildren(h("div", { class: "empty" }, h("h2", { text: "Page not found" }), h("p", {}, h("a", { href: "#/", text: "Go home" }))));
}
function errorView(e) {
  return h("div", { class: "empty fade-in" }, h("h2", { text: "Something went wrong" }), h("p", { class: "muted", text: e.message }),
    h("div", { class: "row", style: "justify-content:center" }, h("button", { class: "btn", text: "Retry", onclick: route }), h("a", { class: "btn", href: "#/", text: "Home" })));
}
window.addEventListener("hashchange", route);

// ----------------------------------------------------------------------- home
async function viewHome() {
  const state = { multi: false, mode: "single", render: "auto", selected: null };
  const urlInput = h("input", { class: "big", type: "url", placeholder: "Paste any website URL to get started...", "aria-label": "Website URL", autocomplete: "off" });
  const multiInput = h("textarea", { class: "hidden", placeholder: "One URL per line (up to 50)", "aria-label": "Website URLs" });
  const err = h("div", { class: "url-error hidden", role: "alert" });
  const go = h("button", { class: "btn primary lg", type: "submit" }, "Scrape");

  const segMode = seg([["single", "Single page"], ["crawl", "Crawl website"]], "single", (v) => { state.mode = v; crawlBox.classList.toggle("hidden", v !== "crawl"); });
  const segRender = seg([["auto", "Auto"], ["static", "Static"], ["js", "JavaScript"]], "auto", (v) => (state.render = v));
  const n = (id, val, min, max, step = 1) => h("input", { type: "number", id, value: val, min, max, step });
  const crawlBox = h("div", { class: "hidden stack", style: "margin-top:14px" },
    h("div", { class: "fields" },
      fld("Max pages", n("c_pages", 10, 1, CONFIG?.limits.max_pages || 100)),
      fld("Max link depth", n("c_depth", 1, 0, CONFIG?.limits.max_depth || 5)),
      fld("Delay between pages (s)", n("c_delay", 1, 0, 30, 0.5)),
      fld("Include URLs matching", h("input", { type: "text", id: "c_inc", placeholder: "/blog/  or  */docs/*  or  re:^.*/p/\\d+" })),
      fld("Exclude URLs matching", h("input", { type: "text", id: "c_exc", placeholder: "/tag/, /login" })),
      fld("“Next page” selector", h("input", { type: "text", id: "c_next", class: "mono", placeholder: "li.next a" }))),
    h("div", { class: "row wrap", style: "gap:18px" },
      chk("c_same", "Stay on the same domain", true), chk("c_sitemap", "Use sitemap.xml", false), chk("c_robots", "Respect robots.txt", true)),
    h("div", { class: "row wrap" }, h("button", { type: "button", class: "btn", text: "Discover pages to choose from…", onclick: discover }),
      h("span", { class: "small muted", id: "pickInfo" })),
    h("div", { id: "pickList" }));
  const precise = h("details", { class: "more", style: "margin-top:12px" },
    h("summary", {}, "Precise data extraction (CSS selectors, optional)"),
    h("div", { class: "stack", style: "margin-top:10px" },
      h("p", { class: "small muted", style: "margin:0", text: "For listing pages: each element matching the item selector becomes one row, with the fields you define. The full page content is still extracted." }),
      fld("Item selector", h("input", { type: "text", id: "d_item", class: "mono", placeholder: "article.product_pod" })),
      h("div", { id: "d_fields", class: "stack" }),
      h("button", { type: "button", class: "btn", text: "+ Add field", onclick: () => addField() })));
  function addField(name = "", sel = "", attr = "text") {
    const row = h("div", { class: "row" },
      h("input", { type: "text", placeholder: "name", value: name, style: "max-width:160px" }),
      h("input", { type: "text", placeholder: "selector, e.g. h3 a", class: "mono", value: sel }),
      h("select", { style: "max-width:150px" }, ...["text", "href", "src", "title", "html"].map((a) => h("option", { value: a, selected: a === attr }, a))),
      h("button", { type: "button", class: "btn ghost", "aria-label": "Remove field", text: "×", onclick: () => row.remove() }));
    $("#d_fields").append(row);
  }
  const opts = h("div", { class: "opts" },
    h("div", { class: "row wrap", style: "gap:18px" },
      h("div", {}, h("div", { class: "eyebrow", style: "margin-bottom:6px", text: "Mode" }), segMode),
      h("div", {}, h("div", { class: "eyebrow", style: "margin-bottom:6px", text: "Rendering" }), segRender),
      h("label", { class: "check", style: "margin-top:20px" }, h("input", { type: "checkbox", id: "multiToggle", onchange: (e) => {
        state.multi = e.target.checked; multiInput.classList.toggle("hidden", !state.multi); urlInput.classList.toggle("hidden", state.multi);
        (state.multi ? multiInput : urlInput).focus(); } }), "Multiple URLs")),
    crawlBox, precise);

  async function discover() {
    const url = urlInput.value.trim();
    if (!url) return showErr("Enter a URL first.");
    $("#pickInfo").replaceChildren(h("span", { class: "spinner" }), " Looking for pages…");
    try {
      const r = await api("/api/discover", { method: "POST", body: { url, use_sitemap: true, limit: 300 } });
      state.selected = new Set();
      const list = h("div", { class: "card flat", style: "max-height:260px;overflow:auto;padding:10px 14px" },
        ...r.pages.map((p) => h("label", { class: "check small", style: "padding:3px 0" },
          h("input", { type: "checkbox", disabled: !p.allowed, onchange: (e) => (e.target.checked ? state.selected.add(p.url) : state.selected.delete(p.url)) }),
          h("span", { class: "trunc", text: p.url }), h("span", { class: "pill", text: p.allowed ? p.source : "robots.txt blocks" }))));
      $("#pickList").replaceChildren(list);
      $("#pickInfo").textContent = `${r.pages.length} pages found${r.sitemap_found ? " (sitemap + links)" : " (links on the page)"}. Tick pages to scrape exactly those; leave all unticked to crawl normally.`;
    } catch (e) { $("#pickInfo").textContent = ""; showErr(e.message); }
  }
  function showErr(m) { err.textContent = m; err.classList.toggle("hidden", !m); }

  const form = h("form", { class: "urlbox", novalidate: true, onsubmit: async (e) => {
    e.preventDefault(); showErr("");
    let urls = state.multi ? multiInput.value.split(/\s+/).filter(Boolean) : [urlInput.value.trim()].filter(Boolean);
    let mode = state.mode;
    if (state.selected && state.selected.size) { urls = [...state.selected]; mode = "single"; }
    if (!urls.length) return showErr("Paste a URL to scrape.");
    const bad = urls.find((u) => !/^(https?:\/\/)?[^\s/$.?#]+\.[^\s]+$|^(https?:\/\/)?localhost(:\d+)?(\/|$)/i.test(u));
    if (bad) return showErr(`“${bad}” doesn't look like a web address.`);
    const list = (id) => $(id).value.split(",").map((s) => s.trim()).filter(Boolean);
    const fields = [...document.querySelectorAll("#d_fields .row")].map((r) => {
      const [nm, sl, at] = r.querySelectorAll("input,select");
      return { name: nm.value.trim(), selector: sl.value.trim(), attr: at.value };
    }).filter((f) => f.name && f.selector);
    const body = {
      urls, mode, render: state.render,
      crawl: { max_pages: +$("#c_pages").value || 10, max_depth: +$("#c_depth").value || 0, delay_seconds: +$("#c_delay").value || 0,
               include_patterns: list("#c_inc"), exclude_patterns: list("#c_exc"), next_page_selector: $("#c_next").value.trim() || null,
               same_domain: $("#c_same").checked, use_sitemap: $("#c_sitemap").checked, respect_robots: $("#c_robots").checked },
      data_extraction: fields.length ? { item_selector: $("#d_item").value.trim() || null, fields } : null,
    };
    go.disabled = true; go.replaceChildren(h("span", { class: "spinner" }), "Starting");
    try {
      const r = await api("/api/projects", { method: "POST", body });
      location.hash = `#/p/${r.project_id}`;
    } catch (e2) { showErr(e2.message); go.disabled = false; go.textContent = "Scrape"; }
  } },
    h("div", { class: "main" }, urlInput, multiInput, go), err, opts);

  const recent = h("div", { class: "projects" }, ...[0, 1, 2].map(() => h("div", { class: "skeleton", style: "height:108px" })));
  app.replaceChildren(h("div", { class: "fade-in" },
    h("section", { class: "hero" },
      h("span", { class: "pill accent", style: "margin-bottom:16px" }, "Scrape once · export everything"),
      h("h1", {}, "Turn any website into anything."),
      h("p", { class: "lead", text: "Paste a URL. Distill extracts the real content (headings, tables, images, links) and turns it into documents, spreadsheets, slides or AI-written reports." }),
      form,
      h("p", { class: "small muted", style: "margin-top:14px" }, "Want specific data? ", h("a", { href: "#/chat", text: "Ask the AI assistant →" }), " e.g. “every product's name and price from this page”.")),
    h("div", { class: "section-title" }, h("h2", { text: "Recent projects" }), h("a", { href: "#/history", class: "small", text: "View all →" })),
    recent,
    h("div", { class: "section-title" }, h("h2", { text: "How it works" })),
    h("div", { class: "steps" },
      h("div", { class: "card flat" }, h("b", { text: "Paste a URL" }), h("p", { class: "small muted", text: "One page, a list of pages, or a whole site with crawl limits." })),
      h("div", { class: "card flat" }, h("b", { text: "We extract the content" }), h("p", { class: "small muted", text: "Menus, ads and cookie banners are removed. Headings, tables and images are kept." })),
      h("div", { class: "card flat" }, h("b", { text: "Choose outputs" }), h("p", { class: "small muted", text: "Pick several formats at once and set the style, length and audience." })),
      h("div", { class: "card flat" }, h("b", { text: "Preview, edit, download" }), h("p", { class: "small muted", text: "Edit the result, rewrite sections, or export the same scrape again in another format." }))),
    h("div", { class: "section-title" }, h("h2", { text: "Supported formats" })),
    h("div", { class: "formats" }, ...[...Object.values(FORMATS).flat(), ["ai", "AI outputs", "Summaries, reports, FAQs, notes and more"]]
      .map(([, l, d]) => h("div", { class: "fmt-chip" }, h("b", { text: l }), h("span", { text: d })))),
    h("div", { class: "section-title" }, h("h2", { text: "What people use it for" })),
    h("div", { class: "grid3" },
      useCase("Research", "Turn a set of articles into a cited research report or study notes."),
      useCase("Pricing & catalogues", "Crawl product listings into a clean spreadsheet, one row per item."),
      useCase("Docs to slides", "Convert documentation pages into a deck or a single PDF handbook."))));
  setTimeout(() => urlInput.focus(), 50);

  try {
    const projects = await api("/api/projects?limit=6");
    recent.replaceChildren(...(projects.length ? projects.map(projectCard) :
      [h("div", { class: "card flat empty", style: "grid-column:1/-1" }, "No projects yet. Paste a URL above to start.")]));
  } catch { recent.replaceChildren(h("div", { class: "muted small", text: "Couldn't load recent projects." })); }
}
function useCase(t, d) { return h("div", { class: "card flat" }, h("b", { text: t }), h("p", { class: "small muted", style: "margin:6px 0 0", text: d })); }
function fld(label, input, hint) { return h("div", {}, h("label", { class: "field" }, label, hint ? h("span", { class: "muted", text: " · " + hint }) : null), input); }
function chk(id, label, checked) { return h("label", { class: "check" }, h("input", { type: "checkbox", id, checked }), label); }
function seg(options, value, onChange) {
  const el = h("div", { class: "seg", role: "radiogroup" });
  for (const [v, l] of options) {
    const b = h("button", { type: "button", class: v === value ? "on" : "", role: "radio", "aria-checked": String(v === value), text: l });
    b.onclick = () => { el.querySelectorAll("button").forEach((x) => { x.classList.remove("on"); x.setAttribute("aria-checked", "false"); }); b.classList.add("on"); b.setAttribute("aria-checked", "true"); onChange(v); };
    el.append(b);
  }
  return el;
}
function projectCard(p) {
  return h("a", { class: "card pcard", href: `#/p/${p.id}` },
    h("div", { class: "row" }, h("div", { class: "t", style: "flex:1", text: p.name }), statusPill(p.status)),
    h("div", { class: "u", text: p.source_url }),
    h("div", { class: "row small muted" }, h("span", { text: `${p.page_count} page${p.page_count === 1 ? "" : "s"}` }),
      p.site_type ? h("span", { class: "pill", text: p.site_type }) : null, h("span", { class: "spacer" }), h("span", { text: ago(p.created_at) })));
}

// ------------------------------------------------------------------ workspace
const STAGES = [["connecting", "Connecting to website"], ["fetching", "Fetching webpage"], ["extracting", "Extracting content"],
                ["structuring", "Structuring information"], ["preparing", "Preparing output"]];

async function viewProject(pid) {
  let p = await api(`/api/projects/${pid}`);
  let tab = "content", pageIdx = 0, docCache = {};
  const render = async () => {
    const running = ["queued", "running"].includes(p.status);
    app.replaceChildren(h("div", { class: "fade-in" }, header(), running ? progressCard() : body()));
    if (!running && p.pages.length && tab === "content") loadDoc();
  };
  function header() {
    const title = h("h1", { class: "title-edit", tabindex: "0", title: "Click to rename", text: p.name });
    title.onclick = () => renameProject(p, (n) => { p.name = n; title.textContent = n; });
    title.onkeydown = (e) => e.key === "Enter" && title.onclick();
    return h("div", { class: "ws-head" },
      h("div", { style: "min-width:0" },
        h("div", { class: "row small muted", style: "margin-bottom:6px" }, h("a", { href: "#/history", text: "Projects" }), "›", statusPill(p.status),
          p.site_type ? h("span", { class: "pill", title: "Detected website type", text: p.site_type }) : null),
        title,
        h("a", { class: "small trunc", style: "display:block;margin-top:4px", href: safeHref(p.source_url), target: "_blank", rel: "noopener noreferrer", text: p.source_url })),
      h("div", { class: "row wrap" },
        p.pages.length ? h("button", { class: "btn", text: "Copy text", onclick: async () => copyText(await api(`/api/projects/${pid}/text`)) }) : null,
        !["queued", "running"].includes(p.status) ? h("button", { class: "btn", text: "Re-scrape", onclick: async () => {
          try { await api(`/api/projects/${pid}/rescrape`, { method: "POST" }); p = await api(`/api/projects/${pid}`); docCache = {}; render(); poll(); }
          catch (e) { toast(e.message, true); } } }) : null,
        h("button", { class: "btn danger", text: "Delete", onclick: () => deleteProject(p) })));
  }
  function progressCard() {
    const j = p.job || { stage: "queued", progress: 0, errors: [] };
    const idx = STAGES.findIndex(([k]) => k === j.stage);
    return h("div", { class: "card", style: "max-width:760px" },
      h("div", { class: "row" }, h("span", { class: "spinner" }), h("h2", { style: "font-size:17px", text: j.stage_label || "Starting…" }),
        h("span", { class: "spacer" }), h("span", { class: "muted small", text: `${Math.round((j.progress || 0) * 100)}%` })),
      h("div", { class: "stages" }, ...STAGES.map(([k, l], i) => h("div", { class: "st" + (i < idx ? " done" : i === idx ? " now" : "") }, h("i"), l))),
      h("div", { class: "progress" }, h("div", { style: `width:${Math.max(3, (j.progress || 0) * 100)}%` })),
      h("p", { class: "small muted mono trunc", style: "margin:12px 0 0;max-width:100%", text: j.detail || "" }),
      p.pages.length ? h("p", { class: "small", text: `${p.pages.length} page(s) extracted so far` }) : null,
      j.errors?.length ? h("div", { class: "alert warn small", style: "margin-top:12px" }, `${j.errors.length} page(s) skipped`,
        h("ul", {}, ...j.errors.slice(-4).map((e) => h("li", {}, h("span", { class: "mono", text: e.url }), " — ", e.error)))) : null,
      h("div", { class: "row", style: "margin-top:16px" }, h("button", { class: "btn", text: "Cancel", onclick: () => api(`/api/projects/${pid}/cancel`, { method: "POST" }).then(() => toast("Cancelling…")) })));
  }
  function body() {
    const j = p.job || {};
    if (!p.pages.length) {
      return h("div", { class: "card", style: "max-width:760px" },
        h("h2", { style: "font-size:18px;margin-bottom:8px", text: p.status === "cancelled" ? "Scrape cancelled" : "Couldn't scrape this website" }),
        h("p", { class: "muted", text: j.error_message || "No pages could be extracted." }),
        j.errors?.length ? h("ul", { class: "small" }, ...j.errors.slice(0, 8).map((e) => h("li", {}, h("span", { class: "mono", text: e.url }), " — ", e.error))) : null,
        h("div", { class: "row", style: "margin-top:14px" },
          h("button", { class: "btn primary", text: "Retry", onclick: async () => { try { await api(`/api/projects/${pid}/rescrape`, { method: "POST" }); p = await api(`/api/projects/${pid}`); render(); poll(); } catch (e) { toast(e.message, true); } } }),
          h("a", { class: "btn", href: "#/", text: "Try another URL" })));
    }
    const t = p.totals;
    const first = p.pages[0];
    const tabs = h("div", { class: "tabs", role: "tablist" }, ...[["content", "Extracted content"], ["pages", `Pages (${p.pages.length})`], ["outputs", `Outputs (${p.outputs.length})`], ["data", "Metadata & links"]]
      .map(([k, l]) => h("button", { role: "tab", class: tab === k ? "on" : "", "aria-selected": String(tab === k), text: l, onclick: () => { tab = k; render(); } })));
    let pane;
    if (tab === "content") pane = h("div", { id: "docPane" }, h("div", { class: "skeleton", style: "height:300px" }));
    else if (tab === "pages") pane = pagesPane();
    else if (tab === "outputs") pane = outputsPane();
    else pane = h("div", { id: "docPane" });
    if (tab === "data") setTimeout(loadMeta, 0);
    return h("div", {},
      j.errors?.length ? h("div", { class: "alert warn small", style: "margin-bottom:16px" }, `${j.errors.length} page(s) were skipped.`,
        h("ul", {}, ...j.errors.slice(0, 5).map((e) => h("li", {}, h("span", { class: "mono", text: e.url }), " — ", e.error)))) : null,
      h("div", { class: "cta" },
        h("div", { style: "flex:1" }, h("h2", { text: "What would you like to turn this content into?" }),
          h("p", { class: "small muted", style: "margin:4px 0 0", text: "Choose one or more formats. Every output is made from this scrape, so the site isn't fetched again." })),
        h("a", { class: "btn primary lg", href: `#/p/${pid}/new`, text: "Choose outputs →" })),
      h("div", { class: "ws-grid" },
        h("div", { class: "stack" },
          h("div", { class: "card", style: "padding:12px" },
            p.has_preview ? h("img", { class: "shot", src: `/api/projects/${pid}/preview.png?t=${Date.parse(p.updated_at)}`, alt: "Screenshot of the website" })
              : h("div", { class: "shot", style: "display:grid;place-items:center;color:var(--faint);font-size:13px", text: "No preview" })),
          h("div", { class: "card" }, h("h3", { text: "Content statistics" }),
            h("div", { class: "stats" }, stat(fmtNum(t.word_count), "words"), stat(p.pages.length, "pages"), stat(Math.max(1, Math.round(t.word_count / 230)), "min read"),
              stat(t.headings, "headings"), stat(t.tables, "tables"), stat(t.images, "images"))),
          h("div", { class: "card" }, h("h3", { text: "Page metadata" }),
            h("dl", { class: "kv" },
              kv("Title", first.title), kv("Type", first.site_type), kv("Author", first.author), kv("Published", first.published_date && String(first.published_date).slice(0, 10)),
              kv("Rendering", first.rendered ? "Browser (JavaScript)" : "Static HTML"), kv("Extractor", first.extractor),
              kv("Scraped", ago(p.job?.completed_at)))),
          first.description ? h("div", { class: "card small muted", text: first.description }) : null),
        h("div", { class: "card", style: "min-width:0" }, tabs, pane)));
  }
  function pagesPane() {
    return h("table", { class: "list" }, h("thead", {}, h("tr", {}, h("th", { text: "Page" }), h("th", { text: "Type" }), h("th", { text: "Words" }), h("th", { text: "Tables" }), h("th"))),
      h("tbody", {}, ...p.pages.map((pg, i) => h("tr", {},
        h("td", {}, h("div", { class: "trunc", style: "font-weight:550", text: pg.title }), h("div", { class: "trunc tiny muted mono", text: pg.url })),
        h("td", {}, h("span", { class: "pill", text: pg.site_type || "—" })), h("td", { text: fmtNum(pg.stats.word_count) }), h("td", { text: pg.stats.tables }),
        h("td", {}, h("button", { class: "btn ghost small", text: "View", onclick: () => { pageIdx = i; tab = "content"; render(); } }))))));
  }
  function outputsPane() {
    if (!p.outputs.length) return h("div", { class: "empty" }, h("p", { text: "No outputs yet." }), h("a", { class: "btn primary", href: `#/p/${pid}/new`, text: "Choose outputs" }));
    return h("div", { class: "out-list" }, ...p.outputs.map(outputRow));
  }
  async function getDoc(i) {
    const pg = p.pages[i];
    if (!docCache[pg.id]) docCache[pg.id] = (await api(`/api/projects/${pid}/pages/${pg.id}`)).document;
    return docCache[pg.id];
  }
  async function loadDoc() {
    const pane = $("#docPane");
    if (!pane) return;
    try {
      const d = await getDoc(pageIdx);
      const picker = p.pages.length > 1 ? h("select", { style: "max-width:420px;margin-bottom:14px", "aria-label": "Page", onchange: (e) => { pageIdx = +e.target.value; loadDoc(); } },
        ...p.pages.map((pg, i) => h("option", { value: i, selected: i === pageIdx, text: `${i + 1}. ${pg.title}` }))) : null;
      pane.replaceChildren(h("div", {}, h("div", { class: "row wrap" }, picker, h("span", { class: "spacer" }),
        h("button", { class: "btn small", text: "Copy page text", onclick: () => copyText(docText(d)) })),
        d.warnings?.length ? h("div", { class: "alert warn small", text: d.warnings.join(" ") }) : null,
        renderDoc(d)));
    } catch (e) { pane.replaceChildren(h("div", { class: "alert bad", text: e.message })); }
  }
  async function loadMeta() {
    const pane = $("#docPane");
    const d = await getDoc(pageIdx);
    pane.replaceChildren(h("div", { class: "stack" },
      h("h3", { text: "Metadata" }),
      h("dl", { class: "kv" }, kv("Language", d.language), kv("Site name", d.site_name), kv("Canonical", d.metadata.canonical_url),
        kv("Open Graph type", d.metadata.og_type), kv("Structured data", (d.metadata.json_ld_types || []).join(", ")),
        kv("Keywords", (d.metadata.keywords || []).join(", ")), kv("Why this type", (d.site_type_signals?.reasons || []).join(", "))),
      h("h3", { text: `Links (${d.links.length})` }),
      d.links.length ? h("div", { class: "dtable" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Text" }), h("th", { text: "URL" }), h("th", { text: "Internal" }))),
        h("tbody", {}, ...d.links.slice(0, 300).map((l) => h("tr", {}, h("td", { text: l.text }), h("td", {}, h("a", { href: safeHref(l.url), target: "_blank", rel: "noopener noreferrer nofollow", class: "mono small", text: l.url })), h("td", { text: l.internal ? "yes" : "no" }))))))
        : h("p", { class: "muted small", text: "No links inside the main content." }),
      d.rows?.length ? h("h3", { text: `Extracted records (${d.rows.length})` }) : null,
      d.rows?.length ? dataTable(Object.keys(d.rows[0]), d.rows.slice(0, 200).map((r) => Object.values(r).map((v) => Array.isArray(v) ? v.join(", ") : v ?? ""))) : null));
  }
  let pollId = null;
  function poll() {
    if (pollId) return;
    pollId = every(1000, async () => {
      try {
        p = await api(`/api/projects/${pid}`);
        if (!["queued", "running"].includes(p.status)) { clearInterval(pollId); pollId = null; toast(p.pages.length ? "Scrape complete" : "Scrape failed", !p.pages.length); }
        render();
      } catch { /* transient */ }
    });
  }
  await render();
  if (["queued", "running"].includes(p.status)) poll();
}
const stat = (v, l) => h("div", { class: "stat" }, h("b", { text: v }), h("span", { text: l }));
const kv = (k, v) => (v ? [h("dt", { text: k }), h("dd", { title: String(v), text: String(v) })] : null);
function docText(d) {
  return d.content.map((b) => b.type === "list" ? b.items.map((x) => "- " + x).join("\n") : b.type === "table" ? d.tables[b.table].rows.map((r) => r.join("\t")).join("\n") : b.text || "").filter(Boolean).join("\n\n");
}
function dataTable(headers, rows) {
  return h("div", { class: "dtable" }, h("table", {}, h("thead", {}, h("tr", {}, ...headers.map((x) => h("th", { text: x })))),
    h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c) => h("td", { text: c })))))));
}
function renderDoc(d) {
  const out = h("article", { class: "doc" });
  out.append(h("h1", { text: d.title }));
  if (d.author || d.published_date) out.append(h("p", { class: "small muted", text: [d.author && `By ${d.author}`, d.published_date && String(d.published_date).slice(0, 10)].filter(Boolean).join(" · ") }));
  let skipped = false;
  for (const b of d.content) {
    if (b.type === "heading") {
      if (!skipped && b.level === 1 && b.text.trim() === d.title.trim()) { skipped = true; continue; }
      out.append(h("h" + Math.min(6, Math.max(2, b.level)), { text: b.text }));
    } else if (b.type === "paragraph") out.append(h("p", { text: b.text }));
    else if (b.type === "list") out.append(h(b.ordered ? "ol" : "ul", {}, ...b.items.map((i) => h("li", { text: i.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\*\*/g, "") }))));
    else if (b.type === "quote") out.append(h("blockquote", { text: b.text }));
    else if (b.type === "code") out.append(h("pre", {}, h("code", { text: b.text })));
    else if (b.type === "table") { const t = d.tables[b.table]; out.append(dataTable(t.headers, t.rows.slice(0, 300))); }
    else if (b.type === "image" && safeHref(b.src)) out.append(h("img", { src: b.src, alt: b.alt || "", loading: "lazy", referrerpolicy: "no-referrer" }));
  }
  if (!d.content.length) out.append(h("p", { class: "muted", text: "No main content blocks were found on this page." }));
  return out;
}
function renameProject(p, done) {
  const input = h("input", { type: "text", value: p.name, maxlength: 200 });
  modal({ title: "Rename project", body: input, confirm: "Save", onConfirm: async () => {
    const r = await api(`/api/projects/${p.id}`, { method: "PATCH", body: { name: input.value } });
    done(r.name); toast("Renamed");
  } });
}
function deleteProject(p, after) {
  modal({ title: "Delete this project?", body: h("p", { class: "muted", text: `“${p.name}”, its scraped pages and all generated files will be permanently deleted.` }),
    confirm: "Delete", danger: true, onConfirm: async () => {
      await api(`/api/projects/${p.id}`, { method: "DELETE" }); toast("Project deleted");
      after ? after() : (location.hash = "#/history");
    } });
}
function outputRow(o) {
  return h("div", { class: "item" },
    h("span", { class: "badge", text: o.file_format === "markdown" ? "md" : o.file_format }),
    h("div", { style: "flex:1;min-width:0" }, h("a", { href: `#/o/${o.id}`, class: "trunc", style: "display:block;font-weight:550", text: o.title || LABEL[o.format] || o.format }),
      h("div", { class: "tiny muted", text: `${o.kind === "ai" ? "AI · " + (LABEL[o.format] || o.format) : o.kind === "extract" ? "AI extraction" : "Conversion"} · ${ago(o.created_at)}` })),
    o.status === "done" ? null : statusPill(o.status),
    o.has_file ? h("a", { class: "btn small", href: `/api/outputs/${o.id}/download`, text: "Download" }) : null);
}

// -------------------------------------------------------------- configure
async function viewConfigure(pid) {
  const p = await api(`/api/projects/${pid}`);
  if (!p.pages.length) { location.hash = `#/p/${pid}`; return; }
  const sel = new Map();   // key -> {kind, format}
  const aiOK = CONFIG?.ai?.configured;
  const panels = h("div", {});
  const genBtn = h("button", { class: "btn primary lg", disabled: true, text: "Generate" });
  const summary = h("span", { class: "muted small", text: "Nothing selected" });

  function choice(kind, key, label, desc) {
    const id = kind + ":" + key;
    const disabled = kind === "ai" && !aiOK;
    const el = h("button", { type: "button", class: "choice" + (disabled ? " disabled" : ""), "aria-pressed": "false", title: disabled ? "Add an AI API key in .env to enable AI outputs" : "" },
      h("b", { text: label }), h("span", { text: desc }));
    el.onclick = () => {
      if (disabled) return toast("AI outputs need an API key: set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env and restart.", true);
      sel.has(id) ? sel.delete(id) : sel.set(id, { kind, format: key });
      el.classList.toggle("on", sel.has(id)); el.setAttribute("aria-pressed", String(sel.has(id)));
      update();
    };
    return el;
  }
  const group = (title, kind, items) => h("div", { class: "fmt-group" }, h("h4", { text: title }), h("div", { class: "choices" }, ...items.map(([k, l, d]) => choice(kind, k, l, d))));

  // customization fields
  const F = {
    title: h("input", { type: "text", placeholder: p.pages[0].title, maxlength: 200 }),
    style: select(["professional", "academic", "casual", "technical"]),
    detail: select(["brief", "standard", "comprehensive"], "standard"),
    images: h("input", { type: "checkbox", checked: true }), tables: h("input", { type: "checkbox", checked: true }),
    sources: h("input", { type: "checkbox", checked: true }), toc: h("input", { type: "checkbox" }),
    sheet: select([["auto", "Automatic (best available)"], ["tables", "Tables on the page"], ["rows", "Precise-mode records"], ["content", "All content blocks"], ["links", "Links"]]),
    columns: h("input", { type: "text", placeholder: "e.g. title, price (blank = all)" }),
    normalize: h("input", { type: "checkbox", checked: true }), combine: h("input", { type: "checkbox", checked: true }),
    slides: h("input", { type: "number", min: 3, max: 40, value: 10 }), theme: select(["light", "dark"]),
    charts: h("input", { type: "checkbox", checked: true }),
    audience: h("input", { type: "text", placeholder: "e.g. product managers, students" }),
    tone: h("input", { type: "text", placeholder: "e.g. neutral, upbeat" }),
    length: h("input", { type: "number", min: 50, max: 8000, placeholder: "auto" }),
    focus: h("input", { type: "text", placeholder: "e.g. pricing, risks, how-to steps" }),
    instructions: h("textarea", { rows: 4, placeholder: "Describe exactly what you want, e.g. “A one-page buyer's guide comparing the three cheapest books, with a table of price and rating.”" }),
    aiFormat: select([["markdown", "Markdown"], ["pdf", "PDF"], ["docx", "DOCX"], ["html", "HTML"], ["txt", "TXT"], ["pptx", "PPTX"]]),
  };
  const pagePick = p.pages.length > 1 ? h("div", { class: "custom-panel" }, h("h4", { text: "Pages to include" }),
    h("div", { class: "stack", style: "max-height:200px;overflow:auto" }, ...p.pages.map((pg) => h("label", { class: "check small" },
      h("input", { type: "checkbox", checked: true, value: pg.id, class: "pagepick" }), h("span", { class: "trunc", text: pg.title }))))) : null;

  function update() {
    const items = [...sel.values()];
    const has = (fn) => items.some(fn);
    const doc = has((i) => i.kind === "convert" && ["pdf", "docx", "markdown", "txt", "html"].includes(i.format));
    const sheet = has((i) => ["csv", "xlsx"].includes(i.format));
    const pptx = has((i) => i.format === "pptx" || i.format === "presentation");
    const ai = has((i) => i.kind === "ai");
    const custom = has((i) => i.format === "custom");
    panels.replaceChildren(...[
      items.length ? h("h2", { style: "font-size:17px;margin:8px 0 12px", text: "Customize" }) : null,
      pagePick,
      doc || ai ? h("div", { class: "custom-panel" }, h("h4", { text: "Document" }),
        h("div", { class: "fields" }, fld("Title", F.title, "optional")),
        h("div", { class: "row wrap", style: "gap:18px;margin-top:12px" }, chkEl(F.images, "Include images"), chkEl(F.tables, "Include tables"),
          chkEl(F.sources, "Include source links"), chkEl(F.toc, "Table of contents"))) : null,
      ai ? h("div", { class: "custom-panel" }, h("h4", { text: "AI writing" }),
        h("div", { class: "fields" }, fld("Writing style", F.style), fld("Detail level", F.detail), fld("Length (words)", F.length, "optional"),
          fld("Audience", F.audience, "optional"), fld("Tone", F.tone, "optional"), fld("Main focus", F.focus, "optional"),
          fld("Deliver AI outputs as", F.aiFormat)),
        h("div", { style: "margin-top:12px" }, fld(custom ? "What should the custom output be? (required)" : "Extra instructions", F.instructions, custom ? null : "optional")),
        h("p", { class: "tiny muted", style: "margin:10px 0 0", text: "AI outputs cite their sources inline, separate facts from interpretation, and are checked for numbers that don't appear in the source." })) : null,
      sheet ? h("div", { class: "custom-panel" }, h("h4", { text: "Spreadsheet" }),
        h("div", { class: "fields" }, fld("What to extract", F.sheet), fld("Columns to include", F.columns, "optional")),
        h("div", { class: "row wrap", style: "gap:18px;margin-top:12px" }, chkEl(F.normalize, "Normalize tables (clean headers, numbers, remove duplicates)"), chkEl(F.combine, "Combine matching tables across pages"))) : null,
      pptx ? h("div", { class: "custom-panel" }, h("h4", { text: "Presentation" }),
        h("div", { class: "fields" }, fld("Number of slides", F.slides), fld("Theme", F.theme)),
        h("div", { class: "row wrap", style: "gap:18px;margin-top:12px" }, chkEl(F.charts, "Add charts for numeric tables"), chkEl(F.images, "Include images"))) : null].filter(Boolean));
    genBtn.disabled = !items.length;
    summary.textContent = items.length ? `${items.length} output${items.length > 1 ? "s" : ""}: ${items.map((i) => LABEL[i.format]).join(", ")}` : "Nothing selected";
  }

  genBtn.onclick = async () => {
    const items = [...sel.values()].map((i) => (i.kind === "ai" ? { ...i, download_format: i.format === "presentation" ? "pptx" : F.aiFormat.value } : i));
    if (items.some((i) => i.format === "custom") && !F.instructions.value.trim()) { F.instructions.focus(); return toast("Describe what you want for the custom output.", true); }
    const page_ids = pagePick ? [...document.querySelectorAll(".pagepick:checked")].map((c) => c.value) : [];
    if (pagePick && !page_ids.length) return toast("Select at least one page.", true);
    const options = {
      title: F.title.value.trim() || null, include_images: F.images.checked, include_tables: F.tables.checked, include_sources: F.sources.checked,
      include_toc: F.toc.checked, sheet_source: F.sheet.value, combine_pages: F.combine.checked, normalize_tables: F.normalize.checked,
      columns: F.columns.value.split(",").map((s) => s.trim()).filter(Boolean), slide_count: +F.slides.value || 10, theme: F.theme.value,
      include_charts: F.charts.checked, style: F.style.value, detail: F.detail.value, length_words: +F.length.value || null,
      audience: F.audience.value.trim() || null, tone: F.tone.value.trim() || null, focus: F.focus.value.trim() || null,
      instructions: F.instructions.value.trim() || null, page_ids,
    };
    genBtn.disabled = true; genBtn.replaceChildren(h("span", { class: "spinner" }), "Starting");
    try {
      const outs = await api(`/api/projects/${pid}/outputs`, { method: "POST", body: { items, options } });
      location.hash = outs.length === 1 ? `#/o/${outs[0].id}` : `#/history?project=${pid}`;
      if (outs.length > 1) sessionStorage.setItem("justGenerated", JSON.stringify(outs.map((o) => o.id)));
    } catch (e) { toast(e.message, true); genBtn.disabled = false; genBtn.textContent = "Generate"; }
  };

  app.replaceChildren(h("div", { class: "fade-in" },
    h("div", { class: "row small muted", style: "margin-bottom:8px" }, h("a", { href: `#/p/${pid}`, text: "← " + p.name })),
    h("h1", { style: "font-size:28px", text: "What would you like to turn this content into?" }),
    h("p", { class: "muted", style: "margin:8px 0 24px", text: `${p.pages.length} page(s) · ${fmtNum(p.totals.word_count)} words · ${p.totals.tables} tables. Select as many outputs as you like.` }),
    !aiOK ? h("div", { class: "alert info small", style: "margin-bottom:16px", text: "AI outputs are turned off because no AI API key is set. Add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env and restart to enable them. All file conversions work without one." }) : null,
    group("Documents", "convert", FORMATS.documents), group("Spreadsheets", "convert", FORMATS.spreadsheets),
    group("Structured data", "convert", FORMATS.structured), group("Presentations", "convert", FORMATS.presentations),
    group("AI-generated", "ai", AI_OUT),
    panels,
    h("div", { class: "sticky-bar" }, summary, h("span", { class: "spacer" }), h("a", { class: "btn", href: `#/p/${pid}`, text: "Cancel" }), genBtn)));
  update();
}
function select(opts, value) {
  return h("select", {}, ...opts.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o[0].toUpperCase() + o.slice(1)]; return h("option", { value: v, selected: v === value, text: l }); }));
}
const chkEl = (input, label) => h("label", { class: "check" }, input, label);

// --------------------------------------------------------------------- output
async function viewOutput(oid) {
  let o = await api(`/api/outputs/${oid}`);
  const project = await api(`/api/projects/${o.project_id}`);
  let tab = "preview", dirty = false, frameKey = Date.now();

  async function refresh() { o = await api(`/api/outputs/${oid}`); render(); }
  function render() {
    const running = ["queued", "running"].includes(o.status);
    const isData = !o.editable;
    const tabs = [["preview", "Preview"], ...(o.editable ? [["edit", "Edit"]] : []), ["compare", "Compare with source"]];
    let main;
    if (running) {
      main = h("div", { class: "card", style: "padding:40px;text-align:center" }, h("span", { class: "spinner", style: "width:22px;height:22px" }),
        h("h2", { style: "font-size:17px;margin-top:14px", text: o.stage || "Working…" }),
        h("p", { class: "muted small", text: o.kind === "ai" ? "AI outputs usually take 20–90 seconds, longer for big sites." : "This usually takes a few seconds." }));
    } else if (o.status === "failed") {
      main = h("div", { class: "card" }, h("h2", { style: "font-size:17px", text: "Generation failed" }), h("p", { class: "muted", text: o.error_message || "Unknown error." }),
        h("button", { class: "btn primary", text: "Try again", onclick: () => regen() }));
    } else if (tab === "preview") {
      main = isData ? dataPreview() : h("div", {},
        o.file_format === "pptx" ? h("p", { class: "small muted", style: "margin:0 0 10px", text: "This preview shows the content. Download the file to see the slide layout. Each ## heading becomes a slide." }) : null,
        h("iframe", { class: "preview-frame", sandbox: "", title: "Document preview", src: `/api/outputs/${oid}/preview.html?k=${frameKey}` }));
    } else if (tab === "edit") {
      const ta = h("textarea", { class: "editor", spellcheck: "true", "aria-label": "Markdown source" });
      ta.value = o.content || "";
      ta.oninput = () => { dirty = true; saveBtn.disabled = false; };
      const saveBtn = h("button", { class: "btn primary", disabled: true, text: "Save & rebuild file" });
      saveBtn.onclick = async () => {
        saveBtn.disabled = true; saveBtn.textContent = "Saving…";
        try { o = await api(`/api/outputs/${oid}`, { method: "PUT", body: { content: ta.value } }); dirty = false; frameKey = Date.now(); toast("Saved. The download now includes your edits."); tab = "preview"; render(); }
        catch (e) { toast(e.message, true); saveBtn.disabled = false; saveBtn.textContent = "Save & rebuild file"; }
      };
      main = h("div", { class: "stack" }, h("div", { class: "row" }, h("span", { class: "small muted", text: "Markdown. Headings, lists, tables and links carry over to every format." }), h("span", { class: "spacer" }), saveBtn), ta);
    } else {
      const left = h("div", {}, h("div", { class: "eyebrow", style: "margin-bottom:8px", text: "Extracted from the website" }), h("div", { class: "skeleton", style: "height:200px" }));
      const right = h("div", {}, h("div", { class: "eyebrow", style: "margin-bottom:8px", text: "Generated" }), h("pre", { text: o.content || "" }));
      main = h("div", { class: "compare" }, left, right);
      Promise.all(project.pages.filter((pg) => !o.configuration.page_ids?.length || o.configuration.page_ids.includes(pg.id)).slice(0, 5)
        .map((pg) => api(`/api/projects/${project.id}/pages/${pg.id}`))).then((docs) =>
        left.replaceChildren(left.firstChild, ...docs.map((d) => renderDoc(d.document)))).catch((e) => left.replaceChildren(h("div", { class: "alert bad", text: e.message })));
    }

    app.replaceChildren(h("div", { class: "fade-in" },
      h("div", { class: "row small muted", style: "margin-bottom:8px" }, h("a", { href: `#/p/${project.id}`, text: "← " + project.name })),
      h("div", { class: "ws-head" },
        h("div", { style: "min-width:0" }, h("div", { class: "row", style: "margin-bottom:6px" }, h("span", { class: "badge", text: o.file_format === "markdown" ? "md" : o.file_format }),
          h("span", { class: "small muted", text: o.kind === "ai" ? `AI · ${LABEL[o.format]}` : o.kind === "extract" ? "AI extraction" : "Direct conversion" }), statusPill(o.status)),
          h("h1", { text: o.title || LABEL[o.format] })),
        h("div", { class: "row wrap" }, o.has_file && !running ? h("a", { class: "btn primary", href: `/api/outputs/${oid}/download`, text: "Download" }) : null)),
      o.warnings?.length && !running ? h("div", { class: "alert warn small", style: "margin-bottom:16px" }, h("b", { text: "Check before using" }), h("ul", {}, ...o.warnings.map((w) => h("li", { text: w })))) : null,
      o.status === "done" && o.error_message ? h("div", { class: "alert bad small", style: "margin-bottom:16px", text: o.error_message }) : null,
      h("div", { class: "out-layout" },
        h("div", { style: "min-width:0" }, running || o.status === "failed" ? null : h("div", { class: "tabs" }, ...tabs.map(([k, l]) => h("button", { class: tab === k ? "on" : "", text: l, onclick: () => {
          if (dirty && !confirm("Discard unsaved edits?")) return; dirty = false; tab = k; render(); } }))), main),
        side(running))));
  }
  function dataPreview() {
    const fmt = o.file_format;
    if (o.kind === "extract") {
      let t = { columns: [], rows: [] };
      try { t = JSON.parse(o.content || "{}"); } catch { /* keep empty */ }
      return h("div", {},
        h("p", { class: "small muted", style: "margin:0 0 10px" }, "Instruction: ", h("i", { text: o.configuration.instruction || "" })),
        h("div", { class: "row wrap", style: "margin-bottom:10px" }, h("span", { class: "small muted", text: `${t.rows.length} rows × ${t.columns.length} columns · download as` }),
          ...["csv", "xlsx", "json", "markdown"].map((f) => h("a", { class: "btn small", href: `/api/outputs/${oid}/download?format=${f}`, text: f.toUpperCase() }))),
        dataTable(t.columns, t.rows.slice(0, 500).map((r) => t.columns.map((c) => cellText(r[c])))),
        t.rows.length > 500 ? h("p", { class: "tiny muted", text: `Showing 500 of ${t.rows.length} rows.` }) : null);
    }
    if (fmt === "csv" || fmt === "xlsx") {
      const sheets = (o.content || "").split(/^## /m).filter(Boolean);
      const parse = (txt) => { const rows = []; let row = [], cur = "", q = false;
        for (let i = 0; i < txt.length; i++) { const c = txt[i];
          if (q) { if (c === '"' && txt[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; }
          else if (c === '"') q = true; else if (c === ",") { row.push(cur); cur = ""; } else if (c === "\n") { row.push(cur.replace(/\r$/, "")); rows.push(row); row = []; cur = ""; } else cur += c; }
        if (cur || row.length) { row.push(cur); rows.push(row); } return rows.filter((r) => r.some((c) => c)); };
      const blocks = fmt === "xlsx" ? sheets.map((s) => { const nl = s.indexOf("\n"); return [s.slice(0, nl), parse(s.slice(nl + 1))]; }) : [["", parse(o.content || "")]];
      return h("div", {}, ...blocks.map(([name, rows]) => h("div", {}, name ? h("h3", { style: "font-size:14px;margin:14px 0 4px", text: `Sheet: ${name}` }) : null,
        rows.length ? dataTable(rows[0], rows.slice(1, 301)) : h("p", { class: "muted", text: "Empty" }),
        rows.length > 301 ? h("p", { class: "tiny muted", text: `Showing 300 of ${rows.length - 1} rows. The download has everything.` }) : null)));
    }
    return h("pre", { class: "code-view", text: o.content || "" });
  }
  function side(running) {
    const docFmts = [["markdown", "Markdown"], ["pdf", "PDF"], ["docx", "DOCX"], ["html", "HTML"], ["txt", "TXT"], ["pptx", "PPTX"]].filter(([f]) => f !== o.file_format);
    const fmtSel = select(docFmts);
    const sections = (o.content || "").split("\n").filter((l) => l.startsWith("## ") && !/^## (Sources|Contents)$/.test(l)).map((l) => l.slice(3).trim());
    const secSel = select(sections.map((s) => [s, s]));
    const instr = h("textarea", { rows: 3, placeholder: "Optional: what should change?" });
    return h("div", { class: "side" },
      o.editable && o.status === "done" ? h("div", { class: "card" }, h("h3", { text: "Export as another format" }),
        h("div", { class: "stack" }, fmtSel, h("button", { class: "btn", text: "Create copy", onclick: async () => {
          try { const n = await api(`/api/outputs/${oid}/render`, { method: "POST", body: { format: fmtSel.value } }); location.hash = `#/o/${n.id}`; } catch (e) { toast(e.message, true); } } }))) : null,
      !running ? h("div", { class: "card" }, h("h3", { text: "Regenerate" }),
        h("div", { class: "stack" },
          o.kind === "ai" && sections.length ? fld("Only this section", secSel, "optional") : null,
          o.kind === "ai" && sections.length ? h("label", { class: "check small" }, h("input", { type: "checkbox", id: "secOnly" }), "Rewrite just this section") : null,
          o.kind === "ai" ? instr : null,
          o.kind === "extract" ? h("textarea", { rows: 3, id: "exInstr", placeholder: "Optional: what should change? e.g. “also include the rating”" }) : null,
          h("button", { class: "btn", text: o.kind === "ai" ? "Regenerate" : o.kind === "extract" ? "Re-extract" : "Rebuild from scrape", onclick: () => regen($("#secOnly")?.checked ? secSel.value : null, (o.kind === "extract" ? $("#exInstr").value : instr.value).trim() || null) }))) : null,
      h("div", { class: "card" }, h("h3", { text: "Details" }),
        h("dl", { class: "kv small" }, kv("Created", ago(o.created_at)), kv("Style", o.kind === "ai" ? o.configuration.style : null), kv("Detail", o.kind === "ai" ? o.configuration.detail : null),
          kv("Pages used", o.configuration.page_ids?.length || project.pages.length), kv("Strategy", o.configuration.ai_stats?.strategy),
          kv("Citation coverage", o.configuration.ai_stats ? Math.round(o.configuration.ai_stats.citation_coverage * 100) + "%" : null)),
        h("div", { class: "row", style: "margin-top:12px" },
          h("a", { class: "btn small", href: `#/p/${project.id}/new`, text: "New output" }),
          h("button", { class: "btn small danger", text: "Delete", onclick: () => modal({ title: "Delete this output?", body: h("p", { class: "muted", text: "The generated file will be removed. The scraped content stays." }),
            confirm: "Delete", danger: true, onConfirm: async () => { await api(`/api/outputs/${oid}`, { method: "DELETE" }); location.hash = `#/p/${project.id}`; } }) }))));
  }
  async function regen(section = null, instructions = null) {
    try { o = await api(`/api/outputs/${oid}/regenerate`, { method: "POST", body: { section, instructions } }); render(); pollIt(); }
    catch (e) { toast(e.message, true); }
  }
  let pid = null;
  function pollIt() {
    if (pid) return;
    pid = every(1200, async () => {
      try { o = await api(`/api/outputs/${oid}`); } catch { return; }
      if (!["queued", "running"].includes(o.status)) { clearInterval(pid); pid = null; frameKey = Date.now(); render(); }
      else render();
    });
  }
  render();
  if (["queued", "running"].includes(o.status)) pollIt();
}

// ---------------------------------------------------------------- assistant
const cellText = (v) => (v == null ? "" : Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v));

/* Safe mini-Markdown for assistant replies: paragraphs, lists, **bold**, *italic*, `code`, links, tables.
   Builds DOM nodes; never uses innerHTML. */
function mdLite(text) {
  const root = h("div", { class: "md" });
  const inline = (s) => {
    const out = [];
    const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\((?:https?:\/\/|\/)[^)\s]+\)|\*[^*\s][^*]*\*)/g;
    let last = 0, m;
    while ((m = re.exec(s))) {
      if (m.index > last) out.push(s.slice(last, m.index));
      const t = m[0];
      if (t.startsWith("**")) out.push(h("strong", { text: t.slice(2, -2) }));
      else if (t.startsWith("`")) out.push(h("code", { text: t.slice(1, -1) }));
      else if (t.startsWith("[")) { const mm = t.match(/^\[([^\]]+)\]\(([^)]+)\)$/); out.push(h("a", { href: mm[2], target: mm[2].startsWith("/") ? null : "_blank", rel: "noopener noreferrer nofollow", text: mm[1] })); }
      else out.push(h("em", { text: t.slice(1, -1) }));
      last = m.index + t.length;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  };
  const lines = (text || "").replace(/\r/g, "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i + 1])) {
      const split = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = split(line); const rows = []; i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(split(lines[i++]));
      root.append(dataTable(head, rows)); continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line); const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) list.append(h("li", {}, ...inline(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ""))));
      root.append(list); continue;
    }
    if (/^#{1,6}\s/.test(line)) { root.append(h("p", {}, h("strong", { text: line.replace(/^#+\s*/, "") }))); i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^\s*([-*]|\d+\.)\s+/.test(lines[i]) && !/^\s*\|/.test(lines[i])) para.push(lines[i++]);
    root.append(h("p", {}, ...inline(para.join(" "))));
  }
  return root;
}

const STEP_LABEL = { scrape_website: "Scraping", get_page_content: "Reading page", extract_data: "Extracting data", export: "Creating file" };

function stepCard(m, running) {
  const d = m.display;
  if (!d) return h("div", { class: "step" }, h("span", { class: "spinner" }), h("span", { text: (STEP_LABEL[m.tool] || m.tool) + "…" }));
  if (d.type === "error") return h("div", { class: "step bad" }, h("span", { class: "dot-ic", text: "!" }), h("span", { text: d.text }));
  if (d.type === "scrape") return h("div", { class: "step" }, h("span", { class: "dot-ic ok", text: "✓" }),
    h("span", {}, `Scraped ${d.pages} page${d.pages === 1 ? "" : "s"} from `, h("b", { text: d.host }), ` · ${fmtNum(d.words)} words` + (d.errors ? ` · ${d.errors} skipped` : "")),
    h("a", { class: "small", href: `#/p/${d.project_id}`, text: "Open project" }));
  if (d.type === "read") return h("div", { class: "step" }, h("span", { class: "dot-ic ok", text: "✓" }), h("span", {}, "Read ", h("i", { text: d.title })));
  if (d.type === "export") return h("div", { class: "result-card" },
    h("div", { class: "row" }, h("span", { class: "badge", text: d.format === "markdown" ? "md" : d.format }), h("b", { class: "trunc", style: "flex:1;max-width:none", text: d.title || "Your file" }),
      h("a", { class: "btn primary small", href: d.download_url, text: "Download" })));
  if (d.type === "extract") {
    const more = d.row_count - d.rows.length;
    return h("div", { class: "result-card" },
      h("div", { class: "row wrap", style: "margin-bottom:8px" }, h("b", { text: `${d.row_count} row${d.row_count === 1 ? "" : "s"} × ${d.columns.length} columns` }),
        h("span", { class: "spacer" }), ...["csv", "xlsx", "json"].map((f) => h("a", { class: "btn small", href: `/api/outputs/${d.output_id}/download?format=${f}`, text: f.toUpperCase() })),
        h("a", { class: "btn small", href: `#/o/${d.output_id}`, text: "Open table" })),
      d.rows.length ? dataTable(d.columns, d.rows.map((r) => d.columns.map((c) => cellText(r[c])))) : null,
      more > 0 ? h("p", { class: "tiny muted", style: "margin:4px 0 0", text: `+ ${more} more rows in the download` }) : null,
      d.warnings?.length ? h("div", { class: "alert warn small", style: "margin-top:8px" }, ...d.warnings.map((w) => h("div", { text: w }))) : null);
  }
  return null;
}

async function viewChat(cid) {
  const aiOK = CONFIG?.ai?.configured;
  let chat = cid ? await api(`/api/chats/${cid}`) : null;
  let chats = await api("/api/chats").catch(() => []);
  const thread = h("div", { class: "thread", "aria-live": "polite" });
  const input = h("textarea", { rows: 2, placeholder: aiOK ? "Paste a URL and say what you want, e.g. “https://books.toscrape.com: every book's title, price and rating from the first 3 pages”" : "Add an AI API key in .env to use the assistant", disabled: !aiOK, "aria-label": "Message" });
  const send = h("button", { class: "btn primary", text: "Send", disabled: !aiOK });
  let pollId = null;

  const examples = [
    ["Product list (demo)", "http://localhost:8765/catalogue/page-1.html: get every book's title, price and availability from all pages"],
    ["Article facts (demo)", "http://localhost:8765/blog/how-we-pick-books.html: extract the scoring criteria with their weights"],
    ["Report data (demo)", "http://localhost:8765/reports/sales-2026.html: monthly units and revenue as a table"],
    ["Real site", "https://books.toscrape.com: title, price and star rating of every book on the first 2 pages"],
  ];

  function draw() {
    const running = chat?.status === "running";
    thread.replaceChildren();
    if (!chat) {
      thread.append(h("div", { class: "chat-empty" },
        h("h1", { text: "What do you want to extract?" }),
        h("p", { class: "muted", text: "Give me a website and describe the data you want in plain English. I'll scrape it, pull out exactly that, and hand you a table you can refine and download." }),
        !aiOK ? h("div", { class: "alert info small", style: "margin:16px auto;max-width:560px;text-align:left", text: "The assistant needs an AI key. Add ANTHROPIC_API_KEY (or OPENAI_API_KEY with AI_PROVIDER=openai) to the .env file in your project folder, then restart Distill." }) : null,
        h("div", { class: "examples" }, ...examples.map(([t, q]) => h("button", { class: "example", disabled: !aiOK, onclick: () => { input.value = q; input.focus(); } },
          h("b", { text: t }), h("span", { text: q }))))));
    } else {
      for (const m of chat.messages) {
        if (m.role === "user") thread.append(h("div", { class: "msg user" }, h("div", { class: "bubble", text: m.content })));
        else if (m.role === "assistant") thread.append(h("div", { class: "msg ai" }, mdLite(m.content)));
        else if (m.role === "error") thread.append(h("div", { class: "msg ai" }, h("div", { class: "alert bad small", text: m.content })));
        else if (m.role === "step") { const c = stepCard(m, running); if (c) thread.append(h("div", { class: "msg ai" }, c)); }
      }
      if (running) thread.append(h("div", { class: "msg ai" }, h("div", { class: "step live" }, h("span", { class: "spinner" }), h("span", { text: chat.status_detail || "Working…" }))));
    }
    input.disabled = !aiOK || running; send.disabled = !aiOK || running;
    requestAnimationFrame(() => thread.lastElementChild?.scrollIntoView({ block: "end" }));
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    send.disabled = true;
    try {
      if (!chat) { chat = await api("/api/chats", { method: "POST", body: { message: text } }); history.replaceState(null, "", `#/chat/${chat.id}`); chats.unshift(chat); drawSide(); }
      else chat = await api(`/api/chats/${chat.id}/messages`, { method: "POST", body: { message: text } });
      input.value = ""; draw(); poll();
    } catch (e) { toast(e.message, true); send.disabled = false; }
  }
  send.onclick = submit;
  input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } };

  function poll() {
    if (pollId) return;
    pollId = every(1000, async () => {
      try { chat = await api(`/api/chats/${chat.id}`); } catch { return; }
      draw();
      if (chat.status !== "running") { clearInterval(pollId); pollId = null; input.focus(); }
    });
  }

  const side = h("aside", { class: "chat-side" });
  function drawSide() {
    side.replaceChildren(h("a", { class: "btn", href: "#/chat", style: "width:100%", text: "+ New chat" }),
      h("div", { class: "eyebrow", style: "margin:16px 0 6px", text: "Recent" }),
      ...(chats.length ? chats.map((c) => h("div", { class: "chat-link" + (chat && c.id === chat.id ? " on" : "") },
        h("a", { href: `#/chat/${c.id}`, class: "trunc", text: c.title }),
        h("button", { class: "icon-x", title: "Delete chat", "aria-label": "Delete chat", text: "×", onclick: async () => {
          await api(`/api/chats/${c.id}`, { method: "DELETE" }); chats = chats.filter((x) => x.id !== c.id);
          if (chat && chat.id === c.id) location.hash = "#/chat"; else drawSide(); } })))
        : [h("p", { class: "tiny muted", text: "No chats yet." })]));
  }
  drawSide();
  app.replaceChildren(h("div", { class: "chat-layout fade-in" }, side,
    h("section", { class: "chat-main" }, thread,
      h("div", { class: "composer" }, input, send),
      h("p", { class: "tiny faint", style: "text-align:center;margin:6px 0 0", text: "The assistant only visits sites you type, never bypasses logins or CAPTCHAs, and flags values it couldn't find on the page." }))));
  draw();
  if (chat?.status === "running") poll();
  setTimeout(() => input.focus(), 50);
}

// -------------------------------------------------------------------- history
async function viewHistory() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const onlyProject = params.get("project");
  const just = JSON.parse(sessionStorage.getItem("justGenerated") || "[]");
  sessionStorage.removeItem("justGenerated");
  let projects = await api("/api/projects?limit=500");
  let outputs = await api("/api/outputs?limit=500");
  let q = "", tab = onlyProject ? "outputs" : "projects";
  const list = h("div");
  const search = h("input", { type: "search", placeholder: "Search projects and files", "aria-label": "Search", oninput: (e) => { q = e.target.value.toLowerCase(); draw(); } });
  function draw() {
    if (tab === "projects") {
      const rows = projects.filter((p) => !q || (p.name + p.source_url).toLowerCase().includes(q));
      list.replaceChildren(rows.length ? h("table", { class: "list" },
        h("thead", {}, h("tr", {}, ...["Project", "Status", "Pages", "Outputs", "Created", ""].map((t) => h("th", { text: t })))),
        h("tbody", {}, ...rows.map((p) => h("tr", {},
          h("td", {}, h("a", { href: `#/p/${p.id}`, class: "trunc", style: "display:block;font-weight:550", text: p.name }), h("div", { class: "trunc tiny muted mono", text: p.source_url })),
          h("td", {}, statusPill(p.status)), h("td", { text: p.page_count }), h("td", { text: p.output_count }), h("td", { class: "small muted", text: ago(p.created_at) }),
          h("td", {}, h("div", { class: "row", style: "justify-content:flex-end;gap:4px" },
            h("button", { class: "btn ghost small", text: "Rename", onclick: () => renameProject(p, (n) => { p.name = n; draw(); }) }),
            h("button", { class: "btn ghost small danger", text: "Delete", onclick: () => deleteProject(p, async () => { projects = await api("/api/projects?limit=500"); outputs = await api("/api/outputs?limit=500"); draw(); }) })))))))
        : h("div", { class: "empty", text: q ? "No matches." : "No projects yet." }));
    } else {
      const rows = outputs.filter((o) => (!onlyProject || o.project_id === onlyProject) && (!q || ((o.title || "") + o.project_name + o.format).toLowerCase().includes(q)));
      list.replaceChildren(rows.length ? h("div", { class: "out-list card" }, ...rows.map((o) => {
        const r = outputRow(o);
        r.querySelector(".tiny").textContent += ` · ${o.project_name}`;
        if (just.includes(o.id)) r.style.background = "var(--accent-soft)";
        return r;
      })) : h("div", { class: "empty", text: q ? "No matches." : "No generated files yet." }));
    }
  }
  app.replaceChildren(h("div", { class: "fade-in" },
    h("div", { class: "row wrap", style: "margin-bottom:18px" }, h("h1", { style: "font-size:26px", text: "History" }), h("span", { class: "spacer" }), h("div", { style: "width:280px" }, search)),
    onlyProject ? h("div", { class: "alert info small", style: "margin-bottom:14px" }, "Showing files for one project. ", h("a", { href: "#/history", text: "Show everything" })) : null,
    h("div", { class: "tabs" }, ...[["projects", `Projects (${projects.length})`], ["outputs", `Generated files (${outputs.length})`]].map(([k, l]) =>
      h("button", { class: tab === k ? "on" : "", text: l, onclick: (e) => { tab = k; e.target.parentNode.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === e.target)); draw(); } }))),
    list));
  draw();
  if (just.length) {
    every(1500, async () => {
      outputs = await api("/api/outputs?limit=500");
      draw();
      if (!outputs.some((o) => just.includes(o.id) && ["queued", "running"].includes(o.status))) clearTimers();
    });
  }
}

// ----------------------------------------------------------------------- boot
(function theme() {
  const saved = (() => { try { return localStorage.getItem("theme"); } catch { return null; } })();
  const dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#themeBtn").onclick = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* storage unavailable */ }
  };
})();
(async function boot() {
  try {
    CONFIG = await api("/api/config");
    const pill = $("#aiPill");
    pill.className = "pill " + (CONFIG.ai.configured ? "ok" : "");
    pill.replaceChildren(h("span", { class: "dot" }), CONFIG.ai.configured ? `AI: ${CONFIG.ai.provider}` : "AI off");
    pill.title = CONFIG.ai.configured ? `Model: ${CONFIG.ai.model}` : "Add an API key in .env to enable AI outputs";
  } catch { /* shown per-view */ }
  route();
})();
