// ── State ──
let currentFolder  = null;
let allPdfs        = [];        // ordered flat list of selected PDF paths (one per article group)
let articleGroups  = [];        // [{parent_key, title, author_display, year, versions:[{path,date_modified,selected}]}]
let zoteroMetadata = {};        // rel_path → {title, author_sort, author_display, year, date_modified, parent_key}
let sessionId      = null;
let pollTimer      = null;      // interval fallback when SSE is unavailable
let statusES       = null;      // EventSource for live job statuses
let activeFilter   = "all";
let zoteroLinks    = {};

// ── CSRF ──
function getCookie(name) {
  const row = document.cookie.split("; ").find(r => r.startsWith(name + "="));
  return row ? decodeURIComponent(row.split("=").slice(1).join("=")) : "";
}
function csrfHeaders(extra = {}) {
  return { "X-CSRFToken": getCookie("csrftoken"), ...extra };
}

// ── Elements ──
const zoteroCollectionSelect = document.getElementById("zotero-collection-select");
const folderPathEl   = document.getElementById("folder-path");
const criteriaInput  = document.getElementById("criteria-input");
const sessionNameInput = document.getElementById("session-name-input");
const pdfSection     = document.getElementById("pdf-section");
const pdfList        = document.getElementById("pdf-list");
const pdfCount       = document.getElementById("pdf-count");
const analyzeBtn     = document.getElementById("analyze-btn");
const stopBtn        = document.getElementById("stop-btn");
const filterTabs     = document.getElementById("filter-tabs");
const fcAll          = document.getElementById("fc-all");
const fcAligned      = document.getElementById("fc-aligned");
const fcExcluded     = document.getElementById("fc-excluded");
const exportCsvBtn   = document.getElementById("export-csv-btn");
const zoteroSaveBtn  = document.getElementById("zotero-save-btn");
const retryErrorsBtn = document.getElementById("retry-errors-btn");
const fcError        = document.getElementById("fc-error");
const historyList    = document.getElementById("history-list");
const newAnalysisBtn = document.getElementById("new-analysis-btn");

// ── History Management ──

async function loadHistory() {
  if (!historyList) return;
  try {
    const res = await fetch("/api/sessions");
    const sessions = await res.json();
    renderHistory(sessions);
    
    // Auto-load most recent session if nothing is active
    if (!sessionId && sessions.length > 0) {
        loadSession(sessions[0].id);
    }
  } catch (err) {
    console.error("Error loading history:", err);
  }
}

