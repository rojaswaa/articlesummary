"""Reference extraction from PDF articles using OCR + Crossref Resolution + Heuristic Fallback (Non-LLM)."""
import logging
import os
import re
import httpx

from .extractor import extract_text

log = logging.getLogger(__name__)

REFERENCE_FIELDS = ["raw_text", "author", "title", "year", "journal", "volume", "issue", "pages", "doi"]


def _extract_journal_details(source_str: str) -> tuple[str, str, str]:
    """
    Heuristically extracts volume, issue, and pages from the journal/source string.
    """
    volume = ""
    issue = ""
    pages = ""
    
    vol_iss_page_match = re.search(r"\b(\d+)\((\d+)\):(\d+[\u2013\u2014\-]\d+)\b", source_str)
    if vol_iss_page_match:
        return vol_iss_page_match.group(1), vol_iss_page_match.group(2), vol_iss_page_match.group(3)
        
    vol_iss_match = re.search(r"\b(\d+)\((\d+)\)", source_str)
    if vol_iss_match:
        volume = vol_iss_match.group(1)
        issue = vol_iss_match.group(2)
        
    pages_match = re.search(r"\b(?:pp\.?|pages?)\s*(\d+[\u2013\u2014\-]\d+|\d+)\b", source_str, re.IGNORECASE)
    if pages_match:
        pages = pages_match.group(1)
        
    if not volume:
        vol_match = re.search(r"\bvol(?:ume)?\.?\s*(\d+)\b", source_str, re.IGNORECASE)
        if vol_match:
            volume = vol_match.group(1)
            
    if not issue:
        iss_match = re.search(r"\b(?:issue|no\.?)\s*(\d+)\b", source_str, re.IGNORECASE)
        if iss_match:
            issue = iss_match.group(1)
            
    if not pages:
        page_range_match = re.search(r"\b(\d+[\u2013\u2014\-]\d+)\b", source_str)
        if page_range_match:
            pages = page_range_match.group(1)
            
    return volume, issue, pages


