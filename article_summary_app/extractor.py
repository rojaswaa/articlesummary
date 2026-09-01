import logging
import os
import re
import fitz  # pymupdf
import pytesseract
from PIL import Image
from mistralai import Mistral
from dotenv import load_dotenv

try:
    from ocrmac import ocrmac
except ImportError:
    ocrmac = None

try:
    from markitdown import MarkItDown
except ImportError:
    MarkItDown = None

log = logging.getLogger(__name__)

load_dotenv()

def _get_mistral_client(api_key=None):
    current_key = api_key or os.getenv("MISTRAL_API_KEY")
    if not current_key:
        raise ValueError("Mistral API key not set.")
    return Mistral(api_key=current_key)

def extract_metadata(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    raw = doc.metadata
    page_count = doc.page_count
    doc.close()
    return {
        "title":      raw.get("title") or "",
        "author":     raw.get("author") or "",
        "subject":    raw.get("subject") or "",
        "keywords":   raw.get("keywords") or "",
        "creator":    raw.get("creator") or "",
        "producer":   raw.get("producer") or "",
        "created":    raw.get("creationDate") or "",
        "modified":   raw.get("modDate") or "",
        "page_count": page_count,
    }

def _extract_text_mistral(pdf_path: str, api_key=None) -> str:
    log.info(f"OCR [Mistral] → {os.path.basename(pdf_path)}")
    client = _get_mistral_client(api_key)
    with open(pdf_path, "rb") as f:
        uploaded = client.files.upload(
            file={"file_name": os.path.basename(pdf_path), "content": f},
            purpose="ocr",
        )
    signed = client.files.get_signed_url(file_id=uploaded.id)
    response = client.ocr.process(
        model="mistral-ocr-latest",
        document={"type": "document_url", "document_url": signed.url},
    )
    client.files.delete(file_id=uploaded.id)
    pages = [page.markdown for page in response.pages]
    return "\n\n".join(pages)

def _extract_text_macocr(pdf_path: str) -> str:
    if ocrmac is None: raise ValueError("ocrmac not installed.")
    log.info(f"OCR [macOS Vision] → {os.path.basename(pdf_path)}")
    doc = fitz.open(pdf_path)
    pages_text = []
    for page in doc:
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        results = ocrmac.OCR(img).recognize()
        page_text = "\n".join([result[0] for result in results if result[0].strip()])
        if page_text.strip(): pages_text.append(page_text)
    doc.close()
    return "\n\n".join(pages_text)

def _extract_text_pytesseract(pdf_path: str) -> str:
    log.info(f"OCR [pytesseract] → {os.path.basename(pdf_path)}")
    doc = fitz.open(pdf_path)
    pages_text = []
    for page in doc:
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        text = pytesseract.image_to_string(img).strip()
        pages_text.append(text)
    doc.close()
    return "\n\n".join(pages_text)

# Below this, a PDF's embedded text layer is considered missing/unusable
# and we fall back to real OCR.
MARKITDOWN_MIN_TEXT_CHARS = 200

def _extract_text_markitdown(pdf_path: str) -> str:
    if MarkItDown is None: raise ValueError("markitdown not installed.")
    log.info(f"Text layer [MarkItDown] → {os.path.basename(pdf_path)}")
    result = MarkItDown().convert(pdf_path)
    return result.text_content or ""

def extract_text(pdf_path: str, profile_data: dict = None) -> tuple[str, str]:
    pd = profile_data or {}
    provider = pd.get("ocr_provider", os.getenv("OCR_PROVIDER", "mistral")).lower()
    mistral_key = pd.get("mistral_api_key")

    # First pass: MarkItDown reads the embedded text layer — free, local,
    # and sufficient for born-digital PDFs. Only scanned/image PDFs (short
    # or empty text layer) proceed to the configured OCR provider.
    try:
        text = _extract_text_markitdown(pdf_path)
        if len(text.strip()) >= MARKITDOWN_MIN_TEXT_CHARS:
            return text, "markitdown"
        log.info(f"MarkItDown got only {len(text.strip())} chars — likely a scanned PDF, using OCR provider '{provider}'.")
    except Exception as e:
        log.warning(f"MarkItDown extraction failed ({e}) — using OCR provider '{provider}'.")

    if provider == "mistral":
        try:
            return _extract_text_mistral(pdf_path, mistral_key), "mistral"
        except Exception as e:
            log.warning(f"Mistral OCR failed ({e}) — falling back to pytesseract.")
            return _extract_text_pytesseract(pdf_path), "pytesseract"
    elif provider == "macocr":
        try:
            return _extract_text_macocr(pdf_path), "macocr"
        except Exception as e:
            log.warning(f"macOS OCR failed ({e}) — falling back to pytesseract.")
            return _extract_text_pytesseract(pdf_path), "pytesseract"
    else:
        return _extract_text_pytesseract(pdf_path), "pytesseract"

def _extract_abstract_from_text(text: str) -> tuple[str, bool]:
    if not text: return "", False
    text_normalized = text.replace("\n", " ").replace("\r", " ")
    abstract_pattern = r"(?:^|\s)abstract[\s:]*(.+?)(?=(?:introduction|keywords|background|methods?|1\.|author|received|accepted|$))"
    match = re.search(abstract_pattern, text_normalized, re.IGNORECASE | re.DOTALL)
    if match:
        abstract = re.sub(r"\s+", " ", match.group(1).strip())[:2000]
        if len(abstract) > 50:
            log.info(f"Extracted real abstract from PDF ({len(abstract)} chars)")
            return abstract, True
    log.info("No abstract found in PDF - will generate AI summary")
    return "", False

def analyze_pdf(pdf_path: str, criteria: str, profile_data: dict = None) -> dict:
    from .analyzer import extract_fields
    from .crossref import extract_doi_from_text, extract_doi_from_metadata, get_crossref_context, query_crossref

    filename = os.path.basename(pdf_path)
    log.info(f"\n{'='*70}")
    log.info(f"ANALYZING: {filename}")
    log.info(f"{'='*70}")

    log.info("Step 1: Extract Metadata")
    metadata = extract_metadata(pdf_path)
    
    log.info(f"Step 2: Extract Text (OCR)")
    text, method = extract_text(pdf_path, profile_data)
    log.info(f"  └─ Method: {method.upper()}, Length: {len(text)} chars")

    log.info("Step 3: Extract DOI")
    doi = extract_doi_from_metadata(metadata) or extract_doi_from_text(text)
    if doi: log.info(f"  • Found DOI: {doi}")
    else: log.info("  • No DOI found")

    crossref_context = ""
    abstract_text = ""
    abstract_source = "ai_generated"

    if doi:
        log.info("Step 4: Query Crossref API")
        crossref_context = get_crossref_context(doi, metadata)
        crossref_data = query_crossref(doi)
        if crossref_data.get("abstract"):
            abstract_text = crossref_data["abstract"]
            abstract_source = "crossref"
            log.info("  └─ Found abstract via Crossref")

    if not abstract_text:
        log.info("Step 5: Extract Abstract from PDF text")
        pdf_abstract, is_real = _extract_abstract_from_text(text)
        if pdf_abstract:
            abstract_text = pdf_abstract
            abstract_source = "extracted"

    log.info("Step 6: Analyze Fields with AI")
    fields = extract_fields(
        text, criteria, metadata, doi=doi,
        crossref_context=crossref_context,
        abstract_text=abstract_text,
        is_real_abstract=(abstract_source in ["extracted", "crossref"]),
        profile_data=profile_data
    )
    
    log.info(f"  └─ Alignment: {fields.get('aligns_with_criteria', False)}")
    log.info(f"  └─ Abstract Source: {abstract_source}")
    log.info(f"{'='*70}\n")

    return {
        "fields": fields,
        "ocr_method": method,
        "doi": doi,
        "abstract_source": abstract_source,
    }