function renderHistory(sessions) {
  historyList.innerHTML = "";
  sessions.forEach(s => {
    const li = document.createElement("li");
    li.className = "history-item";
    if (sessionId === s.id) li.classList.add("active");
    li.dataset.id = s.id;
    
    const date = new Date(s.created_at).toLocaleString();
    const folderName = s.folder.split('/').filter(Boolean).pop() || s.folder;
    
    li.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: flex-start;">
         <span class="history-date">${date}</span>
         <button class="delete-session-btn" data-id="${s.id}" style="background: none; border: none; color: #64748b; cursor: pointer; font-size: 14px; padding: 0; line-height: 1;" title="Delete session">✕</button>
      </div>
      ${s.name ? `<div class="history-folder" style="color: #6366f1; margin-bottom: 2px;">${_escHtml(s.name)}</div>` : ''}
      <div class="history-folder" title="${_escHtml(s.folder)}">${_escHtml(folderName)}</div>
      <div class="history-meta">
        <span>${s.job_count} PDFs</span>
        <span>${s.done_count} analyzed</span>
      </div>
    `;
    
    // Add click handler for the whole item (load session)
    li.addEventListener("click", (e) => {
        if (!e.target.classList.contains("delete-session-btn")) {
            loadSession(s.id);
        }
    });

    // Add click handler for delete button
    const deleteBtn = li.querySelector(".delete-session-btn");
    if (deleteBtn) {
        deleteBtn.addEventListener("click", async (e) => {
            e.stopPropagation(); // prevent loading session
            if (confirm("Are you sure you want to delete this session?")) {
                try {
                    await fetch(`/api/sessions/${s.id}/delete`, { method: "DELETE", headers: csrfHeaders() });
                    if (sessionId === s.id) {
                        newAnalysisBtn.click(); // clear current view if deleting active session
                    }
                    loadHistory();
                } catch (err) {
                    console.error("Error deleting session:", err);
                }
            }
        });
    }

    historyList.appendChild(li);
  });
}

async function loadSession(sid) {
  sessionId = sid;
  
  // Highlight active item
  document.querySelectorAll('.history-item').forEach(i => {
    i.classList.toggle('active', i.dataset.id === sid);
  });

  try {
    // 1. Get session details (criteria, folder)
    const res = await fetch(`/api/sessions/${sid}`);
    const details = await res.json();
    
    currentFolder  = details.folder;
    criteriaInput.value = details.criteria;
    if (sessionNameInput) sessionNameInput.value = details.name || "";
    zoteroLinks    = details.zotero_links    || {};
    zoteroMetadata = details.zotero_metadata || {};
    folderPathEl.textContent = currentFolder;
    folderPathEl.classList.add("has-path");

    // 2. Get statuses and render PDF list
    const sRes = await fetch(`/api/analyze-status/${sid}`);
    const statuses = await sRes.json();

    allPdfs = Object.keys(statuses);
    // Reconstruct article groups from stored metadata (or fall back to flat list)
    if (Object.keys(zoteroMetadata).length > 0) {
      articleGroups = _buildGroupsFromMetadata(allPdfs, zoteroMetadata);
      renderPdfList(articleGroups);
    } else {
      articleGroups = [];
      renderPdfList([]);
    }
    updateListStatuses(statuses);
    updateFilterCounts(statuses);
    updateSummaryBar();
    
    pdfSection.style.display = "flex";
    pdfSection.classList.add("visible");
    filterTabs.style.display = "flex";
    analyzeBtn.disabled = false;
    
    // Resume status watching if needed
    const terminal = ["done", "error", "cancelled"];
    const allDone = Object.values(statuses).every(s => terminal.includes(s.status));
    if (!allDone) {
      startStatusWatch();
      analyzeBtn.classList.add("loading");
      analyzeBtn.disabled = true;
      stopBtn.style.display = "flex";
    } else {
      stopPolling();
      analyzeBtn.classList.remove("loading");
      stopBtn.style.display = "none";
    }
    
    showEmpty();
  } catch (err) {
    console.error("Error loading session details:", err);
  }
}

if (newAnalysisBtn) {
  newAnalysisBtn.addEventListener("click", () => {
    sessionId      = null;
    currentFolder  = null;
    allPdfs        = [];
    articleGroups  = [];
    zoteroMetadata = {};
    zoteroLinks    = {};
    const sumEl = document.getElementById("dedup-summary");
    if (sumEl) sumEl.style.display = "none";
    folderPathEl.textContent = "No folder selected";
    folderPathEl.classList.remove("has-path");
    if (zoteroCollectionSelect) {
      zoteroCollectionSelect.disabled = false;
      zoteroCollectionSelect.value = "";
    }
    criteriaInput.value = "";
    if (sessionNameInput) sessionNameInput.value = "";
    pdfList.innerHTML = "";
    pdfCount.textContent = "0";
    pdfSection.classList.remove("visible");
    filterTabs.style.display = "none";
    showEmpty();
    document.querySelectorAll('.history-item').forEach(i => i.classList.remove('active'));
    stopPolling();
    analyzeBtn.classList.remove("loading");
    analyzeBtn.disabled = false;
    stopBtn.style.display = "none";
  });
}

// ── Actions ──

if (exportCsvBtn) {
  exportCsvBtn.addEventListener("click", () => {
    if (sessionId) {
      window.location.href = '/api/analyze-export/' + sessionId;
    }
  });
}

if (zoteroSaveBtn) {
  zoteroSaveBtn.addEventListener("click", () => {
    if (!sessionId) return;
    openZoteroSaveModal(sessionId);
  });
}

function openZoteroSaveModal(sid) {
  const modal     = document.getElementById("zotero-save-modal");
  const subtitle  = document.getElementById("zs-subtitle");
  const feedEl    = document.getElementById("zs-feed");
  const feedList  = document.getElementById("zs-feed-list");
  const summary   = document.getElementById("zs-summary");
  const sumText   = document.getElementById("zs-summary-text");
  const closeBtn  = document.getElementById("zs-close-btn");

  // Reset
  modal.querySelectorAll(".zl-step").forEach(li => {
    li.classList.remove("active","done","error");
    li.querySelector(".zl-icon").textContent  = "⏳";
    li.querySelector(".zl-detail").textContent = "";
  });
  feedList.innerHTML    = "";
  feedEl.style.display  = "none";
  summary.style.display = "none";
  closeBtn.style.display = "none";
  subtitle.textContent  = "Starting…";
  modal.style.display   = "flex";

  zoteroSaveBtn.disabled = true;

  function zsStep(step, state, detail) {
    const li = modal.querySelector(`.zl-step[data-step="${step}"]`);
    if (!li) return;
    li.classList.remove("active","done","error");
    li.classList.add(state);
    const icons = { active:"🔄", done:"✅", error:"❌" };
    li.querySelector(".zl-icon").textContent   = icons[state] || "⏳";
    if (detail) li.querySelector(".zl-detail").textContent = detail;
  }

  const es = new EventSource(`/api/analyze-zotero-save-stream/${sid}`);

  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    const { step, message } = msg;
    subtitle.textContent = message;

    if (step === "auditing") {
      zsStep("auditing", "active");

    } else if (step === "auditing_done") {
      zsStep("auditing", "done", message);

    } else if (step === "connecting") {
      zsStep("connecting", "done", msg.lib_label || "");

    } else if (step === "fetching" || step === "fetching_fallback") {
      zsStep("fetching", "active", message);

    } else if (step === "fetching_done") {
      zsStep("fetching", "done", `${msg.fetched} of ${msg.total} fetched`);

    } else if (step === "collection" || step === "collection_creating") {
      zsStep("collection", "active", message);

    } else if (step === "collection_found" || step === "collection_created") {
      zsStep("collection", "done", message);

    } else if (step === "resolving" || step === "resolving_parents") {
      zsStep("resolving", "active", message);

    } else if (step === "resolving_done") {
      zsStep("resolving", "done", message);

    } else if (step === "moving") {
      zsStep("moving", "active", message);
      feedEl.style.display = "block";

    } else if (step === "item_moved") {
      // Append to live feed
      const li = document.createElement("li");
      li.style.cssText = "font-size:11px; color:#4ade80; padding:1px 0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;";
      li.textContent = `✓ ${message.replace("✓ ", "")}`;
      feedList.appendChild(li);
      feedEl.scrollTop = feedEl.scrollHeight;
      // Update moving detail
      const movingLi = modal.querySelector('.zl-step[data-step="moving"]');
      if (movingLi) movingLi.querySelector(".zl-detail").textContent = `${msg.moved} / ${msg.total}`;

    } else if (step === "moving_done") {
      zsStep("moving", "done", `${msg.moved} saved · ${msg.failed} failed`);

    } else if (step === "complete") {
      sumText.textContent   = `${msg.saved_count} articles saved to Zotero collection.`;
      summary.style.display = "block";
      closeBtn.style.display = "block";
      subtitle.textContent  = "Complete!";
      es.close();

    } else if (step === "error") {
      subtitle.textContent = message;
      // Mark last active step as error
      modal.querySelectorAll(".zl-step.active").forEach(li => {
        li.classList.replace("active","error");
        li.querySelector(".zl-icon").textContent = "❌";
        li.querySelector(".zl-detail").textContent = message;
      });
      closeBtn.style.display = "block";
      es.close();
    }
  };

  es.onerror = () => {
    subtitle.textContent = "Connection lost. Please try again.";
    closeBtn.style.display = "block";
    es.close();
  };

  closeBtn.addEventListener("click", () => {
    modal.style.display    = "none";
    zoteroSaveBtn.disabled = false;
  }, { once: true });
}

if (retryErrorsBtn) {
  retryErrorsBtn.addEventListener("click", async () => {
    if (!sessionId) return;
    retryErrorsBtn.disabled = true;
    retryErrorsBtn.textContent = "Retrying…";
    try {
      const res = await fetch(`/api/analyze-retry/${sessionId}`, { method: "POST", headers: csrfHeaders() });
      analyzeBtn.classList.add("loading");
      analyzeBtn.disabled = true;
      stopBtn.style.display = "flex";
      retryErrorsBtn.style.display = "none";
      startStatusWatch();
    } catch (err) {
      console.error("Retry error:", err);
    } finally {
      retryErrorsBtn.disabled = false;
      retryErrorsBtn.textContent = "↺ Retry Errors";
    }
  });
}

const detailEmpty    = document.getElementById("detail-empty");
const detailView     = document.getElementById("detail-view");
const detailTitle    = document.getElementById("detail-title");
const detailFilepath = document.getElementById("detail-filepath");
const ocrBadge       = document.getElementById("ocr-badge");
const alignBanner    = document.getElementById("alignment-banner");
const alignIcon      = document.getElementById("alignment-icon");
const alignVerdict   = document.getElementById("alignment-verdict");
const alignReason    = document.getElementById("alignment-reason");
const gridIdentity   = document.getElementById("grid-identity");
const fieldsContent  = document.getElementById("fields-content");
const fieldsMeth     = document.getElementById("fields-methodology");
const fieldsFinding  = document.getElementById("fields-findings");
const citedByBtn     = document.getElementById("cited-by-btn");
const citedByGroup   = document.getElementById("cited-by-group");
const citedByList    = document.getElementById("cited-by-list");
const citedByCount   = document.getElementById("cited-by-count");
const citedBySortBtn = document.getElementById("cited-by-sort-btn");
const citedByCsvBtn  = document.getElementById("cited-by-csv-btn");

// ── Cited By (Semantic Scholar forward citations) ──
let citedByData = null;      // citations for the currently shown article
let citedByRel = null;       // pdf path the citations belong to
let citedBySortDesc = true;  // newest first by default

function resetCitedBy(rel, hasDoi) {
  citedByData = null;
  citedByRel = rel;
  citedBySortDesc = true;
  citedByGroup.style.display = "none";
  citedByList.innerHTML = "";
  citedByBtn.style.display = hasDoi ? "inline-flex" : "none";
  citedByBtn.disabled = false;
  citedByBtn.querySelector(".btn-label").textContent = "🔗 Cited By";
}

citedByBtn.addEventListener("click", async () => {
  if (citedByData) {  // already loaded — just scroll to it
    citedByGroup.scrollIntoView({ behavior: "smooth" });
    return;
  }
  citedByBtn.disabled = true;
  citedByBtn.querySelector(".btn-label").textContent = "⏳ Loading…";
  try {
    const res = await fetch(`/api/analyze-citations/${sessionId}/${encodeURIComponent(citedByRel)}`);
    const data = await res.json();
    if (data.error) {
      alert(data.error);
      citedByBtn.querySelector(".btn-label").textContent = "🔗 Cited By";
      return;
    }
    citedByData = data;
    citedByBtn.querySelector(".btn-label").textContent = `🔗 Cited By (${data.citation_count})`;
    renderCitedBy();
    citedByGroup.style.display = "block";
    citedByGroup.scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    alert("Failed to load citations: " + err.message);
    citedByBtn.querySelector(".btn-label").textContent = "🔗 Cited By";
  } finally {
    citedByBtn.disabled = false;
  }
});

citedBySortBtn.addEventListener("click", () => {
  citedBySortDesc = !citedBySortDesc;
  renderCitedBy();
});

citedByCsvBtn.addEventListener("click", () => {
  if (!citedByData) return;
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [["Author(s)", "Year", "Title", "Journal/Venue", "DOI", "Already in session"]];
  citedByData.citations.forEach(c =>
    rows.push([c.author, c.year, c.title, c.journal, c.doi, c.in_session ? "yes" : "no"]));
  const blob = new Blob([rows.map(r => r.map(esc).join(",")).join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `cited_by_${(citedByData.doi || "article").replace(/[^\w.-]+/g, "_")}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
});

function renderCitedBy() {
  const cits = [...citedByData.citations].sort((a, b) => {
    const ya = parseInt(a.year) || 0, yb = parseInt(b.year) || 0;
    return citedBySortDesc ? yb - ya : ya - yb;
  });
  citedBySortBtn.textContent = citedBySortDesc ? "Year ↓" : "Year ↑";
  citedByCount.textContent = `${cits.length} citing paper${cits.length === 1 ? "" : "s"}`;
  citedByList.innerHTML = "";

  cits.forEach((c, i) => {
    const row = document.createElement("div");
    row.style.cssText = "padding:10px 12px; background:#1a1f2e; border:1px solid #2d3748; border-radius:8px; font-size:13px; line-height:1.5;";
    const doiLink = c.doi
      ? `<a href="https://doi.org/${encodeURIComponent(c.doi).replace(/%2F/gi, "/")}" target="_blank" rel="noopener" style="font-size:11px; color:#6366f1; text-decoration:underline;">https://doi.org/${_escHtml(c.doi)}</a>`
      : "";
    const badge = c.in_session
      ? `<span style="font-size:10px; font-weight:600; background:#14532d; color:#86efac; padding:2px 8px; border-radius:10px; margin-left:8px; white-space:nowrap;">✓ in session</span>`
      : "";
    row.innerHTML = `
      <span style="color:#818cf8; font-weight:bold;">${i + 1}.</span>
      <span style="color:#e2e8f0;">${_escHtml(c.author)}</span>
      <span style="color:#cbd5e1;">${c.year ? `(${_escHtml(c.year)}).` : ""}</span>
      ${_escHtml(c.title)}.
      <span style="font-style:italic; color:#a5b4fc;">${_escHtml(c.journal)}</span>
      ${doiLink}${badge}`;
    citedByList.appendChild(row);
  });
}

// ── Load Zotero Collections ──
async function loadZoteroCollections() {
  if (!zoteroCollectionSelect) return;
  try {
    const res = await fetch("/api/zotero-collections");
    const data = await res.json();
    if (data.libraries) {
      zoteroCollectionSelect.innerHTML = '<option value="">📚 Select Zotero Collection...</option>';
      data.libraries.forEach(lib => {
        const group = document.createElement("optgroup");
        group.label = lib.name;
        
        const allOpt = document.createElement("option");
        allOpt.value = `library:${lib.type}:${lib.id}`;
        allOpt.textContent = `All Items (${lib.name})`;
        group.appendChild(allOpt);

        lib.collections.forEach(c => {
          const opt = document.createElement("option");
          opt.value = `collection:${lib.type}:${lib.id}:${c.id}`;
          opt.textContent = `  ↳ ${c.name}`;
          group.appendChild(opt);
        });
        zoteroCollectionSelect.appendChild(group);
      });
    }
  } catch (err) {
    console.error("Zotero collections error:", err);
  }
}
loadZoteroCollections();

if (zoteroCollectionSelect) {
  zoteroCollectionSelect.addEventListener("change", async (e) => {
    const val = e.target.value;
    if (!val) return;
    
    zoteroCollectionSelect.disabled = true;
    folderPathEl.textContent = "Loading Zotero PDFs...";
    
    let fetchUrl = "/api/zotero-pdfs?";
    const parts = val.split(":");
    const category = parts[0]; 
    const libType = parts[1];
    const libId = parts[2];
    const collKey = parts[3];

    fetchUrl += `library_id=${libId}&library_type=${libType}`;
    if (category === "collection") {
        fetchUrl += `&collection_id=${collKey}`;
    }
    
    // ── Show progress modal ──────────────────────────────────────────────
    const zlModal    = document.getElementById("zotero-loading-modal");
    const zlSubtitle = document.getElementById("zl-subtitle");
    const zlSummary  = document.getElementById("zl-summary");
    const zlSumText  = document.getElementById("zl-summary-text");

    function zlSetStep(step, state, detail) {
      const li = zlModal.querySelector(`.zl-step[data-step="${step}"]`);
      if (!li) return;
      li.classList.remove("active", "done", "error");
      li.classList.add(state);
      const iconMap = { active: "🔄", done: "✅", error: "❌", pending: "⏳" };
      li.querySelector(".zl-icon").textContent = iconMap[state] || "⏳";
      if (detail) li.querySelector(".zl-detail").textContent = detail;
    }

    // Reset all steps to pending
    zlModal.querySelectorAll(".zl-step").forEach(li => {
      li.classList.remove("active","done","error");
      li.querySelector(".zl-icon").textContent = "⏳";
      li.querySelector(".zl-detail").textContent = "";
    });
    zlSummary.style.display = "none";
    zlSubtitle.textContent  = "Connecting…";
    zlModal.style.display   = "flex";

    // Step → which "done" event signals it complete
    const stepDoneMap = {
      connecting: "fetching",         // once fetching starts, connecting is done
      fetching:   "fetching_done",
      metadata:   "metadata_done",
      scanning:   "scanning_done",
      grouping:   "grouping_done",
    };
    let activeStep = null;

    const collLabel = zoteroCollectionSelect.options[zoteroCollectionSelect.selectedIndex].text;

    await new Promise((resolve) => {
      const streamUrl = fetchUrl.replace("/api/zotero-pdfs?", "/api/zotero-pdfs-stream?");
      const es = new EventSource(streamUrl);

      es.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        const { step, message } = msg;
        zlSubtitle.textContent = message;

        // Mark previous step done when next begins
        if (activeStep && step !== activeStep) {
          zlSetStep(activeStep, "done");
        }

        if (step === "connecting") {
          zlSetStep("connecting", "active");
          activeStep = "connecting";

        } else if (step === "fetching") {
          zlSetStep("connecting", "done");
          zlSetStep("fetching", "active");
          activeStep = "fetching";

        } else if (step === "fetching_done") {
          zlSetStep("fetching", "done", `${msg.item_count} items retrieved`);
          activeStep = null;

        } else if (step === "metadata") {
          zlSetStep("metadata", "active");
          activeStep = "metadata";

        } else if (step === "metadata_done") {
          zlSetStep("metadata", "done", `${msg.article_count} articles indexed`);
          activeStep = null;

        } else if (step === "scanning") {
          zlSetStep("scanning", "active");
          activeStep = "scanning";

        } else if (step === "scanning_done") {
          zlSetStep("scanning", "done", `${msg.pdf_count} PDFs found on disk`);
          activeStep = null;

        } else if (step === "grouping") {
          zlSetStep("grouping", "active");
          activeStep = "grouping";

        } else if (step === "grouping_done") {
          zlSetStep("grouping", "done", `${msg.article_count} articles · ${msg.dup_count} duplicates collapsed`);
          activeStep = null;

        } else if (step === "complete") {
          // All steps done — populate state and close
          currentFolder  = msg.path;
          zoteroLinks    = msg.zotero_links    || {};
          zoteroMetadata = msg.zotero_metadata || {};
          articleGroups  = msg.article_groups  || [];
          allPdfs        = articleGroups.map(g => g.versions.find(v => v.selected)?.path || g.versions[0]?.path).filter(Boolean);

          const dupCount = articleGroups.filter(g => g.versions.length > 1).length;
          zlSumText.textContent = `${articleGroups.length} articles loaded${dupCount ? ` · ${dupCount} duplicates collapsed` : ""}`;
          zlSummary.style.display = "block";
          zlSubtitle.textContent  = "Complete!";

          setTimeout(() => {
            zlModal.style.display = "none";
            folderPathEl.textContent = `Zotero: ${collLabel}`;
            folderPathEl.classList.add("has-path");
            pdfSection.style.display = "flex";
            pdfSection.classList.add("visible");
            renderPdfList(articleGroups);
            updateSummaryBar();
            stopPolling();
            sessionId = null;
            showEmpty();
            filterTabs.style.display = "none";
          }, 800);

          es.close();
          resolve();

        } else if (step === "error") {
          if (activeStep) zlSetStep(activeStep, "error", message);
          zlSubtitle.textContent = message;
          es.close();
          resolve();
        }
      };

      es.onerror = () => {
        zlSubtitle.textContent = "Connection error — please try again.";
        es.close();
        resolve();
      };
    });

    zlModal.style.display = "none";
    zoteroCollectionSelect.disabled = false;
  });
}

