/**
 * references.js — Frontend logic for the Reference Extractor page.
 *
 * Handles: Zotero collection browsing, article listing, reference extraction
 * (with polling), result display, CSV export, history, and re-extraction.
 */
(function () {
  "use strict";

  /* ── CSRF helper ── */
  function getCsrf() {
    const c = document.cookie.match(/csrftoken=([^;]+)/);
    return c ? c[1] : "";
  }

  /* ── State ── */
  let libraries = [];
  let currentLibraryId = "";
  let currentLibraryType = "";
  let currentCollectionId = "";
  let articles = [];          // [{path, title, author_display, year, zotero_key, ...}]
  let zoteroLinks = {};
  let zoteroMetadata = {};
  let currentPdfPath = "";
  let currentJobId = "";
  let pollTimer = null;
  let lastResultData = null;   // last loaded extraction result (for view toggling)
  let citationsCache = null;   // fetched citations for the current job
  let inCitationsView = false;

  /* ── DOM refs ── */
  const librarySelect = document.getElementById("library-select");
  const collectionSelect = document.getElementById("collection-select");
  const loadCollectionBtn = document.getElementById("load-collection-btn");
  const browseFolderBtn = document.getElementById("browse-folder-btn");
  const articleLoading = document.getElementById("article-loading");
  const articleListContainer = document.getElementById("article-list-container");
  const articleList = document.getElementById("article-list");
  const articleCount = document.getElementById("article-count");
  const zoteroLoadSteps = document.getElementById("zotero-load-steps");

  const refMain = document.getElementById("ref-main");
  const refEmpty = document.getElementById("ref-empty");
  const extractionProgress = document.getElementById("extraction-progress");
  const extractionError = document.getElementById("extraction-error");
  const extractionErrorMsg = document.getElementById("extraction-error-msg");
  const retryExtractBtn = document.getElementById("retry-extract-btn");
  const refResults = document.getElementById("ref-results");
  const resultTitle = document.getElementById("result-title");
  const resultCount = document.getElementById("result-count");
  const resultOcrBadge = document.getElementById("result-ocr-badge");
  const resultDate = document.getElementById("result-date");
  const refTableBody = document.getElementById("ref-table-body");
  const exportCsvBtn = document.getElementById("export-csv-btn");
  const citationsBtn = document.getElementById("citations-btn");
  const reextractBtn = document.getElementById("reextract-btn");
  const historyList = document.getElementById("ref-history-list");

  // Debug & Log Refs
  const debugContainer = document.getElementById("debug-container");
  const toggleDebugBtn = document.getElementById("toggle-debug-btn");
  const debugContentArea = document.getElementById("debug-content-area");
  const debugLogText = document.getElementById("debug-log-text");
  const debugRawResponse = document.getElementById("debug-raw-response");

  const viewDebugBtn = document.getElementById("view-debug-btn");
  const resultsDebugContainer = document.getElementById("results-debug-container");
  const resultsDebugLogText = document.getElementById("results-debug-log-text");
  const resultsDebugRawResponse = document.getElementById("results-debug-raw-response");

  /* ── Initialization ── */
  loadLibraries();
  setupEventListeners();

  function setupEventListeners() {
    librarySelect.addEventListener("change", onLibraryChange);
    collectionSelect.addEventListener("change", onCollectionChange);
    loadCollectionBtn.addEventListener("click", onLoadCollection);
    exportCsvBtn.addEventListener("click", onExportCsv);
    reextractBtn.addEventListener("click", onReextract);
    retryExtractBtn.addEventListener("click", onReextract);

    if (toggleDebugBtn) {
      toggleDebugBtn.addEventListener("click", () => {
        if (debugContentArea.style.display === "none") {
          debugContentArea.style.display = "block";
          toggleDebugBtn.textContent = "Hide Logs";
        } else {
          debugContentArea.style.display = "none";
          toggleDebugBtn.textContent = "Show Logs";
        }
      });
    }

    if (viewDebugBtn) {
      viewDebugBtn.addEventListener("click", () => {
        if (resultsDebugContainer.style.display === "none") {
          resultsDebugContainer.style.display = "block";
          viewDebugBtn.textContent = "Hide Trace";
        } else {
          resultsDebugContainer.style.display = "none";
          viewDebugBtn.textContent = "🔧 View Trace";
        }
      });
    }

    // History items
    historyList.addEventListener("click", (e) => {
      const deleteBtn = e.target.closest(".ref-history-delete");
      if (deleteBtn) {
        e.stopPropagation();
        onDeleteHistory(deleteBtn.dataset.id);
        return;
      }
      const item = e.target.closest(".ref-history-item");
      if (item) onHistoryClick(item.dataset.id);
    });
  }

  /* ── Zotero Libraries ── */
  async function loadLibraries() {
    try {
      const resp = await fetch("/api/zotero-collections");
      const data = await resp.json();

      if (data.error) {
        librarySelect.innerHTML = `<option value="">⚠ ${data.error}</option>`;
        return;
      }

      libraries = data.libraries || [];
      if (libraries.length === 0) {
        librarySelect.innerHTML = '<option value="">No libraries found</option>';
        return;
      }

      librarySelect.innerHTML = '<option value="">Select a library…</option>';
      libraries.forEach((lib) => {
        const opt = document.createElement("option");
        opt.value = JSON.stringify({ id: lib.id, type: lib.type });
        opt.textContent = lib.name;
        librarySelect.appendChild(opt);
      });
    } catch (err) {
      librarySelect.innerHTML = `<option value="">Error loading libraries</option>`;
      console.error("Failed to load libraries:", err);
    }
  }

  function onLibraryChange() {
    collectionSelect.innerHTML = "";
    collectionSelect.disabled = true;
    loadCollectionBtn.disabled = true;

    if (!librarySelect.value) return;

    const lib = JSON.parse(librarySelect.value);
    currentLibraryId = lib.id;
    currentLibraryType = lib.type;

    const selectedLib = libraries.find(
      (l) => String(l.id) === String(lib.id) && l.type === lib.type
    );

    if (!selectedLib || !selectedLib.collections || selectedLib.collections.length === 0) {
      collectionSelect.innerHTML = '<option value="">No collections found</option>';
      return;
    }

    collectionSelect.innerHTML = '<option value="">Select a collection…</option>';
    // Add "All Items" option
    const allOpt = document.createElement("option");
    allOpt.value = "all";
    allOpt.textContent = "📁 All Items";
    collectionSelect.appendChild(allOpt);

    selectedLib.collections.forEach((col) => {
      const opt = document.createElement("option");
      opt.value = col.id;
      opt.textContent = col.name;
      collectionSelect.appendChild(opt);
    });

    collectionSelect.disabled = false;
  }

  function onCollectionChange() {
    loadCollectionBtn.disabled = !collectionSelect.value;
    currentCollectionId = collectionSelect.value;
  }

  /* ── Load articles from collection ── */
  async function onLoadCollection() {
    if (!collectionSelect.value) return;

    currentCollectionId = collectionSelect.value;
    articles = [];
    articleList.innerHTML = "";
    articleListContainer.style.display = "none";
    zoteroLoadSteps.innerHTML = "";
    zoteroLoadSteps.classList.add("visible");
    articleLoading.classList.remove("visible");

    // Show results panel in empty state
    showPanel("empty");

    const params = new URLSearchParams({
      collection_id: currentCollectionId,
      library_id: currentLibraryId,
      library_type: currentLibraryType,
    });

    try {
      const resp = await fetch(`/api/zotero-pdfs-stream?${params}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const evt = JSON.parse(line.slice(6));
            handleZoteroStep(evt);
            if (evt.step === "complete") {
              onArticlesLoaded(evt);
            }
          } catch (e) { /* skip malformed events */ }
        }
      }
    } catch (err) {
      zoteroLoadSteps.innerHTML += `
        <div class="zl-step error">
          <span class="zl-icon">✗</span>
          <div class="zl-text"><span class="zl-label">Error: ${err.message}</span></div>
        </div>`;
    }
  }

  function handleZoteroStep(evt) {
    const icon = evt.step === "error" ? "✗" :
                 evt.step.endsWith("_done") || evt.step === "complete" ? "✓" : "⟳";
    const cls = evt.step === "error" ? "error" :
                evt.step.endsWith("_done") || evt.step === "complete" ? "done" : "active";

    // Update existing step or add new one
    const existing = zoteroLoadSteps.querySelector(`.zl-step.active`);
    if (existing && cls !== "active") {
      existing.className = `zl-step ${cls}`;
      existing.querySelector(".zl-icon").textContent = icon;
      existing.querySelector(".zl-label").textContent = evt.message;
    }

    if (cls === "active" || evt.step === "error") {
      zoteroLoadSteps.innerHTML += `
        <div class="zl-step ${cls}">
          <span class="zl-icon">${icon}</span>
          <div class="zl-text"><span class="zl-label">${evt.message}</span></div>
        </div>`;
    }

    // Auto-scroll
    zoteroLoadSteps.scrollTop = zoteroLoadSteps.scrollHeight;
  }

  function onArticlesLoaded(evt) {
    zoteroLinks = evt.zotero_links || {};
    zoteroMetadata = evt.zotero_metadata || {};

    const groups = evt.article_groups || [];
    articles = groups.map((g) => {
      const selectedVersion = g.versions.find((v) => v.selected) || g.versions[0];
      return {
        path: selectedVersion.path,
        title: g.title || selectedVersion.path.split("/").pop(),
        author_display: g.author_display || "",
        year: g.year || "",
        parent_key: g.parent_key || "",
      };
    });

    // Sort alphabetically by author, then title
    articles.sort((a, b) => {
      const ak = (a.author_display || a.title).toLowerCase();
      const bk = (b.author_display || b.title).toLowerCase();
      return ak.localeCompare(bk);
    });

    renderArticleList();

    // Hide loading steps, show article list
    setTimeout(() => {
      zoteroLoadSteps.classList.remove("visible");
      zoteroLoadSteps.innerHTML = "";
    }, 800);
  }

  function renderArticleList() {
    articleList.innerHTML = "";
    articleCount.textContent = articles.length;

    articles.forEach((art, idx) => {
      const li = document.createElement("li");
      li.className = "article-item";
      li.dataset.idx = idx;
      li.dataset.path = art.path;

      const authorYear = [art.author_display, art.year].filter(Boolean).join(", ");

      li.innerHTML = `
        <span style="font-size: 14px; flex-shrink: 0;">📄</span>
        <div class="article-info">
          <div class="article-title-text">${escapeHtml(art.title)}</div>
          <div class="article-meta-text">${escapeHtml(authorYear || art.path.split("/").pop())}</div>
        </div>
      `;

      li.addEventListener("click", () => onArticleClick(idx));
      articleList.appendChild(li);
    });

    articleListContainer.style.display = "flex";
  }

  /* ── Browse local folder ── */
  browseFolderBtn.addEventListener("click", async () => {
    browseFolderBtn.disabled = true;
    try {
      const resp = await fetch("/api/browse-folder");
      const data = await resp.json();
      if (!data.path) return;

      currentCollectionId = "";
      articles = data.pdfs.map((rel) => ({
        path: `${data.path.replace(/\/$/, "")}/${rel}`,
        title: rel.split("/").pop(),
        author_display: "",
        year: "",
        parent_key: "",
      }));
      renderArticleList();
    } catch (err) {
      console.error("Browse error:", err);
    } finally {
      browseFolderBtn.disabled = false;
    }
  });

  /* ── Article selection → Extract ── */
  async function onArticleClick(idx) {
    const art = articles[idx];
    if (!art) return;

    currentPdfPath = art.path;

    // Highlight active
    articleList.querySelectorAll(".article-item").forEach((el) => el.classList.remove("active"));
    const li = articleList.querySelector(`[data-idx="${idx}"]`);
    if (li) li.classList.add("active");

    // Start extraction (will check cache server-side)
    await startExtraction(art.path, art.parent_key, false);
  }

  async function startExtraction(pdfPath, zoteroItemKey, forceReextract) {
    showPanel("progress");
    currentJobId = "";

    try {
      const resp = await fetch("/api/references/extract", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrf(),
        },
        body: JSON.stringify({
          pdf_path: pdfPath,
          zotero_item_key: zoteroItemKey || "",
          zotero_collection_key: currentCollectionId || "",
          force_reextract: forceReextract,
        }),
      });

      const data = await resp.json();

      if (data.error) {
        showError(data.error);
        return;
      }

      currentJobId = data.job_id;

      if (data.status === "done" && data.cached) {
        // Load cached result
        await loadResult(data.job_id);
      } else {
        // Poll for completion
        startPolling(data.job_id);
      }
    } catch (err) {
      showError(err.message);
    }
  }

  /* ── Polling ── */
  function startPolling(jobId) {
    stopPolling();
    pollTimer = setInterval(async () => {
      try {
        const resp = await fetch(`/api/references/status/${jobId}`);
        const data = await resp.json();

        if (data.status === "done") {
          stopPolling();
          await loadResult(jobId);
        } else if (data.status === "error") {
          stopPolling();
          showError(data.error || "Extraction failed.", data.debug_log, data.raw_llm_response);
        }
        // else keep polling (processing/pending)
      } catch (err) {
        stopPolling();
        showError("Lost connection to server.");
      }
    }, 2000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  /* ── Load & display results ── */
  async function loadResult(jobId) {
    try {
      const resp = await fetch(`/api/references/result/${jobId}`);
      const data = await resp.json();

      if (data.error) {
        showError(data.error);
        return;
      }

      currentJobId = jobId;
      lastResultData = data;
      citationsCache = null;
      inCitationsView = false;
      if (citationsBtn) {
        citationsBtn.style.display = data.article_doi ? "inline-flex" : "none";
        citationsBtn.textContent = "🔗 Cited By";
        citationsBtn.disabled = false;
      }
      displayResults(data);
      
      // Populate results debug container
      if (resultsDebugLogText) resultsDebugLogText.textContent = data.debug_log || "No logs available.";
      if (resultsDebugRawResponse) resultsDebugRawResponse.textContent = data.raw_llm_response || "No response available.";
      if (resultsDebugContainer) resultsDebugContainer.style.display = "none";
      if (viewDebugBtn) viewDebugBtn.textContent = "🔧 View Trace";
      
      showPanel("results");

      // Update the article list item to show "extracted" badge
      updateArticleBadge(data.pdf_path);

      // Refresh history
      refreshHistory();
    } catch (err) {
      showError(err.message);
    }
  }

  /* ── Cited By (Semantic Scholar forward citations) ── */
  if (citationsBtn) {
    citationsBtn.addEventListener("click", async () => {
      if (inCitationsView) {
        inCitationsView = false;
        citationsBtn.textContent = "🔗 Cited By";
        exportCsvBtn.disabled = false;
        if (lastResultData) displayResults(lastResultData);
        return;
      }
      if (!citationsCache) {
        citationsBtn.disabled = true;
        citationsBtn.textContent = "⏳ Loading…";
        try {
          const resp = await fetch(`/api/references/citations/${currentJobId}`);
          const data = await resp.json();
          if (data.error) {
            alert(data.error);
            return;
          }
          citationsCache = data;
        } catch (err) {
          alert("Failed to load citations: " + err.message);
          return;
        } finally {
          citationsBtn.disabled = false;
          if (!citationsCache) citationsBtn.textContent = "🔗 Cited By";
        }
      }
      inCitationsView = true;
      citationsBtn.textContent = "📚 Back to References";
      exportCsvBtn.disabled = true;
      renderCitations(citationsCache);
    });
  }

  function renderCitations(data) {
    resultCount.textContent = `${data.citation_count} citing papers`;
    refTableBody.innerHTML = "";
    data.citations.forEach((c, i) => {
      const tr = document.createElement("tr");
      let html = "";
      if (c.author) html += `<span style="color:#e2e8f0;">${escapeHtml(c.author)}</span> `;
      if (c.year) html += `<span style="color:#cbd5e1;">(${escapeHtml(c.year)}).</span> `;
      if (c.title) html += `${escapeHtml(c.title)}. `;
      if (c.journal) html += `<span style="font-style:italic;color:#a5b4fc;">${escapeHtml(c.journal)}</span>. `;
      if (c.doi) {
        const doiClean = escapeHtml(c.doi).trim();
        html += `<a href="https://doi.org/${doiClean}" target="_blank" rel="noopener" style="font-size:11px;margin-left:6px;color:#6366f1;text-decoration:underline;">https://doi.org/${doiClean}</a>`;
      }
      tr.innerHTML = `
        <td class="ref-num" style="text-align:center;vertical-align:top;font-weight:bold;color:#818cf8;width:40px;padding:12px 6px;">${i + 1}</td>
        <td class="ref-apa-cell" style="line-height:1.6;padding:12px;text-align:left;vertical-align:top;">${html}</td>`;
      refTableBody.appendChild(tr);
    });
  }

  function displayResults(data) {
    resultTitle.textContent = data.pdf_filename || "Unknown";
    resultCount.textContent = `${data.ref_count} references`;

    // OCR badge
    const method = (data.ocr_method || "").toLowerCase();
    const ocrClass = method === "mistral" ? "mistral" : "pytesseract";
    resultOcrBadge.innerHTML = `<span class="ocr-badge ${ocrClass}">${method.toUpperCase()}</span>`;

    // Date
    if (data.created_at) {
      const d = new Date(data.created_at);
      resultDate.textContent = d.toLocaleDateString("en-US", {
        month: "short", day: "numeric", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    }

    // Table body
    refTableBody.innerHTML = "";
    
    function formatAuthorsAPA(authorStr) {
      if (!authorStr) return "";
      let rawAuthors = authorStr.split(/,\s*|\s+and\s+|\s+&\s+/);
      let parsedAuthors = [];
      
      for (let author of rawAuthors) {
        author = author.trim();
        if (!author) continue;
        
        if (author.includes(",")) {
          let parts = author.split(",");
          let last = parts[0].trim();
          let first = parts[1].trim();
          parsedAuthors.push({ last, first });
        } else {
          let nameParts = author.split(/\s+/);
          if (nameParts.length >= 2) {
            let last = nameParts[nameParts.length - 1];
            let first = nameParts.slice(0, nameParts.length - 1).join(" ");
            parsedAuthors.push({ last, first });
          } else {
            parsedAuthors.push({ last: author, first: "" });
          }
        }
      }
      
      let formattedNames = [];
      for (let i = 0; i < parsedAuthors.length; i++) {
        let { last, first } = parsedAuthors[i];
        let initialsStr = "";
        if (first) {
          let cleanFirst = first.replace(/\./g, " ").replace(/-/g, " ");
          let tokens = cleanFirst.split(/\s+/).filter(t => t.trim());
          let initials = tokens.map(token => token.charAt(0).toUpperCase() + ".");
          initialsStr = initials.join(" ");
        }
        formattedNames.push(initialsStr ? `${last}, ${initialsStr}` : last);
      }
      
      if (formattedNames.length === 0) return "";
      if (formattedNames.length === 1) return formattedNames[0];
      if (formattedNames.length === 2) return `${formattedNames[0]} & ${formattedNames[1]}`;
      
      let joined = formattedNames.slice(0, -1).join(", ");
      return `${joined}, & ${formattedNames[formattedNames.length - 1]}`;
    }

    (data.references || []).forEach((ref, index) => {
      const tr = document.createElement("tr");
      
      // Build standard APA 7th Edition style
      let apaHtml = "";
      
      // 1. Author (APA formatting: Sahu, A. K., Padhy, R. K., & Dhir, A.)
      if (ref.author) {
        let formattedAuthors = formatAuthorsAPA(ref.author);
        if (formattedAuthors) {
          if (!formattedAuthors.endsWith(".")) formattedAuthors += ".";
          apaHtml += `<span class="ref-author" style="font-weight: 500; color: #f8fafc;">${formattedAuthors}</span> `;
        }
      }
      
      // 2. Year (APA format: in parentheses)
      if (ref.year) {
        apaHtml += `<span class="ref-year" style="color: #cbd5e1;">(${escapeHtml(ref.year)}).</span> `;
      }
      
      // 3. Title (Sentence format, ends with period, no quotes)
      if (ref.title) {
        let title = escapeHtml(ref.title).trim();
        if (title.substring(title.length - 1) !== ".") {
          title += ".";
        }
        apaHtml += `<span class="ref-title" style="color: #e2e8f0;">${title}</span> `;
      }
      
      // 4. Source (APA: Journal Name, Volume(Issue), Pages.)
      let sourceHtml = "";
      if (ref.journal) {
        sourceHtml += `<span class="ref-journal" style="font-style: italic; color: #a5b4fc;">${escapeHtml(ref.journal)}</span>`;
      }
      
      let volIss = "";
      if (ref.volume && ref.issue) {
        volIss = `<span class="ref-volume" style="font-style: italic; color: #a5b4fc;">${escapeHtml(ref.volume)}</span>(${escapeHtml(ref.issue)})`;
      } else if (ref.volume) {
        volIss = `<span class="ref-volume" style="font-style: italic; color: #a5b4fc;">${escapeHtml(ref.volume)}</span>`;
      } else if (ref.issue) {
        volIss = `(${escapeHtml(ref.issue)})`;
      }
      
      if (volIss) {
        if (ref.journal) {
          sourceHtml += `, ${volIss}`;
        } else {
          sourceHtml += volIss;
        }
      }
      
      if (ref.pages) {
        let p = escapeHtml(ref.pages).replace(/-/g, "–");
        if (sourceHtml) {
          sourceHtml += `, ${p}`;
        } else {
          sourceHtml += p;
        }
      }
      
      if (sourceHtml) {
        apaHtml += `<span class="ref-source">${sourceHtml}.</span> `;
      }
      
      // 5. DOI
      if (ref.doi) {
        let doiClean = escapeHtml(ref.doi).trim();
        let doiLink = doiClean.startsWith("http") ? doiClean : `https://doi.org/${doiClean}`;
        apaHtml += `<a class="ref-doi-link" href="${doiLink}" target="_blank" rel="noopener" style="font-size: 11px; margin-left: 6px; color: #6366f1; text-decoration: underline;">https://doi.org/${doiClean}</a>`;
      }

      tr.innerHTML = `
        <td class="ref-num" style="text-align: center; vertical-align: top; font-weight: bold; color: #818cf8; width: 40px; padding: 12px 6px;">${index + 1}</td>
        <td class="ref-apa-cell" style="line-height: 1.6; padding: 12px; text-align: left; vertical-align: top;">${apaHtml}</td>
      `;
      refTableBody.appendChild(tr);
    });
  }

  function updateArticleBadge(pdfPath) {
    const basename = pdfPath.split("/").pop();
    articleList.querySelectorAll(".article-item").forEach((li) => {
      const liPath = li.dataset.path || "";
      if (liPath.endsWith(basename) || pdfPath.endsWith(liPath)) {
        // Remove existing badge if any
        const existing = li.querySelector(".article-status");
        if (existing) existing.remove();
        // Add badge
        const badge = document.createElement("span");
        badge.className = "article-status extracted";
        badge.textContent = "✓ Extracted";
        li.appendChild(badge);
      }
    });
  }

  /* ── Panel visibility ── */
  function showPanel(which) {
    refEmpty.style.display = which === "empty" ? "flex" : "none";
    extractionProgress.classList.toggle("visible", which === "progress");
    extractionError.classList.toggle("visible", which === "error");
    refResults.style.display = which === "results" ? "block" : "none";
  }

  function showError(msg, debugLog, rawLlmResponse) {
    extractionErrorMsg.innerHTML = `<div style="font-weight: bold; margin-bottom: 8px;">${escapeHtml(msg)}</div>`;
    
    if (debugContainer) {
      if (debugLog || rawLlmResponse) {
        debugContainer.style.display = "block";
        if (debugLogText) debugLogText.textContent = debugLog || "No logs available.";
        if (debugRawResponse) debugRawResponse.textContent = rawLlmResponse || "No response available.";
      } else {
        debugContainer.style.display = "none";
      }
      
      // Reset toggle states
      if (debugContentArea) debugContentArea.style.display = "none";
      if (toggleDebugBtn) toggleDebugBtn.textContent = "Show Logs";
    }
    
    showPanel("error");
  }

  /* ── Export ── */
  function onExportCsv() {
    if (!currentJobId) return;
    window.open(`/api/references/export/${currentJobId}`, "_blank");
  }

  /* ── Re-extract ── */
  async function onReextract() {
    if (!currentPdfPath) return;
    const art = articles.find((a) => a.path === currentPdfPath);
    await startExtraction(currentPdfPath, art ? art.parent_key : "", true);
  }

  /* ── History ── */
  async function onHistoryClick(jobId) {
    // Highlight
    historyList.querySelectorAll(".ref-history-item").forEach((el) => el.classList.remove("active"));
    const item = historyList.querySelector(`[data-id="${jobId}"]`);
    if (item) item.classList.add("active");

    await loadResult(jobId);
  }

  async function onDeleteHistory(jobId) {
    if (!confirm("Delete this extraction?")) return;

    try {
      await fetch(`/api/references/${jobId}/delete`, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrf() },
      });
      refreshHistory();

      // If we were viewing this job, go back to empty
      if (currentJobId === jobId) {
        currentJobId = "";
        showPanel("empty");
      }
    } catch (err) {
      console.error("Delete failed:", err);
    }
  }

  async function refreshHistory() {
    try {
      const resp = await fetch("/api/references/history");
      const data = await resp.json();

      historyList.innerHTML = "";
      data.filter((j) => j.status === "done").forEach((j) => {
        const d = new Date(j.created_at);
        const dateStr = d.toLocaleDateString("en-US", {
          month: "short", day: "numeric", year: "numeric",
          hour: "2-digit", minute: "2-digit",
        });

        const li = document.createElement("li");
        li.className = `ref-history-item${j.id === currentJobId ? " active" : ""}`;
        li.dataset.id = j.id;
        li.innerHTML = `
          <div style="display: flex; justify-content: space-between; align-items: flex-start;">
            <span class="ref-history-date">${dateStr}</span>
            <button class="ref-history-delete" data-id="${j.id}" title="Delete">✕</button>
          </div>
          <div class="ref-history-name">${escapeHtml(j.pdf_filename)}</div>
          <div class="ref-history-count">${j.ref_count} references</div>
        `;
        historyList.appendChild(li);
      });
    } catch (err) {
      console.error("Failed to refresh history:", err);
    }
  }

  /* ── Utility ── */
  function escapeHtml(text) {
    if (!text) return "";
    const div = document.createElement("div");
    div.textContent = String(text);
    return div.innerHTML;
  }

})();
