"""Crossref REST API integration for bibliographic data enrichment."""
import logging
import re
import requests
from pybtex.database import BibliographyData, Entry
from pybtex.plugin import find_plugin

log = logging.getLogger(__name__)

CROSSREF_API_URL = "https://api.crossref.org/works"


def extract_doi_from_text(text: str) -> str:
    """Extract DOI from OCR text using regex patterns."""
    if not text:
        return ""

    patterns = [
        r"10\.\d{4,}/\S+?(?=\s|$|\n)",  # Standard DOI format
        r"doi\.org/(10\.\d{4,}/\S+?)(?=\s|$|\n)",  # doi.org/10.xxxx/xxx
        r"doi:\s*(10\.\d{4,}/\S+?)(?=\s|$|\n)",  # doi: 10.xxxx/xxx
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            doi = (match.group(1) if match.groups() else match.group(0)).rstrip('.,;:)')
            if doi.startswith("10."):
                log.info(f"  └─ Extract DOI from text: {doi}")
                return doi

    log.info("  └─ Extract DOI from text: NOT FOUND")
    return ""


def extract_doi_from_metadata(metadata: dict) -> str:
    """Extract DOI from PDF metadata."""
    if not metadata:
        return ""

    # Check common metadata fields
    fields_to_check = ["doi", "DOI", "identifier", "Identifier"]
    for field in fields_to_check:
        if field in metadata and metadata[field]:
            doi = metadata[field]
            if isinstance(doi, str) and doi.startswith("10."):
                return doi

    return ""


def query_crossref(doi: str) -> dict:
    """Query Crossref API for a given DOI. Returns structured metadata."""
    if not doi:
        return {}

    try:
        log.info(f"Querying Crossref for DOI: {doi}")

        # Normalize DOI
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip())

        url = f"{CROSSREF_API_URL}/{doi}"
        headers = {"User-Agent": "ArticleSummary (mailto:user@example.com)"}

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if data.get("message"):
                return _parse_crossref_response(data["message"])
        else:
            log.warning(f"Crossref API returned {response.status_code} for DOI: {doi}")

    except requests.exceptions.RequestException as e:
        log.warning(f"Crossref API error for DOI {doi}: {e}")
    except Exception as e:
        log.error(f"Error processing Crossref response: {e}")

    return {}


def _clean_abstract(text: str) -> str:
    """Strip JATS XML tags and leading 'Abstract' heading from abstract text."""
    if not text:
        return text
    # Strip JATS XML tags (e.g. <jats:p>, </jats:sec>, etc.)
    text = re.sub(r"<[^>]+>", " ", text)
    # Strip leading "Abstract" label (with optional colon, period, newline, spaces)
    text = re.sub(r"^\s*abstract\s*[:\.\-]?\s*", "", text, flags=re.IGNORECASE)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_crossref_response(msg: dict) -> dict:
    """Parse Crossref API response into useful fields."""
    result = {}

    # DOI
    if msg.get("DOI"):
        result["doi"] = msg["DOI"]

    # Title
    if msg.get("title"):
        titles = msg["title"]
        result["title"] = titles[0] if isinstance(titles, list) else titles

    # Authors
    if msg.get("author"):
        authors = msg["author"]
        author_names = []
        for author in authors:
            parts = []
            if author.get("given"):
                parts.append(author["given"])
            if author.get("family"):
                parts.append(author["family"])
            if parts:
                author_names.append(" ".join(parts))
        if author_names:
            result["author"] = ", ".join(author_names)

    # Publication year
    if msg.get("issued"):
        issued = msg["issued"]
        if issued.get("date-parts"):
            date_parts = issued["date-parts"][0]
            if date_parts:
                result["year"] = str(date_parts[0])

    # Abstract
    if msg.get("abstract"):
        result["abstract"] = _clean_abstract(msg["abstract"])

    # Journal/container title
    if msg.get("container-title"):
        container = msg["container-title"]
        result["journal"] = container[0] if isinstance(container, list) else container

    # Issue, volume, pages — kept both raw (for APA formatting) and as a
    # display string (for LLM context / fallback formatting)
    for key, out in (("volume", "volume"), ("issue", "issue"), ("page", "pages")):
        if msg.get(key):
            result[out] = str(msg[key])

    metadata_parts = []
    if result.get("volume"): metadata_parts.append(f"Vol. {result['volume']}")
    if result.get("issue"): metadata_parts.append(f"Issue {result['issue']}")
    if result.get("pages"): metadata_parts.append(f"pp. {result['pages']}")
    if metadata_parts:
        result["publication_info"] = ", ".join(metadata_parts)

    # Keywords/subject
    if msg.get("subject"):
        result["keywords"] = ", ".join(msg["subject"])

    # Type
    if msg.get("type"):
        result["resource_type"] = msg["type"]

    # Published date (as YYYY-MM-DD)
    if msg.get("published"):
        published = msg["published"]
        if published.get("date-parts"):
            date_parts = published["date-parts"][0]
            if len(date_parts) >= 3:
                result["published_date"] = f"{date_parts[0]:04d}-{date_parts[1]:02d}-{date_parts[2]:02d}"
            elif date_parts:
                result["published_date"] = str(date_parts[0])

    return result