// ── Helpers ──
function _escHtml(str) {
  return String(str ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function _fmtDate(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleDateString("en-US", { month: "short", year: "numeric" }); }
  catch { return ""; }
}
function _extractKey(link) {
  const m = (link || "").match(/items\/([A-Z0-9]+)$/);
  return m ? m[1] : null;
}

// Build article groups from flat path list + metadata (used when restoring a session)
function _buildGroupsFromMetadata(paths, metadata) {
  const byParent = {};
  paths.forEach(p => {
    const m = metadata[p] || {};
    const pk = m.parent_key || p.split("/")[0];
    if (!byParent[pk]) byParent[pk] = [];
    byParent[pk].push({ path: p, date_modified: m.date_modified || "", meta: m });
  });
  const groups = Object.entries(byParent).map(([pk, versions]) => {
    versions.sort((a, b) => b.date_modified.localeCompare(a.date_modified));
    const fm = versions[0].meta;
    return {
      parent_key:     pk,
      title:          fm.title || versions[0].path.split("/").pop(),
      author_sort:    fm.author_sort || "",
      author_display: fm.author_display || "",
      year:           fm.year || "",
      versions: versions.map((v, i) => ({ path: v.path, date_modified: v.date_modified, selected: i === 0 })),
    };
  });
  groups.sort((a, b) => a.author_sort.localeCompare(b.author_sort) || a.year.localeCompare(b.year));
  return groups;
}

function updateSelectedPdfs() {
  allPdfs = Array.from(document.querySelectorAll(".pdf-item")).map(li => li.dataset.path);
}

function updateSummaryBar() {
  const sumEl   = document.getElementById("dedup-summary");
  const sumArt  = document.getElementById("sum-articles");
  const sumPdfs = document.getElementById("sum-pdfs");
  const sumAli  = document.getElementById("sum-aligned");
  const sumDupW = document.getElementById("sum-dup-wrap");
  const sumDups = document.getElementById("sum-dups");
  const dupPill = document.getElementById("dup-pill");

  if (!articleGroups.length) {
    if (sumEl)   sumEl.style.display  = "none";
    if (dupPill) dupPill.style.display = "none";
    return;
  }

  const totalPdfs = articleGroups.reduce((n, g) => n + g.versions.length, 0);
  const totalArts = articleGroups.length;
  const dupPdfs   = totalPdfs - totalArts;   // PDFs that were collapsed

  // Count articles with duplicates (groups with >1 version)
  const dupArticles = articleGroups.filter(g => g.versions.length > 1).length;

  const alignedKeys = new Set();
  document.querySelectorAll(".pdf-item.aligned").forEach(li => {
    const k = _extractKey(zoteroLinks[li.dataset.path]);
    if (k) alignedKeys.add(k);
  });

  if (sumEl) {
    sumEl.style.display = "flex";
    if (sumArt)  sumArt.textContent  = totalArts;
    if (sumPdfs) sumPdfs.textContent = totalPdfs;
    if (sumAli)  sumAli.textContent  = alignedKeys.size || "—";
    if (sumDupW && sumDups) {
      if (dupPdfs > 0) { sumDupW.style.display = ""; sumDups.textContent = dupPdfs; }
      else               sumDupW.style.display = "none";
    }
  }

  // Small pill next to section title
  if (dupPill) {
    if (dupArticles > 0) {
      dupPill.style.display = "";
      dupPill.textContent   = `${dupArticles} duplicate${dupArticles > 1 ? "s" : ""}`;
      dupPill.title         = `${dupArticles} article${dupArticles > 1 ? "s have" : " has"} multiple PDF versions collapsed`;
    } else {
      dupPill.style.display = "none";
    }
  }

  // Global count pill next to the processed/total counter
  const countPill = document.getElementById("dup-count-pill");
  if (countPill) {
    if (dupPdfs > 0) {
      countPill.style.display = "";
      countPill.textContent   = `${dupPdfs} duplicate${dupPdfs > 1 ? "s" : ""} of ${totalPdfs}`;
      countPill.title         = `${dupPdfs} PDF${dupPdfs > 1 ? "s were" : " was"} collapsed — ${totalPdfs} total PDFs across ${totalArts} unique articles`;
    } else {
      countPill.style.display = "none";
    }
  }
}

function renderPdfList(groups) {
  pdfList.innerHTML = "";

  // Handle legacy flat array (passed from folder browse, not Zotero)
  if (!groups || groups.length === 0) {
    if (allPdfs.length === 0) {
      pdfList.innerHTML = `<li style="color:#475569;font-size:12px;padding:10px 0;text-align:center;">No PDF files found.</li>`;
      pdfCount.textContent = "0 / 0";
      return;
    }
    // Flat list (no Zotero metadata) — build minimal groups
    groups = allPdfs.map(rel => ({
      parent_key: rel,
      title: rel.split("/").pop(),
      author_display: "",
      year: "",
      versions: [{ path: rel, date_modified: "", selected: true }],
    }));
  }

  // articleGroups may have been passed in or built locally
  if (groups !== articleGroups) articleGroups = groups;
  allPdfs = groups.map(g => g.versions.find(v => v.selected)?.path || g.versions[0].path);
  pdfCount.textContent = `0 / ${groups.length}`;
  pdfSection.classList.add("visible");
  updateSummaryBar();

  groups.forEach(group => {
    const selectedPath = group.versions.find(v => v.selected)?.path || group.versions[0].path;
    const multiVersion = group.versions.length > 1;
    const title        = _escHtml(group.title || selectedPath.split("/").pop());
    const meta         = [group.author_display, group.year].filter(Boolean).join(" · ");

    const li = document.createElement("li");
    li.className      = "pdf-item";
    li.dataset.path   = selectedPath;
    li.dataset.filter = "all";
    li.style.cssText  = "display:flex; flex-direction:column; padding:0;";

    // Main row
    const row = document.createElement("div");
    row.className  = "pdf-item-row";
    row.style.cssText = "display:grid; grid-template-columns:2fr 1fr 1fr; gap:8px; align-items:center; padding:8px 10px; cursor:pointer;";
    row.innerHTML = `
      <div style="display:flex; align-items:center; gap:8px; overflow:hidden; min-width:0;">
        <span class="pdf-icon">📄</span>
        <div class="pdf-name" style="min-width:0;">
          <span class="pdf-title" title="${title}">${title}</span>
          ${meta ? `<span class="pdf-meta">${_escHtml(meta)}</span>` : ""}
        </div>
        ${multiVersion ? `<button class="versions-badge" tabindex="-1">${group.versions.length} versions ▾</button>` : ""}
      </div>
      <div style="text-align:center;"><span class="pdf-status pending">pending</span></div>
      <div class="pdf-alignment-indicator" style="text-align:center; font-weight:bold; color:#64748b;">-</div>`;

    row.addEventListener("click", (e) => {
      if (e.target.closest(".versions-badge") || e.target.closest(".versions-dropdown")) return;
      onPdfClick(li, li.dataset.path);
    });
    li.appendChild(row);

    // Versions dropdown (multi-version only)
    if (multiVersion) {
      const dropdown = document.createElement("div");
      dropdown.className    = "versions-dropdown";
      dropdown.style.display = "none";

      group.versions.forEach((v, i) => {
        const label = document.createElement("label");
        label.className = "version-item";
        label.innerHTML = `
          <input type="checkbox" ${v.selected ? "checked" : ""} data-vpath="${_escHtml(v.path)}">
          <span class="version-filename" title="${_escHtml(v.path.split("/").pop())}">${_escHtml(v.path.split("/").pop())}</span>
          <span class="version-date">${_fmtDate(v.date_modified)}</span>`;

        label.querySelector("input").addEventListener("change", (e) => {
          e.stopPropagation();
          if (!e.target.checked) { e.target.checked = true; return; } // must keep one checked
          // Uncheck all others in this dropdown
          dropdown.querySelectorAll("input[type=checkbox]").forEach(cb => {
            if (cb !== e.target) cb.checked = false;
          });
          // Update selected path on the <li>
          li.dataset.path = e.target.dataset.vpath;
          updateSelectedPdfs();
          updateSummaryBar();
        });
        dropdown.appendChild(label);
      });

      // Toggle button
      const badge = row.querySelector(".versions-badge");
      badge.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = dropdown.style.display !== "none";
        dropdown.style.display = open ? "none" : "flex";
        badge.textContent = open ? `${group.versions.length} versions ▾` : `${group.versions.length} versions ▲`;
      });

      li.appendChild(dropdown);
    }

    pdfList.appendChild(li);
  });
}