def _resolve_via_crossref(raw_cit: str, client: httpx.Client, mailto: str, dbg_log) -> dict | None:
    """
    Queries the Crossref REST API with an unstructured citation string
    and returns parsed, verified fields if a high-score match is found.
    """
    # Clean leading bracketed or decimal prefix list markers
    clean_cit = re.sub(r"^\[(?:\d+|[a-zA-Z]+\+?\d+)\]\s*", "", raw_cit)
    clean_cit = re.sub(r"^\d+\.\s*", "", clean_cit)
    clean_cit = clean_cit.strip()
    
    url = "https://api.crossref.org/works"
    params = {"query.bibliographic": clean_cit, "rows": 1}
    if mailto:
        params["mailto"] = mailto
    
    try:
        r = client.get(url, params=params, timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            items = data.get("message", {}).get("items", [])
            if items:
                match = items[0]
                score = match.get("score", 0.0)
                # Score >= 30.0 generally guarantees a highly accurate match
                if score >= 30.0:
                    # 1. Resolve Year
                    year = ""
                    for key in ["published-print", "published-online", "issued", "created"]:
                        dp = match.get(key, {}).get("date-parts", [])
                        if dp and dp[0] and dp[0][0]:
                            year = str(dp[0][0])
                            break
                            
                    # 2. Resolve Authors list
                    authors = match.get("author", [])
                    auth_list = []
                    for a in authors:
                        given = a.get("given", "").strip()
                        family = a.get("family", "").strip()
                        if given and family:
                            auth_list.append(f"{given} {family}")
                        elif family:
                            auth_list.append(family)
                        elif given:
                            auth_list.append(given)
                    author_str = ", ".join(auth_list)
                    
                    dbg_log(f"      ✔ Resolved via Crossref (Score: {score:.1f}): {match.get('DOI')}")
                    return {
                        "raw_text": raw_cit,
                        "author": author_str,
                        "title": match.get("title", [""])[0],
                        "year": year,
                        "journal": match.get("container-title", [""])[0],
                        "volume": match.get("volume", "") or "",
                        "issue": match.get("issue", "") or "",
                        "pages": match.get("page", "") or "",
                        "doi": match.get("DOI", "") or ""
                    }
    except Exception as e:
        dbg_log(f"      ⚠ Crossref query failed or timed out: {e}")
    return None


def _parse_citation_heuristically(raw_cit: str, order_idx: int) -> dict:
    """
    Fallback parser using regex-lookbehinds to extract fields locally.
    """
    number_pattern = re.compile(r"^\[(?:\d+|[a-zA-Z]+\+?\d+)\]")
    decimal_pattern = re.compile(r"^\d+\.\s")
    
    # Strip numbered markers
    prefix_match = number_pattern.match(raw_cit) or decimal_pattern.match(raw_cit)
    if prefix_match:
        raw_cit_clean = raw_cit[prefix_match.end():].strip()
    else:
        raw_cit_clean = raw_cit
        
    # Extract DOI
    doi_str = ""
    doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", raw_cit_clean, re.IGNORECASE)
    if doi_match:
        doi_str = doi_match.group(0)
        raw_cit_clean = raw_cit_clean.replace(doi_match.group(0), "").strip()
        raw_cit_clean = re.sub(r"\s*doi:\s*$", "", raw_cit_clean, flags=re.IGNORECASE)
        raw_cit_clean = re.sub(r"\s*https?://doi\.org/\s*$", "", raw_cit_clean, flags=re.IGNORECASE)

    # Split citation fields using lookbehinds
    split_pattern = (
        r"(?<!\b[A-Z])(?<!\bet al)(?<!\bVol)(?<!\bEd)(?<!\bpp)(?<!\bCoRR)(?<!\babs)(?<!\barXiv)"
        r"(?<!\bProc)(?<!\bInt)(?<!\bConf)(?<!\bTrans)(?<!\bJour)(?<!\bSci)(?<!\bLett)(?<!\bSyst)"
        r"(?<!\bTech)(?<!\bRep)(?<!\bUniv)(?<!\bNo)(?<!\bno)\.\s+"
    )
    blocks = re.split(split_pattern, raw_cit_clean)
    blocks = [b.strip() for b in blocks if b.strip()]
    
    authors_str = ""
    year_str = ""
    title_str = ""
    source_str = ""
    
    if len(blocks) >= 1:
        # Check for year near start (APA)
        year_match = re.search(r"\((\d{4})\)", blocks[0])
        if not year_match:
            year_match = re.search(r"\[(\d{4})\]", blocks[0])
            
        if year_match:
            year_str = year_match.group(1)
            authors_str = blocks[0].replace(year_match.group(0), "").strip()
            authors_str = re.sub(r"^[,\s\.\-]+|[,\s\.\-]+$", "", authors_str)
            
            if len(blocks) >= 2:
                title_str = blocks[1]
            if len(blocks) >= 3:
                source_str = ". ".join(blocks[2:])
        else:
            # IEEE Style / year at end
            authors_str = re.sub(r"^[,\s\.\-]+|[,\s\.\-]+$", "", blocks[0])
            for block in blocks[1:]:
                y_match = re.search(r"\b(19\d{2}|20\d{2})\b", block)
                if y_match:
                    year_str = y_match.group(1)
                    break
            
            if len(blocks) >= 2:
                title_str = blocks[1]
            if len(blocks) >= 3:
                source_raw = ". ".join(blocks[2:])
                if year_str:
                    source_raw = re.sub(rf"[,\s\.]+{year_str}[,\s\.]*$", "", source_raw).strip()
                source_str = source_raw
                
    title_str = re.sub(r"^[,\s\.\"\']+|[,\s\.\"\']+$", "", title_str)
    source_str = re.sub(r"^[,\s\.\"\']+|[,\s\.\"\']+$", "", source_str)
    authors_str = re.sub(r"^[,\s\.\"\']+|[,\s\.\"\']+$", "", authors_str)
    
    # Volume, Issue, Pages heuristics
    volume, issue, pages = _extract_journal_details(source_str)

    return {
        "raw_text": raw_cit,
        "author": authors_str,
        "title": title_str,
        "year": year_str,
        "journal": source_str,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi_str,
        "order": order_idx
    }


def parse_rule_based_references(markdown_text: str, mailto: str, dbg_log) -> list[dict]:
    # Locate bibliography section in markdown
    heading_patterns = [
        r"(?i)(?:^|\n)(?:#+\s+)?references\b",
        r"(?i)(?:^|\n)(?:#+\s+)?bibliography\b",
        r"(?i)(?:^|\n)(?:#+\s+)?works\s+cited\b"
    ]
    
    ref_start_idx = -1
    for pat in heading_patterns:
        match = re.search(pat, markdown_text)
        if match:
            ref_start_idx = match.start()
            break
            
    if ref_start_idx != -1:
        ref_text = markdown_text[ref_start_idx:]
    else:
        ref_text = markdown_text
        
    lines = ref_text.splitlines()
    
    cleaned_lines = []
    for line in lines:
        l = line.strip()
        if not l:
            continue
        if re.match(r"^#+\s+(?:References|Bibliography|Works Cited)", l, re.IGNORECASE):
            continue
        l_cleaned = re.sub(r"^[\-\*\+]\s+", "", l)
        cleaned_lines.append(l_cleaned)

    raw_citations = []
    current_citation = []
    
    number_pattern = re.compile(r"^\[(?:\d+|[a-zA-Z]+\+?\d+)\]") # matches [1]
    decimal_pattern = re.compile(r"^\d+\.\s") # matches 1. 
    
    is_numbered = False
    numbered_count = sum(1 for line in cleaned_lines if number_pattern.match(line) or decimal_pattern.match(line))
    if len(cleaned_lines) > 0 and numbered_count > len(cleaned_lines) * 0.15:
        is_numbered = True

    for line in cleaned_lines:
        if re.match(r"^#+\s+", line) and not re.search(r"references|bibliography|works\s+cited", line, re.IGNORECASE):
            break
            
        if is_numbered:
            if number_pattern.match(line) or decimal_pattern.match(line):
                if current_citation:
                    raw_citations.append(" ".join(current_citation))
                    current_citation = []
                current_citation.append(line)
            else:
                current_citation.append(line)
        else:
            if re.match(r"^[A-Z][a-zA-Z'\-\s]+,\s+[A-Z]\.", line) or re.match(r"^[A-Z][a-zA-Z'\-\s]+\s+&\s+[A-Z]", line) or re.match(r"^[A-Z][a-zA-Z'\-\s]+,?\s+and\s+[A-Z]", line):
                if current_citation:
                    raw_citations.append(" ".join(current_citation))
                    current_citation = []
                current_citation.append(line)
            else:
                if not current_citation:
                    current_citation.append(line)
                else:
                    if any(re.search(r"\b(19\d{2}|20\d{2})\b", c) for c in current_citation) and re.match(r"^[A-Z]", line):
                        raw_citations.append(" ".join(current_citation))
                        current_citation = [line]
                    else:
                        current_citation.append(line)
                        
    if current_citation:
        raw_citations.append(" ".join(current_citation))
        
    parsed_citations = []
    
    # Batch resolve via Crossref using connection pooling
    with httpx.Client() as client:
        for idx, raw_cit in enumerate(raw_citations, 1):
            raw_cit = raw_cit.strip()
            if not raw_cit:
                continue
                
            dbg_log(f"    • Resolving #{idx}: {raw_cit[:60]}...")
            
            # Attempt Crossref
            parsed = _resolve_via_crossref(raw_cit, client, mailto, dbg_log)
            
            # Local fallback if Crossref failed or was inaccurate
            if not parsed:
                parsed = _parse_citation_heuristically(raw_cit, idx)
                dbg_log("      ⚡ Fallback to local heuristic parsing")
            else:
                parsed["order"] = idx
                
            parsed_citations.append(parsed)
            
    return parsed_citations


def extract_references(pdf_path: str, profile_data: dict = None) -> dict:
    debug_messages = []
    def dbg_log(msg):
        log.info(msg)
        debug_messages.append(msg)
        
    filename = os.path.basename(pdf_path)
    dbg_log(f"EXTRACTING REFERENCES FOR: {filename}")
    dbg_log("Method: Crossref Works Resolution API + Heuristic Fallback")
    
    # Crossref asks for a contact email (polite pool); user's email, else env, else none
    mailto_email = os.getenv("CROSSREF_MAILTO", "")
    pd = profile_data or {}
    if "user_id" in pd:
        try:
            from django.contrib.auth.models import User
            user = User.objects.get(id=pd["user_id"])
            if user.email:
                mailto_email = user.email
        except Exception:
            pass
            
    method = "unknown"
    try:
        dbg_log("Step 1: Extract Text (OCR)")
        text, method = extract_text(pdf_path, profile_data)
        dbg_log(f"  \u2514\u2500 OCR Method used: {method.upper()}, Extracted text length: {len(text)} chars")

        if not text.strip():
            dbg_log("  \u2514\u2500 WARNING: OCR returned no text. Checking if file is empty or scanned incorrectly.")

        dbg_log("Step 2: Locate article DOI")
        from .extractor import extract_metadata
        from .crossref import extract_doi_from_metadata, extract_doi_from_text
        doi = extract_doi_from_metadata(extract_metadata(pdf_path)) or extract_doi_from_text(text)
        dbg_log(f"  \u2514\u2500 DOI: {doi or 'not found'}")

        references = None
        source = "heuristic"
        if doi:
            dbg_log("Step 3: Fetching structured references from Semantic Scholar")
            from .semantic_scholar import get_references
            references = get_references(doi)
            if references:
                source = "semantic_scholar"
                dbg_log(f"  \u2514\u2500 Semantic Scholar returned {len(references)} references")
            else:
                dbg_log("  \u2514\u2500 Paper not indexed (or no references) \u2014 falling back to local parsing")

        if not references:
            dbg_log("Step 3b: Resolving citations via Crossref / Heuristics")
            references = parse_rule_based_references(text, mailto_email, dbg_log)
            dbg_log(f"  \u2514\u2500 Completed parsing. Extracted count: {len(references)} references")

        return {
            "references": references,
            "ocr_method": method,
            "article_doi": doi,
            "ref_source": source,
            "ref_count": len(references),
            "debug_log": "\n".join(debug_messages),
            "raw_llm_response": f"N/A (source: {source})",
        }
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        dbg_log(f"  \u2514\u2500 FATAL ERROR DURING EXTRACTION: {e}")
        dbg_log(f"Traceback:\n{tb}")
        return {
            "references": [],
            "ocr_method": method,
            "ref_count": 0,
            "error": str(e),
            "debug_log": "\n".join(debug_messages),
            "raw_llm_response": "N/A (Crossref Resolution API)",
        }