def _split_names(author_str: str) -> list[tuple[str, str]]:
    """'First Last, First Last' → [(first, last), ...]."""
    names = []
    for author in author_str.split(","):
        author = author.strip()
        if not author:
            continue
        parts = author.rsplit(" ", 1)
        names.append((parts[0], parts[1]) if len(parts) == 2 else (author, ""))
    return names


def _format_authors_apa(author_str: str) -> str:
    """Format 'First Last, First Last' as APA: 'Last, F., & Last, F.'."""
    names = _split_names(author_str)
    if not names:
        return ""
    if len(names) == 1:
        first, last = names[0]
        return f"{last}, {first}" if last else first
    formatted = []
    for first, last in names:
        initials = ". ".join(f[0] for f in first.split()) + "." if first else ""
        formatted.append(f"{last}, {initials}" if last and initials else (last or first))
    return ", ".join(formatted[:-1]) + ", & " + formatted[-1]


def format_apa_reference(doi: str) -> str:
    """
    Generate a proper APA 7th edition reference from Crossref data using pybtex.
    Returns formatted APA reference or empty string if DOI not found.
    """
    if not doi:
        return ""

    crossref_data = query_crossref(doi)
    if not crossref_data:
        return ""

    try:
        # pybtex wants "Last, First and Last, First"
        bibtex_authors = " and ".join(
            f"{last}, {first}" if last else first
            for first, last in _split_names(crossref_data.get("author", ""))
        )
        entry_fields = [
            ("author", bibtex_authors),
            ("title", crossref_data.get("title", "")),
            ("journal", crossref_data.get("journal", "")),
            ("year", crossref_data.get("year", "")),
            ("doi", doi),
        ]
        for bib_key, data_key in (("volume", "volume"), ("number", "issue"), ("pages", "pages")):
            if crossref_data.get(data_key):
                entry_fields.append((bib_key, crossref_data[data_key]))

        bib_data = BibliographyData({"article_key": Entry("article", entry_fields)})
        apa_style = find_plugin("pybtex.style.formatting", "apa7")()
        formatted_list = list(apa_style.format_entries(bib_data.entries.values()))
        if formatted_list and hasattr(formatted_list[0], "text"):
            apa_ref = str(formatted_list[0].text).replace("<newblock>", " ").strip()
            if apa_ref:
                # pybtex's APA7 style omits authors — prepend them manually
                author_formatted = _format_authors_apa(crossref_data.get("author", ""))
                if author_formatted:
                    apa_ref = f"{author_formatted}. {apa_ref}"
                log.info(f"Generated APA7 reference from Crossref for {doi}")
                return apa_ref
    except Exception as e:
        log.warning(f"APA7 formatting failed: {e}. Using fallback formatting.")

    return _format_apa_reference_fallback(crossref_data, doi)