// ── Analysis ──
analyzeBtn.addEventListener("click", async () => {
  if (analyzeBtn.disabled) return;
  const criteria = criteriaInput.value.trim();
  if (!criteria || !currentFolder) return;
  
  analyzeBtn.classList.add("loading");
  analyzeBtn.disabled = true;
  stopBtn.style.display = "flex";

  stopPolling();
  resetAllStatuses("pending");
  showEmpty();
  filterTabs.style.display = "flex";
  activeFilter = "all";

  const modal = document.getElementById("loading-modal");
  const progressBar = document.getElementById("loading-progress");
  const modalStatus = document.getElementById("modal-status");
  modal.style.display = "flex";
  progressBar.style.width = "0%";

  try {
    const res  = await fetch("/api/analyze-start", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name: sessionNameInput ? sessionNameInput.value.trim() : "", folder: currentFolder, pdfs: allPdfs, criteria, zoteroLinks, zoteroMetadata }),
    });
    const data = await res.json();
    sessionId = data.session_id;

    const eventSource = new EventSource(`/api/analyze-progress/${sessionId}`);
    eventSource.onmessage = (e) => {
        const pct = JSON.parse(e.data).progress;
        progressBar.style.width = pct + "%";
        if (pct >= 100) {
            modal.style.display = "none";
            eventSource.close();
            startStatusWatch();
            loadHistory(); // Refresh history sidebar
        }
    };
  } catch (err) {
    modal.style.display = "none";
    analyzeBtn.classList.remove("loading");
    analyzeBtn.disabled = false;
  }
});

