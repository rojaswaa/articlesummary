"""Shared helpers for the view modules."""
import logging
import os
from pathlib import Path

from ..models import Profile, Session

log = logging.getLogger("article_summary_app.views")

ZOTERO_STORAGE = os.path.expanduser("~/Zotero/storage")


def get_user_profile(user):
    """Ensures a profile exists for the user and returns it."""
    if not hasattr(user, 'profile'):
        Profile.objects.get_or_create(user=user)
    return user.profile


def build_profile_data(profile, min_tokens: int = 0) -> dict:
    """Snapshot of the profile's provider settings, passed to worker threads.

    max_tokens controls chat-completion output size. For LM Studio it derives
    from the configured context length, but is capped at 16384: huge values
    (e.g. 262144) make LM Studio crash/unload from KV cache allocation."""
    max_tok = profile.lmstudio_context_length if profile.ai_provider == "lmstudio" else profile.max_tokens
    max_tok = max(min(max_tok, 16384), min_tokens)
    return {
        "ai_provider": profile.ai_provider,
        "ollama_base_url": profile.ollama_base_url,
        "ollama_model": profile.ollama_model,
        "lmstudio_base_url": profile.lmstudio_base_url,
        "lmstudio_model": profile.lmstudio_model,
        "llama_server_base_url": profile.llama_server_base_url,
        "llama_server_model": profile.llama_server_model,
        "gemini_api_key": profile.gemini_api_key,
        "gemini_model": profile.gemini_model,
        "ocr_provider": profile.ocr_provider,
        "mistral_api_key": profile.mistral_api_key,
        "temperature": profile.temperature,
        "max_tokens": max_tok,
    }


def remember_allowed_folder(request, folder: str) -> None:
    """Record a folder the user explicitly opened so PDFs inside it may be served."""
    folders = request.session.get("allowed_folders", [])
    if folder not in folders:
        folders.append(folder)
        request.session["allowed_folders"] = folders[-20:]


def is_allowed_folder(request, folder: str) -> bool:
    """A folder may start an analysis only if the user opened it via the folder
    picker, used it in a previous session, or it lives inside Zotero storage."""
    if not folder:
        return False
    try:
        f = Path(folder).resolve()
    except (OSError, ValueError):
        return False
    roots = {ZOTERO_STORAGE}
    roots.update(request.session.get("allowed_folders", []))
    roots.update(Session.objects.filter(user=request.user).values_list("folder", flat=True))
    for root in roots:
        if not root:
            continue
        try:
            if f.is_relative_to(Path(root).resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def resolve_allowed_pdf(request, path: str) -> Path | None:
    """Resolve a client-supplied path, allowing only .pdf files inside folders the
    user has opened (browsed folders, their sessions' folders, Zotero storage).
    Returns the resolved Path, or None if the path is outside the allowed roots."""
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return None
    if p.suffix.lower() != ".pdf" or not p.is_file():
        return None

    roots = {ZOTERO_STORAGE}
    roots.update(request.session.get("allowed_folders", []))
    roots.update(Session.objects.filter(user=request.user).values_list("folder", flat=True))

    for root in roots:
        if not root:
            continue
        try:
            if p.is_relative_to(Path(root).resolve()):
                return p
        except (OSError, ValueError):
            continue
    return None
