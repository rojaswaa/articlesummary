"""Search orchestration: fan out to providers, harmonize/dedupe, evaluate.

Records arrive already harmonized (see search_providers._record). This module
merges duplicates across sources and evaluates each unique article against the
user's criteria by reusing the LLM analyzer.
"""
import concurrent.futures
import logging
import re

from .search_providers import PROVIDERS, PROVIDER_DIALECTS, normalize_query, _year_bounds, _year_ok

log = logging.getLogger(__name__)


def _norm_title(title: str) -> str:
    """Lowercase, strip punctuation/whitespace — for title-based dedup fallback."""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def _dedup_key(rec: dict):
    """DOI when present, else normalized title + year. None if neither exists
    (record can't be deduped and is kept as-is)."""
    if rec.get("doi"):
        return ("doi", rec["doi"])
    nt = _norm_title(rec.get("title"))
    if nt:
        return ("title", nt, rec.get("year", ""))
    return None


def _merge(into: dict, other: dict) -> None:
    """Fill blank fields of `into` from `other`, keeping the longer abstract, and
    accumulate the set of sources this article was found in."""
    for k in ("title", "authors", "year", "doi", "venue", "url"):
        if not into.get(k) and other.get(k):
            into[k] = other[k]
    if len(other.get("abstract") or "") > len(into.get("abstract") or ""):
        into["abstract"] = other["abstract"]
    into.setdefault("sources", [into.get("source")] if into.get("source") else [])
    if other.get("source") and other["source"] not in into["sources"]:
        into["sources"].append(other["source"])


def harmonize_and_dedup(records) -> list[dict]:
    """Collapse duplicate records (DOI first, then title+year) into unique
    articles, each carrying the list of sources it came from."""
    merged: dict = {}
    passthrough: list[dict] = []
    for rec in records:
        key = _dedup_key(rec)
        if key is None:
            rec["sources"] = [rec["source"]] if rec.get("source") else []
            passthrough.append(rec)
            continue
        if key in merged:
            _merge(merged[key], rec)
        else:
            rec = dict(rec)
            rec["sources"] = [rec["source"]] if rec.get("source") else []
            merged[key] = rec
    return list(merged.values()) + passthrough


def fetch_all(query: str, sources: list[str], profile, cancel=None, on_progress=None,
              year_from=None, year_to=None, filt=None) -> list[dict]:
    """Query every selected provider concurrently and return deduped articles.

    on_progress(source, count, done) is called during fetching so callers can
    surface per-provider progress; `done` marks a provider finished/errored.
    year_from/year_to and `filt` (scope / journal_only / has_abstract / full_text)
    constrain results at the API level where supported, with post-fetch guards
    dropping anything out-of-range or (when has_abstract is set) abstract-less.
    """
    cancel = cancel or (lambda: False)
    report = on_progress or (lambda *a: None)
    filt = filt or {}
    require_abstract = bool(filt.get("has_abstract"))
    lo, hi = _year_bounds(year_from, year_to)
    records: list[dict] = []

    def run(source):
        fn, key_attr = PROVIDERS[source]
        api_key = getattr(profile, key_attr, "") if key_attr else ""
        q = normalize_query(query, PROVIDER_DIALECTS.get(source, "plain"))
        out = []
        error = None
        try:
            for rec in fn(q, api_key=api_key, cancel=cancel,
                          year_from=year_from, year_to=year_to, filt=filt):
                if not _year_ok(rec.get("year"), lo, hi):
                    continue
                if require_abstract and not (rec.get("abstract") or "").strip():
                    continue
                out.append(rec)
                if len(out) % 50 == 0:
                    report(source, len(out), False)
        except Exception as e:
            error = str(e)
            log.error(f"Provider {source} failed: {e}")
        report(source, len(out), True)
        log.info(f"[search] {source}: fetched {len(out)}" + (f" (error: {error})" if error else ""))
        return out

    active = [s for s in sources if s in PROVIDERS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active) or 1) as pool:
        for out in pool.map(run, active):
            records.extend(out)

    deduped = harmonize_and_dedup(records)
    log.info(f"[search] fetched {len(records)} records → {len(deduped)} unique after dedup")
    return deduped


def evaluate_article(article: dict, criteria: str, profile_data: dict) -> dict:
    """Score one article against the criteria via the LLM analyzer, using its
    title + abstract as the document text."""
    from .analyzer import extract_fields

    abstract = article.get("abstract") or ""
    text = f"Title: {article.get('title', '')}\n\nAbstract: {abstract}"
    return extract_fields(
        text, criteria,
        metadata={"title": article.get("title"), "author": article.get("authors")},
        doi=article.get("doi", ""),
        abstract_text=abstract, is_real_abstract=bool(abstract),
        profile_data=profile_data,
    )
