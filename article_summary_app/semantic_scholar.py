"""Semantic Scholar Academic Graph API client.

Used for structured reference lists (replacing OCR bibliography parsing when
the article has a DOI) and forward citations. Free API; an optional key in
SEMANTIC_SCHOLAR_API_KEY raises the rate limit.
"""
import html
import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

S2_BASE = "https://api.semanticscholar.org/graph/v1"
PAPER_FIELDS = "title,authors,year,venue,journal,externalIds"


def _headers() -> dict:
    key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    return {"x-api-key": key} if key else {}


def _get(url: str, params: dict) -> dict | None:
    """GET with one retry on the shared-pool rate limit (HTTP 429)."""
    for attempt in range(2):
        try:
            r = httpx.get(url, params=params, headers=_headers(), timeout=15.0)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code == 429 and attempt == 0:
                time.sleep(1.5)
                continue
            log.warning(f"Semantic Scholar returned {r.status_code} for {url}")
            return None
        except httpx.HTTPError as e:
            log.warning(f"Semantic Scholar request failed: {e}")
            return None
    return None


def _paper_to_fields(paper: dict) -> dict:
    """Map an S2 paper object onto the app's reference field names."""
    journal = paper.get("journal") or {}
    authors = ", ".join(a.get("name", "") for a in (paper.get("authors") or []) if a.get("name"))
    year = str(paper.get("year") or "")
    # S2 data sometimes carries HTML entities (e.g. "Economics &amp; Finance")
    title = html.unescape(paper.get("title") or "")
    venue = html.unescape(journal.get("name") or paper.get("venue") or "")
    doi = (paper.get("externalIds") or {}).get("DOI", "") or ""

    raw_parts = [p for p in (authors, f"({year})." if year else "", title + "." if title else "", venue) if p]
    if doi:
        raw_parts.append(f"https://doi.org/{doi}")

    return {
        "raw_text": " ".join(raw_parts),
        "author": authors,
        "title": title,
        "year": year,
        "journal": venue,
        "volume": journal.get("volume") or "",
        "issue": "",  # S2 doesn't expose issue numbers
        "pages": journal.get("pages") or "",
        "doi": doi,
    }


# Highly-cited papers can have tens of thousands of citing papers; at the
# anonymous-pool rate limit each 500-item page costs ~1s, so cap the fetch.
MAX_LINKED_PAPERS = 1000

def _fetch_linked_papers(doi: str, endpoint: str, wrapper_key: str) -> list[dict] | None:
    """Shared fetch for /references and /citations (paginated, capped)."""
    results = []
    offset = 0
    while len(results) < MAX_LINKED_PAPERS:
        data = _get(
            f"{S2_BASE}/paper/DOI:{doi}/{endpoint}",
            {"fields": PAPER_FIELDS, "limit": 500, "offset": offset},
        )
        if data is None:
            return None if offset == 0 else results
        for row in data.get("data", []):
            paper = row.get(wrapper_key) or {}
            if paper.get("title"):
                results.append(_paper_to_fields(paper))
        nxt = data.get("next")
        if not nxt or nxt <= offset:
            break
        offset = nxt
    return results[:MAX_LINKED_PAPERS]


def get_references(doi: str) -> list[dict] | None:
    """Structured reference list for a DOI, or None if the paper isn't indexed."""
    if not doi:
        return None
    refs = _fetch_linked_papers(doi, "references", "citedPaper")
    if refs:
        for i, r in enumerate(refs, 1):
            r["order"] = i
    return refs


def get_citations(doi: str) -> list[dict] | None:
    """Papers that cite the given DOI, or None if the paper isn't indexed."""
    if not doi:
        return None
    return _fetch_linked_papers(doi, "citations", "citingPaper")