def _format_apa_reference_fallback(crossref_data: dict, doi: str) -> str:
    """
    Fallback manual APA reference formatting if pybtex fails.
    """
    parts = []

    # Authors
    if crossref_data.get("author"):
        parts.append(crossref_data["author"])

    # Year
    if crossref_data.get("year"):
        parts.append(f"({crossref_data['year']}).")

    # Title
    if crossref_data.get("title"):
        parts.append(f"{crossref_data['title']}.")

    # Journal and publication info
    journal_parts = []
    if crossref_data.get("journal"):
        journal_parts.append(f"*{crossref_data['journal']}*")

    if crossref_data.get("publication_info"):
        journal_parts.append(f", {crossref_data['publication_info']}")

    if journal_parts:
        parts.append("".join(journal_parts) + ".")

    # DOI
    if doi:
        parts.append(f"https://doi.org/{doi}")

    apa_ref = " ".join(parts)
    log.info(f"Generated APA reference (fallback) from Crossref for {doi}")
    return apa_ref


def get_crossref_context(doi: str, metadata: dict = None) -> str:
    """
    Get Crossref data for a DOI and format it as context for LLM.
    Returns a formatted string with bibliographic information.
    """
    crossref_data = query_crossref(doi)

    if not crossref_data:
        log.warning(f"No Crossref data found for DOI: {doi}")
        return ""

    lines = ["## Bibliographic Data (from Crossref)"]

    if crossref_data.get("title"):
        lines.append(f"**Title:** {crossref_data['title']}")

    if crossref_data.get("author"):
        lines.append(f"**Authors:** {crossref_data['author']}")

    if crossref_data.get("year"):
        lines.append(f"**Publication Year:** {crossref_data['year']}")

    if crossref_data.get("journal"):
        lines.append(f"**Journal:** {crossref_data['journal']}")

    if crossref_data.get("publication_info"):
        lines.append(f"**Publication Details:** {crossref_data['publication_info']}")

    if crossref_data.get("abstract"):
        lines.append(f"**Abstract:** {crossref_data['abstract']}")

    if crossref_data.get("keywords"):
        lines.append(f"**Keywords:** {crossref_data['keywords']}")

    if crossref_data.get("resource_type"):
        lines.append(f"**Type:** {crossref_data['resource_type']}")

    return "\n".join(lines)


def enrich_fields_with_crossref(fields: dict, doi: str) -> dict:
    """
    Enrich extracted fields with Crossref data.
    Crossref data takes precedence over LLM extraction for certain fields.
    """
    if not doi:
        return fields

    crossref_data = query_crossref(doi)

    if not crossref_data:
        log.info(f"      [Crossref enrichment] No Crossref data found for {doi}")
        return fields

    log.info(f"      [Crossref enrichment] Found {len(crossref_data)} fields in Crossref")

    # Always use the actual DOI we extracted (not what the model guessed)
    fields["doi"] = doi

    # Author ALWAYS uses Crossref (LLM often has placeholder names like "Author2")
    if crossref_data.get("author"):
        fields["author"] = crossref_data["author"]
        log.info(f"        • author: '{str(crossref_data['author'])[:40]}' (from Crossref, always authoritative)")

    # Generate APA reference from Crossref data (includes correct authors)
    apa_ref = format_apa_reference(doi)
    if apa_ref:
        fields["apa_reference"] = apa_ref

    # Conditional override: only replace weak/empty LLM extractions
    for field_name in ("title", "year", "abstract", "resource_type"):
        crossref_key = field_name
        if crossref_data.get(crossref_key):
            # Only override if the LLM didn't extract it well (empty or generic)
            current_value = fields.get(field_name, "")
            # Convert to string for length check, handle None/null
            current_str = str(current_value) if current_value is not None else ""

            # Replace if empty, placeholder, or too short (bad extraction)
            if not current_str or current_str == "—" or current_str == "None" or len(current_str) < 5:
                old_value = current_str[:40] if current_str else "EMPTY"
                new_value = str(crossref_data[crossref_key])[:40]
                fields[field_name] = crossref_data[crossref_key]
                log.info(f"        • {field_name}: '{old_value}' → '{new_value}' (from Crossref)")
            else:
                log.info(f"        • {field_name}: kept LLM value (good quality extraction)")

    return fields
