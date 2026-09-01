"""Article-search API clients.

One function per source. Each yields records in a common harmonized schema so
the orchestrator (search.py) can merge/dedupe them uniformly:

    {source, title, authors, year, doi, abstract, venue, url}

Every provider degrades to yielding nothing on failure or a missing API key —
one dead source never kills a multi-source search. Providers paginate to
exhaustion (searches run locally, results are unlimited); a `cancel` callable,
checked each page, lets a runaway search stop.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)


def normalize_query(query: str, dialect: str) -> str:
    """Adapt a boolean search query to what a given provider's API understands.

    - "boolean": Europe PMC / CORE speak AND/OR/NOT + parens + wildcards → pass through.
    - "s2": Semantic Scholar bulk uses symbol operators → AND→+, OR→|, NOT→-.
    - "plain": OpenAlex / Crossref / Springer / arXiv do plain full-text and reject
      wildcards-in-quotes → strip operators, parens and wildcards to a bag of terms.
    """
    q = (query or "").strip()
    if dialect == "boolean":
        return q
    if dialect == "s2":
        q = re.sub(r"\bAND\b", "+", q)
        q = re.sub(r"\bOR\b", "|", q)
        q = re.sub(r"\bNOT\b", "-", q)
        return re.sub(r"\s+", " ", q).strip()
    # plain
    q = re.sub(r"\b(AND|OR|NOT)\b", " ", q)
    q = q.replace("(", " ").replace(")", " ").replace("*", "").replace("?", "")
    return re.sub(r"\s+", " ", q).strip()

REQUEST_TIMEOUT = 30.0

# ponytail: hard page ceiling as a safety valve against a pathological
# never-ending cursor. ~unlimited in practice; lower it if a source misbehaves.
MAX_PAGES = 10000


def _no_cancel() -> bool:
    return False


def _year_bounds(year_from, year_to):
    """Parse (from, to) year inputs to int bounds; None where absent/invalid."""
    def _p(v):
        try:
            return int(str(v)[:4])
        except (TypeError, ValueError):
            return None
    return _p(year_from), _p(year_to)


def _year_ok(year, lo, hi) -> bool:
    """Whether a record's year is within [lo, hi]. Unparseable years pass
    (we can't prove they're out of range, so we don't drop them)."""
    try:
        y = int(str(year)[:4])
    except (TypeError, ValueError):
        return True
    return not ((lo and y < lo) or (hi and y > hi))


def _get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict | None:
    """GET JSON with one retry on rate-limit (HTTP 429). None on any failure."""
    for attempt in range(2):
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 and attempt == 0:
                time.sleep(2.0)
                continue
            log.warning(f"{url} returned {r.status_code}: {r.text[:200]}")
            return None
        except (httpx.HTTPError, ValueError) as e:
            log.warning(f"Request to {url} failed: {e}")
            return None
    return None


def _record(source, title, authors, year, doi, abstract, venue, url):
    """Build a harmonized record, normalizing every field to a clean string."""
    return {
        "source": source,
        "title": (title or "").strip(),
        "authors": (authors or "").strip(),
        "year": str(year or "").strip(),
        "doi": (doi or "").strip().lower().replace("https://doi.org/", ""),
        "abstract": (abstract or "").strip(),
        "venue": (venue or "").strip(),
        "url": (url or "").strip(),
    }


# ── Crossref ── (no key; polite pool via mailto)

def crossref(query, api_key="", cancel=_no_cancel, year_from=None, year_to=None, filt=None):
    filt = filt or {}
    lo, hi = _year_bounds(year_from, year_to)
    filters = []
    if lo:
        filters.append(f"from-pub-date:{lo}-01-01")
    if hi:
        filters.append(f"until-pub-date:{hi}-12-31")
    if filt.get("journal_only"):
        filters.append("type:journal-article")
    if filt.get("has_abstract"):
        filters.append("has-abstract:true")
    if filt.get("full_text"):
        filters.append("has-full-text:true")
    # Scope: query.bibliographic matches title/author/container instead of everything.
    query_key = "query.bibliographic" if filt.get("scope") == "title_abstract" else "query"
    cursor = "*"
    for _ in range(MAX_PAGES):
        if cancel():
            return
        params = {
            query_key: query, "rows": 100, "cursor": cursor,
            "mailto": "articlesummary@example.com",
        }
        if filters:
            params["filter"] = ",".join(filters)
        data = _get_json("https://api.crossref.org/works", params)
        items = ((data or {}).get("message") or {}).get("items") or []
        if not items:
            return
        for it in items:
            authors = ", ".join(
                f"{a.get('given', '')} {a.get('family', '')}".strip()
                for a in (it.get("author") or [])
            )
            parts = (it.get("published") or {}).get("date-parts") or [[None]]
            year = parts[0][0] if parts and parts[0] else ""
            titles = it.get("title") or [""]
            venues = it.get("container-title") or [""]
            yield _record(
                "crossref", titles[0], authors, year, it.get("DOI"),
                it.get("abstract"), venues[0] if venues else "", it.get("URL"),
            )
        cursor = ((data or {}).get("message") or {}).get("next-cursor")
        if not cursor:
            return


# ── OpenAlex ── (no key)

def _openalex_abstract(inv_index):
    """Reconstruct plain-text abstract from OpenAlex's inverted index."""
    if not inv_index:
        return ""
    positions = [(pos, word) for word, locs in inv_index.items() for pos in locs]
    positions.sort()
    return " ".join(word for _, word in positions)


def openalex(query, api_key="", cancel=_no_cancel, year_from=None, year_to=None, filt=None):
    filt = filt or {}
    lo, hi = _year_bounds(year_from, year_to)
    filters = []
    if lo:
        filters.append(f"from_publication_date:{lo}-01-01")
    if hi:
        filters.append(f"to_publication_date:{hi}-12-31")
    if filt.get("journal_only"):
        filters.append("type:article")
    if filt.get("has_abstract"):
        filters.append("has_abstract:true")
    if filt.get("full_text"):
        filters.append("has_fulltext:true")
    # Scope: title_and_abstract.search is a filter, replacing the broad search param.
    scoped = filt.get("scope") == "title_abstract"
    if scoped:
        filters.append(f"title_and_abstract.search:{query}")
    cursor = "*"
    for _ in range(MAX_PAGES):
        if cancel():
            return
        params = {
            "per-page": 200, "cursor": cursor,
            "mailto": "articlesummary@example.com",
        }
        if not scoped:
            params["search"] = query
        if filters:
            params["filter"] = ",".join(filters)
        data = _get_json("https://api.openalex.org/works", params)
        results = (data or {}).get("results") or []
        if not results:
            return
        for w in results:
            authors = ", ".join(
                (a.get("author") or {}).get("display_name", "")
                for a in (w.get("authorships") or [])
            )
            venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
            yield _record(
                "openalex", w.get("title"), authors, w.get("publication_year"),
                w.get("doi"), _openalex_abstract(w.get("abstract_inverted_index")),
                venue, w.get("doi") or (w.get("id") or ""),
            )
        cursor = ((data or {}).get("meta") or {}).get("next_cursor")
        if not cursor:
            return


# ── Semantic Scholar ── (optional key raises rate limit)

def semantic_scholar(query, api_key="", cancel=_no_cancel, year_from=None, year_to=None, filt=None):
    filt = filt or {}
    lo, hi = _year_bounds(year_from, year_to)
    headers = {"x-api-key": api_key} if api_key else None
    token = None
    for _ in range(MAX_PAGES):
        if cancel():
            return
        params = {
            "query": query,
            "fields": "title,authors,year,venue,abstract,externalIds,url",
        }
        if lo or hi:  # S2 accepts "2015-2020", "2015-", or "-2020"
            params["year"] = f"{lo or ''}-{hi or ''}"
        if filt.get("journal_only"):
            params["publicationTypes"] = "JournalArticle,Review"
        if filt.get("full_text"):
            params["openAccessPdf"] = ""  # S2: presence of this param restricts to items with a PDF
        if token:
            params["token"] = token
        data = _get_json(
            "https://api.semanticscholar.org/graph/v1/paper/search/bulk",
            params, headers,
        )
        papers = (data or {}).get("data") or []
        if not papers:
            return
        for p in papers:
            authors = ", ".join(a.get("name", "") for a in (p.get("authors") or []))
            doi = (p.get("externalIds") or {}).get("DOI", "")
            yield _record(
                "semantic_scholar", p.get("title"), authors, p.get("year"),
                doi, p.get("abstract"), p.get("venue"), p.get("url"),
            )
        token = (data or {}).get("token")
        if not token:
            return


# ── Europe PMC (PubMed) ── (no key)

def europepmc(query, api_key="", cancel=_no_cancel, year_from=None, year_to=None, filt=None):
    filt = filt or {}
    lo, hi = _year_bounds(year_from, year_to)
    if lo or hi:
        query = f"({query}) AND (PUB_YEAR:[{lo or 1000} TO {hi or 3000}])"
    if filt.get("journal_only"):
        query = f"({query}) AND (PUB_TYPE:\"Journal Article\")"
    if filt.get("has_abstract"):
        query = f"({query}) AND (HAS_ABSTRACT:y)"
    if filt.get("full_text"):
        query = f"({query}) AND (HAS_FT:y)"
    cursor = "*"
    for _ in range(MAX_PAGES):
        if cancel():
            return
        data = _get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
                "query": query, "format": "json", "pageSize": 1000,
                "cursorMark": cursor, "resultType": "core",
            })
        results = ((data or {}).get("resultList") or {}).get("result") or []
        if not results:
            return
        for r in results:
            yield _record(
                "europepmc", r.get("title"), r.get("authorString"), r.get("pubYear"),
                r.get("doi"), r.get("abstractText"), r.get("journalTitle"),
                f"https://doi.org/{r.get('doi')}" if r.get("doi") else "",
            )
        nxt = (data or {}).get("nextCursorMark")
        if not nxt or nxt == cursor:
            return
        cursor = nxt


