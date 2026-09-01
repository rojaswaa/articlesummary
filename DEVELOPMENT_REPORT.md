# Technical Migration & Architectural Refactor Report
**Project:** ArticleSummaryV2 (Django-based Systematic Literature Review Tool)
**Date:** April 16, 2026
**Objective:** Migration to the `google-genai` SDK, implementation of persistent multi-user session management, and resolution of Zotero API synchronization issues.

---

## 1. Gemini SDK Migration (Legacy to V1)
We transitioned the core analysis engine from the deprecated `google-generativeai` package to the modern `google-genai` SDK.

*   **SDK Change:** Replaced `google-generativeai==0.8.3` with `google-genai==0.3.0` in `requirements.txt`.
*   **Implementation Detail:**
    *   Switched from global `genai.configure()` to class-based `genai.Client(api_key=...)` instances in `analyzer.py`.
    *   Refactored `get_available_models` to handle the new `Model` object structure, specifically targeting the `supported_generation_methods` attribute.
    *   **Filter Fallback:** Implemented a robust filtering logic that accounts for variations in API responses where `methods` might return empty; the system now uses a regex-based name check (Gemini/Gemma) while excluding embedding models to populate selection menus reliably.

---

## 2. Multi-User Architectural Refactor
The application was moved from a "Single-User/Stateless" model to a "Multi-User/Persistent" model using the Django ORM.

*   **Database Schema:**
    *   **`Profile` Model:** 1:1 relationship with Django's `User`. Stores per-user API keys (Gemini, Mistral, Zotero) and local AI configurations (Ollama/LM Studio URLs). This eliminated the dependency on a shared server-side `.env` file.
    *   **`Session` Model:** UUID-based storage for research projects. Tracks the specific "Research Criteria" (prompt), the target folder/Zotero collection, and metadata.
    *   **`PDFJob` Model:** Stores individual processing results. Each job is linked to a `Session`. This allows users to leave the app and return to find their labels (Done, Error, Aligns) exactly as they were.
*   **Session Management:**
    *   Implemented `get_user_profile` helper in `views.py` to handle lazy-creation of profiles for existing users.
    *   Implemented **Auto-Restore**: `main.js` now automatically fetches and reloads the most recent session via the `/api/sessions` endpoint upon page load.
    *   **Data Isolation:** All database queries are strictly scoped to `request.user` to ensure privacy and data integrity between different researchers.

---

## 3. Local AI Infrastructure Management
Added integrated control for local LLM servers directly from the Django dashboard.

*   **Ollama/LM Studio Control:** Implemented `manage_ollama` and `manage_lmstudio` views using Python’s `subprocess` module.
*   **Process Verification:** The system uses `pgrep` to verify server state and `pkill` (for Ollama) or `lms server stop` (for LM Studio) to terminate processes.
*   **Connectivity Health-Checks:** Integrated `httpx` heartbeats in the `admin` view to provide real-time "Online/Offline" status indicators for all configured AI providers.

---

## 4. Deep Dive: The Zotero Export Crisis ("26 vs. 66 Files")
A significant technical hurdle was encountered during the "Save to Zotero" feature, where 66 PDFs were analyzed and aligned, but only 26 items appeared in the final Zotero collection.

### Root Cause Analysis:
The issue was a combination of three factors:
1.  **Parent-Child Hierarchy:** In Zotero, a PDF is an `attachment` (child) of a `Top-Level Item` (the Article metadata). Zotero collections are designed to display Top-Level Items. Our initial export attempted to move the PDF ID directly. Zotero accepted the update but refused to display a child-item in the root of a collection.
2.  **Library ID Mismatch:** The articles were spread across a **Group Library** and a **Personal Library**. When the app queried the Group Library for IDs belonging to the Personal Library, the API returned `404 Not Found`.
3.  **Read-Only Field Conflict:** The Zotero API rejects updates if "read-only" fields (like `version`, `deleted`, or `meta`) are included in the JSON payload.

### The "Ironclad Sync" Solution:
We implemented a three-stage resolution strategy in `views.py`:
*   **Stage 1: Local Metadata Caching:** The app now performs a `zot.everything(zot.items())` call at the start of the export to cache the entire library structure locally.
*   **Stage 2: Parent Resolution (The "Climb"):** For every aligned PDF, the app looks up its ID in the local cache. If it identifies the item as an `attachment`, it automatically "climbs" to the `parentItem` ID.
*   **Stage 3: Partial Update Payloads:** To bypass the "Invalid Keys" error, we refactored the update logic to send **Partial Payloads**. We only send the `key`, `version`, and the updated `collections` array. This satisfies Zotero’s strict write-requirements.

---

## 5. Stability & Performance Improvements
*   **Analysis Timeout:** Implemented a `concurrent.futures.ThreadPoolExecutor` with a strict **180-second (3-minute) timeout** per file. This prevents a single hung OCR process or stalled AI API from blocking the entire analysis queue.
*   **Race Condition Prevention:** Modified `main.js` to immediately disable the "Analyze All" button upon the first click, preventing duplicate session creation.
*   **Deduplication:** Added `dict.fromkeys()` deduplication to both the `browse_folder` and `zotero_pdfs` views to ensure the analysis queue is unique.
*   **JS Cache Busting:** Incremented versioning (`v=3.2`) on static assets to ensure client-side browsers force-download the latest logic.

---

## Final System State
The application is now a production-ready, multi-user tool. It handles the full lifecycle of a literature review—from library ingestion and AI-powered extraction to persistent state management and robust Zotero synchronization—all while maintaining high fault tolerance for external API and local server failures.
