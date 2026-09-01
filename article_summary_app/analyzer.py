import json
import os
import re
import logging
import httpx
from openai import OpenAI
from dotenv import load_dotenv

try:
    from google import genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

load_dotenv()
log = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 150.0

FIELDS = [
    "year", "authors_year", "author", "title", "doi", "apa_reference",
    "resource_type", "abstract", "abstract_source", "keywords", "location",
    "purpose_objectives", "research_questions",
    "survey_interview_focus_questions", "sample", "design",
    "main_findings", "aligns_with_criteria", "alignment_reason",
]

def _build_prompt(text: str, criteria: str, metadata: dict, crossref_context: str = "", abstract_text: str = "", is_real_abstract: bool = False) -> str:
    prompt = f"""You are conducting a systematic literature review. Analyze the document below.

RESEARCH CRITERIA (evaluate alignment against this):
{criteria}

METADATA AVAILABLE (use if present, otherwise extract from full text):
- Author(s): {metadata.get('author') or 'NOT PROVIDED - EXTRACT FROM TEXT'}
- Title:     {metadata.get('title')  or 'NOT PROVIDED - EXTRACT FROM TEXT'}
- Subject:   {metadata.get('subject') or 'NOT PROVIDED - EXTRACT FROM TEXT'}
- Keywords:  {metadata.get('keywords') or 'NOT PROVIDED - EXTRACT FROM TEXT'}
- Created:   {metadata.get('created') or 'NOT PROVIDED - EXTRACT FROM TEXT'}"""

    if abstract_text and is_real_abstract:
        prompt += f"\n\nABSTRACT (extracted from document):\n{abstract_text}"
        prompt += "\n(This is the actual abstract from the paper. Use it directly.)"

    if crossref_context:
        prompt += f"\n\n{crossref_context}\n(Use this authoritative data as reference - prefer these values over OCR extraction)"

    prompt += f"\n\nDOCUMENT FULL TEXT:\n{text[:12000]}"

    prompt += """

CRITICAL INSTRUCTIONS:
1. Return ONLY a valid JSON object with the exact fields specified below
2. DO NOT add extra fields like "context", "notes", "reasoning", "explanation", or "thinking"
3. DO NOT wrap in markdown code blocks (no ```json)
4. Start your response with { and end with }

REQUIRED JSON STRUCTURE (exactly these 18 fields):
{
  "year": "publication year as number, e.g. 2023",
  "authors_year": "APA in-text citation, e.g. Smith & Jones, 2023",
  "author": "full author name(s) as listed in the document",
  "title": "complete document title",
  "doi": "DOI if available, format 10.xxxx/xxxxx, or null",
  "resource_type": "one of: Journal Article, Conference Paper, Book Chapter, Report, Dissertation, Preprint, Other",
  "abstract": "verbatim abstract text",
  "abstract_source": "extracted if abstract was provided above, ai_generated otherwise",
  "keywords": "comma-separated list of topics",
  "location": "country name, or null",
  "purpose_objectives": "3 to 5 sentences describing purpose",
  "research_questions": "formatted as RQ1: [question]; RQ2: [question], or null",
  "survey_interview_focus_questions": "comma-separated list, or null",
  "sample": "formatted as n=X participants, [demographics]",
  "design": "one of: Quantitative, Qualitative, Mixed Methods, Theoretical, Literature Review, Meta-Analysis",
  "main_findings": "5 to 7 sentences summarising findings",
  "aligns_with_criteria": "true if relevant, false otherwise",
  "alignment_reason": "2 to 3 sentences explaining alignment"
}
Return ONLY the JSON object."""
    return prompt

def coerce_bool(value) -> bool:
    """Normalise LLM output for boolean fields: handles True, "true", "True", "yes"."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")

def is_aligned(fields: dict) -> bool:
    """Whether a result's aligns_with_criteria is truthy, tolerating legacy string values."""
    return coerce_bool((fields or {}).get("aligns_with_criteria"))

