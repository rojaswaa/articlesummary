"""Unpaywall API: find an open-access PDF URL for a DOI.

Free API; every request must carry a contact email (?email=). Returns the best
open-access PDF link when one exists, else None.
"""
import logging

import httpx

log = logging.getLogger(__name__)


def get_oa_pdf_url(doi: str, email: str) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower().replace("https://doi.org/", "")
    try:
        r = httpx.get(f"https://api.unpaywall.org/v2/{doi}",
                      params={"email": email or "articlesummary@example.com"}, timeout=15)
        if r.status_code != 200:
            return None
        best = (r.json() or {}).get("best_oa_location") or {}
        return best.get("url_for_pdf") or None
    except (httpx.HTTPError, ValueError) as e:
        log.debug(f"Unpaywall lookup failed for {doi}: {e}")
        return None