stopBtn.addEventListener("click", async () => {
  if (!sessionId) return;
  
  // Instant UI feedback
  stopBtn.disabled = true;
  stopBtn.textContent = "Stopping...";
  
  try {
    const res = await fetch("/api/analyze-stop", {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ session_id: sessionId }),
    });
    
    if (res.ok) {
        // Trigger one final poll to update the list to 'cancelled' and stop everything
        await pollStatus();
    }
  } catch (err) {
    console.error("Error stopping:", err);
  } finally {
    stopBtn.disabled = false;
    stopBtn.textContent = "■ Stop";
  }
});

function applyStatuses(statuses) {
  updateListStatuses(statuses);
  updateFilterCounts(statuses);
  updateSummaryBar();

  const terminal = ["done", "error", "cancelled"];
  const allDone = Object.keys(statuses).length > 0 &&
                  Object.values(statuses).every(s => terminal.includes(s.status));
  if (allDone) {
    stopPolling();
    analyzeBtn.classList.remove("loading");
    analyzeBtn.disabled = false;
    stopBtn.style.display = "none";
    loadHistory();
  }
  return allDone;
}

async function pollStatus() {
  if (!sessionId) return;
  try {
    const res = await fetch(`/api/analyze-status/${sessionId}`);
    applyStatuses(await res.json());
  } catch (err) {}
}