# ── arXiv ── (no key; Atom XML)

_ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _arxiv_page(query, start, page):
    """One arXiv page, retrying the 503/timeout the arXiv API throws under load."""
    for attempt in range(3):
        try:
            r = httpx.get("https://export.arxiv.org/api/query", params={
                "search_query": f"all:{query}", "start": start, "max_results": page,
            }, timeout=60.0, follow_redirects=True)
            if r.status_code in (429, 503):
                time.sleep(5.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                log.warning(f"arXiv returned {r.status_code}")
                return None
            return ET.fromstring(r.text)
        except (httpx.HTTPError, ET.ParseError) as e:
            log.warning(f"arXiv request failed (attempt {attempt + 1}): {e}")
            time.sleep(3.0)
    return None


def arxiv(query, api_key="", cancel=_no_cancel, year_from=None, year_to=None, filt=None):
    lo, hi = _year_bounds(year_from, year_to)  # arXiv API has no year filter → filter here
    start = 0
    page = 100
    for _ in range(MAX_PAGES):
        if cancel():
            return
        root = _arxiv_page(query, start, page)
        if root is None:
            return
        entries = root.findall("a:entry", _ATOM)
        if not entries:
            return
        for e in entries:
            published = e.findtext("a:published", "", _ATOM)
            year = published[:4] if published else ""
            if not _year_ok(year, lo, hi):
                continue
            authors = ", ".join(
                (n.findtext("a:name", "", _ATOM) or "").strip()
                for n in e.findall("a:author", _ATOM)
            )
            yield _record(
                "arxiv", e.findtext("a:title", "", _ATOM), authors, year,
                e.findtext("arxiv:doi", "", _ATOM), e.findtext("a:summary", "", _ATOM),
                e.findtext("arxiv:journal_ref", "", _ATOM), e.findtext("a:id", "", _ATOM),
            )
        start += page
        time.sleep(3.0)  # arXiv asks callers to throttle bulk paging


# ── CORE ── (requires key)

def core(query, api_key="", cancel=_no_cancel, year_from=None, year_to=None, filt=None):
    if not api_key:
        return
    filt = filt or {}
    lo, hi = _year_bounds(year_from, year_to)
    if lo:
        query = f"({query}) AND yearPublished>={lo}"
    if hi:
        query = f"({query}) AND yearPublished<={hi}"
    headers = {"Authorization": f"Bearer {api_key}"}
    offset = 0
    limit = 100
    for _ in range(MAX_PAGES):
        if cancel():
            return
        # CORE's search engine is slow; give it room and retry a read timeout once.
        data = None
        for attempt in range(2):
            try:
                # CORE v3 search is a POST (a GET redirects to an HTML page).
                r = httpx.post("https://api.core.ac.uk/v3/search/works",
                               json={"q": query, "limit": limit, "offset": offset},
                               headers=headers, timeout=120.0)
                if r.status_code == 429:
                    time.sleep(5.0)
                    continue
                if r.status_code != 200:
                    log.warning(f"CORE returned {r.status_code}: {r.text[:200]}")
                    return
                data = r.json()
                break
            except httpx.TimeoutException:
                log.warning(f"CORE timed out (attempt {attempt + 1}), offset={offset}")
                continue
            except (httpx.HTTPError, ValueError) as e:
                log.warning(f"CORE request failed: {e}")
                return
        if data is None:
            return
        results = data.get("results") or []
        if not results:
            return
        for w in results:
            authors = ", ".join(a.get("name", "") for a in (w.get("authors") or []))
            yield _record(
                "core", w.get("title"), authors, w.get("yearPublished"),
                w.get("doi"), w.get("abstract"), w.get("publisher"),
                w.get("doi") and f"https://doi.org/{w.get('doi')}" or (w.get("downloadUrl") or ""),
            )
        offset += limit
        if offset >= (data.get("totalHits") or 0):
            return


# ── Springer Nature (Open Access API) ── (requires key)

def springer(query, api_key="", cancel=_no_cancel, year_from=None, year_to=None, filt=None):
    if not api_key:
        return
    lo, hi = _year_bounds(year_from, year_to)
    if lo:
        query = f"{query} onlinedatefrom:{lo}-01-01"
    if hi:
        query = f"{query} onlinedateto:{hi}-12-31"
    start = 1
    page = 25  # free tier rejects large page sizes as a "premium feature"
    # Springer's free tier caps pagination at s<=100; going past it 403s.
    # ponytail: bump both if you have a premium key.
    while start <= 100:
        if cancel():
            return
        data = _get_json("https://api.springernature.com/openaccess/json", {
            "q": query, "api_key": api_key, "s": start, "p": page,
        })
        records = (data or {}).get("records") or []
        if not records:
            return
        for rec in records:
            authors = ", ".join(c.get("creator", "") for c in (rec.get("creators") or []))
            urls = rec.get("url") or []
            url = urls[0].get("value", "") if urls else ""
            abstract = rec.get("abstract")
            if isinstance(abstract, dict):  # OA API sometimes wraps it as {"p": "..."}
                abstract = abstract.get("p") or ""
            yield _record(
                "springer", rec.get("title"), authors,
                (rec.get("publicationDate") or "")[:4], rec.get("doi"),
                abstract, rec.get("publicationName"), url,
            )
        start += page


PROVIDER_LABELS = {
    "crossref": "Crossref",
    "openalex": "OpenAlex",
    "semantic_scholar": "Semantic Scholar",
    "europepmc": "Europe PMC (PubMed)",
    "arxiv": "arXiv",
    "core": "CORE",
    "springer": "Springer Nature",
}

# Which query dialect each source's API speaks (see normalize_query)
PROVIDER_DIALECTS = {
    "crossref": "plain",
    "openalex": "plain",
    "semantic_scholar": "s2",
    "europepmc": "boolean",
    "arxiv": "plain",
    "core": "boolean",
    "springer": "plain",
}

# Registry: source key → (provider function, Profile attr holding its API key)
PROVIDERS = {
    "crossref": (crossref, None),
    "openalex": (openalex, None),
    "semantic_scholar": (semantic_scholar, "semantic_scholar_api_key"),
    "europepmc": (europepmc, None),
    "arxiv": (arxiv, None),
    "core": (core, "core_api_key"),
    "springer": (springer, "springer_api_key"),
}
