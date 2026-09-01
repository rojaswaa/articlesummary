/**
 * search.js — Frontend logic for the Article Search page.
 *
 * Runs a multi-source search, polls progress, renders harmonized +
 * criteria-evaluated results, and manages search history.
 */
(function () {
  "use strict";

  function getCsrf() {
    const c = document.cookie.match(/csrftoken=([^;]+)/);
    return c ? c[1] : "";
  }

  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
    );

  const LABELS = {
    crossref: "Crossref", openalex: "OpenAlex", semantic_scholar: "Semantic Scholar",
    europepmc: "Europe PMC", arxiv: "arXiv", core: "CORE", springer: "Springer",
  };

  let currentId = null;
  let pollTimer = null;
  let alignedOnly = false;
  let sourceFilter = "";
  const PAGE_SIZE = 50;
  let page = 1;
  let numPages = 1;

  /* ── Run a search ── */
  async function runSearch() {
    const query = $("query").value.trim();
    const criteria = $("criteria").value.trim();
    const name = $("name").value.trim();
    const yearFrom = $("year-from").value.trim();
    const yearTo = $("year-to").value.trim();
    const filters = {
      scope: $("f-scope").checked ? "title_abstract" : "all",
      journal_only: $("f-journal").checked,
      has_abstract: $("f-abstract").checked,
      full_text: $("f-fulltext").checked,
    };
    const sources = [...document.querySelectorAll(".source-cb:checked")].map((c) => c.value);

    if (!query || !criteria) { alert("Query and criteria are both required."); return; }
    if (!sources.length) { alert("Select at least one source."); return; }
    if (yearFrom && yearTo && +yearFrom > +yearTo) { alert("Year From must be ≤ Year To."); return; }

    setRunning(true);
    try {
      const resp = await fetch("/api/search/start", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
        body: JSON.stringify({ query, criteria, name, sources, year_from: yearFrom, year_to: yearTo, filters }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "Search failed to start");
      currentId = data.search_id;
      page = 1;
      $("empty").style.display = "none";
      $("results").classList.remove("visible");
      $("results-body").innerHTML = "";
      watch(currentId);
    } catch (e) {
      alert(e.message);
      setRunning(false);
    }
  }

  /* ── Watch progress by polling ──
   * Plain 2s polling of the status endpoint. Simpler and far more reliable than
   * SSE on Django's dev server, which buffers/stalls streaming responses under
   * concurrent background work. Runs continuously while a search is open (so
   * live counts always update); stops only on a terminal state or when another
   * search is opened. */
  function watch(id) {
    $("status-bar").classList.add("visible");
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

    const tick = async () => {
      if (id !== currentId) { clearInterval(pollTimer); pollTimer = null; return; }
      let st;
      try {
        const resp = await fetch(`/api/search/status/${id}`);
        if (!resp.ok) return;
        st = await resp.json();
      } catch (_) { return; }  // transient — try again next tick

      applyState(st, id);
      if (st.total > 0) loadResults(id);

      if (["done", "error", "cancelled"].includes(st.status)) {
        clearInterval(pollTimer); pollTimer = null;
        if (st.status === "error") alert("Search error: " + (st.error || "unknown"));
        loadResults(id);
        loadHistory();
      }
    };

    tick();  // immediate first read
    pollTimer = setInterval(tick, 2000);
  }

  /* Apply a status tick to counts, phase text, providers, log, and controls. */
  function applyState(st, id) {
    $("status-bar").classList.add("visible");
    $("count-fetched").textContent = st.fetched || 0;
    $("count-total").textContent = st.unique || st.total || 0;
    $("count-eval").textContent = st.evaluated;
    $("count-aligned").textContent = st.aligned;
    const phase = st.phase || st.status;
    // Spinner only while there's active background work.
    $("status-spin").style.display = (phase === "searching" || phase === "evaluating") ? "" : "none";
    $("status-text").textContent =
      phase === "searching" ? "Searching sources…"
      : phase === "evaluating" ? "Evaluating articles against criteria…"
      : phase === "searched" ? "Search complete — ready to evaluate."
      : phase === "paused" ? "Evaluation paused."
      : phase;
    renderProviders(st.providers || {});
    renderLog(st.log || []);
    // The search buttons (Run/Stop) only reflect the fetch phase.
    setRunning(phase === "searching");
    updateControls(st);
  }

  /* Show Evaluate / Resume / Pause depending on phase + whether a worker is live. */
  function updateControls(st) {
    const phase = st.phase || st.status;
    const total = st.total || 0;
    const pending = Math.max(total - (st.evaluated || 0), 0);
    const controls = $("eval-controls");
    const evalBtn = $("eval-btn"), pauseBtn = $("pause-btn");
    const restartBtn = $("restart-btn"), hint = $("eval-hint");

    // Pause only when a worker is genuinely running; an 'evaluating' status with
    // no live heartbeat is a dead run → treat it as pausable/resumable.
    if (phase === "evaluating" && st.live) {
      controls.style.display = "flex";
      evalBtn.style.display = "none";
      restartBtn.style.display = "none";
      pauseBtn.style.display = "";
      // Leave a "Pausing…" label intact until the phase actually flips to paused.
      if (!pauseBtn.dataset.pausing) pauseBtn.textContent = "⏸ Pause";
      hint.textContent = `${pending} remaining`;
    } else if (phase !== "searching") {
      controls.style.display = "flex";
      pauseBtn.style.display = "none";
      // Evaluate/Resume only while there's something pending.
      evalBtn.style.display = pending > 0 ? "" : "none";
      evalBtn.textContent = st.evaluated > 0 ? "▶ Resume evaluation" : "▶ Evaluate";
      // Restart is available once any article exists (re-evaluates everything).
      restartBtn.style.display = total > 0 ? "" : "none";
      hint.textContent = pending > 0
        ? `${pending} article${pending === 1 ? "" : "s"} to evaluate`
        : `${total} evaluated`;
    } else {
      controls.style.display = "none";
    }
  }

  /* ── Fetch + render one page of results ── */
  async function loadResults(id, restore) {
    try {
      const params = new URLSearchParams({
        page, page_size: PAGE_SIZE, aligned_only: alignedOnly ? "1" : "0",
      });
      if (sourceFilter) params.set("source", sourceFilter);
      const resp = await fetch(`/api/search/result/${id}?${params}`);
      if (!resp.ok) return;
      const data = await resp.json();
      numPages = data.num_pages || 1;
      page = data.page || 1;
      if (restore) restoreForm(data);
      render(data.articles || [], data.total || 0);
    } catch (_) { /* transient during evaluation */ }
  }

  /* Repopulate the search form from a saved search (only when opening one). */
  function restoreForm(data) {
    $("query").value = data.query || "";
    $("criteria").value = data.criteria || "";
    $("name").value = data.name || "";
    $("year-from").value = data.year_from || "";
    $("year-to").value = data.year_to || "";
    const f = data.filters || {};
    $("f-scope").checked = f.scope === "title_abstract";
    $("f-journal").checked = !!f.journal_only;
    $("f-abstract").checked = !!f.has_abstract;
    $("f-fulltext").checked = !!f.full_text;
    const srcs = new Set(data.sources || []);
    document.querySelectorAll(".source-cb").forEach((cb) => {
      if (!cb.disabled) cb.checked = srcs.has(cb.value);
    });
    // Populate the results Source filter with the providers this search queried.
    const sel = $("source-filter");
    sel.innerHTML = `<option value="">All</option>`
      + (data.sources || []).map((s) => `<option value="${esc(s)}">${esc(LABELS[s] || s)}</option>`).join("");
    sel.value = sourceFilter;
  }

  function render(rows, total) {
    $("results").classList.add("visible");
    $("results-count").textContent = `${total} article${total === 1 ? "" : "s"}`;

    $("pager").style.display = numPages > 1 ? "flex" : "none";
    $("page-info").textContent = `Page ${page} of ${numPages}`;
    $("prev-page").disabled = page <= 1;
    $("next-page").disabled = page >= numPages;

    $("results-body").innerHTML = rows.map((a) => {
      let badge;
      if (a.status === "error") badge = `<span class="badge error">error</span>`;
      else if (a.status !== "done") badge = `<span class="badge pending">…</span>`;
      else badge = a.aligns
        ? `<span class="badge aligns">✓ aligns</span>`
        : `<span class="badge no">no</span>`;

      const sources = (a.sources || []).map((s) => `<span class="src-tag">${esc(s)}</span>`).join("");
      const doi = a.doi
        ? `<a class="srch-doi-link" href="https://doi.org/${esc(a.doi)}" target="_blank" rel="noopener">${esc(a.doi)}</a>`
        : (a.url ? `<a class="srch-doi-link" href="${esc(a.url)}" target="_blank" rel="noopener">link</a>` : "—");
      const title = a.url && !a.doi
        ? esc(a.title)
        : `<a class="srch-doi-link" style="color:#e2e8f0;font-weight:600;" href="${a.doi ? "https://doi.org/" + esc(a.doi) : esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>`;
      const reason = a.alignment_reason
        ? `<div class="srch-reason cell-clip" title="Click to expand">${esc(a.alignment_reason)}</div>`
        : "";

      return `<tr>
        <td>${badge}</td>
        <td class="srch-title-cell"><div class="cell-clip">${title}</div>${reason}</td>
        <td class="srch-authors-cell"><div class="cell-clip">${esc(a.authors) || "—"}</div></td>
        <td>${esc(a.year) || "—"}</td>
        <td>${sources || "—"}</td>
        <td style="max-width:140px;word-break:break-all;">${doi}</td>
      </tr>`;
    }).join("");
  }

  /* ── Per-provider progress + run log ── */
  function renderProviders(providers) {
    const keys = Object.keys(providers);
    const panel = $("provider-progress");
    if (!keys.length) { panel.style.display = "none"; return; }
    panel.style.display = "flex";
    panel.innerHTML = keys.map((k) => {
      const p = providers[k];
      const mark = p.done ? `<span class="dot"></span>` : `<span class="spin"></span>`;
      return `<div class="prov-chip ${p.done ? "done" : ""}">${mark} ${esc(LABELS[k] || k)} <b>${p.count}</b></div>`;
    }).join("");
  }

  function renderLog(lines) {
    if (!lines.length) return;
    $("debug-panel").style.display = "block";
    const pre = $("debug-log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 10;
    pre.textContent = lines.join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  function setRunning(running) {
    $("run-btn").disabled = running;
    $("run-btn").textContent = running ? "Running…" : "🔎 Run Search";
    $("stop-btn").style.display = running ? "" : "none";
  }

  /* ── Stop ── */
  async function stopSearch() {
    if (!currentId) return;
    await fetch(`/api/search/stop/${currentId}`, {
      method: "POST", headers: { "X-CSRFToken": getCsrf() },
    });
  }

  /* ── Evaluate / Resume ── */
  async function startEvaluation() {
    if (!currentId) return;
    const pb = $("pause-btn");
    delete pb.dataset.pausing;
    pb.disabled = false;
    pb.textContent = "⏸ Pause";
    $("eval-btn").disabled = true;
    try {
      const resp = await fetch(`/api/search/evaluate/${currentId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
        body: JSON.stringify({ criteria: $("criteria").value.trim(), source: sourceFilter }),
      });
      if (!resp.ok) { alert((await resp.json()).error || "Could not start evaluation."); return; }
      watch(currentId);
    } finally {
      $("eval-btn").disabled = false;
    }
  }

  /* ── Push aligned articles to Zotero ── */
  let zoteroLibraries = [];

  async function openZoteroPanel() {
    if (!currentId) return;
    const panel = $("zotero-panel");
    if (panel.style.display !== "none") { panel.style.display = "none"; return; }
    panel.style.display = "flex";
    $("zotero-status").textContent = "Loading libraries…";
    try {
      const data = await (await fetch("/api/zotero-collections")).json();
      zoteroLibraries = data.libraries || [];
      if (data.error || !zoteroLibraries.length) {
        $("zotero-status").textContent = data.error || "No Zotero libraries — check Settings.";
        return;
      }
      $("zotero-library").innerHTML = zoteroLibraries
        .map((l, i) => `<option value="${i}">${esc(l.name)}</option>`).join("");
      populateZoteroCollections();
      $("zotero-status").textContent = "";
    } catch (e) {
      $("zotero-status").textContent = "Failed to load libraries: " + e.message;
    }
  }

  function populateZoteroCollections() {
    const lib = zoteroLibraries[+$("zotero-library").value] || { collections: [] };
    $("zotero-collection").innerHTML = `<option value="">(top level)</option>`
      + (lib.collections || []).map((col) => `<option value="${esc(col.id)}">${esc(col.name)}</option>`).join("");
  }

  async function confirmZoteroSave() {
    const lib = zoteroLibraries[+$("zotero-library").value];
    if (!lib) return;
    const btn = $("zotero-confirm");
    const searchId = currentId;
    btn.disabled = true;
    $("zotero-status").textContent = "Starting…";
    try {
      const resp = await fetch(`/api/search/zotero-save/${searchId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
        body: JSON.stringify({
          library_id: lib.id, library_type: lib.type,
          parent_collection: $("zotero-collection").value,
          enrich: $("zotero-enrich").checked,
          fetch_pdf: $("zotero-pdf").checked,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) { $("zotero-status").textContent = data.error || "Save failed."; return; }
      pollZoteroStatus(searchId);
    } catch (e) {
      $("zotero-status").textContent = "Save failed: " + e.message;
    } finally {
      btn.disabled = false;
    }
  }

  function pollZoteroStatus(searchId) {
    const timer = setInterval(async () => {
      let p;
      try { p = await (await fetch(`/api/search/zotero-status/${searchId}`)).json(); }
      catch (_) { return; }
      if (p.idle) { clearInterval(timer); return; }
      $("zotero-status").textContent =
        `Saved ${p.saved}/${p.total}` + (p.pdfs ? ` · ${p.pdfs} PDFs` : "")
        + (p.pdf_failed ? ` · ${p.pdf_failed} PDF fails` : "")
        + (p.skipped ? ` · ${p.skipped} skipped` : "") + (p.done ? " · done" : "…");
      if (p.done) {
        clearInterval(timer);
        if (p.error) { $("zotero-status").textContent = "Error: " + p.error; return; }
        setTimeout(() => { $("zotero-panel").style.display = "none"; }, 4000);
      }
    }, 2000);
  }

  /* ── Restart evaluation from scratch ── */
  async function restartEvaluation() {
    if (!currentId) return;
    const scope = sourceFilter ? ` (${LABELS[sourceFilter] || sourceFilter} only)` : "";
    if (!confirm(`Re-evaluate all articles${scope} from the beginning? This discards the current verdicts.`)) return;
    $("restart-btn").disabled = true;
    try {
      const resp = await fetch(`/api/search/restart-eval/${currentId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrf() },
        body: JSON.stringify({ criteria: $("criteria").value.trim(), source: sourceFilter }),
      });
      if (!resp.ok) { alert((await resp.json()).error || "Could not restart evaluation."); return; }
      watch(currentId);
    } finally {
      $("restart-btn").disabled = false;
    }
  }

  /* ── Pause ── */
  async function pauseEvaluation() {
    if (!currentId) return;
    // Pause takes effect between chunks, so show that it's pending until the
    // status stream reports 'paused' (updateControls then swaps to Resume).
    const btn = $("pause-btn");
    btn.dataset.pausing = "1";
    btn.disabled = true;
    btn.textContent = "⏸ Pausing…";
    try {
      await fetch(`/api/search/pause/${currentId}`, {
        method: "POST", headers: { "X-CSRFToken": getCsrf() },
      });
    } catch (_) {
      btn.disabled = false;
      btn.textContent = "⏸ Pause";
      delete btn.dataset.pausing;
    }
  }

  /* ── History ── */
  async function loadHistory() {
    try {
      const resp = await fetch("/api/searches");
      const list = await resp.json();
      $("history-list").innerHTML = list.map((s) => `
        <li class="srch-history-item ${s.id === currentId ? "active" : ""}" data-id="${s.id}">
          <button class="srch-history-delete" data-id="${s.id}" title="Delete">✕</button>
          <span class="srch-history-date">${new Date(s.created_at).toLocaleString()}</span>
          <div class="srch-history-name">${esc(s.name || s.query).slice(0, 40)}</div>
          <div class="srch-history-count">${s.article_count} articles · ${esc(s.status)}</div>
        </li>`).join("");
    } catch (_) { /* ignore */ }
  }

  async function openSearch(id) {
    currentId = id;
    page = 1;
    $("empty").style.display = "none";
    await loadResults(id, true);  // restore form fields from the saved search
    // Always poll while a search is open — this reflects any live background
    // evaluation (counts + Pause button) and stops itself on a terminal state.
    watch(id);
    loadHistory();
  }

  async function deleteSearch(id) {
    if (!confirm("Delete this search?")) return;
    await fetch(`/api/searches/${id}/delete`, {
      method: "POST", headers: { "X-CSRFToken": getCsrf() },
    });
    if (id === currentId) { currentId = null; $("results").classList.remove("visible"); $("empty").style.display = "flex"; }
    loadHistory();
  }

  /* ── Wire up ── */
  $("run-btn").addEventListener("click", runSearch);
  $("stop-btn").addEventListener("click", stopSearch);
  $("eval-btn").addEventListener("click", startEvaluation);
  $("pause-btn").addEventListener("click", pauseEvaluation);
  $("restart-btn").addEventListener("click", restartEvaluation);
  $("export-btn").addEventListener("click", () => {
    if (currentId) window.location = `/api/search/export/${currentId}`;
  });
  $("zotero-btn").addEventListener("click", openZoteroPanel);
  $("zotero-library").addEventListener("change", populateZoteroCollections);
  $("zotero-confirm").addEventListener("click", confirmZoteroSave);
  $("zotero-cancel").addEventListener("click", () => { $("zotero-panel").style.display = "none"; });
  $("aligned-only").addEventListener("change", (e) => {
    alignedOnly = e.target.checked;
    page = 1;
    if (currentId) loadResults(currentId);
  });
  $("source-filter").addEventListener("change", (e) => {
    sourceFilter = e.target.value;
    page = 1;
    if (currentId) loadResults(currentId);
  });
  $("toggle-log").addEventListener("click", () => {
    const pre = $("debug-log");
    const show = pre.style.display === "none";
    pre.style.display = show ? "block" : "none";
    $("toggle-log").textContent = show ? "Hide" : "Show";
    if (show) pre.scrollTop = pre.scrollHeight;
  });
  $("prev-page").addEventListener("click", () => {
    if (page > 1) { page--; loadResults(currentId); }
  });
  $("next-page").addEventListener("click", () => {
    if (page < numPages) { page++; loadResults(currentId); }
  });
  $("results-body").addEventListener("click", (e) => {
    const r = e.target.closest(".srch-reason");
    if (r) r.classList.toggle("expanded");
  });
  $("history-list").addEventListener("click", (e) => {
    const del = e.target.closest(".srch-history-delete");
    if (del) { e.stopPropagation(); deleteSearch(del.dataset.id); return; }
    const item = e.target.closest(".srch-history-item");
    if (item) openSearch(item.dataset.id);
  });
})();