// Watch job statuses live via SSE; fall back to 2s polling if the stream drops.
function startStatusWatch() {
  if (!sessionId || statusES || pollTimer) return;
  if (window.EventSource) {
    statusES = new EventSource(`/api/analyze-status-stream/${sessionId}`);
    statusES.onmessage = (e) => applyStatuses(JSON.parse(e.data));
    statusES.onerror = () => {
      if (statusES) { statusES.close(); statusES = null; }
      if (sessionId && !pollTimer) pollTimer = setInterval(pollStatus, 2000);
    };
  } else {
    pollTimer = setInterval(pollStatus, 2000);
  }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (statusES)  { statusES.close(); statusES = null; }
}

function updateListStatuses(statuses) {
  document.querySelectorAll(".pdf-item").forEach(li => {
    const s = statuses[li.dataset.path];
    if (!s) return;
    const pill = li.querySelector(".pdf-status");
    pill.className = `pdf-status ${s.status}`;
    pill.textContent = s.status;
    li.classList.remove("aligned", "excluded", "cancelled-item", "error-item");
    const alignInd = li.querySelector(".pdf-alignment-indicator");
    if (s.status === "done") {
      li.classList.add(s.aligns ? "aligned" : "excluded");
      li.dataset.filter = s.aligns ? "aligned" : "excluded";
      alignInd.textContent = s.aligns ? "✓" : "✗";
    } else if (s.status === "error") {
      li.classList.add("error-item");
      li.dataset.filter = "error";
      alignInd.textContent = "⚠";
    } else if (s.status === "cancelled") {
      li.classList.add("cancelled-item");
      li.dataset.filter = "cancelled";
      alignInd.textContent = "—";
    }
  });
}