def _parse_response(raw: str) -> dict:
    """Parse the LLM's JSON response. Raises ValueError on unparseable output
    so the job lands in 'error' status (retryable) instead of a fake result."""
    log.info(f"    • Model response length: {len(raw)} chars")
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match: cleaned = match.group(0)
    data = None
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        log.warning("    • Initial JSON parse failed. Attempting cleanup...")
        try:
            fixed = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', cleaned)
            data = json.loads(fixed)
        except Exception as e:
            log.error(f"    • JSON parse failed even after cleanup: {e}")
            raise ValueError(f"Failed to parse AI response as JSON ({e}). Raw start: {raw[:120]!r}")
    if not isinstance(data, dict):
        raise ValueError("AI response was valid JSON but not an object.")
    data["aligns_with_criteria"] = coerce_bool(data.get("aligns_with_criteria"))
    return data

def get_available_models(base_url: str, provider: str = "ollama") -> list[dict]:
    url = (base_url or "").rstrip("/")
    if provider == "gemini":
        api_key = base_url or os.getenv("GEMINI_API_KEY")
        fallback = [{"key": "gemini-2.0-flash", "display_name": "Gemini 2.0 Flash"}]
        if not api_key: return fallback
        if not HAS_GEMINI: return fallback
        try:
            client = genai.Client(api_key=api_key)
            models = []
            for m in client.models.list():
                name = getattr(m, 'name', '')
                display_name = getattr(m, 'display_name', '')
                methods = getattr(m, 'supported_generation_methods', [])
                is_generative = any('generateContent' in method for method in (methods or []))
                if not methods and ('gemini' in name.lower() or 'gemma' in name.lower()):
                    if 'embedding' not in name.lower() and 'aqa' not in name.lower():
                        is_generative = True
                if is_generative:
                    mid = name.split('/')[-1] if '/' in name else name
                    models.append({"key": mid, "display_name": display_name or mid})
            return models if models else fallback
        except Exception as e:
            log.warning(f"Gemini model listing failed: {e}")
            return fallback
    try:
        if provider == "ollama":
            resp = httpx.get(f"{url}/api/tags", timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                return [{"key": m["name"], "display_name": m["name"]} for m in data.get("models", [])]
        else:
            resp = httpx.get(f"{url}/v1/models", timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", data.get("models", []))
                return [{"key": m["id"] if isinstance(m, dict) else m, "display_name": m["id"] if isinstance(m, dict) else m} for m in items]
    except Exception as e:
        log.debug(f"Model listing failed for {provider}: {e}")
    return []

def check_connectivity(profile_data: dict) -> dict:
    status = {"ollama": {"available": False}, "lmstudio": {"available": False}, "gemini": {"available": False}, "mistral": {"available": False}}
    try:
        url = profile_data.get("ollama_base_url", "").rstrip("/")
        if httpx.get(f"{url}/api/tags", timeout=2.0).status_code == 200: status["ollama"]["available"] = True
    except Exception: pass
    try:
        url = profile_data.get("lmstudio_base_url", "").rstrip("/")
        if httpx.get(f"{url}/v1/models", timeout=2.0).status_code == 200: status["lmstudio"]["available"] = True
    except Exception: pass
    try:
        api_key = profile_data.get("gemini_api_key")
        if api_key and HAS_GEMINI:
            client = genai.Client(api_key=api_key)
            for _ in client.models.list(config={'page_size': 1}):
                status["gemini"]["available"] = True
                break
    except Exception: pass
    try:
        m_key = profile_data.get("mistral_api_key")
        if m_key and httpx.get("https://api.mistral.ai/v1/models", headers={"Authorization": f"Bearer {m_key}"}, timeout=2.0).status_code == 200:
            status["mistral"]["available"] = True
    except Exception: pass
    return status

# Servers (by base_url) known to reject response_format=json_object — remembered
# so we don't re-attempt (and re-warn) on every single call.
_NO_JSON_FORMAT: set[str] = set()


def _chat_openai_compatible(base_url: str, api_key: str, model: str, prompt: str,
                            temperature: float, max_tokens: int) -> str:
    """Call any OpenAI-compatible server (Ollama, LM Studio, llama-server).

    Requests native JSON output; falls back (once per server) for servers that
    don't support response_format.
    """
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=LLM_TIMEOUT_SECONDS)
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if base_url in _NO_JSON_FORMAT:
        return client.chat.completions.create(**kwargs).choices[0].message.content
    try:
        resp = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception as e:
        if "response_format" not in str(e) and "400" not in str(e):
            raise
        _NO_JSON_FORMAT.add(base_url)
        log.info(f"    • {base_url} doesn't support response_format=json_object; disabling it for this server.")
        resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content

def extract_fields(ocr_text: str, criteria: str, metadata: dict = None, doi: str = "", crossref_context: str = "", abstract_text: str = "", is_real_abstract: bool = False, profile_data: dict = None) -> dict:
    from .crossref import enrich_fields_with_crossref
    pd = profile_data or {}
    provider = pd.get("ai_provider", "ollama")
    model = pd.get(f"{provider}_model")
    if not model: model = os.getenv(f"{provider.upper()}_MODEL")
    temperature = pd.get("temperature", 0.0)
    max_tokens = pd.get("max_tokens", 2048)

    log.info(f"    • LLM Provider: {provider.upper()}")
    log.info(f"    • Model: {model}")

    user_prompt = _build_prompt(ocr_text, criteria, metadata or {}, crossref_context, abstract_text, is_real_abstract)
    log.info(f"    • Prompt length: {len(user_prompt)} chars")
    raw = ""
    try:
        if provider == "gemini":
            if not HAS_GEMINI:
                raise ValueError("Gemini selected but the google-genai package is not installed.")
            if not pd.get("gemini_api_key"):
                raise ValueError("Gemini selected but no API key is configured in Settings.")
            client = genai.Client(
                api_key=pd.get("gemini_api_key"),
                http_options={"timeout": int(LLM_TIMEOUT_SECONDS * 1000)},
            )
            resp = client.models.generate_content(
                model=pd.get("gemini_model"),
                contents=user_prompt,
                config={
                    "temperature": temperature,
                    "max_output_tokens": max_tokens,
                    "response_mime_type": "application/json",
                },
            )
            raw = resp.text
        elif provider == "lmstudio":
            base = pd.get("lmstudio_base_url", "http://localhost:1234").rstrip("/")
            raw = _chat_openai_compatible(f"{base}/v1", "lm-studio", pd.get("lmstudio_model"),
                                          user_prompt, temperature, max_tokens)
        elif provider == "llama_server":
            base = pd.get("llama_server_base_url", "http://localhost:8012").rstrip("/")
            raw = _chat_openai_compatible(f"{base}/v1", "llama-server", pd.get("llama_server_model"),
                                          user_prompt, temperature, max_tokens)
        else:
            base = pd.get("ollama_base_url", "http://localhost:11434").rstrip("/")
            raw = _chat_openai_compatible(f"{base}/v1", "ollama", pd.get("ollama_model"),
                                          user_prompt, temperature, max_tokens)
    except Exception as e:
        log.error(f"    • AI Provider error ({provider}): {e}")
        raise ValueError(f"AI Provider error: {str(e)}")

    data = _parse_response(raw or "")
    result = {field: data.get(field) for field in FIELDS}
    result["aligns_with_criteria"] = coerce_bool(result["aligns_with_criteria"])
    if abstract_text and is_real_abstract:
        result["abstract"] = abstract_text
        result["abstract_source"] = "extracted"
    elif not result.get("abstract_source"):
        result["abstract_source"] = "ai_generated"
    if doi: result = enrich_fields_with_crossref(result, doi)
    return result
