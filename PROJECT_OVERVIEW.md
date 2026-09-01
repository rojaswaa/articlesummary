# Article Summary V3 — Project Overview

> **Purpose:** A Django-based web application that automates **systematic literature review** by batch-analysing PDF academic articles with AI, extracting structured metadata, assessing alignment against user-defined research criteria, and optionally syncing results back to Zotero. It also provides a dedicated **Reference Extractor** that parses the bibliography section of individual articles into structured, exportable reference lists, and an **Article Search** module that discovers articles across seven scholarly APIs (Crossref, OpenAlex, Semantic Scholar, Europe PMC, arXiv, CORE, Springer Nature), harmonizes and de-duplicates the results, and evaluates each against research criteria with the LLM.

---

## Table of Contents

1. [High-Level Architecture](#high-level-architecture)
2. [Technology Stack](#technology-stack)
3. [Directory Structure](#directory-structure)
4. [Data Models](#data-models)
5. [Core Processing Pipeline](#core-processing-pipeline)
6. [Reference Extraction Pipeline](#reference-extraction-pipeline)
6b. [Article Search Module](#article-search-module)
7. [Module Breakdown](#module-breakdown)
8. [URL Routes & API Endpoints](#url-routes--api-endpoints)
9. [Frontend Architecture](#frontend-architecture)
10. [AI Provider Integration](#ai-provider-integration)
11. [OCR Provider Integration](#ocr-provider-integration)
12. [Zotero Integration](#zotero-integration)
13. [Crossref Integration](#crossref-integration)
14. [Configuration & Environment](#configuration--environment)
15. [Management Commands](#management-commands)
16. [Authentication & Security](#authentication--security)
17. [Logging](#logging)
18. [Test Suite](#test-suite)
19. [How to Run](#how-to-run)
20. [Key Design Decisions](#key-design-decisions)

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (SPA-like)                       │
│   index.html · settings.html · references.html · login/signup  │
│   main.js · references.js · pdf-viewer.js · style.css           │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP / SSE
┌────────────────────────────▼────────────────────────────────────┐
│                      Django 6.0 Backend                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐   │
│  │  Views   │  │ Analyzer │  │Extractor │  │   Crossref    │   │
│  │(auth,    │  │(LLM call)│  │(OCR/PDF) │  │(DOI lookup)   │   │
│  │analysis, │  │          │  │          │  │               │   │
│  │admin,    │  │          │  │          │  │               │   │
│  │zotero,   │  │          │  │          │  │               │   │
│  │reference)│  │          │  │          │  │               │   │
│  └──────────┘  └──────────┘  └──────────┘  └───────────────┘   │
│                        │                                        │
│              ┌─────────▼─────────┐                              │
│              │   SQLite (DB)     │                              │
│              │  User, Profile,   │                              │
│              │  Session, PDFJob, │                              │
│              │  RefExtractionJob,│                              │
│              │  ExtractedRef     │                              │
│              └───────────────────┘                              │
└─────────────────────────────────────────────────────────────────┘
         │              │              │               │
    ┌────▼───┐   ┌──────▼─────┐  ┌────▼────┐   ┌──────▼──────┐
    │ Ollama │   │ LM Studio  │  │ Gemini  │   │ Mistral OCR │
    │llama-  │   │            │  │  API    │   │    API      │
    │server  │   │            │  │         │   │             │
    └────────┘   └────────────┘  └─────────┘   └─────────────┘
         │              │              │               │
    ┌────▼──────────────▼──────────────▼───────────────▼──┐
    │              Zotero (local storage + API)            │
    └─────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Layer        | Technology                                                       |
| ------------ | ---------------------------------------------------------------- |
| **Framework** | Django 6.0 (Python)                                             |
| **Database**  | SQLite3                                                         |
| **Frontend**  | Vanilla HTML/CSS/JS (server-rendered templates + AJAX/SSE)       |
| **AI / LLM**  | OpenAI-compatible API (Ollama, LM Studio, llama-server), Gemini |
| **OCR**       | Mistral OCR API, macOS Vision (`ocrmac`), Tesseract              |
| **PDF**       | PyMuPDF (`fitz`), Pillow                                        |
| **Bibliography** | Crossref REST API, `pybtex` (APA7 formatting), `pyzotero`   |
| **Config**    | `python-dotenv` (`.env` file)                                   |

---

## Directory Structure

```
articleSummaryV3/
├── manage.py                          # Django management entry point
├── requirements.txt                   # Python dependencies
├── .env                               # Environment variables (secrets, provider config)
├── .gitignore
├── db.sqlite3                         # SQLite database
├── app.log                            # Rotating log file
│
├── article_summary/                   # Django PROJECT package
│   ├── settings.py                    # Settings (DB, logging, installed apps, etc.)
│   ├── urls.py                        # Root URL conf → includes app URLs
│   ├── wsgi.py                        # WSGI entry + LM Studio cleanup on shutdown
│   └── asgi.py
│
├── article_summary_app/               # Django APP package (all business logic)
│   ├── apps.py                        # AppConfig — startup checks (tesseract, Zotero, API keys)
│   ├── models.py                      # Profile, Session, PDFJob, ReferenceExtractionJob, ExtractedReference, ArticleSearch, SearchResultArticle
│   ├── urls.py                        # All URL patterns (pages + API)
│   ├── analyzer.py                    # LLM prompt building, response parsing, provider dispatch
│   ├── extractor.py                   # PDF text extraction (OCR) + metadata + pipeline orchestration
│   ├── crossref.py                    # DOI extraction, Crossref queries, APA formatting, enrichment
│   ├── ref_extractor.py               # Reference extraction: OCR → local heuristic parser (non-LLM)
│   ├── search_providers.py            # Article Search: per-source API clients, query dialects, filters
│   ├── search.py                      # Article Search: fan-out, harmonize/dedupe, evaluation
│   ├── semantic_scholar.py            # Semantic Scholar client (references, citations, bulk search)
│   ├── tests.py                       # Unit tests
│   │
│   ├── views/                         # View modules
│   │   ├── __init__.py                # Re-exports all views for urls.py
│   │   ├── common.py                  # Shared helpers (profile lookup, PDF path security)
│   │   ├── auth.py                    # signup, login, logout
│   │   ├── analysis.py                # Main analysis pipeline, sessions, folder browsing, export
│   │   ├── admin.py                   # Settings page, AI config saving, Ollama/LM Studio mgmt
│   │   ├── zotero.py                  # Zotero collection loading, saving aligned articles
│   │   ├── references.py             # Reference extraction: extract, status, results, export, history
│   │   └── search.py                 # Article Search: start/stop, status/SSE, results, export, history
│   │
│   ├── templatetags/
│   │   └── json_extras.py             # |jsonify and |split_path_last filters
│   │
│   └── management/commands/           # CLI management commands for Zotero maintenance
│       ├── dedup_collection.py
│       ├── export_missing_items.py
│       ├── remap_from_collection.py
│       ├── remap_zotero_links.py
│       └── report_skipped.py
│
├── templates/                         # Django HTML templates
│   ├── index.html                     # Main dashboard (analysis UI)
│   ├── search.html                    # Article Search page
│   ├── references.html                # Reference Extractor page
│   ├── settings.html                  # Provider/model configuration
│   ├── login.html
│   └── signup.html
│
├── static/
│   ├── css/style.css                  # Application stylesheet
│   └── js/
│       ├── main.js                    # Core frontend logic (42 KB) — analysis page
│       ├── search.js                  # Article Search frontend logic
│       ├── references.js              # Reference Extractor frontend logic
│       └── pdf-viewer.js             # In-browser PDF preview
│
└── staticfiles/                       # Collected static files (generated by collectstatic)
```

---

## Data Models

Defined in [`models.py`](article_summary_app/models.py):

### `Profile` (one-to-one with `User`)

Stores per-user configuration for AI/OCR providers and Zotero credentials. Auto-created via a `post_save` signal on `User`.

| Field Group          | Fields                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------- |
| **AI Provider**      | `ai_provider`, `ollama_base_url`, `ollama_model`, `lmstudio_base_url`, `lmstudio_model`, `lmstudio_model_arch`, `lmstudio_max_context`, `lmstudio_context_length`, `lmstudio_eval_batch_size`, `lmstudio_flash_attention`, `lmstudio_keep_model_in_memory`, `lmstudio_try_mmap`, `lmstudio_num_experts`, `lmstudio_llama_k_cache_quant_type`, `lmstudio_llama_v_cache_quant_type`, `gemini_api_key`, `gemini_model`, `llama_server_base_url`, `llama_server_model` |
| **OCR**              | `ocr_provider`, `mistral_api_key`                                                            |
| **Generation**       | `reasoning`, `temperature`, `max_tokens`                                                     |
| **Zotero**           | `zotero_user_id`, `zotero_api_key`, `zotero_library_type`, `zotero_api_mode`                 |

### `Session`

Represents a single analysis run — a batch of PDFs from a folder analysed against a set of research criteria.

| Field              | Purpose                                                       |
| ------------------ | ------------------------------------------------------------- |
| `id`               | UUID primary key                                              |
| `name`             | User-defined session label                                    |
| `user`             | FK → `User`                                                   |
| `folder`           | Filesystem path to the PDF folder                             |
| `criteria`         | Research criteria text the PDFs are evaluated against          |
| `zotero_links`     | JSONField — `{pdf_path: zotero_link_url}`                     |
| `zotero_metadata`  | JSONField — `{pdf_path: {title, author_sort, year, …}}`       |
| `is_cancelled`     | Boolean flag to stop in-progress analysis                     |
| `load_progress`    | Integer 0–100, tracks model loading (used by LM Studio)       |
| `created_at`       | Auto timestamp                                                |

### `PDFJob`

One job per PDF within a session. Tracks processing status and stores the AI result.

| Field       | Purpose                                                            |
| ----------- | ------------------------------------------------------------------ |
| `session`   | FK → `Session`                                                     |
| `pdf_path`  | Relative path to the PDF within the session folder                 |
| `status`    | `pending` → `processing` → `done` / `error` / `cancelled`         |
| `result`    | JSONField — contains `fields` dict (18 structured fields) + `ocr_method`, `doi`, `abstract_source` |
| `error`     | Error message text (if status is `error`)                          |

### `ReferenceExtractionJob`

Represents a single reference extraction run for one PDF article.

| Field                  | Purpose                                                          |
| ---------------------- | ---------------------------------------------------------------- |
| `id`                   | UUID primary key                                                 |
| `user`                 | FK → `User`                                                      |
| `pdf_path`             | Full filesystem path to the PDF                                  |
| `zotero_item_key`      | Zotero item key for linking back to the source article           |
| `zotero_collection_key`| Zotero collection the article was loaded from                    |
| `status`               | `pending` → `processing` → `done` / `error`                     |
| `error`                | Error message text (if status is `error`)                        |
| `ocr_method`           | Which OCR provider was used (`mistral`, `macocr`, `pytesseract`) |
| `debug_log`            | Execution log capturing parameters, warnings, steps, and tracebacks |
| `raw_llm_response`     | The unparsed raw text response returned by the LLM client        |
| `created_at`           | Auto timestamp                                                   |
| `updated_at`           | Auto-updated timestamp                                           |

Indexed on `(user, pdf_path)` for fast cache lookups.

### `ExtractedReference`

One row per parsed reference within a `ReferenceExtractionJob`.

| Field       | Purpose                                              |
| ----------- | ---------------------------------------------------- |
| `job`       | FK → `ReferenceExtractionJob`                        |
| `order`     | Integer preserving the order from the bibliography   |
| `raw_text`  | Original reference text as it appeared in the document |
| `author`    | Author name(s)                                       |
| `title`     | Title of the referenced work                         |
| `year`      | Publication year                                     |
| `journal`   | Journal, book, conference, or other source name      |
| `volume`    | Volume number                                        |
| `issue`     | Issue number                                         |
| `pages`     | Page range                                           |
| `doi`       | DOI identifier (if present)                          |

### `ArticleSearch`

A multi-source article search run: a query evaluated against criteria across several scholarly APIs.

| Field                    | Purpose                                                              |
| ------------------------ | ------------------------------------------------------------------- |
| `id`                     | UUID primary key                                                    |
| `user`                   | FK → `User`                                                         |
| `name`                   | Optional user label                                                 |
| `query`                  | Search query (boolean syntax allowed; adapted per API)              |
| `criteria`               | Research criteria each result is LLM-evaluated against              |
| `sources`                | JSON list of provider keys queried                                  |
| `year_from` / `year_to`  | Optional publication-year range (API-level filter)                  |
| `filters`                | JSON — `scope`, `journal_only`, `has_abstract`, `full_text` toggles |
| `status`                 | `pending` → `searching` → `searched` → `evaluating` ⇄ `paused` → `done` / `error` / `cancelled` |
| `is_cancelled`           | Cancellation flag checked mid-run                                   |
| `is_paused`              | Pause flag the evaluation loop checks between chunks                |
| `heartbeat`              | Last tick of a live worker; a stale one means the run was orphaned by a restart |
| `progress`               | JSON — phase + per-provider fetch counts + fetched/unique/evaluated/aligned |
| `debug_log`              | Timestamped trace of the run (surfaced in the UI Run Log panel)     |
| `created_at`             | Auto timestamp                                                      |

### `SearchResultArticle`

One harmonized article within a search, plus its criteria evaluation.

| Field         | Purpose                                                          |
| ------------- | ---------------------------------------------------------------- |
| `search`      | FK → `ArticleSearch`                                             |
| `title`, `authors`, `year`, `doi`, `abstract`, `venue`, `url` | Harmonized metadata |
| `sources`     | JSON list of APIs this article was found in (merged on dedup)    |
| `status`      | `pending` → `processing` → `done` / `error` / `cancelled`        |
| `evaluation`  | JSON — analyzer fields incl. `aligns_with_criteria` + `alignment_reason` |
| `error`       | Error text if evaluation failed                                  |

> `Profile` also gained `core_api_key`, `springer_api_key`, and `semantic_scholar_api_key` for the search sources that require keys.

---

## Core Processing Pipeline

The analysis of a single PDF follows this multi-step pipeline, orchestrated in [`extractor.py → analyze_pdf()`](article_summary_app/extractor.py#L122-L182):

```
PDF file on disk
       │
       ▼
┌─────────────────────────┐
│ Step 1: Extract Metadata│  PyMuPDF reads title, author, dates,
│         (fitz)          │  keywords from PDF metadata dict
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 2: OCR / Extract   │  Full text extraction via:
│         Text            │    • Mistral OCR API (default)
│                         │    • macOS Vision (ocrmac)
│                         │    • Tesseract (fallback)
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 3: Extract DOI     │  Regex search in metadata + OCR text
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 4: Query Crossref  │  If DOI found → fetch structured
│         API             │  bibliographic data (title, authors,
│                         │  year, abstract, journal, APA ref)
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 5: Extract Abstract│  Try Crossref abstract first,
│                         │  then regex on PDF text,
│                         │  else AI generates one
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 6: LLM Analysis    │  Send OCR text + metadata + criteria
│                         │  to AI provider → get structured
│                         │  JSON with 18 fields
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 7: Crossref        │  Override author, DOI, APA ref,
│         Enrichment      │  and weak LLM fields with
│                         │  authoritative Crossref data
└────────────┬────────────┘
             ▼
     Structured result dict
     stored in PDFJob.result
```

### The 18 Extracted Fields

These are the structured fields the AI extracts for each article:

| Field                              | Description                                          |
| ---------------------------------- | ---------------------------------------------------- |
| `year`                             | Publication year                                     |
| `authors_year`                     | APA in-text citation (e.g., "Smith & Jones, 2023")   |
| `author`                           | Full author names                                    |
| `title`                            | Document title                                       |
| `doi`                              | DOI identifier                                       |
| `apa_reference`                    | Full APA 7th edition reference (enriched via Crossref)|
| `resource_type`                    | Journal Article, Conference Paper, etc.              |
| `abstract`                         | Abstract text                                        |
| `abstract_source`                  | `extracted`, `crossref`, or `ai_generated`           |
| `keywords`                         | Comma-separated topic list                           |
| `location`                         | Country of study                                     |
| `purpose_objectives`               | Purpose statement (3–5 sentences)                    |
| `research_questions`               | Formatted as RQ1, RQ2, etc.                          |
| `survey_interview_focus_questions` | Instrument questions (if applicable)                 |
| `sample`                           | Sample description                                   |
| `design`                           | Quantitative, Qualitative, Mixed, etc.               |
| `main_findings`                    | Summarised findings (5–7 sentences)                  |
| `aligns_with_criteria`             | Boolean — does the paper match the research criteria? |
| `alignment_reason`                 | Explanation of alignment decision                    |

---

## Reference Extraction Pipeline

The reference extraction feature provides a separate, faster, non-LLM pipeline for extracting the bibliography section from a single article. Orchestrated in [`ref_extractor.py → extract_references()`](article_summary_app/ref_extractor.py):

```
PDF file from Zotero
       │
       ▼
┌─────────────────────────┐
│ Step 1: OCR / Extract   │  Full text extraction via the
│         Text            │  user's configured OCR provider
│                         │  (same as analysis pipeline)
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 2: Locate & Group  │  Locates the bibliography heading and
│         Bibliography    │  aggregates multi-line citation lists
│                         │  into raw entries locally
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 3: Heuristic Parse │  Regex engine splits out Authors,
│                         │  Year, Title, Journal details (Volume,
│                         │  Issue, Pages, DOIs) on the fly
└────────────┬────────────┘
             ▼
     List of ExtractedReference
     objects saved to database
```

### The 9 Reference Fields

| Field       | Description                                                |
| ----------- | ---------------------------------------------------------- |
| `raw_text`  | Complete original reference text as it appears in the paper |
| `author`    | Author name(s) as listed                                   |
| `title`     | Title of the referenced work                               |
| `year`      | Publication year (4 digits)                                |
| `journal`   | Journal name, book title, conference, or other source      |
| `volume`    | Volume number                                              |
| `issue`     | Issue number                                               |
| `pages`     | Page range                                                 |
| `doi`       | DOI if present in the reference                            |

### Key Differences from the Analysis Pipeline

| Aspect              | Analysis Pipeline                    | Reference Extraction                 |
| ------------------- | ------------------------------------ | ------------------------------------ |
| **Scope**           | Batch (many PDFs per session)        | Single article at a time             |
| **LLM output**      | 18 metadata fields about the article | N reference entries from bibliography|
| **Crossref**        | Used for enrichment                  | Not used (raw extraction only)       |
| **Caching**         | Per-session PDFJob results           | Per-PDF, reusable across sessions    |
| **Timeout**         | 180 seconds per PDF                  | 300 seconds (bibliographies can be long) |
| **Max tokens**      | User-configured (default 2048)       | Minimum 4096 (overridden if user's setting is lower) |

### Detailed Extraction Debugging & Trace Logging

To diagnose why certain files fail to extract bibliography references, the system implements a thorough debugging system:
- **Trace Logs:** Granular logging captures parameters sent to the LLM, raw response characters, JSON parsing warnings, character snippet dumps on decodes, and retry status logs.
- **Python Stack Trace:** Any fatal errors or exceptions during OCR, prompt-building, or LLM generation capture the full traceback logs.
- **Raw Outputs:** Stores the raw unparsed response text returned by the model, enabling direct diagnosis of truncated tokens or formatting wrapping (like markdown fences).
- **Database Persistence:** Stores trace logs (`debug_log`) and raw outputs (`raw_llm_response`) directly in the `ReferenceExtractionJob` database record.
- **Frontend Panel:** Displays a collapsible "🔧 Extraction Debug Logs & Trace" widget in both the success view (via a "View Trace" button) and error view (which renders automatically on failure) for instant inspection.

---

## Article Search Module

A third pipeline (page: `/search/`) that discovers articles across multiple scholarly APIs, harmonizes the results, and evaluates each against user-defined criteria with the LLM. Distinct from the analysis pipeline (which starts from PDFs on disk) — here the corpus is *discovered* from the web.

```
Query + Criteria + Sources + Filters
       │
       ▼
┌─────────────────────────┐
│ Step 1: Fan-out fetch   │  Query selected APIs concurrently
│  (search_providers.py)  │  (ThreadPoolExecutor, one per source)
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 2: Harmonize +     │  Common record schema; dedupe by DOI,
│  dedupe (search.py)     │  then normalized title+year; merge sources
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Step 3: Evaluate        │  Each unique article's title+abstract +
│  (reuses analyzer)      │  criteria → LLM → aligns + reason
└────────────┬────────────┘
             ▼
   SearchResultArticle rows (paginated in the UI)
```

### Sources (7)

| Source            | Key? | Query dialect | Notes                                             |
| ----------------- | ---- | ------------- | ------------------------------------------------- |
| **Crossref**      | no   | plain         | Relevance-ranked full-text                        |
| **OpenAlex**      | no   | plain         | Meters its anonymous pool (429 when exhausted)    |
| **Semantic Scholar** | optional | symbol (`+`/`|`) | Bulk search endpoint; key raises rate limit   |
| **Europe PMC**    | no   | boolean       | Full boolean (`AND`/`OR`/`NOT`, wildcards, fields) |
| **arXiv**         | no   | plain         | Atom XML; preprints; year filtered in-provider    |
| **CORE**          | yes  | boolean       | POST endpoint; slow (120s timeout + retry)        |
| **Springer Nature** | yes | plain        | Open Access API; free tier: `p=25`, `s≤100`       |

### Query Harmonization

Boolean queries aren't universal. `normalize_query(query, dialect)` adapts per API: **boolean** engines (Europe PMC, CORE) get the query verbatim; **Semantic Scholar** gets symbol operators (`AND`→`+`, `OR`→`|`); **plain** full-text engines (OpenAlex, Crossref, Springer, arXiv) get operators/parens/wildcards stripped to a bag of terms (they otherwise 400 on wildcards-in-quotes).

### Filters (all applied API-level, with post-fetch guards)

| Filter               | Effect                                                        |
| -------------------- | ------------------------------------------------------------- |
| **Year From / To**   | Publication-year range                                        |
| **Title/abstract**   | Match terms in title/abstract only, not full text             |
| **Journal articles** | Drop books/datasets/preprints — the practical peer-reviewed proxy |
| **Has abstract**     | Only records with an abstract (also skips un-evaluatable ones) |
| **Full text**        | Only records with available full text                         |

No API exposes a true "peer-reviewed" flag; "journal articles only" (excluding preprint sources) is the honest proxy.

### Progress & Debugging

A background daemon thread runs the pipeline. `ArticleSearch.progress` (per-provider fetch counts + phase) and `debug_log` (timestamped trace) are persisted and streamed to the UI via SSE — the page shows per-provider progress chips and a collapsible Run Log. Every step also logs to `app.log` with a `[search <id>]` prefix. Status ticks use cheap aggregate counts, and results are `bulk_create`d, so large result sets stay responsive.

### Two Phases: Search then Evaluate

Search and evaluation are decoupled. `search_start` runs **phase 1** only (fetch + harmonize + store) and stops at `searched`. **Phase 2** (evaluation) is user-triggered and:

- **Resumable** — only `pending` articles are evaluated, so re-running continues where it left off.
- **Pausable** — the loop checks the pause flag between chunks (chunk size = worker count), so pausing takes effect after the current article.
- **Restartable** — `restart-eval` resets every article to `pending` and re-evaluates from scratch.

### Crash / Restart Recovery

Workers are process-bound daemon threads, so a server restart orphans in-flight runs. Two mechanisms recover them: a **startup reclaim** (`AppConfig.ready`) resets orphaned `evaluating` → `paused` and `searching` → `error`; and a **heartbeat** (ticked every 5s while a worker lives) lets the running app detect a dead run without a restart — the status endpoint reports a stale `evaluating` as `paused` so the UI self-heals to a Resume button. SQLite runs in WAL mode with a busy timeout so the concurrent worker/heartbeat writes don't collide.

### Concurrency & Bounding

Sources are fetched in parallel; providers paginate to exhaustion (unlimited) with a `cancel` check each page. Evaluation reuses the analyzer's concurrency (local = 1 worker, Gemini = 3), each article under a 180s timeout. Results are paginated server-side (50/page).

---

## Module Breakdown

### [`analyzer.py`](article_summary_app/analyzer.py)

- **`_build_prompt()`** — Constructs the LLM prompt with metadata, abstract, Crossref context, and full text (capped at 12,000 chars). Includes strict JSON-output instructions.
- **`_parse_response()`** — Robust JSON parser that strips `<think>` tags, markdown fences, fixes bad escapes, and coerces booleans.
- **`extract_fields()`** — Dispatches the prompt to the configured AI provider (Ollama/LM Studio/Gemini/llama-server).
- **`get_available_models()`** — Queries provider APIs to list available models.
- **`check_connectivity()`** — Pings all configured providers to check availability.

### [`extractor.py`](article_summary_app/extractor.py)

- **`extract_metadata()`** — Reads PDF metadata via PyMuPDF.
- **`extract_text()`** — Routes to the configured OCR provider (Mistral → macOS Vision → Tesseract fallback chain).
- **`analyze_pdf()`** — Full pipeline orchestrator (the 7 steps described above).

### [`crossref.py`](article_summary_app/crossref.py)

- **`extract_doi_from_text()` / `extract_doi_from_metadata()`** — DOI extraction from OCR text and PDF metadata.
- **`query_crossref()`** — Fetches structured data from the Crossref REST API.
- **`format_apa_reference()`** — Generates APA 7th edition references using `pybtex`, with a manual fallback formatter.
- **`enrich_fields_with_crossref()`** — Overrides weak LLM-extracted fields (author, DOI, APA, title) with authoritative Crossref data.

### [`ref_extractor.py`](article_summary_app/ref_extractor.py)

- **`_extract_journal_details()`** — Heuristically extracts volume, issue, and page numbers from a raw journal source string.
- **`parse_rule_based_references()`** — Locates reference headings, filters list formatting, resolves boundary lines, and performs lookbehind regex field splitting.
- **`extract_references()`** — Full pipeline: OCR → Local Heuristic Parsing (no LLM required). Reuses `extract_text()` from `extractor.py`.

### Views

| Module                 | Responsibility                                                       |
| ---------------------- | -------------------------------------------------------------------- |
| [`common.py`](article_summary_app/views/common.py)    | Profile helper, PDF path security (`resolve_allowed_pdf`), session folder tracking |
| [`auth.py`](article_summary_app/views/auth.py)      | Signup, login, logout                                                 |
| [`analysis.py`](article_summary_app/views/analysis.py)  | Dashboard, folder picker, analysis start/stop/retry/export, SSE streams, session CRUD, PDF serving |
| [`admin.py`](article_summary_app/views/admin.py)     | Settings page, save AI/OCR/Zotero config, start/stop Ollama & LM Studio |
| [`zotero.py`](article_summary_app/views/zotero.py)    | Load Zotero collections/PDFs, save aligned articles to Zotero collections |
| [`references.py`](article_summary_app/views/references.py) | Reference Extractor: extract, poll status, fetch results, CSV export, history, delete |
| [`search.py`](article_summary_app/views/search.py)   | Article Search: start/stop, status (JSON + SSE), paginated results, CSV export, history, delete |

Two non-view modules back the search page: [`search_providers.py`](article_summary_app/search_providers.py) (per-source API clients, query-dialect normalization, filter application) and [`search.py`](article_summary_app/search.py) (concurrent fan-out, harmonize/dedupe, per-article evaluation).

---

## URL Routes & API Endpoints

### Pages

| URL             | View                | Purpose                         |
| --------------- | ------------------- | ------------------------------- |
| `/`             | `index`             | Main dashboard (article analysis) |
| `/search/`      | `search_index`      | Article Search page             |
| `/references/`  | `references_index`  | Reference Extractor page        |
| `/login/`       | `user_login`        | Login page                      |
| `/signup/`      | `user_signup`       | Registration page               |
| `/logout/`      | `user_logout`       | Logout action                   |
| `/settings/`    | `admin`             | Provider & model configuration  |

### API Endpoints

| Method | URL                                           | Purpose                                |
| ------ | --------------------------------------------- | -------------------------------------- |
| GET    | `/api/browse-folder`                          | macOS native folder picker (osascript) |
| GET    | `/api/zotero-collections`                     | List Zotero libraries & collections    |
| GET    | `/api/zotero-pdfs`                            | Load PDFs from a Zotero collection     |
| GET    | `/api/zotero-pdfs-stream`                     | SSE stream of Zotero loading progress  |
| POST   | `/api/metadata`                               | Extract PDF metadata                   |
| POST   | `/api/extract`                                | OCR extract text from a PDF            |
| POST   | `/api/analyze-start`                          | Start a batch analysis session         |
| GET    | `/api/analyze-status/<session_id>`            | Poll job statuses (JSON)               |
| GET    | `/api/analyze-status-stream/<session_id>`     | SSE stream of job statuses             |
| GET    | `/api/analyze-progress/<session_id>`          | SSE stream of model loading progress   |
| GET    | `/api/analyze-result/<session_id>/<pdf_path>` | Get result for a specific PDF          |
| POST   | `/api/analyze-stop`                           | Cancel a running analysis              |
| POST   | `/api/analyze-retry/<session_id>`             | Retry failed/cancelled jobs            |
| GET    | `/api/analyze-export/<session_id>`            | Export aligned results as CSV          |
| POST   | `/api/analyze-zotero-save/<session_id>`       | Save aligned articles to Zotero        |
| GET    | `/api/analyze-zotero-save-stream/<session_id>`| SSE stream of Zotero save progress     |
| GET    | `/api/sessions`                               | List all user sessions                 |
| GET    | `/api/sessions/<session_id>`                  | Get session details                    |
| DELETE | `/api/sessions/<session_id>/delete`           | Delete a session                       |
| GET    | `/pdf?path=...`                               | Serve a PDF file (with security check) |
| POST   | `/settings/save-ai-config`                    | Save provider/model settings           |
| GET    | `/settings/models`                            | Query available models for a provider  |
| POST   | `/settings/manage-ollama`                     | Start/stop Ollama server               |
| POST   | `/settings/manage-lmstudio`                   | Start/stop LM Studio server            |

### Reference Extractor API

| Method      | URL                                        | Purpose                                     |
| ----------- | ------------------------------------------ | -------------------------------------------- |
| POST        | `/api/references/extract`                  | Start extraction for a single PDF (checks cache first) |
| GET         | `/api/references/status/<job_id>`          | Poll extraction job status                   |
| GET         | `/api/references/result/<job_id>`          | Get all extracted references for a completed job |
| GET         | `/api/references/export/<job_id>`          | Export references as CSV                     |
| GET         | `/api/references/history`                  | List all user's extraction jobs              |
| DELETE/POST | `/api/references/<job_id>/delete`          | Delete an extraction job and its references  |

### Article Search API

| Method      | URL                                       | Purpose                                    |
| ----------- | ----------------------------------------- | ------------------------------------------ |
| POST        | `/api/search/start`                       | Start a search (query, criteria, sources, year range, filters); stops at `searched` |
| POST        | `/api/search/evaluate/<search_id>`        | Start or resume evaluation of pending articles |
| POST        | `/api/search/pause/<search_id>`           | Pause evaluation (stops between chunks; resumable) |
| POST        | `/api/search/restart-eval/<search_id>`    | Re-evaluate every article from scratch     |
| GET         | `/api/search/status/<search_id>`          | Poll search state (phase, counts, providers, log) |
| GET         | `/api/search/status-stream/<search_id>`   | SSE stream of search/evaluation progress   |
| GET         | `/api/search/result/<search_id>`          | One page of results + the saved search config (`page`, `page_size`, `aligned_only`) |
| POST        | `/api/search/stop/<search_id>`            | Cancel a running search                    |
| GET         | `/api/search/export/<search_id>`          | Export aligned results as CSV              |
| GET         | `/api/searches`                           | List all user's searches                   |
| DELETE/POST | `/api/searches/<search_id>/delete`        | Delete a search and its results            |

---

## Frontend Architecture

The frontend is a **server-rendered SPA hybrid**:

- Django templates render the initial HTML shell (`index.html`, `references.html`, `settings.html`)
- [`main.js`](static/js/main.js) (42 KB) handles all dynamic interaction for the analysis page via AJAX and SSE
- [`references.js`](static/js/references.js) handles the Reference Extractor page: Zotero collection browsing (SSE), article listing, extraction with polling, result table rendering, CSV export, and history management
- [`pdf-viewer.js`](static/js/pdf-viewer.js) provides in-browser PDF preview
- [`style.css`](static/css/style.css) (19 KB) — shared CSS design system (dark theme); per-page styles are inlined in templates
- Real-time updates via **Server-Sent Events (SSE)** for analysis progress, Zotero loading, and Zotero saving
- **Auto-Restore on Load:** Upon initial dashboard loading, `main.js` queries the `/api/sessions` endpoint to automatically restore and display the user's most recent batch analysis session, preserving persistent state across page refreshes.
- **Consistent navigation** across all pages: 📊 Analysis · 🔎 Search · 📚 References · ⚙ Settings

---

## AI Provider Integration

The app supports **4 LLM backends**, all managed through the user's Profile settings:

| Provider        | Protocol             | Default URL                | Notes                                            |
| --------------- | -------------------- | -------------------------- | ------------------------------------------------ |
| **Ollama**      | OpenAI-compatible    | `http://localhost:11434`   | Local. Can be started/stopped from the settings UI. |
| **LM Studio**   | OpenAI-compatible    | `http://localhost:1234`    | Local. Supports model preloading with advanced options (contextLength, evalBatchSize, flashAttention, keepModelInMemory, tryMmap, numExperts, cache quantizations). Can be started/stopped from UI. Auto-unloads model on Django shutdown via WSGI atexit handler. |
| **llama-server**| OpenAI-compatible    | `http://localhost:8012`    | Local llama.cpp server.                          |
| **Gemini**      | Google `genai` SDK   | Cloud (API key)            | Cloud-based. Supports parallel processing (3 workers). |

### SDK Migration (Legacy to V1)
The core analysis and listing engine utilizes the modern `google-genai` SDK rather than the deprecated `google-generativeai` package:
- **Client-Based Initialization:** Switched from global configurations to instantiating independent, thread-safe `genai.Client(api_key=...)` instances scoped to each user profile.
- **Dynamic Model Fetching:** Queries client models with a robust filter fallback. If `supported_generation_methods` is empty in API responses, a name-based fallback (matching Gemini/Gemma while excluding embeddings/AQA models) is used to safely list generative models in the settings interface.

All OpenAI-compatible providers go through [`_chat_openai_compatible()`](article_summary_app/analyzer.py#L179-L200), which requests JSON output mode and gracefully falls back if the server doesn't support `response_format`.

### LM Studio Integration Issues & Fixes
To support large local models and advanced configurations dynamically, the following issues were resolved in the LM Studio subsystem:
- **REST API Payload Key Validation:**
  - **Issue:** Attempting to preload models in LM Studio using camelCase keys (like `contextLength`, `evalBatchSize`, etc.) or sending unsupported flags (like `keepModelInMemory`, `tryMmap`, and K/V cache quantizations) caused the server's native REST API (`POST /api/v1/models/load`) to fail with a fatal `unrecognized_keys` rejection.
  - **Fix:** Refactored the preloading logic (`_load_lmstudio_model`) to construct the payload using only the strict snake_case parameters recognized by the native LM Studio API: `model`, `context_length`, `eval_batch_size`, `flash_attention`, and `num_experts`. All other advanced memory configuration settings are stored persistently in the database profile but safely omitted from the load request.
- **Dynamic Context Window Tuning (`max_tokens`):**
  - **Issue:** Even when a larger context size was configured via `lmstudio_context_length` (e.g. `262144`), the chat completion API requests sent to `/v1/chat/completions` were still using a hardcoded `4096` tokens fallback, ignoring the configured capacity. However, setting `max_tokens` directly to `262144` in completions caused LM Studio to crash or unload the model due to excessive KV cache memory allocation for potential outputs.
  - **Fix:** Updated the completion payloads in both `views/references.py` and `views/analysis.py` to use `profile.lmstudio_context_length` when using LM Studio, but capped the completions request `max_tokens` value at `16384` (using `min(profile.lmstudio_context_length, 16384)`). This allows the massive context length (e.g., `262144`) loaded in the model on-disk to accept large inputs (e.g. 50,000+ tokens of PDF OCR text), while preventing the local model server from crashing by keeping output allocations within safety limits.

**Concurrency:** Local providers process PDFs sequentially (1 worker); Gemini uses 3 parallel workers. Each individual PDF has a **180-second timeout**.

---

## OCR Provider Integration

| Provider          | Module               | Notes                                  |
| ----------------- | -------------------- | -------------------------------------- |
| **MarkItDown**    | `markitdown`         | **First pass for every PDF.** Local, free text-layer extraction (pdfminer-based) returning markdown. If the PDF yields fewer than 200 chars (scanned/image PDF), the configured OCR provider below takes over. |
| **Mistral OCR**   | `mistralai` SDK      | Cloud API. Uploads PDF, returns markdown per page. Default OCR provider. |
| **macOS Vision**  | `ocrmac`             | macOS-only. Uses Apple's Vision framework. Local, no API key needed. |
| **Tesseract**     | `pytesseract`        | Universal fallback. Requires `tesseract` binary on PATH. Renders pages at 300 DPI. |

Extraction chain: MarkItDown (text layer) → configured OCR provider (Mistral or macOS Vision) → Tesseract. Born-digital PDFs never hit a paid API; only scanned PDFs proceed to real OCR.

---

## Zotero Integration

The Zotero integration is bidirectional and the most complex subsystem:

### Loading PDFs from Zotero
1. Connects to Zotero via `pyzotero` (local or remote API mode)
2. Fetches items from a specific collection or entire library
3. Builds a metadata index (authors, titles, year) from parent items
4. Scans local Zotero storage (`~/Zotero/storage/`) for matching PDFs
5. Groups multiple PDF versions of the same article, picks the newest
6. Sorts by author alphabetically
7. Returns the ordered list with `zotero_links` and `zotero_metadata` maps

### Saving Aligned Articles to Zotero (Ironclad Sync)
To resolve issues where exported items failed to display in collections or threw errors, the export pipeline implements a robust **"Ironclad Sync"** mechanism:
1. **Session Auditing:** Filters aligned PDFs in the session and pulls their linked Zotero item keys.
2. **Local Metadata Caching:** Caches the user's entire library structure at the start of export to minimize API roundtrips and check parent/child associations.
3. **Parent Item climbing:** Automatically resolves child `attachment` items up to their top-level `parentItem` key. The top-level articles are added to collections, ensuring they display in the Zotero application.
4. **Cross-Library Fallback:** Handles collections containing items whose PDFs exist in the personal library. If group library keys are missing from the group cache, the synchronizer queries the personal library client.
5. **Partial Payloads:** Sends updates containing only the necessary properties (`key`, `version`, and `collections`). This bypasses write failures caused by trying to upload read-only metadata fields (like `meta` or `deleted`).

Both operations support **SSE streaming** for real-time progress updates in the UI.

---

## Crossref Integration

[`crossref.py`](article_summary_app/crossref.py) provides:

1. **DOI extraction** — Regex patterns for `10.xxxx/xxxx`, `doi.org/`, `doi:` formats
2. **API querying** — Fetches structured data from `https://api.crossref.org/works/{doi}`
3. **APA 7 formatting** — Uses `pybtex` with the `apa7` style plugin, with manual fallback
4. **Field enrichment** — Overrides weak LLM-extracted fields with authoritative Crossref data:
   - `author` is **always** overridden from Crossref (LLMs often produce placeholder names)
   - `title`, `year`, `abstract`, `resource_type` are overridden only when the LLM extraction is empty or too short
   - `doi` and `apa_reference` are always set from Crossref

---

## Configuration & Environment

All configuration is managed via the `.env` file at the project root and per-user `Profile` model settings. The `.env` values serve as system defaults; per-user Profile settings override them.

### Key `.env` Variables

| Variable                | Purpose                                       |
| ----------------------- | --------------------------------------------- |
| `DJANGO_SECRET_KEY`     | Django secret key                             |
| `DJANGO_DEBUG`          | Debug mode (`true`/`false`)                   |
| `DJANGO_ALLOWED_HOSTS`  | Comma-separated allowed hosts                 |
| `APP_LOG_LEVEL`         | App logging level (default: `INFO`)           |
| `AI_PROVIDER`           | Default AI provider                           |
| `OLLAMA_BASE_URL`       | Ollama server URL                             |
| `OLLAMA_MODEL`          | Default Ollama model                          |
| `LMSTUDIO_BASE_URL`     | LM Studio server URL                          |
| `LMSTUDIO_MODEL`        | Default LM Studio model                       |
| `LMSTUDIO_MAX_CONTEXT`  | Max context window size for LM Studio         |
| `LLAMA_SERVER_BASE_URL` | llama.cpp server URL                          |
| `GEMINI_API_KEY`        | Google Gemini API key                         |
| `GEMINI_MODEL`          | Default Gemini model                          |
| `OCR_PROVIDER`          | Default OCR provider (`mistral`/`macocr`/`pytesseract`) |
| `MISTRAL_API_KEY`       | Mistral API key (for OCR)                     |
| `ZOTERO_USER_ID`        | Zotero user ID                                |
| `ZOTERO_API_KEY`        | Zotero API key                                |
| `ZOTERO_LIBRARY_TYPE`   | `user` or `group`                             |
| `ZOTERO_API_MODE`       | `remote` or `local`                           |
| `TEMPERATURE`           | LLM temperature                               |
| `MAX_TOKENS`            | LLM max output tokens                         |

---

## Management Commands

Located in [`management/commands/`](article_summary_app/management/commands/). These are CLI tools for Zotero library maintenance:

| Command                  | Purpose                                                                     |
| ------------------------ | --------------------------------------------------------------------------- |
| `dedup_collection`       | Remove duplicate items from a Zotero collection (by DOI or title). Keeps the copy with the most complete metadata. Items are removed from the collection only, not deleted from the library. |
| `export_missing_items`   | Find PDFs whose linked Zotero items no longer exist and export their stored metadata to CSV. |
| `remap_from_collection`  | Fix dead `zotero_links` in a session by matching against items in a Zotero collection using title similarity. |
| `remap_zotero_links`     | Bulk re-map Zotero links in sessions.                                      |
| `report_skipped`         | List aligned PDFs that would be skipped during "Save to Zotero" because their linked items are missing. |

---

## Authentication & Security

- **Authentication:** Standard Django auth (session-based) with `@login_required` on all views.
- **PDF serving security:** The [`resolve_allowed_pdf()`](article_summary_app/views/common.py#L28-L53) function restricts file serving to:
  - Folders the user has explicitly browsed (tracked in session)
  - Folders from the user's analysis sessions
  - The Zotero storage directory (`~/Zotero/storage`)
  - Only `.pdf` files
  - Path traversal attacks are prevented via `Path.is_relative_to()`

---

## Logging

Configured in [`settings.py`](article_summary/settings.py#L131-L169):

- **Handlers:** Console + rotating file (`app.log`, 5 MB max, 3 backups)
- **App logger** (`article_summary_app`): Level controlled by `APP_LOG_LEVEL` env var (default: `INFO`)
- **Noisy library loggers** (`httpx`, `httpcore`, `openai`): Suppressed to `WARNING`
- Detailed per-PDF analysis logging (step-by-step with timing)

---

## Test Suite

[`tests.py`](article_summary_app/tests.py) covers:

| Test Class                | What it Tests                                                      |
| ------------------------- | ------------------------------------------------------------------ |
| `ParseResponseTests`      | JSON parsing robustness (clean, markdown-wrapped, think tags, bad escapes, garbage) |
| `CoerceBoolTests`         | Boolean coercion for LLM string outputs                            |
| `AbstractExtractionTests` | Regex-based abstract extraction from PDF text                      |
| `ZoteroHelperTests`       | Zotero link regex, author formatting, article grouping/dedup       |
| `ResolveAllowedPdfTests`  | PDF serving security (allowed folders, path traversal, non-PDF rejection) |
| `FieldsContractTests`     | Ensures the FIELDS list has exactly 19 entries                     |

Run with: `python manage.py test article_summary_app`

---

## How to Run

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env   # Edit with your API keys and preferences

# 4. Run migrations
python manage.py migrate

# 5. Create a superuser (optional)
python manage.py createsuperuser

# 6. Start the development server
python manage.py runserver
```

**Prerequisites:**
- Python 3.12+
- `tesseract` on PATH (for fallback OCR)
- At least one AI provider running (Ollama, LM Studio, or a Gemini/Mistral API key)
- Zotero desktop app installed (optional, for Zotero integration)

---

## Key Design Decisions

1. **Per-user profiles** — All provider settings are stored per-user, allowing multiple researchers to use the same instance with different configurations.

2. **Background threading** — Analysis runs in daemon threads (`threading.Thread`) to avoid blocking the Django request cycle. SSE streams provide real-time updates.

3. **Graceful LLM parsing** — The `_parse_response()` function handles diverse model outputs: markdown-wrapped JSON, reasoning traces (`<think>` tags from reasoning models), bad escape sequences, and non-boolean alignment values. The reference extractor's `_parse_ref_response()` uses the same robustness patterns.

4. **Crossref as authoritative source** — Author names and DOIs from Crossref always override LLM output, since models frequently hallucinate or produce placeholder author names.

5. **OCR fallback chain** — Mistral (cloud, high quality) → macOS Vision (local, no cost) → Tesseract (universal fallback). Shared by both the analysis and reference extraction pipelines.

6. **Security-first PDF serving** — Files are only served from explicitly allowed directories; path traversal is blocked at the resolver level.

7. **Concurrent analysis** — Cloud providers (Gemini) use 3 parallel workers; local providers use 1 to avoid overloading the model server. Each PDF has an individual 180-second timeout with cleanup.

8. **Zotero bidirectionality** — Users can load PDFs from Zotero and save aligned results back to new collections, maintaining the link between analysis sessions and reference management.

9. **macOS integration** — Folder browsing uses AppleScript (`osascript`) for native folder picker dialogs. Ollama and LM Studio servers can be started/stopped from the settings UI via process management.

10. **Reference extraction caching** — Extracted references are persisted per-PDF per-user. Re-selecting a previously extracted article instantly shows cached results without re-running OCR + Heuristic Parser, with an explicit re-extract option available.

11. **Module reuse** — The reference extractor (`ref_extractor.py`) reuses the existing OCR infrastructure (`extractor.py → extract_text()`), avoiding code duplication while replacing the LLM dispatch logic entirely with a fast local regex heuristic engine.

12. **Zotero Ironclad Sync** — The export sync utilizes local metadata caching, hierarchy climbing (resolving attachments to parent items), and minimal JSON payloads (sending only `key`, `version`, and `collections`) to prevent read-only validation errors and ensure files appear properly in Zotero collections.

13. **Thread-Safe local/cloud timeouts** — High robustness against hung OCR or network sockets is achieved using individual file execution timeouts (180s for metadata/analysis, 300s for reference extraction) coupled with automatic process termination checking (`pgrep`/`pkill`).

14. **Local Server Lifecycle Management** — Ollama and LM Studio processes are managed dynamically via subprocess checks. On startup, Django reads the active database configuration and automatically preloads the selected model in a background thread for the active local provider (LM Studio or Ollama). On shutdown, an `atexit` cleanup hook reads the database configuration and automatically sends a request to offload the active model (sending `keep_alive = 0` to Ollama or unload payload to LM Studio) and stops the LM Studio server process to release all system memory.