function updateFilterCounts(statuses) {
  const all      = Object.values(statuses);
  const aligned  = all.filter(s => s.status === "done" && s.aligns).length;
  const excluded = all.filter(s => s.status === "done" && !s.aligns).length;
  const errors   = all.filter(s => s.status === "error").length;
  const cancelled = all.filter(s => s.status === "cancelled").length;

  // Count unique aligned articles (unique parent keys)
  const alignedKeys = new Set();
  Object.entries(statuses).forEach(([path, s]) => {
    if (s.status === "done" && s.aligns) {
      const k = _extractKey(zoteroLinks[path]);
      if (k) alignedKeys.add(k);
    }
  });

  const processed = all.filter(s => ["done", "error", "cancelled"].includes(s.status)).length;
  pdfCount.textContent = `${processed} / ${all.length}`;

  fcAll.textContent     = all.length;
  fcAligned.textContent = alignedKeys.size > 0
    ? `${aligned}${alignedKeys.size !== aligned ? ` (${alignedKeys.size} articles)` : ""}`
    : aligned;
  fcExcluded.textContent = excluded;
  fcError.textContent    = errors;

  exportCsvBtn.style.display  = aligned > 0 ? "block" : "none";
  zoteroSaveBtn.style.display = (aligned > 0 && Object.keys(zoteroLinks).length > 0) ? "block" : "none";
  retryErrorsBtn.style.display = (errors > 0 || cancelled > 0) ? "block" : "none";

  // Update summary bar aligned count
  const sumAli = document.getElementById("sum-aligned");
  if (sumAli) sumAli.textContent = alignedKeys.size > 0 ? alignedKeys.size : (aligned || "—");

  // Drive duplicate pills from zoteroLinks + statuses
  // (works even when articleGroups is empty, e.g. restored sessions)
  if (Object.keys(zoteroLinks).length > 0) {
    const keyCount = {};
    Object.keys(statuses).forEach(path => {
      const k = _extractKey(zoteroLinks[path]);
      if (k) keyCount[k] = (keyCount[k] || 0) + 1;
    });
    const totalPdfCount  = Object.keys(statuses).length;
    const dupPdfCount    = Object.values(keyCount).filter(c => c > 1).reduce((s, c) => s + c - 1, 0);
    const dupArtsCount   = Object.values(keyCount).filter(c => c > 1).length;

    const dupPill    = document.getElementById("dup-pill");
    const countPill  = document.getElementById("dup-count-pill");

    if (dupPill) {
      if (dupArtsCount > 0) {
        dupPill.style.display = "";
        dupPill.textContent   = `${dupArtsCount} duplicate${dupArtsCount > 1 ? "s" : ""}`;
        dupPill.title         = `${dupArtsCount} article${dupArtsCount > 1 ? "s have" : " has"} multiple PDF versions`;
      } else {
        dupPill.style.display = "none";
      }
    }

    if (countPill && totalPdfCount > 0) {
      countPill.style.display = "";
      countPill.textContent   = `${dupPdfCount} duplicate${dupPdfCount !== 1 ? "s" : ""} of ${totalPdfCount}`;
      countPill.title         = `${dupPdfCount} PDF${dupPdfCount !== 1 ? "s are" : " is"} a duplicate — ${totalPdfCount} total PDFs, ${Object.keys(keyCount).length} unique articles`;
    }
  }
}

document.querySelectorAll(".filter-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    activeFilter = tab.dataset.filter;
    document.querySelectorAll(".filter-tab").forEach(t => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".pdf-item").forEach(li => {
      li.style.display = activeFilter === "all" || li.dataset.filter === activeFilter ? "" : "none";
    });
  });
});

function resetAllStatuses(status) {
  document.querySelectorAll(".pdf-item").forEach(li => {
    const pill = li.querySelector(".pdf-status");
    if (pill) { pill.className = `pdf-status ${status}`; pill.textContent = status; }
    const ind = li.querySelector(".pdf-alignment-indicator");
    if (ind) ind.textContent = "-";
    li.classList.remove("aligned", "excluded", "cancelled-item", "error-item");
    li.dataset.filter = "all";
    li.style.display = "";
  });
}

async function onPdfClick(el, rel) {
  const status = el.querySelector(".pdf-status").textContent;
  document.querySelectorAll(".pdf-item").forEach(i => i.classList.remove("active"));
  el.classList.add("active");

  if (currentFolder && rel) loadPdf(currentFolder, rel);

  if (!sessionId || status === "pending" || status === "processing") {
    detailEmpty.style.display = "none";
    detailView.style.display = "flex";
    detailView.classList.add("visible");
    detailTitle.textContent = (zoteroMetadata[rel] && zoteroMetadata[rel].title) || rel.split("/").pop();
    detailFilepath.textContent = rel;
    alignVerdict.textContent = status === "processing" ? "Processing..." : "Not analyzed yet";
    resetCitedBy(rel, false);
    return;
  }

  try {
    const res = await fetch(`/api/analyze-result/${sessionId}/${encodeURIComponent(rel)}`);
    const data = await res.json();
    renderResult(rel, data);
  } catch (err) {}
}

function renderResult(rel, data) {
  const fields = data.fields;
  detailEmpty.style.display = "none";
  detailView.style.display = "flex";
  detailView.classList.add("visible");
  detailTitle.textContent = fields.title || rel.split("/").pop();
  detailFilepath.textContent = rel;
  resetCitedBy(rel, !!(data.doi || fields.doi));
  ocrBadge.textContent = data.ocr_method;
  ocrBadge.className = `ocr-badge ${data.ocr_method}`;

  const aligns = !!fields.aligns_with_criteria;
  alignBanner.className = `alignment-banner ${aligns ? "aligns" : "not-aligns"}`;
  alignIcon.textContent = aligns ? "✅" : "❌";
  alignVerdict.textContent = aligns ? "Aligns" : "Excluded";
  alignReason.textContent = fields.alignment_reason || "";

  renderCards(gridIdentity, [
    { label: "Year", value: fields.year },
    { label: "Author(s)", value: fields.author },
    { label: "Resource Type", value: fields.resource_type },
    { label: "Location", value: fields.location },
    { label: "DOI", value: fields.doi, isDoi: true },
  ]);
  
  renderTextFields(fieldsContent, [
    { label: "Abstract", value: fields.abstract },
    { label: "Keywords", value: fields.keywords },
    { label: "Purpose & Objectives", value: fields.purpose_objectives },
  ]);

  renderTextFields(fieldsMeth, [
    { label: "Research Questions", value: fields.research_questions },
    { label: "Survey/Interview Questions", value: fields.survey_interview_focus_questions },
    { label: "Sample", value: fields.sample },
    { label: "Design", value: fields.design },
  ]);

  renderTextFields(fieldsFinding, [
    { label: "Main Findings", value: fields.main_findings },
  ]);
}

function renderCards(container, items) {
  container.innerHTML = "";
  items.forEach(({ label, value, isDoi }) => {
    const card = document.createElement("div");
    card.className = "meta-card";
    const valHtml = isDoi && value
      ? `<a href="https://doi.org/${encodeURIComponent(value).replace(/%2F/gi, "/")}" target="_blank" rel="noopener" class="doi-link">${_escHtml(value)}</a>`
      : `<span>${_escHtml(value) || "—"}</span>`;
    card.innerHTML = `<span class="meta-label">${label}</span>${valHtml}`;
    container.appendChild(card);
  });
}

function renderTextFields(container, items) {
  container.innerHTML = "";
  items.forEach(({ label, value }) => {
    const block = document.createElement("div");
    block.className = "text-field";
    block.innerHTML = `<span class="meta-label">${label}</span><span class="text-field-value">${_escHtml(value) || "—"}</span>`;
    container.appendChild(block);
  });
}

function showEmpty() {
  detailEmpty.style.display = "flex";
  detailView.style.display = "none";
  detailView.classList.remove("visible");
}
loadHistory();
